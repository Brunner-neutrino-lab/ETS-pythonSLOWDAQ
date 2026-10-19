"""Generic PID controller with anti-windup."""

from __future__ import annotations

import time


class PIDController:
    """Discrete PID controller with output clamping and back-calculation
    anti-windup.

    Direct-acting by default: output rises while *measured* is below the
    setpoint (e.g. a heater).  With ``reverse=True`` output rises while
    *measured* is above it (e.g. a dry-N2 purge lowering humidity).
    """

    def __init__(
        self,
        Kp: float = 1.0,
        Ki: float = 0.0,
        Kd: float = 0.0,
        setpoint: float = 0.0,
        output_min: float = 0.0,
        output_max: float = 1.0,
        reverse: bool = False,
    ):
        self.Kp = Kp
        self.Ki = Ki
        self.Kd = Kd
        self.setpoint = setpoint
        self.output_min = output_min
        self.output_max = output_max
        self.reverse = reverse
        self._integral = 0.0
        self._prev_error = 0.0
        self._prev_time: float | None = None

    def update(self, measured: float) -> float:
        """Compute new output given a *measured* process-variable reading."""
        now = time.monotonic()
        error = self.setpoint - measured
        if self.reverse:
            error = -error

        if self._prev_time is None:
            dt = 0.0
        else:
            dt = now - self._prev_time

        # Proportional
        p_term = self.Kp * error

        # Integral
        self._integral += error * dt
        i_term = self.Ki * self._integral

        # Derivative
        d_term = self.Kd * (error - self._prev_error) / dt if dt > 0 else 0.0

        output = p_term + i_term + d_term

        # Clamp + anti-windup (back-calculation)
        if output > self.output_max:
            output = self.output_max
            self._integral -= error * dt
        elif output < self.output_min:
            output = self.output_min
            self._integral -= error * dt

        self._prev_error = error
        self._prev_time = now
        return output

    def reset(self) -> None:
        self._integral = 0.0
        self._prev_error = 0.0
        self._prev_time = None
