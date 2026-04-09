"""LabJack T7 driver for thermocouples, level sensors, and pressure gauges."""

from __future__ import annotations

import logging
import math
from typing import Any

from slowcontrol.core.registry import register_driver
from slowcontrol.drivers.base import SensorDriver

log = logging.getLogger(__name__)

# Thermocouple Extended-Feature index for each type
TC_TYPE_EF = {
    "K": 24,
    "T": 22,
    "J": 21,
    "E": 20,
    "R": 23,
    "S": 25,
    "C": 30,
}


def frg_pressure_mbar(voltage: float) -> float:
    """Convert Pfeiffer full-range gauge voltage to pressure (mbar).

    Standard PKR-251 transfer function:  P = 10^(1.667·V − 11.33)
    """
    exponent = (10.0 / 6.0) * voltage - (10.0 / 6.0) * 6.8
    return 10.0 ** exponent


@register_driver("labjack_t7")
class LabJackT7Driver(SensorDriver):
    """Driver for one LabJack T7 with configurable channel types.

    Supported sensor types per channel:
      - ``thermocouple`` : uses LJM extended features for CJC + conversion
      - ``voltage``       : raw single-ended AIN read
      - ``frg_pressure``  : differential read with Pfeiffer FRG conversion
    """

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        mqtt,
        poll_interval: float = 1.0,
    ):
        super().__init__(name, config, mqtt, poll_interval)
        self._handle = None
        self._serial = config.get("serial", "ANY")
        self._channels: dict[str, dict] = config.get("channels", {})
        self._ljm = None

    def connect(self) -> None:
        import labjack.ljm as ljm

        self._ljm = ljm
        self._handle = ljm.openS("T7", "ANY", self._serial)
        info = ljm.getHandleInfo(self._handle)
        log.info(
            "LabJack T7 connected: serial=%s  type=%d  conn=%d",
            self._serial,
            info[0],
            info[1],
        )
        self._configure_channels()

    def disconnect(self) -> None:
        if self._handle is not None:
            self._ljm.close(self._handle)
            self._handle = None
            log.info("LabJack T7 disconnected: %s", self._serial)

    def read(self) -> dict[str, Any]:
        ljm = self._ljm
        readings: dict[str, Any] = {}

        for ch_name, ch_cfg in self._channels.items():
            sensor = ch_cfg.get("sensor", "voltage")
            ain = ch_cfg["ain"]

            if sensor == "thermocouple":
                # EF_READ_A returns temperature in configured unit (Kelvin)
                temp_k = ljm.eReadName(self._handle, f"AIN{ain}_EF_READ_A")
                readings[ch_name] = round(temp_k - 273.15, 3)  # publish °C

            elif sensor == "frg_pressure":
                pos = ain[0] if isinstance(ain, list) else ain
                voltage = ljm.eReadName(self._handle, f"AIN{pos}")
                readings[ch_name] = frg_pressure_mbar(voltage)

            else:  # voltage
                voltage = ljm.eReadName(self._handle, f"AIN{ain}")
                readings[ch_name] = round(voltage, 6)

        return readings

    # ── channel configuration ──────────────────────────────────

    def _configure_channels(self) -> None:
        ljm = self._ljm
        for ch_name, ch_cfg in self._channels.items():
            sensor = ch_cfg.get("sensor", "voltage")
            ain = ch_cfg["ain"]

            if sensor == "thermocouple":
                self._configure_thermocouple(ch_name, ain, ch_cfg)
            elif sensor == "frg_pressure":
                self._configure_frg(ch_name, ain)
            else:
                self._configure_voltage(ch_name, ain)

    def _configure_thermocouple(
        self, name: str, ain: int, cfg: dict
    ) -> None:
        ljm = self._ljm
        tc_type = cfg.get("tc_type", "T")
        ef_index = TC_TYPE_EF.get(tc_type, 22)
        p = f"AIN{ain}"

        ljm.eWriteName(self._handle, f"{p}_EF_INDEX", 0)       # reset EF
        ljm.eWriteName(self._handle, f"{p}_EF_INDEX", ef_index)
        ljm.eWriteName(self._handle, f"{p}_EF_CONFIG_A", 1)    # Kelvin
        ljm.eWriteName(self._handle, f"{p}_EF_CONFIG_B", 60052)  # CJC modbus
        ljm.eWriteName(self._handle, f"{p}_EF_CONFIG_D", 1)    # internal CJC
        ljm.eWriteName(self._handle, f"{p}_EF_CONFIG_E", 0)    # CJC offset
        ljm.eWriteName(self._handle, f"{p}_RESOLUTION_INDEX", 8)
        log.debug("Configured %s: TC type %s on AIN%d", name, tc_type, ain)

    def _configure_frg(self, name: str, ain) -> None:
        ljm = self._ljm
        if isinstance(ain, list):
            pos, neg = ain
        else:
            pos, neg = ain, ain + 1
        p = f"AIN{pos}"
        ljm.eWriteName(self._handle, f"{p}_NEGATIVE_CH", neg)
        ljm.eWriteName(self._handle, f"{p}_RESOLUTION_INDEX", 8)
        log.debug(
            "Configured %s: FRG differential AIN%d−AIN%d", name, pos, neg
        )

    def _configure_voltage(self, name: str, ain: int) -> None:
        ljm = self._ljm
        p = f"AIN{ain}"
        ljm.eWriteName(self._handle, f"{p}_EF_INDEX", 0)
        ljm.eWriteName(self._handle, f"{p}_RESOLUTION_INDEX", 8)
        log.debug("Configured %s: voltage on AIN%d", name, ain)
