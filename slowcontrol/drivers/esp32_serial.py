"""ESP32 serial driver — reads JSON sensor data over USB serial."""

from __future__ import annotations

import json
import logging
from typing import Any

from slowcontrol.core.registry import register_driver
from slowcontrol.drivers.base import SensorDriver

log = logging.getLogger(__name__)


@register_driver("esp32_serial")
class ESP32SerialDriver(SensorDriver):
    """Read JSON-formatted sensor data from an ESP32 over serial.

    Expects the ESP32 to print one JSON object per line, e.g.::

        {"temperature": 22.1, "humidity": 45.2, "pressure": 1013.25}

    Each key becomes an MQTT channel under this driver's topic prefix.
    """

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        mqtt,
        poll_interval: float = 1.0,
    ):
        super().__init__(name, config, mqtt, poll_interval)
        self._port = config.get("port", "/dev/ttyUSB1")
        self._baud = config.get("baud", 115200)
        self._ser = None

    def connect(self) -> None:
        import serial

        self._ser = serial.Serial(self._port, self._baud, timeout=2)
        self._ser.reset_input_buffer()
        log.info(
            "ESP32 serial connected on %s @ %d baud", self._port, self._baud
        )

    def disconnect(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
            self._ser = None

    def read(self) -> dict[str, Any]:
        if not self._ser:
            return {}

        try:
            line = self._ser.readline().decode().strip()
            if not line:
                return {}
            data = json.loads(line)
            if isinstance(data, dict):
                return {
                    k: v for k, v in data.items() if isinstance(v, (int, float))
                }
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            log.debug("Failed to parse ESP32 data")

        return {}
