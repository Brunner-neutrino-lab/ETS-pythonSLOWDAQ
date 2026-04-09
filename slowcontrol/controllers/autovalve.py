"""Autonomous LN2 fill-valve controller (state machine)."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from enum import Enum
from typing import Any

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

        # Runtime state
        self._state = FillState.WAITING
        self._fill_start: float | None = None
        self._last_fill_end: float | None = None
        self._enabled = True

        # Rolling gradient buffer (≈60 samples at 1 Hz)
        self._level_history: dict[str, deque] = {
            ch: deque(maxlen=60) for ch in self._level_channels
        }
        self._overfill_value = 0.0
        self._latest_levels: dict[str, float] = {}

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        super().start()
        self.mqtt.subscribe("ets/sensors/#", self._on_sensor_data)
        self.mqtt.subscribe(
            "ets/commands/autovalve/mode", self._on_mode_command
        )
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._control_loop, name="autovalve", daemon=True
        )
        self._thread.start()
        log.info("Autovalve controller started (mode=%s)", self._mode)

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
        self.publish_status()

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
