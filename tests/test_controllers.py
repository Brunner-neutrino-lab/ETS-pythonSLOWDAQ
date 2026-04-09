"""Tests for controller logic."""

from __future__ import annotations

import time
from collections import deque
from unittest.mock import MagicMock

import pytest

from slowcontrol.controllers.pid import PIDController
from slowcontrol.controllers.autovalve import AutovalveController, FillState


# ── PID controller ─────────────────────────────────────────────


class TestPIDController:
    def test_proportional_only(self):
        pid = PIDController(Kp=2.0, Ki=0, Kd=0, setpoint=10.0, output_max=100.0)
        out = pid.update(8.0)
        assert out == pytest.approx(2.0 * 2.0)  # Kp * error

    def test_output_clamping(self):
        pid = PIDController(
            Kp=10.0, setpoint=100.0, output_min=0.0, output_max=1.0
        )
        out = pid.update(0.0)
        assert out == 1.0

    def test_reset(self):
        pid = PIDController(Kp=1.0, Ki=1.0, setpoint=10.0)
        pid.update(5.0)
        pid.reset()
        assert pid._integral == 0.0
        assert pid._prev_time is None


# ── Autovalve controller ──────────────────────────────────────


class TestAutovalve:
    @pytest.fixture
    def av(self, mock_mqtt):
        config = {
            "mode": "threshold",
            "level_channels": ["RES1"],
            "overfill_channel": "RES7",
            "valve_relay": "ln2_valve",
            "fill_timeout": 10,
            "cooldown": 5,
            "overfill_threshold": 9.8,
            "overfill_close": 9.0,
            "full_tank": 9.5,
            "gradient_trigger": -1e-4,
            "scan_interval": 0.1,
        }
        av = AutovalveController("autovalve", config, mock_mqtt)
        # Don't start threads — we'll drive the state machine manually
        return av

    def test_initial_state(self, av):
        assert av._state == FillState.WAITING

    def test_threshold_triggers_fill(self, av):
        """When level is below threshold, WAITING → FILLING."""
        av._latest_levels["RES1"] = 2.0  # well below full_tank * 0.5
        av._enabled = True
        av._step()
        assert av._state == FillState.FILLING

    def test_overfill_closes_valve(self, av, mock_mqtt):
        """Overfill sensor exceeding threshold forces FILLING → COOLDOWN."""
        av._state = FillState.FILLING
        av._fill_start = time.time()
        av._overfill_value = 10.0  # above 9.8
        av._step()
        assert av._state == FillState.COOLDOWN
        # Should have published a relay close command
        close_msgs = [
            (t, p)
            for t, p in mock_mqtt._published
            if "ln2_valve" in t and p.get("action") == "close"
        ]
        assert len(close_msgs) >= 1

    def test_fill_timeout(self, av, mock_mqtt):
        """Fill timeout forces FILLING → COOLDOWN."""
        av._state = FillState.FILLING
        av._fill_start = time.time() - 100  # well past the 10s timeout
        av._overfill_value = 0
        av._step()
        assert av._state == FillState.COOLDOWN

    def test_cooldown_to_waiting(self, av):
        """COOLDOWN expires after cooldown_time → WAITING."""
        av._state = FillState.COOLDOWN
        av._last_fill_end = time.time() - 100  # well past 5s cooldown
        av._step()
        assert av._state == FillState.WAITING

    def test_gradient_computation(self):
        history = deque([(0, 10.0), (10, 9.5), (20, 9.0), (30, 8.5)])
        grad = AutovalveController._compute_gradient(history)
        assert grad == pytest.approx(-0.05, abs=1e-6)

    def test_get_state_returns_dict(self, av):
        state = av.get_state()
        assert "state" in state
        assert "mode" in state
        assert "enabled" in state


# ── Config loading ─────────────────────────────────────────────


class TestConfigLoading:
    def test_load_yaml(self, tmp_path):
        from slowcontrol.core.config import AppConfig

        cfg_file = tmp_path / "test.yaml"
        cfg_file.write_text(
            """
mqtt:
  broker: 127.0.0.1
  port: 1884
  client_id: test

drivers:
  my_sensor:
    type: dht_serial
    poll_interval: 2.0
    port: /dev/ttyUSB1
    baud: 9600

relays:
  test_relay:
    pin: 5
    default: closed
    active_low: true

interlocks:
  enabled: false
  watchdog_timeout: 30
  rules: {}
"""
        )
        cfg = AppConfig.from_yaml(cfg_file)
        assert cfg.mqtt.broker == "127.0.0.1"
        assert cfg.mqtt.port == 1884
        assert "my_sensor" in cfg.drivers
        assert cfg.drivers["my_sensor"].type == "dht_serial"
        assert cfg.drivers["my_sensor"].poll_interval == 2.0
        assert cfg.drivers["my_sensor"].params["port"] == "/dev/ttyUSB1"
        assert cfg.relays["test_relay"].pin == 5
        assert cfg.interlocks.enabled is False
