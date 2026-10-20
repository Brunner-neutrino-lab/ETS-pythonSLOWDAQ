"""Humidity controller — PID output mapped to relay duty-cycling."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from slowcontrol.controllers.base import Controller
from slowcontrol.controllers.pid import PIDController
from slowcontrol.core.registry import register_controller

log = logging.getLogger(__name__)


@register_controller("humidity_pid")
class HumidityController(Controller):
    """PID-based humidity control with relay duty-cycling.

    The PID output (0–1) is converted into an on/off duty-cycle over a
    configurable period (default 600 s), driving a relay to control
    dry-nitrogen flow.

    Can be switched off at runtime via ``ets/commands/humidity/enable``
    ``{"enabled": false}``: the valve is closed and left for manual control
    until ``{"enabled": true}`` starts a fresh cycle.
    """

    def __init__(self, name: str, config: dict[str, Any], mqtt):
        super().__init__(name, config, mqtt)
        self._setpoint = config.get("setpoint", 7.2)
        self._relay = config.get("relay", "gn2_valve")
        self._cycle_duration = config.get("cycle_duration", 600)

        self._pid = PIDController(
            Kp=config.get("Kp", 1.0),
            Ki=config.get("Ki", 0.0),
            Kd=config.get("Kd", 0.0),
            setpoint=self._setpoint,
            output_min=0.0,
            output_max=1.0,
            reverse=True,  # dry N2 lowers humidity: purge harder when above setpoint
        )

        self._enabled = bool(config.get("start_enabled", True))
        self._current_humidity: float | None = None
        self._duty_cycle = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()   # cuts a cycle short (stop / on-off toggle)

    # ── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        super().start()
        self.mqtt.subscribe("ets/sensors/+/humidity", self._on_humidity)
        self.mqtt.subscribe(
            "ets/commands/humidity/setpoint", self._on_setpoint
        )
        self.mqtt.subscribe(
            "ets/commands/humidity/enable", self._on_enable
        )
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._duty_cycle_loop, name="humidity-pid", daemon=True
        )
        self._thread.start()
        log.info(
            "Humidity PID started (setpoint=%.1f%%, enabled=%s)",
            self._setpoint, self._enabled,
        )

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._relay_close()
        if self._thread:
            self._thread.join(timeout=self._cycle_duration + 5)
        super().stop()

    def get_state(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "setpoint": self._setpoint,
            "measured": self._current_humidity,
            "duty_cycle": round(self._duty_cycle, 3),
            "relay": self._relay,
            "ts": time.time(),
        }

    # ── MQTT handlers ──────────────────────────────────────────

    def _on_humidity(self, topic: str, payload: dict) -> None:
        value = payload.get("value")
        if value is not None:
            self._current_humidity = float(value)

    def _on_setpoint(self, topic: str, payload: dict) -> None:
        value = payload.get("value")
        if value is not None:
            self._setpoint = float(value)
            self._pid.setpoint = self._setpoint
            log.info("Humidity setpoint → %.1f%%", self._setpoint)
            self.publish_status()

    def _on_enable(self, topic: str, payload: dict) -> None:
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            log.warning("Bad humidity enable payload %r (want enabled: true|false)",
                        payload)
            return
        if enabled != self._enabled:
            self._enabled = enabled
            if enabled:
                self._pid.reset()   # fresh start, no stale integral/derivative
            log.info("Humidity PID %s", "enabled" if enabled
                     else "disabled (GN2 valve closed, manual control)")
            self._wake.set()        # act now, not at the end of the current cycle
        self.publish_status()

    # ── duty-cycle loop ────────────────────────────────────────

    def _sleep(self, seconds: float | None) -> bool:
        """Wait up to *seconds* (None = until woken).  True if cut short by
        stop() or an on/off toggle, in which case the loop restarts."""
        woke = self._wake.wait(seconds)
        if woke:
            self._wake.clear()
        return woke

    def _duty_cycle_loop(self) -> None:
        while not self._stop.is_set():
            if not self._enabled:
                # Parked: hold the valve closed until re-enabled.  Closing
                # here (not in the handler) also covers a toggle that lands
                # between the enabled check and _relay_open below.
                self._duty_cycle = 0.0
                self._relay_close()
                self.publish_status()
                self._sleep(None)
                continue

            # Compute PID
            if self._current_humidity is not None:
                self._duty_cycle = self._pid.update(self._current_humidity)
            else:
                self._duty_cycle = 0.0

            on_time = self._duty_cycle * self._cycle_duration
            off_time = self._cycle_duration - on_time

            # ON phase
            if on_time > 0:
                self._relay_open()
                self.publish_status()
                if self._sleep(on_time):
                    continue

            # OFF phase — skipped at 100 % duty so the valve stays open
            # across cycles instead of blipping closed at each boundary.
            if off_time > 0:
                self._relay_close()
                self.publish_status()
                if self._sleep(off_time):
                    continue

    def _relay_open(self) -> None:
        self.publish_command(
            f"ets/commands/relay/{self._relay}",
            {"action": "open", "source": "humidity_pid"},
        )

    def _relay_close(self) -> None:
        self.publish_command(
            f"ets/commands/relay/{self._relay}",
            {"action": "close", "source": "humidity_pid"},
        )
