"""Abstract base class for autonomous controllers."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from slowcontrol.core.mqtt import MQTTClient

log = logging.getLogger(__name__)


class Controller(ABC):
    """Base class for autonomous control loops.

    Controllers subscribe to MQTT sensor topics, apply logic,
    and publish MQTT commands (relay actions, setpoints, etc.).
    """

    def __init__(self, name: str, config: dict[str, Any], mqtt: MQTTClient):
        self.name = name
        self.config = config
        self.mqtt = mqtt
        self._running = False

    @abstractmethod
    def start(self) -> None:
        """Subscribe to topics and begin control loop."""
        self._running = True

    @abstractmethod
    def stop(self) -> None:
        """Unsubscribe and clean up."""
        self._running = False

    @abstractmethod
    def get_state(self) -> dict[str, Any]:
        """Return current controller state for status reporting."""

    def publish_command(self, topic: str, payload: dict) -> None:
        self.mqtt.publish(topic, payload)

    def publish_status(self) -> None:
        state = self.get_state()
        self.mqtt.publish(f"ets/status/{self.name}", state, retain=True)
