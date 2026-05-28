"""LakeShore 350 temperature controller driver (Ethernet / TCP SCPI).

Polls RTD inputs and heater output 1 at the configured rate, plus a slower
poll of loop-1 control parameters (PID gains, heater range, manual output,
output mode, setpoint ramp).  Accepts MQTT commands to drive loop 1.

Topics published (each {"value": <num>, "ts": <epoch>}):
  ets/sensors/lakeshore/rtd/{A|B|C|D}      K           1 Hz
  ets/sensors/lakeshore/heater/1           %           1 Hz
  ets/sensors/lakeshore/setpoint/1         K           1 Hz
  ets/sensors/lakeshore/pid_p/1            (gain)      ~0.2 Hz
  ets/sensors/lakeshore/pid_i/1            (gain)      ~0.2 Hz
  ets/sensors/lakeshore/pid_d/1            (gain)      ~0.2 Hz
  ets/sensors/lakeshore/range/1            0..5        ~0.2 Hz
  ets/sensors/lakeshore/mout/1             %           ~0.2 Hz
  ets/sensors/lakeshore/outmode/1          mode int    ~0.2 Hz
  ets/sensors/lakeshore/ramp_on/1          0/1         ~0.2 Hz
  ets/sensors/lakeshore/ramp_rate/1        K/min       ~0.2 Hz
  ets/sensors/lakeshore/rampst/1           0/1         ~0.2 Hz

Commands subscribed:
  ets/commands/lakeshore/setpoint   {"value": K, "loop": 1}
  ets/commands/lakeshore/pid        {"output": 1, "p": .., "i": .., "d": ..}
  ets/commands/lakeshore/range      {"output": 1, "range": 0..5}
  ets/commands/lakeshore/mout       {"output": 1, "value": 0..100}
  ets/commands/lakeshore/outmode    {"output": 1, "mode": 0..5}
  ets/commands/lakeshore/ramp       {"output": 1, "on": bool, "rate": K/min}

OUTMODE mode values:
  0=Off, 1=Closed Loop PID, 2=Zone, 3=Open Loop, 4=Monitor Out, 5=Warmup Supply
RANGE values (outputs 1 & 2):
  0=Off, 1..5 = power range 1 (lowest) .. 5 (highest, ≈50 W on output 1)
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any

from slowcontrol.core.registry import register_driver
from slowcontrol.drivers.base import SensorDriver

log = logging.getLogger(__name__)

# Read loop-1 control parameters every Nth poll tick (1 Hz × 5 = 5 s).
_SLOW_POLL_EVERY = 5


@register_driver("lakeshore350")
class LakeShore350Driver(SensorDriver):
    """Read RTDs / heater output / loop-1 control params; accept commands via MQTT."""

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
        self._tick = 0   # increments each poll; slow params poll every _SLOW_POLL_EVERY

    # ── lifecycle ──────────────────────────────────────────────

    def connect(self) -> None:
        self._open_socket()
        idn = self._query("*IDN?")
        log.info("LakeShore 350 connected: %s", idn.strip())
        self.mqtt.subscribe("ets/commands/lakeshore/setpoint", self._on_setpoint)
        self.mqtt.subscribe("ets/commands/lakeshore/pid",      self._on_pid)
        self.mqtt.subscribe("ets/commands/lakeshore/range",    self._on_range)
        self.mqtt.subscribe("ets/commands/lakeshore/mout",     self._on_mout)
        self.mqtt.subscribe("ets/commands/lakeshore/outmode",  self._on_outmode)
        self.mqtt.subscribe("ets/commands/lakeshore/ramp",     self._on_ramp)

    def disconnect(self) -> None:
        self.mqtt.unsubscribe("ets/commands/lakeshore/setpoint", self._on_setpoint)
        self.mqtt.unsubscribe("ets/commands/lakeshore/pid",      self._on_pid)
        self.mqtt.unsubscribe("ets/commands/lakeshore/range",    self._on_range)
        self.mqtt.unsubscribe("ets/commands/lakeshore/mout",     self._on_mout)
        self.mqtt.unsubscribe("ets/commands/lakeshore/outmode",  self._on_outmode)
        self.mqtt.unsubscribe("ets/commands/lakeshore/ramp",     self._on_ramp)
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

        # Slow-poll control parameters every 5 s (a SCPI round trip is ~10 ms;
        # batching them on a fraction of the poll cycle keeps the loop snappy).
        if self._tick % _SLOW_POLL_EVERY == 0:
            pid = self._read_pid(1)
            if pid is not None:
                readings["pid_p/1"] = round(pid[0], 3)
                readings["pid_i/1"] = round(pid[1], 3)
                readings["pid_d/1"] = round(pid[2], 3)
            rng = self._read_range(1)
            if rng is not None:
                readings["range/1"] = rng
            mout = self._read_mout(1)
            if mout is not None:
                readings["mout/1"] = round(mout, 2)
            outmode = self._read_outmode(1)
            if outmode is not None:
                readings["outmode/1"] = outmode[0]   # just the mode int
            ramp = self._read_ramp(1)
            if ramp is not None:
                readings["ramp_on/1"]   = ramp[0]
                readings["ramp_rate/1"] = round(ramp[1], 3)
            rampst = self._read_rampst(1)
            if rampst is not None:
                readings["rampst/1"] = rampst
        self._tick += 1

        return readings

    # ── SCPI read helpers ──────────────────────────────────────

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

    def _read_pid(self, output: int) -> tuple[float, float, float] | None:
        try:
            parts = [p.strip() for p in self._query(f"PID? {output}").strip().split(",")]
            return float(parts[0]), float(parts[1]), float(parts[2])
        except (ValueError, IndexError, socket.error):
            return None

    def _read_range(self, output: int) -> int | None:
        try:
            return int(self._query(f"RANGE? {output}").strip())
        except (ValueError, socket.error):
            return None

    def _read_mout(self, output: int) -> float | None:
        try:
            return float(self._query(f"MOUT? {output}").strip())
        except (ValueError, socket.error):
            return None

    def _read_outmode(self, output: int) -> tuple[int, int, int] | None:
        try:
            parts = [p.strip() for p in self._query(f"OUTMODE? {output}").strip().split(",")]
            return int(parts[0]), int(parts[1]), int(parts[2])
        except (ValueError, IndexError, socket.error):
            return None

    def _read_ramp(self, output: int) -> tuple[int, float] | None:
        try:
            parts = [p.strip() for p in self._query(f"RAMP? {output}").strip().split(",")]
            return int(parts[0]), float(parts[1])
        except (ValueError, IndexError, socket.error):
            return None

    def _read_rampst(self, output: int) -> int | None:
        try:
            return int(self._query(f"RAMPST? {output}").strip())
        except (ValueError, socket.error):
            return None

    # ── SCPI write helpers ─────────────────────────────────────

    def set_setpoint(self, loop: int, value_k: float) -> None:
        self._send(f"SETP {loop},{value_k:.4f}")
        log.info("LakeShore setpoint loop %d → %.4f K", loop, value_k)

    def set_pid(self, output: int, p: float, i: float, d: float) -> None:
        self._send(f"PID {output},{p:.3f},{i:.3f},{d:.3f}")
        log.info("LakeShore PID out %d → P=%.3f I=%.3f D=%.3f", output, p, i, d)

    def set_range(self, output: int, rng: int) -> None:
        self._send(f"RANGE {output},{int(rng)}")
        log.info("LakeShore RANGE out %d → %d", output, int(rng))

    def set_mout(self, output: int, value_pct: float) -> None:
        # MOUT only applies when OUTMODE is Closed Loop / Zone / Open Loop.
        v = max(0.0, min(100.0, float(value_pct)))
        self._send(f"MOUT {output},{v:.2f}")
        log.info("LakeShore MOUT out %d → %.2f%%", output, v)

    def set_outmode(self, output: int, mode: int) -> None:
        # OUTMODE takes mode, input, powerup. Read the current input/powerup and
        # preserve them so the operator only ever has to think about `mode`.
        current = self._read_outmode(output)
        inp = current[1] if current else 1            # default: input A
        powerup = current[2] if current else 0
        self._send(f"OUTMODE {output},{int(mode)},{inp},{powerup}")
        log.info("LakeShore OUTMODE out %d → mode=%d (input=%d powerup=%d)",
                 output, int(mode), inp, powerup)

    def set_ramp(self, output: int, on: bool, rate_k_per_min: float) -> None:
        rate = max(0.1, min(100.0, float(rate_k_per_min)))
        self._send(f"RAMP {output},{1 if on else 0},{rate:.3f}")
        log.info("LakeShore RAMP out %d → on=%s rate=%.3f K/min",
                 output, bool(on), rate)

    # ── MQTT command handlers ──────────────────────────────────

    def _on_setpoint(self, topic: str, payload: dict) -> None:
        value = payload.get("value")
        loop = payload.get("loop", 1)
        if value is not None:
            self.set_setpoint(int(loop), float(value))

    def _on_pid(self, topic: str, payload: dict) -> None:
        out = int(payload.get("output", 1))
        try:
            self.set_pid(out, float(payload["p"]), float(payload["i"]), float(payload["d"]))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Bad PID payload %r: %s", payload, exc)

    def _on_range(self, topic: str, payload: dict) -> None:
        out = int(payload.get("output", 1))
        rng = payload.get("range")
        if rng is None:
            return
        self.set_range(out, int(rng))

    def _on_mout(self, topic: str, payload: dict) -> None:
        out = int(payload.get("output", 1))
        value = payload.get("value")
        if value is None:
            return
        self.set_mout(out, float(value))

    def _on_outmode(self, topic: str, payload: dict) -> None:
        out = int(payload.get("output", 1))
        mode = payload.get("mode")
        if mode is None:
            return
        self.set_outmode(out, int(mode))

    def _on_ramp(self, topic: str, payload: dict) -> None:
        out = int(payload.get("output", 1))
        on = bool(payload.get("on", False))
        rate = payload.get("rate")
        if rate is None:
            return
        self.set_ramp(out, on, float(rate))

    # ── low-level socket I/O ───────────────────────────────────
    # The LakeShore 350 closes idle TCP sockets after a few hours.  Detect
    # the resulting EPIPE / read-zero and transparently reconnect+retry once
    # so the driver self-heals instead of silently logging "Failed to read"
    # forever.

    def _open_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect((self._host, self._port))
        self._sock = s

    def _query(self, command: str) -> str:
        with self._lock:
            try:
                return self._raw_query(command)
            except (OSError, ConnectionError) as exc:
                log.warning("LakeShore I/O error on %r (%s); reconnecting", command, exc)
                self._open_socket()
                return self._raw_query(command)

    def _raw_query(self, command: str) -> str:
        if not self._sock:
            raise ConnectionError("LakeShore not connected")
        self._sock.sendall((command + "\r\n").encode())
        data = self._sock.recv(1024)
        if not data:                      # peer closed the connection
            raise ConnectionError("LakeShore closed the connection")
        return data.decode()

    def _send(self, command: str) -> None:
        """Fire-and-forget write (used for set_* methods)."""
        with self._lock:
            try:
                self._raw_send(command)
            except (OSError, ConnectionError) as exc:
                log.warning("LakeShore write error on %r (%s); reconnecting", command, exc)
                self._open_socket()
                self._raw_send(command)

    def _raw_send(self, command: str) -> None:
        if not self._sock:
            raise ConnectionError("LakeShore not connected")
        self._sock.sendall((command + "\r\n").encode())
