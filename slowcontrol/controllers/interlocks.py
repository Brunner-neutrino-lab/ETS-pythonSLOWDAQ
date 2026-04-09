"""Software safety interlock system."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from slowcontrol.controllers.base import Controller
from slowcontrol.core.registry import register_controller

log = logging.getLogger(__name__)

# Comparison operators by name
_OPS = {
    ">": float.__gt__,
    "<": float.__lt__,
    ">=": float.__ge__,
    "<=": float.__le__,
}


@register_controller("interlocks")
class InterlockManager(Controller):
    """Watchdog-based safety interlock system.

    Subscribes to all sensor data.  If any rule is violated the
    corresponding relay is forced closed and an alert is published.
    A watchdog timer closes all valves if no sensor data arrives for
    ``watchdog_timeout`` seconds.
    """

    def __init__(self, name: str, config: dict[str, Any], mqtt):
        super().__init__(name, config, mqtt)
        self._watchdog_timeout: float = config.get("watchdog_timeout", 60)
        self._rules: dict[str, dict] = config.get("rules", {})
        self._violations: dict[str, str] = {}
        self._last_sensor_time: float = time.time()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        super().start()
        self.mqtt.subscribe("ets/sensors/#", self._on_sensor)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._watchdog_loop, name="interlocks", daemon=True
        )
        self._thread.start()
        log.info("Interlock manager started (%d rules)", len(self._rules))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        super().stop()

    def get_state(self) -> dict[str, Any]:
        return {
            "all_ok": len(self._violations) == 0,
            "violations": dict(self._violations),
            "last_sensor_time": self._last_sensor_time,
            "ts": time.time(),
        }

    # ── sensor monitoring ──────────────────────────────────────

    def _on_sensor(self, topic: str, payload: dict) -> None:
        self._last_sensor_time = time.time()
        value = payload.get("value")
        if value is None:
            return

        for rule_name, rule in self._rules.items():
            rule_topic = rule.get("topic", "")
            if topic != rule_topic:
                continue

            threshold = float(rule.get("threshold", 0))
            condition = rule.get("condition", ">")
            op = _OPS.get(condition)
            if op is None:
                continue

            if op(float(value), threshold):
                if rule_name not in self._violations:
                    msg = f"{topic}: {value} {condition} {threshold}"
                    self._violations[rule_name] = msg
                    log.warning("INTERLOCK TRIGGERED: %s — %s", rule_name, msg)
                    self._execute_action(rule)
                    self.publish_status()
                    self.mqtt.publish(
                        f"ets/alerts/warning/{rule_name}",
                        {
                            "message": msg,
                            "value": value,
                            "threshold": threshold,
                            "ts": time.time(),
                        },
                    )
            else:
                self._violations.pop(rule_name, None)

    def _execute_action(self, rule: dict) -> None:
        relay = rule.get("relay")
        if relay:
            self.publish_command(
                f"ets/commands/relay/{relay}",
                {"action": "close", "source": "interlock"},
            )

    # ── watchdog ───────────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            elapsed = time.time() - self._last_sensor_time
            if elapsed > self._watchdog_timeout:
                if "watchdog" not in self._violations:
                    msg = f"No sensor data for {elapsed:.0f}s"
                    self._violations["watchdog"] = msg
                    log.critical(
                        "WATCHDOG: %s — closing all relays", msg
                    )
                    self._close_all_relays()
                    self.publish_status()
            elif "watchdog" in self._violations:
                del self._violations["watchdog"]
                log.info("Watchdog: sensor data resumed")
                self.publish_status()
            self._stop.wait(5.0)

    def _close_all_relays(self) -> None:
        for name in ("ln2_valve", "gn2_valve"):
            self.publish_command(
                f"ets/commands/relay/{name}",
                {"action": "close", "source": "watchdog"},
            )
