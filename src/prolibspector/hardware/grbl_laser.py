"""GRBL laser-stage control - real driver not included in the public edition.

PUBLIC-EDITION STUB
===================
The private ProLIBSpector edition drives the laser stage over a GRBL serial
link: homing with limit-switch verification, alarm ($X) recovery, synchronous
motion completion, gated laser firing interlocked with the safety checklist, and
per-pulse power/duration control. Firing a Class-4 laser from software is the
part of this product that carries real physical risk, so the driver is not
published.

Two classes live here:

* :class:`GrblLaserController` - unavailable-device shim. It imports cleanly and
  raises :class:`GrblLaserError` from ``connect()``. It will not open a serial
  port and cannot emit motion or laser commands.
* :class:`SimulatedGrblLaserController` - a genuine, fully working software
  simulation. It tracks position, feed rate and laser state in memory so the
  motion/geometry layers can be exercised with no hardware and no beam. It is
  clearly labelled as simulated and never touches a serial port.

The probe helpers report an honest "driver not included" result rather than
guessing at a controller's state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

GRBL_UNAVAILABLE_REASON = (
    "The GRBL laser driver is not included in the public edition. Software-controlled "
    "laser firing ships only with the private ProLIBSpector edition. "
    "Use the simulated laser controller to exercise motion planning without a beam."
)

DEFAULT_BAUDRATE = 115200


class GrblLaserError(RuntimeError):
    """Raised for GRBL connection, motion, or firing failures."""


@dataclass
class GrblProbeResult:
    """Outcome of probing a serial port for a GRBL controller."""

    port: str
    baudrate: int
    ok: bool = False
    alarm_active: bool = False
    responded: bool = False
    version: str | None = None
    status: str | None = None
    message: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def detail(self) -> str:
        return self.message


def probe_grbl_controller(
    port: str,
    *,
    baudrate: int = DEFAULT_BAUDRATE,
    timeout_s: float = 1.0,
) -> GrblProbeResult:
    """Report that no GRBL probe can be performed in this edition.

    The port is never opened. ``alarm_active`` stays ``False`` so the UI's
    "Unlock and Recheck" action stays correctly unavailable - claiming an alarm
    we did not observe would invite a user to send ``$X`` blindly.
    """
    del timeout_s  # nothing is waited on; no port is opened
    logger.info("GRBL probe skipped for %s: driver not included in the public edition.", port)
    return GrblProbeResult(
        port=str(port),
        baudrate=int(baudrate),
        ok=False,
        alarm_active=False,
        responded=False,
        message=GRBL_UNAVAILABLE_REASON,
        notes=[GRBL_UNAVAILABLE_REASON],
    )


def unlock_and_reprobe_grbl_controller(
    port: str,
    *,
    baudrate: int = DEFAULT_BAUDRATE,
    timeout_s: float = 1.0,
) -> GrblProbeResult:
    """Report that no unlock can be sent in this edition.

    No ``$X`` is transmitted and no port is opened.
    """
    del timeout_s
    result = probe_grbl_controller(port, baudrate=baudrate)
    result.notes.append("No $X unlock was sent: this edition cannot open the controller.")
    return result


class GrblLaserController:
    """Unavailable-device shim for the real GRBL serial controller."""

    is_simulated = False

    def __init__(self, port: str = "", *, baudrate: int = DEFAULT_BAUDRATE, **_kwargs) -> None:
        self.port = str(port)
        self.baudrate = int(baudrate)

    @property
    def is_connected(self) -> bool:
        return False

    def connect(self) -> str:
        """Always raise: this edition cannot drive a physical laser stage."""
        raise GrblLaserError(GRBL_UNAVAILABLE_REASON)

    def disconnect(self) -> None:
        """No-op: no port was opened."""
        return None

    def _refuse(self, action: str) -> None:
        raise GrblLaserError(f"Cannot {action}: {GRBL_UNAVAILABLE_REASON}")

    def home(self) -> str:
        self._refuse("home the laser stage")
        raise AssertionError("unreachable")  # pragma: no cover

    def move_to(self, x_mm: float, y_mm: float, *, feed_mm_min: float | None = None) -> str:
        self._refuse("move the laser stage")
        raise AssertionError("unreachable")  # pragma: no cover

    def fire(self, *, power_percent: float, pulse_ms: float) -> str:
        self._refuse("fire the laser")
        raise AssertionError("unreachable")  # pragma: no cover

    def unlock(self) -> str:
        self._refuse("clear the GRBL alarm")
        raise AssertionError("unreachable")  # pragma: no cover

    def status(self) -> str:
        self._refuse("read controller status")
        raise AssertionError("unreachable")  # pragma: no cover


class SimulatedGrblLaserController:
    """In-memory GRBL stage simulation. No serial port, no beam.

    This one is real and complete: it accepts the same calls as the private
    driver and tracks the resulting state, so motion planning and plate geometry
    can be exercised end to end without hardware.
    """

    is_simulated = True

    def __init__(self, port: str = "SIMULATED", *, baudrate: int = DEFAULT_BAUDRATE, **_kwargs) -> None:
        self.port = str(port)
        self.baudrate = int(baudrate)
        self._connected = False
        self.homed = False
        self.x_mm = 0.0
        self.y_mm = 0.0
        self.feed_mm_min = 3000.0
        self.laser_on = False
        self.shots_fired = 0
        self.command_log: list[str] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> str:
        self._connected = True
        self.command_log.append("connect")
        return f"Connected to simulated GRBL controller on {self.port} (Simulated)"

    def disconnect(self) -> None:
        self._connected = False
        self.laser_on = False
        self.command_log.append("disconnect")

    def _require_connection(self, action: str) -> None:
        if not self._connected:
            raise GrblLaserError(f"Simulated GRBL controller is not connected; cannot {action}.")

    def home(self) -> str:
        self._require_connection("home")
        self.x_mm = 0.0
        self.y_mm = 0.0
        self.homed = True
        self.command_log.append("$H")
        return "Homed (simulated)"

    def move_to(self, x_mm: float, y_mm: float, *, feed_mm_min: float | None = None) -> str:
        self._require_connection("move")
        if not self.homed:
            raise GrblLaserError("Simulated GRBL controller must be homed before moving.")
        if feed_mm_min is not None:
            self.feed_mm_min = float(feed_mm_min)
        self.x_mm = float(x_mm)
        self.y_mm = float(y_mm)
        command = f"G1 X{self.x_mm:.3f} Y{self.y_mm:.3f} F{self.feed_mm_min:.0f}"
        self.command_log.append(command)
        return command

    def fire(self, *, power_percent: float, pulse_ms: float) -> str:
        self._require_connection("fire")
        if not self.homed:
            raise GrblLaserError("Simulated GRBL controller must be homed before firing.")
        if not 0.0 <= float(power_percent) <= 100.0:
            raise ValueError("Laser power must be between 0 and 100 percent.")
        if float(pulse_ms) <= 0.0:
            raise ValueError("Laser pulse duration must be greater than zero.")
        self.shots_fired += 1
        self.laser_on = False  # pulse completes within the call
        command = f"M3 S{float(power_percent) * 10.0:.0f} (pulse {float(pulse_ms):.1f} ms, simulated)"
        self.command_log.append(command)
        return command

    def unlock(self) -> str:
        self._require_connection("unlock")
        self.command_log.append("$X")
        return "Alarm cleared (simulated)"

    def status(self) -> str:
        state = "Idle" if self._connected else "Disconnected"
        return f"<{state}|MPos:{self.x_mm:.3f},{self.y_mm:.3f},0.000|Sim:1>"


__all__ = [
    "DEFAULT_BAUDRATE",
    "GRBL_UNAVAILABLE_REASON",
    "GrblLaserController",
    "GrblLaserError",
    "GrblProbeResult",
    "SimulatedGrblLaserController",
    "probe_grbl_controller",
    "unlock_and_reprobe_grbl_controller",
]
