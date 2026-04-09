"""LakeShore 350 temperature controller driver (Ethernet / TCP SCPI)."""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any

from slowcontrol.core.registry import register_driver
from slowcontrol.drivers.base import SensorDriver

log = logging.getLogger(__name__)


@register_driver("lakeshore350")
class LakeShore350Driver(SensorDriver):
    """Read RTDs and heater output; accept setpoint commands via MQTT."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        mqtt,
        poll_interval: float = 1.0,
    ):
        super().__init__(name, config, mqtt, poll_interval)
        self._host = config["host"]
        self._port = config.get("port", 7777)
        self._inputs = config.get("inputs", ["A", "B", "C", "D"])
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    # ── lifecycle ──────────────────────────────────────────────

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(5.0)
        self._sock.connect((self._host, self._port))
        idn = self._query("*IDN?")
        log.info("LakeShore 350 connected: %s", idn.strip())
        self.mqtt.subscribe(
            "ets/commands/lakeshore/setpoint", self._on_setpoint
        )

    def disconnect(self) -> None:
        self.mqtt.unsubscribe(
            "ets/commands/lakeshore/setpoint", self._on_setpoint
        )
        if self._sock:
            self._sock.close()
            self._sock = None

    # ── polling ────────────────────────────────────────────────

    def read(self) -> dict[str, Any]:
        readings: dict[str, Any] = {}

        for inp in self._inputs:
            temp = self._read_temperature(inp)
            if temp is not None:
                readings[f"rtd/{inp}"] = round(temp, 4)

        heater = self._read_heater(1)
        if heater is not None:
            readings["heater/1"] = round(heater, 2)

        setpoint = self._read_setpoint(1)
        if setpoint is not None:
            readings["setpoint/1"] = round(setpoint, 4)

        return readings

    # ── SCPI helpers ───────────────────────────────────────────

    def _read_temperature(self, ch: str) -> float | None:
        try:
            return float(self._query(f"KRDG? {ch}").strip())
        except (ValueError, socket.error):
            log.warning("Failed to read temperature for input %s", ch)
            return None

    def _read_heater(self, output: int) -> float | None:
        try:
            return float(self._query(f"HTR? {output}").strip())
        except (ValueError, socket.error):
            log.warning("Failed to read heater output %d", output)
            return None

    def _read_setpoint(self, loop: int) -> float | None:
        try:
            return float(self._query(f"SETP? {loop}").strip())
        except (ValueError, socket.error):
            return None

    def set_setpoint(self, loop: int, value_k: float) -> None:
        """Send a new setpoint to the controller."""
        self._send(f"SETP {loop},{value_k:.4f}")
        log.info("LakeShore setpoint loop %d → %.4f K", loop, value_k)

    # ── MQTT command handler ───────────────────────────────────

    def _on_setpoint(self, topic: str, payload: dict) -> None:
        value = payload.get("value")
        loop = payload.get("loop", 1)
        if value is not None:
            self.set_setpoint(int(loop), float(value))

    # ── low-level socket I/O ───────────────────────────────────

    def _query(self, command: str) -> str:
        with self._lock:
            self._send(command)
            return self._recv()

    def _send(self, command: str) -> None:
        if not self._sock:
            raise ConnectionError("LakeShore not connected")
        self._sock.sendall((command + "\r\n").encode())

    def _recv(self, bufsize: int = 1024) -> str:
        if not self._sock:
            raise ConnectionError("LakeShore not connected")
        return self._sock.recv(bufsize).decode()
