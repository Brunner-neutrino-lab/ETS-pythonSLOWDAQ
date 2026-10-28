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

    A missing or dropped port (USB adapter unplugged / re-enumerating) is not
    fatal: the driver keeps polling and reopens the port as soon as it is
    back, logging one warning per outage rather than one per poll.
    """

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        mqtt,
        poll_interval: float = 5.0,
    ):
        super().__init__(name, config, mqtt, poll_interval)
        self._port = config.get("port", "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AK06FZMI-if00-port0")
        self._baud = config.get("baud", 9600)
        self._ser = None
        self._outage = False   # warned about the current outage already

    def connect(self) -> None:
        """Open the port.  Never raises: on failure the port stays closed and
        read() retries each poll (so a missing adapter at startup is fine)."""
        import serial

        try:
            ser = serial.Serial(self._port, self._baud, timeout=2)
            ser.reset_input_buffer()
        except (serial.SerialException, OSError) as exc:
            self._ser = None
            if not self._outage:
                self._outage = True
                log.warning("DHT serial port %s unavailable (%s); retrying every poll",
                            self._port, exc)
            return
        self._ser = ser
        if self._outage:
            self._outage = False
            log.info("DHT serial port %s is back", self._port)
        log.info("DHT serial connected on %s @ %d baud", self._port, self._baud)

    def disconnect(self) -> None:
        if self._ser:
            try:
                self._ser.close()
            except OSError:
                pass  # handle already dead after a USB drop
            self._ser = None

    def read(self) -> dict[str, Any]:
        import serial

        if not self._ser:
            # Port missing or lost (USB re-enumeration) — retry once per poll.
            self.connect()
            if not self._ser:
                return {}

        line = ""
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
        except serial.SerialException as exc:
            log.warning("DHT serial connection lost (%s); retrying every poll", exc)
            self._outage = True   # the warning above covers this outage
            self.disconnect()

        return {}
