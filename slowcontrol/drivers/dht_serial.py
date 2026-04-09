"""DHT11/22 humidity sensor via Arduino serial bridge."""

from __future__ import annotations

import logging
from typing import Any

from slowcontrol.core.registry import register_driver
from slowcontrol.drivers.base import SensorDriver

log = logging.getLogger(__name__)


@register_driver("dht_serial")
class DHTSerialDriver(SensorDriver):
    """Read humidity + temperature from an Arduino DHT sensor over serial.

    Expects the Arduino to send lines in the format ``humidity,temperature``
    (e.g. ``45.2,22.1``).
    """

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        mqtt,
        poll_interval: float = 5.0,
    ):
        super().__init__(name, config, mqtt, poll_interval)
        self._port = config.get("port", "/dev/ttyUSB0")
        self._baud = config.get("baud", 9600)
        self._ser = None

    def connect(self) -> None:
        import serial

        self._ser = serial.Serial(self._port, self._baud, timeout=2)
        self._ser.reset_input_buffer()
        log.info("DHT serial connected on %s @ %d baud", self._port, self._baud)

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

            parts = line.split(",")
            if len(parts) >= 2:
                humidity = float(parts[0])
                temperature = float(parts[1])
                return {
                    "humidity": round(humidity, 1),
                    "temperature": round(temperature, 1),
                }
        except (ValueError, UnicodeDecodeError):
            log.warning("Failed to parse DHT data: %s", line)

        return {}
