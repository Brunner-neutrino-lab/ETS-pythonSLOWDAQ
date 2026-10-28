"""Tests for driver utilities and base class."""

from __future__ import annotations

import math
import time
from unittest.mock import MagicMock, patch

import pytest

from slowcontrol.drivers.base import SensorDriver
from slowcontrol.drivers.labjack_t7 import frg_pressure_mbar


# ── FRG pressure conversion ───────────────────────────────────


class TestFRGConversion:
    def test_known_voltages(self):
        # At ~4V → should give ~1e-4.67 mbar (low vacuum)
        p = frg_pressure_mbar(4.0)
        assert 1e-6 < p < 1e-3

    def test_monotonic(self):
        """Higher voltage → higher pressure."""
        p_low = frg_pressure_mbar(2.0)
        p_high = frg_pressure_mbar(6.0)
        assert p_high > p_low

    def test_zero_voltage(self):
        p = frg_pressure_mbar(0.0)
        assert p > 0
        assert p < 1e-10  # very low pressure


# ── Base driver polling ───────────────────────────────────────


class _DummyDriver(SensorDriver):
    """Concrete implementation for testing the base class."""

    def __init__(self, mqtt, poll_interval=0.05):
        super().__init__("test", {}, mqtt, poll_interval)
        self.read_count = 0

    def connect(self):
        pass

    def read(self):
        self.read_count += 1
        return {"ch1": 42.0}

    def disconnect(self):
        pass


class TestBaseSensorDriver:
    def test_polling_publishes(self, mock_mqtt):
        d = _DummyDriver(mock_mqtt, poll_interval=0.02)
        d.start()
        time.sleep(0.15)
        d.stop()
        assert d.read_count >= 3
        assert len(mock_mqtt._published) >= 3
        topic, payload = mock_mqtt._published[0]
        assert topic == "ets/sensors/test/ch1"
        assert payload["value"] == 42.0
        assert "ts" in payload

    def test_stop_sets_flag(self, mock_mqtt):
        d = _DummyDriver(mock_mqtt)
        d.start()
        d.stop()
        assert not d.is_running


# ── DHT serial driver ─────────────────────────────────────────


class _FakePort:
    """Stand-in for serial.Serial: replays lines; an Exception item is raised."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.is_open = True

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False

    def readline(self):
        item = self.lines.pop(0) if self.lines else b""
        if isinstance(item, Exception):
            raise item
        return item


class TestDHTSerialDriver:
    LOGGER = "slowcontrol.drivers.dht_serial"

    @staticmethod
    def _opener(*outcomes):
        """serial.Serial replacement: each call returns/raises the next outcome."""
        queue = list(outcomes)

        def open_port(*args, **kwargs):
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return open_port

    def _warnings(self, caplog):
        return [r.getMessage() for r in caplog.records
                if r.name == self.LOGGER and r.levelname == "WARNING"]

    def test_missing_port_at_startup_retries_with_one_warning(self, mock_mqtt, caplog):
        import serial
        from slowcontrol.drivers.dht_serial import DHTSerialDriver

        missing = serial.SerialException("could not open port: No such file or directory")
        opener = self._opener(missing, missing, missing, _FakePort([b"45.2,297.4\n"]))
        caplog.set_level("INFO", logger=self.LOGGER)
        with patch("serial.Serial", side_effect=opener):
            d = DHTSerialDriver("humidity", {"port": "/dev/nope"}, mock_mqtt)
            d.connect()                      # what start() calls: must not raise
            assert d.read() == {}            # still missing
            assert d.read() == {}            # still missing
            assert d.read() == {"humidity": 45.2, "temperature": 297.4}
        warnings = self._warnings(caplog)
        assert len(warnings) == 1 and "unavailable" in warnings[0]
        assert any("is back" in r.getMessage() for r in caplog.records)

    def test_start_with_missing_port_still_polls(self, mock_mqtt):
        import serial
        from slowcontrol.drivers.dht_serial import DHTSerialDriver

        with patch("serial.Serial", side_effect=serial.SerialException("missing")):
            d = DHTSerialDriver("humidity", {}, mock_mqtt, poll_interval=0.02)
            d.start()
            try:
                assert d.is_running
            finally:
                d.stop()

    def test_drop_mid_run_warns_once_then_recovers(self, mock_mqtt, caplog):
        import serial
        from slowcontrol.drivers.dht_serial import DHTSerialDriver

        dropped = serial.SerialException("device reports readiness to read but returned no data")
        missing = serial.SerialException("could not open port")
        opener = self._opener(_FakePort([b"40.0,296.0\n", dropped]),
                              missing, missing, _FakePort([b"41.0,296.5\n"]))
        caplog.set_level("INFO", logger=self.LOGGER)
        with patch("serial.Serial", side_effect=opener):
            d = DHTSerialDriver("humidity", {}, mock_mqtt)
            d.connect()
            results = [d.read() for _ in range(5)]
        assert results == [{"humidity": 40.0, "temperature": 296.0}, {}, {}, {},
                           {"humidity": 41.0, "temperature": 296.5}]
        warnings = self._warnings(caplog)
        assert len(warnings) == 1 and "connection lost" in warnings[0]
