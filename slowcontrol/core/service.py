"""Main orchestrator — starts drivers, controllers, and manages lifecycle."""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any

from slowcontrol.core.config import AppConfig
from slowcontrol.core.mqtt import MQTTClient
from slowcontrol.core.registry import (
    CONTROLLER_REGISTRY,
    DRIVER_REGISTRY,
    load_plugins,
)
from slowcontrol.controllers.base import Controller
from slowcontrol.drivers.base import SensorDriver

log = logging.getLogger(__name__)


class SlowControlService:
    """Top-level service that owns all drivers, relays, and controllers."""

    def __init__(self, config_path: str | Path):
        self.config = AppConfig.from_yaml(config_path)
        self.mqtt = MQTTClient(
            broker=self.config.mqtt.broker,
            port=self.config.mqtt.port,
            client_id=self.config.mqtt.client_id,
        )
        self.drivers: dict[str, SensorDriver] = {}
        self.controllers: dict[str, Controller] = {}
        self._relay_manager: Any = None
        self._stop = threading.Event()

    # ── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        load_plugins()
        self.mqtt.connect()
        self._start_relays()
        self._start_drivers()
        self._start_controllers()
        self._start_interlocks()
        self._start_heartbeat()
        log.info("Slow control service started")

    def stop(self) -> None:
        log.info("Shutting down slow control service")
        self._stop.set()
        for ctrl in self.controllers.values():
            ctrl.stop()
        for driver in self.drivers.values():
            driver.stop()
        if self._relay_manager:
            self._relay_manager.stop()
        self.mqtt.disconnect()
        log.info("Slow control service stopped")

    def run_forever(self) -> None:
        """Block until SIGINT / SIGTERM."""
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        finally:
            self.stop()

    # ── internal start helpers ─────────────────────────────────

    def _start_relays(self) -> None:
        if not self.config.relays:
            return
        if "gpio_relay" not in DRIVER_REGISTRY:
            log.warning("GPIO relay driver not available — relays disabled")
            return
        relay_cls = DRIVER_REGISTRY["gpio_relay"]
        relay_configs = {
            name: {
                "pin": r.pin,
                "default": r.default,
                "active_low": r.active_low,
            }
            for name, r in self.config.relays.items()
        }
        self._relay_manager = relay_cls("relays", relay_configs, self.mqtt)
        self._relay_manager.start()

    def _start_drivers(self) -> None:
        for name, dcfg in self.config.drivers.items():
            if dcfg.type not in DRIVER_REGISTRY:
                log.error("Unknown driver type '%s' for '%s'", dcfg.type, name)
                continue
            driver_cls = DRIVER_REGISTRY[dcfg.type]
            try:
                driver = driver_cls(
                    name=name,
                    config=dcfg.params,
                    mqtt=self.mqtt,
                    poll_interval=dcfg.poll_interval,
                )
                driver.start()
                self.drivers[name] = driver
            except Exception:
                log.exception("Failed to start driver '%s'", name)

    def _start_controllers(self) -> None:
        for name, ccfg in self.config.controllers.items():
            if not ccfg.enabled:
                log.info("Controller '%s' is disabled", name)
                continue
            if name not in CONTROLLER_REGISTRY:
                log.error("Unknown controller '%s'", name)
                continue
            ctrl_cls = CONTROLLER_REGISTRY[name]
            try:
                ctrl = ctrl_cls(name=name, config=ccfg.params, mqtt=self.mqtt)
                ctrl.start()
                self.controllers[name] = ctrl
            except Exception:
                log.exception("Failed to start controller '%s'", name)

    def _start_interlocks(self) -> None:
        if not self.config.interlocks.enabled:
            return
        if "interlocks" not in CONTROLLER_REGISTRY:
            log.warning("Interlock controller not available")
            return
        interlock_cfg = {
            "watchdog_timeout": self.config.interlocks.watchdog_timeout,
            "rules": {
                rname: {
                    "topic": r.topic,
                    "condition": r.condition,
                    "threshold": r.threshold,
                    "action": r.action,
                    "relay": r.relay,
                }
                for rname, r in self.config.interlocks.rules.items()
            },
        }
        ctrl_cls = CONTROLLER_REGISTRY["interlocks"]
        ctrl = ctrl_cls("interlocks", interlock_cfg, self.mqtt)
        ctrl.start()
        self.controllers["interlocks"] = ctrl

    def _start_heartbeat(self) -> None:
        def heartbeat():
            start = time.time()
            while not self._stop.is_set():
                self.mqtt.publish(
                    "ets/status/service/heartbeat",
                    {
                        "uptime": round(time.time() - start, 1),
                        "drivers": list(self.drivers.keys()),
                        "controllers": list(self.controllers.keys()),
                        "ts": time.time(),
                    },
                    retain=True,
                )
                self._stop.wait(10.0)

        t = threading.Thread(target=heartbeat, name="heartbeat", daemon=True)
        t.start()
