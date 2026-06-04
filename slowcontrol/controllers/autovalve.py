"""Autonomous LN2 fill-valve controller (state machine)."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from slowcontrol.controllers.base import Controller
from slowcontrol.core.registry import register_controller

log = logging.getLogger(__name__)


class FillState(Enum):
    WAITING = "WAITING"
    FILLING = "FILLING"
    COOLDOWN = "COOLDOWN"


@register_controller("autovalve")
class AutovalveController(Controller):
    """LN2 fill-valve state machine.

    Monitors liquid-level sensor gradients (or thresholds) to decide when
    to open / close the fill valve.  Publishes relay commands over MQTT.
    """

    def __init__(self, name: str, config: dict[str, Any], mqtt):
        super().__init__(name, config, mqtt)

        # Config
        self._mode = config.get("mode", "gradient")
        self._level_channels: list[str] = config.get(
            "level_channels", ["RES1", "RES2"]
        )
        self._overfill_channel: str = config.get("overfill_channel", "RES7")
        self._valve_relay: str = config.get("valve_relay", "ln2_valve")
        self._fill_timeout: float = config.get("fill_timeout", 600)
        self._cooldown_time: float = config.get("cooldown", 900)
        self._overfill_threshold: float = config.get("overfill_threshold", 9.8)
        self._overfill_close: float = config.get("overfill_close", 9.0)
        self._full_tank: float = config.get("full_tank", 9.5)
        self._gradient_trigger: float = config.get("gradient_trigger", -1e-4)
        self._scan_interval: float = config.get("scan_interval", 1.0)

        # Runtime state.  start_enabled defaults to False so a service restart
        # (planned or after a power cut) does NOT auto-arm the autovalve;
        # operator must explicitly re-enable from the control page.  Override
        # with `start_enabled: true` in config.yaml if you ever want the old
        # auto-arm behaviour back.
        self._state = FillState.WAITING
        self._fill_start: float | None = None
        self._last_fill_end: float | None = None
        self._enabled = bool(config.get("start_enabled", False))

        # Rolling gradient buffer (≈60 samples at 1 Hz)
        self._level_history: dict[str, deque] = {
            ch: deque(maxlen=60) for ch in self._level_channels
        }
        self._overfill_value = 0.0
        self._latest_levels: dict[str, float] = {}

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        # Persisted runtime state.  Path defaults to runtime/autovalve.yaml
        # under the working dir (the systemd unit sets WorkingDirectory=
        # /home/pi/ETS-pythonSLOWDAQ).  Override with ETS_RUNTIME_DIR.
        # mode + tunables (NOT enabled — fail-safe stays off across restarts)
        # are written on every change and reapplied here over the config
        # defaults set above.
        runtime_dir = Path(os.environ.get("ETS_RUNTIME_DIR", "runtime"))
        self._persist_path = runtime_dir / "autovalve.yaml"
        self._load_persisted_state()

    # ── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        super().start()
        self.mqtt.subscribe("ets/sensors/#", self._on_sensor_data)
        self.mqtt.subscribe(
            "ets/commands/autovalve/mode", self._on_mode_command
        )
        self.mqtt.subscribe(
            "ets/commands/autovalve/set", self._on_set_command
        )
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._control_loop, name="autovalve", daemon=True
        )
        self._thread.start()
        # Republish so the retained status reflects the current (post-restart)
        # state — without this, operators see the stale FILLING/COOLDOWN value
        # from before the bounce until the next state transition.
        self.publish_status()
        log.info("Autovalve controller started (mode=%s, enabled=%s)",
                 self._mode, self._enabled)

    def stop(self) -> None:
        self._stop.set()
        self._close_valve()
        if self._thread:
            self._thread.join(timeout=5)
        super().stop()

    def get_state(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "mode": self._mode,
            "enabled": self._enabled,
            "overfill_value": self._overfill_value,
            "levels": dict(self._latest_levels),
            "fill_start": self._fill_start,
            # Tunables — exposed so the GUI can read them back.  Each is also
            # settable via ets/commands/autovalve/set {<param>: <value>}.
            "level_channels": list(self._level_channels),
            "overfill_channel": self._overfill_channel,
            "full_tank": self._full_tank,
            "overfill_threshold": self._overfill_threshold,
            "overfill_close": self._overfill_close,
            "gradient_trigger": self._gradient_trigger,
            "fill_timeout": self._fill_timeout,
            "cooldown": self._cooldown_time,
            "ts": time.time(),
        }

    # ── MQTT handlers ──────────────────────────────────────────

    def _on_sensor_data(self, topic: str, payload: dict) -> None:
        parts = topic.split("/")
        if len(parts) < 4:
            return
        channel = parts[-1]
        value = payload.get("value")
        ts = payload.get("ts", time.time())
        if value is None:
            return

        self._latest_levels[channel] = value

        if channel in self._level_history:
            self._level_history[channel].append((ts, value))

        if channel == self._overfill_channel:
            self._overfill_value = value

    def _on_mode_command(self, topic: str, payload: dict) -> None:
        mode = payload.get("mode")
        persisted_change = False
        if mode == "auto":
            self._enabled = True
            log.info("Autovalve enabled")
        elif mode == "manual":
            self._enabled = False
            self._close_valve()
            log.info("Autovalve disabled (manual mode)")
        elif mode in ("gradient", "threshold"):
            self._mode = mode
            log.info("Autovalve mode → %s", mode)
            persisted_change = True
        self.publish_status()
        if persisted_change:
            self._save_persisted_state()

    # Whitelist of operator-tunable parameters: attribute name → coercer.
    # Times are seconds, thresholds are volts, channels are MQTT channel names
    # (the last segment of ets/sensors/<driver>/<channel>).
    _TUNABLES = {
        "full_tank":          ("_full_tank",          "float"),
        "overfill_threshold": ("_overfill_threshold", "float"),
        "overfill_close":     ("_overfill_close",    "float"),
        "gradient_trigger":   ("_gradient_trigger",  "float"),
        "fill_timeout":       ("_fill_timeout",      "positive_float"),
        "cooldown":           ("_cooldown_time",     "non_negative_float"),
        "level_channels":     ("_level_channels",    "channel_list"),
        "overfill_channel":   ("_overfill_channel",  "channel_str"),
    }

    def _on_set_command(self, topic: str, payload: dict) -> None:
        """Apply runtime parameter tweaks.  Payload is {param: value, ...}.

        Unknown params are ignored with a warning; invalid values are
        rejected per-param so a typo in one field doesn't poison the rest.
        """
        if not isinstance(payload, dict):
            log.warning("autovalve/set: payload must be a mapping, got %r", payload)
            return

        applied: dict[str, Any] = {}
        for key, raw in payload.items():
            if key not in self._TUNABLES:
                log.warning("autovalve/set: unknown param %r ignored", key)
                continue
            attr, kind = self._TUNABLES[key]
            try:
                value = self._coerce_tunable(kind, raw)
            except (TypeError, ValueError) as exc:
                log.warning("autovalve/set: rejected %s=%r (%s)", key, raw, exc)
                continue

            # Special: changing level_channels has to rebuild the history buffer
            # so gradient mode doesn't compute on stale data from removed channels.
            if attr == "_level_channels":
                self._level_history = {
                    ch: deque(self._level_history.get(ch, ()), maxlen=60)
                    for ch in value
                }

            setattr(self, attr, value)
            applied[key] = value
            log.info("Autovalve param %s → %s", key, value)

        if applied:
            self.publish_status()
            self._save_persisted_state()

    # ── persistence ────────────────────────────────────────────
    # Written whenever a tunable or mode changes; reloaded at startup so
    # operator tweaks survive a service bounce.  `enabled` is deliberately
    # NOT persisted: the autovalve must always come up disabled (see
    # start_enabled note in __init__).

    _PERSIST_KEYS = (
        "mode",
        "full_tank", "overfill_threshold", "overfill_close",
        "gradient_trigger", "fill_timeout", "cooldown",
        "level_channels", "overfill_channel",
    )

    def _load_persisted_state(self) -> None:
        path = self._persist_path
        if not path.exists():
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.warning("autovalve: could not load %s (%s); using config defaults",
                        path, exc)
            return
        if not isinstance(data, dict):
            log.warning("autovalve: %s is malformed; using config defaults", path)
            return

        applied = []
        for key, raw in data.items():
            if key == "mode":
                if raw in ("gradient", "threshold"):
                    self._mode = raw
                    applied.append("mode")
                continue
            if key not in self._TUNABLES:
                continue
            attr, kind = self._TUNABLES[key]
            try:
                value = self._coerce_tunable(kind, raw)
            except (TypeError, ValueError) as exc:
                log.warning("autovalve: persisted %s=%r rejected (%s)", key, raw, exc)
                continue
            if attr == "_level_channels":
                # Reset history for the persisted channel set; no prior data
                # exists yet at construction time.
                self._level_history = {ch: deque(maxlen=60) for ch in value}
            setattr(self, attr, value)
            applied.append(key)
        if applied:
            log.info("autovalve: applied persisted runtime state from %s (%s)",
                     path, ", ".join(applied))

    def _save_persisted_state(self) -> None:
        path = self._persist_path
        data = {
            "mode":               self._mode,
            "full_tank":          self._full_tank,
            "overfill_threshold": self._overfill_threshold,
            "overfill_close":     self._overfill_close,
            "gradient_trigger":   self._gradient_trigger,
            "fill_timeout":       self._fill_timeout,
            "cooldown":           self._cooldown_time,
            "level_channels":     list(self._level_channels),
            "overfill_channel":   self._overfill_channel,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("# Autovalve runtime state — written automatically when a\n"
                        "# tunable or mode is changed via ets/commands/autovalve/{set,mode}.\n"
                        "# Delete this file to revert to the defaults in config.yaml.\n"
                        "# `enabled` is intentionally NOT persisted — the autovalve must\n"
                        "# always come up disabled (fail-safe).\n")
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("autovalve: could not persist runtime state to %s (%s)",
                        path, exc)

    @staticmethod
    def _coerce_tunable(kind: str, raw: Any) -> Any:
        if kind == "float":
            return float(raw)
        if kind == "positive_float":
            v = float(raw)
            if v <= 0:
                raise ValueError("must be > 0")
            return v
        if kind == "non_negative_float":
            v = float(raw)
            if v < 0:
                raise ValueError("must be >= 0")
            return v
        if kind == "channel_str":
            s = str(raw).strip()
            if not s:
                raise ValueError("channel name cannot be empty")
            return s
        if kind == "channel_list":
            if not isinstance(raw, (list, tuple)):
                raise TypeError("expected a list of channel names")
            cleaned = [str(c).strip() for c in raw if str(c).strip()]
            if not cleaned:
                raise ValueError("at least one channel required")
            return cleaned
        raise ValueError(f"unknown coercer {kind!r}")

    # ── control loop ───────────────────────────────────────────

    def _control_loop(self) -> None:
        while not self._stop.is_set():
            if self._enabled:
                self._step()
                self.publish_status()
            self._stop.wait(self._scan_interval)

    def _step(self) -> None:
        if self._state == FillState.WAITING:
            self._handle_waiting()
        elif self._state == FillState.FILLING:
            self._handle_filling()
        elif self._state == FillState.COOLDOWN:
            self._handle_cooldown()

    # ── state handlers ─────────────────────────────────────────

    def _handle_waiting(self) -> None:
        if self._should_fill():
            self._open_valve()
            self._state = FillState.FILLING
            self._fill_start = time.time()
            log.info("Autovalve: WAITING → FILLING")

    def _handle_filling(self) -> None:
        elapsed = time.time() - (self._fill_start or time.time())

        # Safety: fill timeout
        if elapsed > self._fill_timeout:
            log.warning(
                "Autovalve: fill timeout (%.0fs) — closing", elapsed
            )
            self._close_valve()
            self._transition_to_cooldown()
            return

        # Safety: overfill
        if self._overfill_value > self._overfill_threshold:
            log.warning(
                "Autovalve: overfill (%.2fV > %.2fV) — closing",
                self._overfill_value,
                self._overfill_threshold,
            )
            self._close_valve()
            self._transition_to_cooldown()
            return

        # Normal completion: level reached target
        for ch in self._level_channels:
            if self._latest_levels.get(ch, 0) >= self._full_tank:
                log.info(
                    "Autovalve: tank full (%s=%.2fV) — closing",
                    ch,
                    self._latest_levels[ch],
                )
                self._close_valve()
                self._transition_to_cooldown()
                return

    def _handle_cooldown(self) -> None:
        if self._last_fill_end is None:
            self._transition_to_cooldown()
            return
        elapsed = time.time() - self._last_fill_end
        if elapsed >= self._cooldown_time:
            self._state = FillState.WAITING
            log.info(
                "Autovalve: COOLDOWN → WAITING (%.0fs elapsed)", elapsed
            )

    def _transition_to_cooldown(self) -> None:
        self._state = FillState.COOLDOWN
        self._last_fill_end = time.time()
        self._fill_start = None
        log.info("Autovalve: → COOLDOWN")

    # ── decision logic ─────────────────────────────────────────

    def _should_fill(self) -> bool:
        if self._mode == "gradient":
            return self._check_gradient()
        elif self._mode == "threshold":
            return self._check_threshold()
        return False

    def _check_gradient(self) -> bool:
        for ch in self._level_channels:
            history = self._level_history.get(ch)
            if not history or len(history) < 10:
                continue
            grad = self._compute_gradient(history)
            if grad < self._gradient_trigger:
                log.info(
                    "Autovalve: gradient trigger on %s (%.2e < %.2e)",
                    ch,
                    grad,
                    self._gradient_trigger,
                )
                return True
        return False

    def _check_threshold(self) -> bool:
        for ch in self._level_channels:
            value = self._latest_levels.get(ch)
            if value is not None and value < self._full_tank * 0.5:
                return True
        return False

    @staticmethod
    def _compute_gradient(history: deque) -> float:
        if len(history) < 2:
            return 0.0
        t0, v0 = history[0]
        t1, v1 = history[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        return (v1 - v0) / dt

    # ── valve commands ─────────────────────────────────────────

    def _open_valve(self) -> None:
        self.publish_command(
            f"ets/commands/relay/{self._valve_relay}",
            {"action": "open", "source": "autovalve"},
        )

    def _close_valve(self) -> None:
        self.publish_command(
            f"ets/commands/relay/{self._valve_relay}",
            {"action": "close", "source": "autovalve"},
        )
