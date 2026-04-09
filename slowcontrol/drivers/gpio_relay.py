"""GPIO relay controller for Raspberry Pi."""

from __future__ import annotations

import logging
import time
from typing import Any

from slowcontrol.core.registry import register_driver

log = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO

    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False


@register_driver("gpio_relay")
class GPIORelayManager:
    """Manages GPIO-controlled relays.  Subscribes to MQTT commands.

    Not a regular polling sensor — event-driven via MQTT.
    On startup each relay is set to its configured default (usually closed).
    On shutdown every relay is forced closed for safety.
    """

    def __init__(self, name: str, config: dict[str, Any], mqtt, **kwargs):
        self.name = name
        self.mqtt = mqtt
        self._relays = config  # {relay_name: {pin, default, active_low}}
        self._states: dict[str, str] = {}
        self._handlers: dict[str, Any] = {}

    # ── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        if HAS_GPIO:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)

        for rname, rcfg in self._relays.items():
            pin = rcfg["pin"]
            default = rcfg.get("default", "closed")
            active_low = rcfg.get("active_low", True)

            if HAS_GPIO:
                GPIO.setup(pin, GPIO.OUT)
                self._set_pin(pin, default, active_low)

            self._states[rname] = default

            handler = self._make_handler(rname, rcfg)
            self._handlers[rname] = handler
            topic = f"ets/commands/relay/{rname}"
            self.mqtt.subscribe(topic, handler)
            self._publish_state(rname)

        log.info("Relay manager started: %s", list(self._relays.keys()))

    def stop(self) -> None:
        for rname, rcfg in self._relays.items():
            if HAS_GPIO:
                self._set_pin(
                    rcfg["pin"], "closed", rcfg.get("active_low", True)
                )
            self._states[rname] = "closed"
            self._publish_state(rname)

            topic = f"ets/commands/relay/{rname}"
            handler = self._handlers.get(rname)
            if handler:
                self.mqtt.unsubscribe(topic, handler)

        if HAS_GPIO:
            GPIO.cleanup()
        log.info("Relay manager stopped — all relays closed")

    @property
    def is_running(self) -> bool:
        return bool(self._states)

    def get_state(self, relay_name: str) -> str:
        return self._states.get(relay_name, "unknown")

    # ── internal ───────────────────────────────────────────────

    def _make_handler(self, rname: str, rcfg: dict):
        def handler(topic: str, payload: dict) -> None:
            action = payload.get("action", "").lower()
            if action in ("open", "close", "closed"):
                if action == "close":
                    action = "closed"
                pin = rcfg["pin"]
                active_low = rcfg.get("active_low", True)
                if HAS_GPIO:
                    self._set_pin(pin, action, active_low)
                self._states[rname] = action
                self._publish_state(rname)
                log.info("Relay %s → %s (pin %d)", rname, action, pin)

        return handler

    @staticmethod
    def _set_pin(pin: int, state: str, active_low: bool) -> None:
        if state == "open":
            GPIO.output(pin, GPIO.LOW if active_low else GPIO.HIGH)
        else:  # closed
            GPIO.output(pin, GPIO.HIGH if active_low else GPIO.LOW)

    def _publish_state(self, rname: str) -> None:
        self.mqtt.publish(
            f"ets/status/relays/{rname}",
            {"state": self._states[rname], "ts": time.time()},
            retain=True,
        )
