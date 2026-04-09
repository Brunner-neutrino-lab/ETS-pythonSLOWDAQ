"""Abstract base class for sensor drivers."""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

from slowcontrol.core.mqtt import MQTTClient

log = logging.getLogger(__name__)


class SensorDriver(ABC):
    """Base class for all hardware sensor drivers.

    Subclasses implement connect / read / disconnect.
    The base class handles the polling-loop thread and MQTT publishing.
    """

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        mqtt: MQTTClient,
        poll_interval: float = 1.0,
    ):
        self.name = name
        self.config = config
        self.mqtt = mqtt
        self.poll_interval = poll_interval
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = False

    @property
    def topic_prefix(self) -> str:
        return f"ets/sensors/{self.name}"

    # ── subclass interface ─────────────────────────────────────

    @abstractmethod
    def connect(self) -> None:
        """Open connection to hardware."""

    @abstractmethod
    def read(self) -> dict[str, Any]:
        """Read all channels.  Return ``{channel_name: value}``."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close hardware connection."""

    # ── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        log.info("Starting driver: %s", self.name)
        self.connect()
        self._connected = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name=f"driver-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        log.info("Stopping driver: %s", self.name)
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._connected:
            self.disconnect()
            self._connected = False

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── internal ───────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                readings = self.read()
                ts = time.time()
                for channel, value in readings.items():
                    topic = f"{self.topic_prefix}/{channel}"
                    self.mqtt.publish(topic, {"value": value, "ts": ts})
            except Exception:
                log.exception("Error reading %s", self.name)
            self._stop.wait(self.poll_interval)
