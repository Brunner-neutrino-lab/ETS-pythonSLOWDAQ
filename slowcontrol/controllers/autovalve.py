"""Autonomous LN2 fill-valve controller: separate auto-fill and auto-close.

Auto-close (on at boot) supervises the LN2 valve whoever opened it — the
auto-fill, or an operator pressing "open" — and closes it as soon as ANY
close condition holds:
  * the fill timer (fill_timeout) has run out since the valve opened,
  * the overfill channel is above overfill_threshold,
  * any of the optional close_channels is at or above full_tank.

Auto-fill (always off at boot) opens the valve when ALL fill conditions
hold: every fill channel below fill_trigger (threshold mode) or falling
faster than gradient_trigger (gradient mode), the cooldown since the last
close has elapsed, and no close condition is already active.

There is never an automated fill without automated close: turning auto-fill
on turns auto-close on, and turning auto-close off turns auto-fill off.
"""

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
    WAITING = "WAITING"     # valve closed; auto-fill may open it
    FILLING = "FILLING"     # valve open (auto-fill or manual)
    COOLDOWN = "COOLDOWN"   # valve closed recently; auto-fill holds off


# How long to wait for the relay to confirm an auto-fill "open" before retrying.
_OPEN_CONFIRM_S = 5.0


@register_controller("autovalve")
class AutovalveController(Controller):
    """LN2 fill valve: auto-fill (all-of open conditions) plus auto-close
    (any-of close conditions).  Publishes relay commands over MQTT.
    """

    def __init__(self, name: str, config: dict[str, Any], mqtt):
        super().__init__(name, config, mqtt)

        self._valve_relay: str = config.get("valve_relay", "ln2_valve")
        self._scan_interval: float = config.get("scan_interval", 1.0)

        # Auto-close conditions (any one closes the valve)
        self._fill_timeout: float = config.get("fill_timeout", 600)
        self._overfill_channel: str = config.get("overfill_channel", "RES7")
        self._overfill_threshold: float = config.get("overfill_threshold", 9.8)
        self._overfill_close: float = config.get("overfill_close", 9.0)  # reported only
        self._close_channels: list[str] = list(config.get("close_channels", []))
        self._full_tank: float = config.get("full_tank", 9.5)

        # Auto-fill conditions (all must hold to open the valve).
        # `level_channels` is the pre-split name, still accepted.
        self._mode: str = config.get("mode", "threshold")
        self._fill_channels: list[str] = list(
            config.get("fill_channels", config.get("level_channels", ["RES1"]))
        )
        self._fill_trigger: float = config.get(
            "fill_trigger", self._full_tank * 0.5
        )
        self._gradient_trigger: float = config.get("gradient_trigger", -1e-4)
        self._cooldown_time: float = config.get("cooldown", 900)

        # On/off switches.  Deliberately neither read from config nor
        # persisted: auto-fill is ALWAYS off at boot (an operator must arm
        # it), auto-close is always on.
        self._autofill = False
        self._autoclose = True
        if config.get("start_enabled"):
            log.warning("autovalve: start_enabled is ignored; auto-fill is "
                        "always off at boot")

        # Valve tracking, driven by ets/status/relays/<valve_relay>.
        self._valve_open: bool | None = None     # None until the relay reports
        self._opened_at: float | None = None
        self._opened_by_autofill = False
        self._open_sent_at: float | None = None  # auto-fill "open" awaiting confirm
        self._close_reason: str | None = None    # set once we decide to close
        self._last_close_at: float | None = None
        self._last_close_reason: str | None = None
        self._state = FillState.WAITING

        # Rolling gradient buffer per fill channel (≈60 samples at 1 Hz)
        self._level_history: dict[str, deque] = {
            ch: deque(maxlen=60) for ch in self._fill_channels
        }
        self._latest_levels: dict[str, float] = {}

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        # Persisted runtime state.  Path defaults to runtime/autovalve.yaml
        # under the working dir (the systemd unit sets WorkingDirectory=
        # /home/pi/ETS-pythonSLOWDAQ).  Override with ETS_RUNTIME_DIR.
        # mode + tunables (NOT the on/off switches) are written on every
        # change and reapplied here over the config defaults set above.
        runtime_dir = Path(os.environ.get("ETS_RUNTIME_DIR", "runtime"))
        self._persist_path = runtime_dir / "autovalve.yaml"
        self._load_persisted_state()

    @property
    def _valve_topic(self) -> str:
        return f"ets/status/relays/{self._valve_relay}"

    # ── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        super().start()
        self.mqtt.subscribe("ets/sensors/#", self._on_sensor_data)
        self.mqtt.subscribe(self._valve_topic, self._on_valve_status)
        self.mqtt.subscribe("ets/commands/autovalve/autofill", self._on_autofill_command)
        self.mqtt.subscribe("ets/commands/autovalve/autoclose", self._on_autoclose_command)
        self.mqtt.subscribe("ets/commands/autovalve/mode", self._on_mode_command)
        self.mqtt.subscribe("ets/commands/autovalve/set", self._on_set_command)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._control_loop, name="autovalve", daemon=True
        )
        self._thread.start()
        # Republish so the retained status reflects the current (post-restart)
        # state rather than whatever was retained before the bounce.
        self.publish_status()
        log.info("Autovalve controller started (auto-fill=OFF, auto-close=ON, "
                 "fill mode=%s)", self._mode)

    def stop(self) -> None:
        self._stop.set()
        self._close_valve()
        if self._thread:
            self._thread.join(timeout=5)
        super().stop()

    def get_state(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            valve_open = self._valve_open
            return {
                "state": self._state.value,
                "mode": self._mode,
                "autofill": self._autofill,
                "autoclose": self._autoclose,
                "valve_open": valve_open,
                "valve_open_s": (round(now - self._opened_at, 1)
                                 if valve_open and self._opened_at else None),
                "fill_source": (("auto-fill" if self._opened_by_autofill else "manual")
                                if valve_open else None),
                "last_close_reason": self._last_close_reason,
                "last_close_at": self._last_close_at,
                "overfill_value": self._latest_levels.get(self._overfill_channel),
                "levels": dict(self._latest_levels),
                # Tunables — exposed so the GUI can read them back.  Each is also
                # settable via ets/commands/autovalve/set {<param>: <value>}.
                "fill_channels": list(self._fill_channels),
                "fill_trigger": self._fill_trigger,
                "gradient_trigger": self._gradient_trigger,
                "cooldown": self._cooldown_time,
                "close_channels": list(self._close_channels),
                "full_tank": self._full_tank,
                "fill_timeout": self._fill_timeout,
                "overfill_channel": self._overfill_channel,
                "overfill_threshold": self._overfill_threshold,
                "overfill_close": self._overfill_close,
                "ts": now,
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

        history = self._level_history.get(channel)
        if history is not None:
            history.append((ts, value))

    def _on_valve_status(self, topic: str, payload: dict) -> None:
        state = payload.get("state")
        if state not in ("open", "closed"):
            return
        is_open = state == "open"
        with self._lock:
            now = time.time()
            was_open = self._valve_open
            self._valve_open = is_open
            if is_open and not was_open:
                self._opened_at = now
                self._opened_by_autofill = self._open_sent_at is not None
                self._open_sent_at = None
                self._close_reason = None
                self._state = FillState.FILLING
                log.info(
                    "Autovalve: LN2 valve opened (%s); auto-close %s",
                    "auto-fill" if self._opened_by_autofill else "manual",
                    f"ON, timer {self._fill_timeout:.0f} s" if self._autoclose
                    else "OFF - it will NOT close automatically",
                )
            elif was_open and not is_open:
                held = now - self._opened_at if self._opened_at else None
                reason = self._close_reason
                if reason is None:
                    # Closed by someone else: the interlock usually beats our
                    # 1 Hz scan on overfill, so say which condition was active.
                    active = self._close_condition(now)
                    reason = (f"closed externally while {active}" if active
                              else "closed externally (manual or interlock)")
                self._last_close_at = now
                self._last_close_reason = reason
                self._opened_at = None
                self._opened_by_autofill = False
                self._close_reason = None
                self._state = FillState.COOLDOWN
                log.info("Autovalve: LN2 valve closed after %s: %s -> COOLDOWN",
                         f"{held:.0f} s" if held is not None else "?", reason)
        self.publish_status()

    def _on_autofill_command(self, topic: str, payload: dict) -> None:
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            log.warning("autovalve/autofill: want {\"enabled\": true|false}, got %r", payload)
            return
        self._set_autofill(enabled)
        self.publish_status()

    def _on_autoclose_command(self, topic: str, payload: dict) -> None:
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            log.warning("autovalve/autoclose: want {\"enabled\": true|false}, got %r", payload)
            return
        self._set_autoclose(enabled)
        self.publish_status()

    def _on_mode_command(self, topic: str, payload: dict) -> None:
        """Fill-condition type (gradient | threshold), plus the pre-split
        aliases auto (= auto-fill on) and manual (= auto-fill off)."""
        mode = payload.get("mode")
        if mode == "auto":
            self._set_autofill(True)
        elif mode == "manual":
            self._set_autofill(False)
        elif mode in ("gradient", "threshold"):
            with self._lock:
                self._mode = mode
            log.info("Autovalve: fill mode -> %s", mode)
            self._save_persisted_state()
        else:
            log.warning("autovalve/mode: unknown mode %r ignored", mode)
            return
        self.publish_status()

    def _set_autofill(self, on: bool) -> None:
        with self._lock:
            if on:
                if not self._autoclose:
                    self._autoclose = True
                    log.info("Autovalve: auto-close ON (required by auto-fill)")
                if not self._autofill:
                    self._autofill = True
                    log.info("Autovalve: auto-fill ON")
            elif self._autofill:
                self._autofill = False
                self._open_sent_at = None
                log.info("Autovalve: auto-fill OFF")
                if self._valve_open and self._opened_by_autofill:
                    self._request_close("auto-fill switched off")

    def _set_autoclose(self, on: bool) -> None:
        with self._lock:
            if on:
                if not self._autoclose:
                    self._autoclose = True
                    log.info("Autovalve: auto-close ON")
                return
            if self._autofill:
                # Never an automated fill without automated close.
                self._set_autofill(False)
            if self._autoclose:
                self._autoclose = False
                log.warning("Autovalve: auto-close OFF - the LN2 valve will not "
                            "be closed automatically")

    # Whitelist of operator-tunable parameters: attribute name → coercer.
    # Times are seconds, thresholds are volts, channels are MQTT channel names
    # (the last segment of ets/sensors/<driver>/<channel>).
    _TUNABLES = {
        # auto-fill
        "fill_channels":      ("_fill_channels",     "channel_list"),
        "level_channels":     ("_fill_channels",     "channel_list"),  # legacy name
        "fill_trigger":       ("_fill_trigger",      "float"),
        "gradient_trigger":   ("_gradient_trigger",  "float"),
        "cooldown":           ("_cooldown_time",     "non_negative_float"),
        # auto-close
        "fill_timeout":       ("_fill_timeout",      "positive_float"),
        "overfill_channel":   ("_overfill_channel",  "channel_str"),
        "overfill_threshold": ("_overfill_threshold", "float"),
        "overfill_close":     ("_overfill_close",    "float"),
        "close_channels":     ("_close_channels",    "channel_list_or_empty"),
        "full_tank":          ("_full_tank",          "float"),
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
        with self._lock:
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
                self._apply_tunable(attr, value)
                applied[key] = value
                log.info("Autovalve param %s -> %s", key, value)

        if applied:
            self.publish_status()
            self._save_persisted_state()

    def _apply_tunable(self, attr: str, value: Any) -> None:
        if attr == "_fill_channels":
            # Keep history for channels that stay selected so gradient mode
            # doesn't start from scratch; drop removed ones.
            self._level_history = {
                ch: deque(self._level_history.get(ch, ()), maxlen=60)
                for ch in value
            }
        setattr(self, attr, value)

    # ── persistence ────────────────────────────────────────────
    # Written whenever a tunable or the fill mode changes; reloaded at
    # startup so operator tweaks survive a service bounce.  The auto-fill /
    # auto-close switches are deliberately NOT persisted (see __init__).

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
        if "fill_channels" in data:
            data.pop("level_channels", None)   # pre-split name; new one wins

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
            self._apply_tunable(attr, value)
            applied.append(key)
        if applied:
            log.info("autovalve: applied persisted runtime state from %s (%s)",
                     path, ", ".join(applied))

    def _save_persisted_state(self) -> None:
        path = self._persist_path
        with self._lock:
            data = {
                "mode":               self._mode,
                "fill_channels":      list(self._fill_channels),
                "fill_trigger":       self._fill_trigger,
                "gradient_trigger":   self._gradient_trigger,
                "cooldown":           self._cooldown_time,
                "fill_timeout":       self._fill_timeout,
                "overfill_channel":   self._overfill_channel,
                "overfill_threshold": self._overfill_threshold,
                "overfill_close":     self._overfill_close,
                "close_channels":     list(self._close_channels),
                "full_tank":          self._full_tank,
            }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("# Autovalve runtime state — written automatically when a\n"
                        "# tunable or mode is changed via ets/commands/autovalve/{set,mode}.\n"
                        "# Delete this file to revert to the defaults in config.yaml.\n"
                        "# The auto-fill / auto-close switches are intentionally NOT\n"
                        "# persisted: auto-fill always boots OFF, auto-close ON.\n")
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
        if kind in ("channel_list", "channel_list_or_empty"):
            if not isinstance(raw, (list, tuple)):
                raise TypeError("expected a list of channel names")
            cleaned = [str(c).strip() for c in raw if str(c).strip()]
            if not cleaned and kind == "channel_list":
                raise ValueError("at least one channel required")
            return cleaned
        raise ValueError(f"unknown coercer {kind!r}")

    # ── control loop ───────────────────────────────────────────

    def _control_loop(self) -> None:
        while not self._stop.is_set():
            self._step()
            self.publish_status()
            self._stop.wait(self._scan_interval)

    def _step(self) -> None:
        with self._lock:
            now = time.time()

            if self._valve_open:
                # Auto-close: any one condition closes the valve, whoever opened it.
                if self._close_reason is None and self._autoclose:
                    reason = self._close_condition(now)
                    if reason:
                        self._request_close(reason)
                        return
                if self._close_reason is not None:
                    self._close_valve()   # repeat until the relay reports closed
                return

            if self._valve_open is None:
                return   # relay state not reported yet: never open blind

            if self._state == FillState.COOLDOWN:
                if (self._last_close_at is not None
                        and now - self._last_close_at < self._cooldown_time):
                    return
                self._state = FillState.WAITING
                log.info("Autovalve: COOLDOWN -> WAITING (%.0fs elapsed)",
                         now - (self._last_close_at or now))

            if not (self._autofill and self._autoclose):
                return
            if self._open_sent_at is not None:
                if now - self._open_sent_at < _OPEN_CONFIRM_S:
                    return
                log.warning("Autovalve: auto-fill open not confirmed by the relay; retrying")
                self._open_sent_at = None

            # Auto-fill: all conditions must hold, and none of the close
            # conditions (the timer aside) may already be true.
            why = self._fill_condition()
            if why is None:
                return
            blocked = self._close_condition(now, include_timer=False)
            if blocked:
                return
            log.info("Autovalve: auto-fill, %s -> opening", why)
            self._open_sent_at = now
            self._open_valve()

    def _request_close(self, reason: str) -> None:
        if self._close_reason is None:
            self._close_reason = reason
            log.warning("Autovalve: auto-close, %s -> closing", reason)
        self._close_valve()

    # ── decision logic ─────────────────────────────────────────

    def _close_condition(self, now: float, include_timer: bool = True) -> str | None:
        """Return why the valve should close (first condition that holds), or None."""
        if include_timer and self._opened_at is not None:
            elapsed = now - self._opened_at
            if elapsed >= self._fill_timeout:
                return f"fill timer ({elapsed:.0f} s >= {self._fill_timeout:.0f} s)"
        ov = self._latest_levels.get(self._overfill_channel)
        if ov is not None and ov > self._overfill_threshold:
            return (f"overfill ({self._overfill_channel}={ov:.2f} V > "
                    f"{self._overfill_threshold:.2f} V)")
        for ch in self._close_channels:
            v = self._latest_levels.get(ch)
            if v is not None and v >= self._full_tank:
                return f"full ({ch}={v:.2f} V >= {self._full_tank:.2f} V)"
        return None

    def _fill_condition(self) -> str | None:
        """Return a description if ALL fill conditions hold, else None.
        A channel with no data yet counts as not met (never fill blind)."""
        if not self._fill_channels:
            return None
        parts = []
        if self._mode == "threshold":
            for ch in self._fill_channels:
                v = self._latest_levels.get(ch)
                if v is None or v >= self._fill_trigger:
                    return None
                parts.append(f"{ch}={v:.2f} V")
            return (f"all fill channels below {self._fill_trigger:.2f} V "
                    f"({', '.join(parts)})")
        if self._mode == "gradient":
            for ch in self._fill_channels:
                history = self._level_history.get(ch)
                if not history or len(history) < 10:
                    return None
                grad = self._compute_gradient(history)
                if grad >= self._gradient_trigger:
                    return None
                parts.append(f"{ch}={grad:.2e} V/s")
            return (f"all fill channels falling faster than "
                    f"{self._gradient_trigger:.2e} V/s ({', '.join(parts)})")
        return None

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
