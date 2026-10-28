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


VALVE_STATUS = "ets/status/relays/ln2_valve"
VALVE_CMD = "ets/commands/relay/ln2_valve"


class TestAutovalve:
    @pytest.fixture
    def av(self, mock_mqtt):
        config = {
            "mode": "threshold",
            "fill_channels": ["RES1", "RES2"],
            "fill_trigger": 4.5,
            "close_channels": [],
            "full_tank": 9.5,
            "overfill_channel": "RES7",
            "overfill_threshold": 9.8,
            "valve_relay": "ln2_valve",
            "fill_timeout": 300,
            "cooldown": 900,
            "gradient_trigger": -1e-4,
            "scan_interval": 0.1,
        }
        av = AutovalveController("autovalve", config, mock_mqtt)
        # Don't start threads — drive _step() and the handlers directly.
        av._on_valve_status(VALVE_STATUS, {"state": "closed"})
        return av

    @staticmethod
    def _valve(av, state):
        av._on_valve_status(VALVE_STATUS, {"state": state})

    @staticmethod
    def _cmds(mock_mqtt):
        return [p["action"] for t, p in mock_mqtt._published if t == VALVE_CMD]

    # ── boot defaults ──

    def test_boot_autofill_off_autoclose_on(self, mock_mqtt):
        av = AutovalveController("autovalve", {"start_enabled": True}, mock_mqtt)
        assert av._autofill is False
        assert av._autoclose is True
        assert av._state == FillState.WAITING

    # ── auto-close: any condition closes, whoever opened the valve ──

    def test_autoclose_times_out_a_manual_fill(self, av, mock_mqtt):
        self._valve(av, "open")                       # operator pressed open
        assert av._state == FillState.FILLING
        av._step()
        assert self._cmds(mock_mqtt) == []            # timer not expired yet
        av._opened_at = time.time() - 301
        av._step()
        assert self._cmds(mock_mqtt) == ["close"]
        self._valve(av, "closed")
        assert av._state == FillState.COOLDOWN
        assert av._last_close_reason.startswith("fill timer")

    def test_autoclose_on_overfill(self, av, mock_mqtt):
        self._valve(av, "open")
        av._latest_levels["RES7"] = 9.9
        av._step()
        assert self._cmds(mock_mqtt) == ["close"]
        assert av._close_reason.startswith("overfill (RES7=9.90 V")

    def test_autoclose_on_optional_close_channel(self, av, mock_mqtt):
        av._on_set_command("ets/commands/autovalve/set", {"close_channels": ["RES1"]})
        self._valve(av, "open")
        av._latest_levels.update(RES1=9.6, RES7=8.0)  # before overfill
        av._step()
        assert self._cmds(mock_mqtt) == ["close"]
        assert av._close_reason.startswith("full (RES1=9.60 V")

    def test_close_channels_default_to_none(self, av, mock_mqtt):
        self._valve(av, "open")
        av._latest_levels.update(RES1=9.9, RES2=9.9, RES7=8.0)
        av._step()
        assert self._cmds(mock_mqtt) == []

    def test_close_resent_until_relay_confirms(self, av, mock_mqtt):
        self._valve(av, "open")
        av._latest_levels["RES7"] = 9.9
        av._step()
        av._step()                                    # relay hasn't confirmed yet
        assert self._cmds(mock_mqtt) == ["close", "close"]

    def test_autoclose_off_leaves_manual_fill_alone(self, av, mock_mqtt):
        av._on_autoclose_command("t", {"enabled": False})
        self._valve(av, "open")
        av._opened_at = time.time() - 10_000
        av._latest_levels["RES7"] = 9.9
        av._step()
        assert self._cmds(mock_mqtt) == []

    def test_external_close_records_active_condition(self, av):
        self._valve(av, "open")
        av._latest_levels["RES7"] = 9.9
        self._valve(av, "closed")                     # interlock beat our scan
        assert av._last_close_reason.startswith("closed externally while overfill")

    # ── auto-fill: all conditions must hold ──

    def test_autofill_needs_every_fill_channel(self, av, mock_mqtt):
        av._on_autofill_command("t", {"enabled": True})
        av._latest_levels.update(RES1=4.0, RES2=5.0, RES7=8.0)
        av._step()
        assert self._cmds(mock_mqtt) == []            # RES2 not below 4.5
        av._latest_levels["RES2"] = 4.0
        av._step()
        assert self._cmds(mock_mqtt) == ["open"]
        self._valve(av, "open")
        assert av._opened_by_autofill is True

    def test_autofill_never_fills_on_missing_data(self, av, mock_mqtt):
        av._on_autofill_command("t", {"enabled": True})
        av._latest_levels.update(RES1=1.0, RES7=8.0)  # no RES2 reading yet
        av._step()
        assert self._cmds(mock_mqtt) == []

    def test_autofill_blocked_while_a_close_condition_holds(self, av, mock_mqtt):
        av._on_autofill_command("t", {"enabled": True})
        av._latest_levels.update(RES1=1.0, RES2=1.0, RES7=9.9)  # overfill still wet
        av._step()
        assert self._cmds(mock_mqtt) == []

    def test_autofill_waits_out_cooldown(self, av, mock_mqtt):
        av._on_autofill_command("t", {"enabled": True})
        self._valve(av, "open")
        self._valve(av, "closed")                     # any close starts cooldown
        av._latest_levels.update(RES1=1.0, RES2=1.0, RES7=8.0)
        av._step()
        assert self._cmds(mock_mqtt) == []
        assert av._state == FillState.COOLDOWN
        av._last_close_at = time.time() - 901
        av._step()
        assert av._state == FillState.WAITING
        assert self._cmds(mock_mqtt) == ["open"]

    def test_autofill_never_opens_before_relay_reports(self, mock_mqtt):
        av = AutovalveController("autovalve", {"fill_channels": ["RES1"],
                                               "fill_trigger": 4.5}, mock_mqtt)
        av._on_autofill_command("t", {"enabled": True})
        av._latest_levels.update(RES1=1.0, RES7=8.0)
        av._step()
        assert self._cmds(mock_mqtt) == []

    def test_autofill_open_retried_if_not_confirmed(self, av, mock_mqtt):
        av._on_autofill_command("t", {"enabled": True})
        av._latest_levels.update(RES1=1.0, RES2=1.0, RES7=8.0)
        av._step()
        av._step()                                    # still awaiting confirmation
        assert self._cmds(mock_mqtt) == ["open"]
        av._open_sent_at = time.time() - 10
        av._step()
        assert self._cmds(mock_mqtt) == ["open", "open"]

    def test_gradient_mode_needs_every_channel_falling(self, av, mock_mqtt):
        av._on_mode_command("t", {"mode": "gradient"})
        av._on_autofill_command("t", {"enabled": True})
        av._latest_levels["RES7"] = 8.0
        for i in range(20):
            av._level_history["RES1"].append((i, 9.0 - 0.01 * i))   # falling
            av._level_history["RES2"].append((i, 9.0))              # flat
        av._step()
        assert self._cmds(mock_mqtt) == []
        av._level_history["RES2"].clear()
        for i in range(20):
            av._level_history["RES2"].append((i, 9.0 - 0.01 * i))
        av._step()
        assert self._cmds(mock_mqtt) == ["open"]

    # ── never an automated fill without automated close ──

    def test_autofill_on_turns_autoclose_on(self, av):
        av._on_autoclose_command("t", {"enabled": False})
        av._on_autofill_command("t", {"enabled": True})
        assert av._autofill is True and av._autoclose is True

    def test_autoclose_off_turns_autofill_off_and_closes_its_fill(self, av, mock_mqtt):
        av._on_autofill_command("t", {"enabled": True})
        av._latest_levels.update(RES1=1.0, RES2=1.0, RES7=8.0)
        av._step()
        self._valve(av, "open")                       # auto-fill's open confirmed
        av._on_autoclose_command("t", {"enabled": False})
        assert av._autofill is False and av._autoclose is False
        assert self._cmds(mock_mqtt) == ["open", "close"]
        av._step()                                    # keeps closing until confirmed
        assert self._cmds(mock_mqtt) == ["open", "close", "close"]
        self._valve(av, "closed")
        assert av._last_close_reason == "auto-fill switched off"

    def test_autofill_off_leaves_a_manual_fill_open(self, av, mock_mqtt):
        av._on_autofill_command("t", {"enabled": True})
        self._valve(av, "open")                       # operator's own fill
        av._on_autofill_command("t", {"enabled": False})
        assert self._cmds(mock_mqtt) == []

    def test_legacy_mode_aliases(self, av):
        av._on_mode_command("t", {"mode": "auto"})
        assert av._autofill is True and av._autoclose is True
        av._on_mode_command("t", {"mode": "manual"})
        assert av._autofill is False and av._autoclose is True

    @pytest.mark.parametrize("payload", [{}, {"enabled": "yes"}, {"enabled": 1}])
    def test_bad_switch_payload_ignored(self, av, payload):
        av._on_autofill_command("t", payload)
        av._on_autoclose_command("t", payload)
        assert av._autofill is False and av._autoclose is True

    # ── tunables, status, persistence ──

    def test_gradient_computation(self):
        history = deque([(0, 10.0), (10, 9.5), (20, 9.0), (30, 8.5)])
        grad = AutovalveController._compute_gradient(history)
        assert grad == pytest.approx(-0.05, abs=1e-6)

    def test_get_state_reports_both_switches(self, av):
        state = av.get_state()
        for key in ("state", "mode", "autofill", "autoclose", "valve_open",
                    "fill_channels", "close_channels", "last_close_reason"):
            assert key in state
        assert state["fill_channels"] == ["RES1", "RES2"]
        assert state["close_channels"] == []

    def test_fill_channels_must_not_be_empty(self, av):
        av._on_set_command("t", {"fill_channels": []})
        assert av._fill_channels == ["RES1", "RES2"]
        av._on_set_command("t", {"close_channels": []})   # close may be empty
        assert av._close_channels == []

    def test_legacy_level_channels_become_fill_channels(self, mock_mqtt,
                                                        isolated_runtime_dir):
        isolated_runtime_dir.mkdir(parents=True)
        (isolated_runtime_dir / "autovalve.yaml").write_text(
            "mode: threshold\nlevel_channels: [RES3]\nfill_trigger: 9.0\n")
        av = AutovalveController("autovalve", {}, mock_mqtt)
        assert av._fill_channels == ["RES3"]
        assert av._fill_trigger == 9.0
        assert set(av._level_history) == {"RES3"}

    def test_persisted_tunables_survive_restart(self, av, mock_mqtt,
                                                isolated_runtime_dir):
        """Tunables set at runtime are reloaded over config; switches are not."""
        av._on_autofill_command("t", {"enabled": True})
        av._on_set_command("ets/commands/autovalve/set",
                           {"fill_timeout": 42, "cooldown": 7,
                            "close_channels": ["RES2"], "fill_channels": ["RES1"]})
        assert (isolated_runtime_dir / "autovalve.yaml").exists()

        av2 = AutovalveController("autovalve", {"fill_timeout": 10}, mock_mqtt)
        assert av2._fill_timeout == 42.0
        assert av2._cooldown_time == 7.0
        assert av2._close_channels == ["RES2"]
        assert av2._fill_channels == ["RES1"]
        assert av2._autofill is False and av2._autoclose is True


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
