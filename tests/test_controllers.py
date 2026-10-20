"""Tests for controller logic."""

from __future__ import annotations

import time
from collections import deque
from unittest.mock import MagicMock

import pytest

from slowcontrol.controllers.pid import PIDController
from slowcontrol.controllers.autovalve import AutovalveController, FillState
from slowcontrol.controllers.humidity import HumidityController


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

    def test_reverse_acting(self):
        """Reverse PID drives output up when measured is *above* setpoint."""
        direct = PIDController(Kp=2.0, setpoint=10.0, output_max=100.0)
        reverse = PIDController(Kp=2.0, setpoint=10.0, output_max=100.0,
                                reverse=True)
        assert direct.update(12.0) == 0.0          # clamped at output_min
        assert reverse.update(12.0) == pytest.approx(4.0)
        assert reverse.update(8.0) == 0.0


# ── Humidity PID controller ───────────────────────────────────


class TestHumidityPID:
    RELAY_TOPIC = "ets/commands/relay/gn2_valve"

    @pytest.fixture
    def hum(self, mock_mqtt):
        config = {"setpoint": 7.2, "Kp": 1.0, "cycle_duration": 600,
                  "relay": "gn2_valve"}
        return HumidityController("humidity_pid", config, mock_mqtt)

    def _run_cycles(self, hum, monkeypatch, waits, on_sleep=None):
        """Run the duty-cycle loop until it has slept *waits* times.

        *on_sleep(n)* runs at each earlier sleep (n = 1-based count);
        returning True simulates the sleep being cut short by a toggle.
        """
        calls = []

        def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= waits:
                hum._stop.set()
                return True
            return bool(on_sleep and on_sleep(len(calls)))

        monkeypatch.setattr(hum, "_sleep", fake_sleep)
        hum._duty_cycle_loop()
        return calls

    @staticmethod
    def _wait_for(cond, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not cond() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cond()

    def _relay_actions(self, mock_mqtt):
        return [p["action"] for t, p in mock_mqtt._published
                if t == self.RELAY_TOPIC]

    def test_humid_opens_and_stays_open(self, hum, mock_mqtt, monkeypatch):
        """Above setpoint → 100 % duty, no close blip between cycles."""
        hum._on_humidity("ets/sensors/humidity/humidity", {"value": 35.7})
        waits = self._run_cycles(hum, monkeypatch, waits=3)
        assert hum._duty_cycle == 1.0
        assert waits == [600, 600, 600]
        assert self._relay_actions(mock_mqtt) == ["open", "open", "open"]

    def test_dry_keeps_valve_closed(self, hum, mock_mqtt, monkeypatch):
        hum._on_humidity("ets/sensors/humidity/humidity", {"value": 5.0})
        self._run_cycles(hum, monkeypatch, waits=2)
        assert hum._duty_cycle == 0.0
        assert self._relay_actions(mock_mqtt) == ["close", "close"]

    def test_partial_duty_cycles_valve(self, hum, mock_mqtt, monkeypatch):
        hum._pid.Kp = 0.1
        hum._on_humidity("ets/sensors/humidity/humidity", {"value": 12.2})
        waits = self._run_cycles(hum, monkeypatch, waits=2)
        assert hum._duty_cycle == pytest.approx(0.5)
        assert waits == pytest.approx([300, 300])
        assert self._relay_actions(mock_mqtt) == ["open", "close"]

    def test_off_closes_valve_and_parks(self, hum, mock_mqtt, monkeypatch):
        hum._on_humidity("ets/sensors/humidity/humidity", {"value": 35.7})
        hum._on_enable("ets/commands/humidity/enable", {"enabled": False})
        waits = self._run_cycles(hum, monkeypatch, waits=1)
        assert waits == [None]                      # parked until woken
        assert hum._duty_cycle == 0.0
        assert self._relay_actions(mock_mqtt) == ["close"]
        assert hum.get_state()["enabled"] is False

    def test_off_then_on_restarts_cycle(self, hum, mock_mqtt, monkeypatch):
        """Toggling cuts the current cycle short instead of waiting it out."""
        hum._on_humidity("ets/sensors/humidity/humidity", {"value": 35.7})

        def toggle(n):          # 1st sleep → off, 2nd sleep → on
            hum._on_enable("ets/commands/humidity/enable", {"enabled": n == 2})
            return True

        waits = self._run_cycles(hum, monkeypatch, waits=3, on_sleep=toggle)
        assert waits == [600, None, 600]
        assert self._relay_actions(mock_mqtt) == ["open", "close", "open"]

    def test_start_disabled_from_config(self, mock_mqtt):
        hum = HumidityController("humidity_pid", {"start_enabled": False},
                                 mock_mqtt)
        assert hum.get_state()["enabled"] is False

    @pytest.mark.parametrize("payload", [{}, {"enabled": "no"}, {"enabled": 0}])
    def test_bad_enable_payload_ignored(self, hum, payload):
        hum._on_enable("ets/commands/humidity/enable", payload)
        assert hum._enabled is True
        assert not hum._wake.is_set()

    def test_off_takes_effect_mid_cycle(self, hum, mock_mqtt):
        """Real thread: "off" closes the valve now, not after the 600 s cycle."""
        hum._on_humidity("ets/sensors/humidity/humidity", {"value": 35.7})
        hum.start()
        try:
            self._wait_for(lambda: self._relay_actions(mock_mqtt) == ["open"])
            hum._on_enable("ets/commands/humidity/enable", {"enabled": False})
            self._wait_for(
                lambda: self._relay_actions(mock_mqtt) == ["open", "close"])
        finally:
            hum.stop()
        assert not hum._thread.is_alive()


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

    def test_persisted_tunables_survive_restart(self, av, mock_mqtt,
                                                isolated_runtime_dir):
        """Tunables set at runtime are reloaded over config; enabled is not."""
        av._enabled = True
        av._on_set_command("ets/commands/autovalve/set",
                           {"fill_timeout": 42, "cooldown": 7})
        assert (isolated_runtime_dir / "autovalve.yaml").exists()

        av2 = AutovalveController("autovalve", {"fill_timeout": 10}, mock_mqtt)
        assert av2._fill_timeout == 42.0
        assert av2._cooldown_time == 7.0
        assert av2._enabled is False


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
