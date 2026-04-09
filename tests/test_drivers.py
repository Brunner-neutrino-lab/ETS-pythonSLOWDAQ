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
