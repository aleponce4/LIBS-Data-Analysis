"""A GRBL controller that exists only as bytes on a pretend serial port.

`SimulatedGrblLaserController` in :mod:`~prolibspector.hardware.grbl_laser` fakes
the controller at the Python level: call `move_to()` and it updates a position
attribute. That is fast and fine for exercising acquisition logic, but it skips
every line of the code that actually talks to hardware -- the command framing,
the ack ledger, the status parser, the RX-budget flow control.

This module fakes the layer underneath instead. `GrblSerialSimulator` implements
enough of pyserial's `Serial` to be handed to `GrblLaserController` as its
`serial_factory`, so the *real* controller runs unmodified and every byte it
writes is parsed by something that answers the way GRBL 1.1 does. That is the
difference between "the app runs without hardware" and "the driver is tested".

    from prolibspector.hardware.grbl_laser import GrblLaserController
    from prolibspector.hardware.grbl_simulator import GrblSerialSimulator

    controller = GrblLaserController("SIM", serial_factory=GrblSerialSimulator)
    controller.connect()
    controller.home()
    controller.move_to(x=10.0, y=4.0, feed_mm_min=600.0)

The emulation is deliberately partial. It covers the dialect this application
speaks -- status polling, `$$` settings, homing, alarm and unlock, jogging,
G0/G1 moves, M3/M5 spindle gating, and the realtime bytes -- and rejects
anything else with `error:20` the way a real controller rejects an unsupported
word. It does not implement the planner, so motion completes instantly and
feed rates are parsed but not honoured for timing.

The fault-injection hooks exist because the interesting controller code is the
error handling: a port that goes away mid-stream, a command that never acks, a
controller sitting in alarm. Those paths are unreachable against a device that
always behaves.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

# GRBL answers `$$` with the full settings block. These four are the ones this
# application reads back: max spindle RPM, laser-mode enable, and the X/Y travel
# limits it validates planned moves against.
DEFAULT_SETTINGS: dict[str, float] = {
    "30": 1000.0,  # $30 maximum spindle speed, the S-value ceiling for M3
    "32": 1.0,  # $32 laser mode: 1 keeps the beam off during non-motion moves
    "110": 12000.0,  # $110 X max rate (mm/min)
    "111": 12000.0,  # $111 Y max rate (mm/min)
    "120": 500.0,  # $120 X acceleration (mm/sec^2)
    "121": 500.0,  # $121 Y acceleration (mm/sec^2)
    "130": 300.0,  # $130 X max travel (mm)
    "131": 200.0,  # $131 Y max travel (mm)
}

WELCOME_BANNER = "Grbl 1.1f ['$' for help]"

_REALTIME_BYTES = {b"?", b"!", b"~", b"\x18"}
_AXIS_RE = re.compile(r"(?P<axis>[XYZ])\s*(?P<value>-?\d+(?:\.\d+)?)", re.IGNORECASE)
_SPINDLE_RE = re.compile(r"\bS\s*(?P<value>\d+(?:\.\d+)?)", re.IGNORECASE)
_FEED_RE = re.compile(r"\bF\s*(?P<value>\d+(?:\.\d+)?)", re.IGNORECASE)


@dataclass
class SimulatorFaults:
    """Failure modes to inject, so error paths can be reached deliberately.

    Every field defaults to "behave correctly". A caller opts in to exactly the
    misbehaviour it wants to test.
    """

    #: Commands answered with ``error:1`` instead of ``ok``.
    error_commands: set[str] = field(default_factory=set)
    #: Commands that are swallowed entirely -- no ack, no error. Reproduces a
    #: controller that has stopped servicing its serial queue.
    no_response_commands: set[str] = field(default_factory=set)
    #: Commands that block the writer until :attr:`block_release` is set.
    block_commands: set[str] = field(default_factory=set)
    #: Raised from ``write()`` once armed. Simulates the cable being pulled.
    raise_on_write: BaseException | None = None
    #: Raised from ``read()``/``readline()`` once armed.
    raise_on_read: BaseException | None = None
    #: Start in the alarm state a real controller enters after a reset when
    #: homing is required, so ``$X`` recovery can be exercised.
    start_in_alarm: bool = False


class GrblSerialSimulator:
    """Enough of ``serial.Serial`` for :class:`GrblLaserController` to drive.

    Constructed with pyserial's argument order so it can be passed directly as a
    ``serial_factory``. Extra keyword arguments are accepted and ignored, because
    the controller passes options this emulator has no analogue for.
    """

    def __init__(
        self,
        port: str = "SIM",
        baudrate: int = 115200,
        *,
        timeout: float | None = 1.0,
        write_timeout: float | None = 1.0,
        faults: SimulatorFaults | None = None,
        settings: dict[str, float] | None = None,
        **_ignored,
    ) -> None:
        self.port = str(port)
        self.baudrate = int(baudrate)
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.is_open = True

        self.faults = faults or SimulatorFaults()
        self.settings = dict(settings or DEFAULT_SETTINGS)

        self.state = "Alarm" if self.faults.start_in_alarm else "Idle"
        self.x_mm = 0.0
        self.y_mm = 0.0
        self.z_mm = 0.0
        self.spindle_on = False
        self.spindle_speed = 0.0
        self.homed = False
        #: Modal distance mode. GRBL powers up in G90 (absolute).
        self.absolute_mode = True

        #: Every command line received, in order. The assertion surface for tests.
        self.commands: list[str] = []
        #: Set once a blocking command has been entered; lets a test synchronise.
        self.block_started = threading.Event()
        #: Set to release a blocked writer.
        self.block_release = threading.Event()
        if not self.faults.block_commands:
            self.block_release.set()

        self._rx = bytearray()
        self._lock = threading.Lock()
        self._emit(WELCOME_BANNER)

    # ---- pyserial surface -------------------------------------------------

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx)

    def write(self, data: bytes) -> int:
        if self.faults.raise_on_write is not None:
            raise self.faults.raise_on_write
        if not self.is_open:
            raise OSError("write to a closed port")

        # Realtime bytes are single characters GRBL acts on immediately; they are
        # not line-terminated and never receive an 'ok'.
        if data in _REALTIME_BYTES:
            self._handle_realtime(data)
            return len(data)

        text = data.decode("ascii", errors="replace")
        for line in text.splitlines():
            command = line.strip()
            if command:
                self._handle_command(command)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        if self.faults.raise_on_read is not None:
            raise self.faults.raise_on_read
        with self._lock:
            chunk = bytes(self._rx[:size])
            del self._rx[: len(chunk)]
        return chunk

    def readline(self) -> bytes:
        if self.faults.raise_on_read is not None:
            raise self.faults.raise_on_read
        with self._lock:
            index = self._rx.find(b"\n")
            if index == -1:
                return b""
            line = bytes(self._rx[: index + 1])
            del self._rx[: index + 1]
        return line

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._rx.clear()

    def reset_output_buffer(self) -> None:
        return None

    def close(self) -> None:
        self.is_open = False

    # ---- protocol ---------------------------------------------------------

    def _emit(self, line: str) -> None:
        with self._lock:
            self._rx.extend(f"{line}\n".encode("ascii"))

    def status_line(self) -> str:
        """The `<...>` report GRBL sends in reply to `?`."""
        feed_speed = f"|FS:0,{int(self.spindle_speed)}" if self.spindle_on else "|FS:0,0"
        return (
            f"<{self.state}|WPos:{self.x_mm:.3f},{self.y_mm:.3f},{self.z_mm:.3f}{feed_speed}>"
        )

    def _handle_realtime(self, data: bytes) -> None:
        if data == b"?":
            self._emit(self.status_line())
        elif data == b"!":
            self.state = "Hold"
        elif data == b"~":
            if self.state == "Hold":
                self.state = "Idle"
        elif data == b"\x18":  # Ctrl-X soft reset
            self.spindle_on = False
            self.spindle_speed = 0.0
            self.state = "Alarm" if not self.homed else "Idle"
            self._emit(WELCOME_BANNER)

    def _handle_command(self, command: str) -> None:
        self.commands.append(command)

        if command in self.faults.block_commands:
            self.block_started.set()
            self.block_release.wait(2.0)

        if command in self.faults.no_response_commands:
            return
        if command in self.faults.error_commands:
            self._emit("error:1")
            return

        # An alarm locks out everything except unlock, homing, and settings reads.
        if self.state == "Alarm" and not (
            command.startswith("$X") or command.startswith("$H") or command == "$$"
        ):
            self._emit("error:9")  # G-code locked out during alarm or jog state
            return

        if command == "$$":
            for key in sorted(self.settings, key=int):
                self._emit(f"${key}={self.settings[key]:g}")
            self._emit("ok")
        elif command.startswith("$H"):
            self.x_mm = self.y_mm = self.z_mm = 0.0
            self.homed = True
            self.state = "Idle"
            self._emit("ok")
        elif command.startswith("$X"):
            self.state = "Idle"
            self._emit("ok")
        elif command.startswith("$J="):
            # A jog carries its own distance mode and does not disturb the modal
            # state, which is why this passes `relative` explicitly instead of
            # letting _apply_motion consult self.absolute_mode.
            body = command[3:]
            self._apply_motion(body, relative="G91" in body.upper())
            self._emit("ok")
        elif command.startswith(("G0", "G1", "G90", "G91", "G21", "G4")):
            upper = command.upper()
            if "G90" in upper:
                self.absolute_mode = True
            elif "G91" in upper:
                self.absolute_mode = False
            self._apply_motion(command)
            self._emit("ok")
        elif command.startswith("M3"):
            match = _SPINDLE_RE.search(command)
            self.spindle_speed = float(match.group("value")) if match else 0.0
            self.spindle_on = True
            self._emit("ok")
        elif command.startswith("M5"):
            self.spindle_on = False
            self.spindle_speed = 0.0
            self._emit("ok")
        elif command.startswith("$"):
            # $30=1000 style writes.
            key, _, value = command[1:].partition("=")
            if value and key.isdigit():
                self.settings[key] = float(value)
                self._emit("ok")
            else:
                self._emit("error:3")
        else:
            self._emit("error:20")  # unsupported or invalid g-code command

    def _apply_motion(self, gcode: str, *, relative: bool | None = None) -> None:
        """Move instantly to the commanded point.

        No planner, no acceleration ramp: this emulator exists to prove the
        controller's framing and parsing, not to model kinematics. Anything
        measuring motion *timing* has to run against real hardware.

        `relative` overrides the modal G90/G91 state, which is what a jog needs.
        """
        is_relative = self.absolute_mode is False if relative is None else relative
        for match in _AXIS_RE.finditer(gcode):
            value = float(match.group("value"))
            axis = match.group("axis").upper()
            if axis == "X":
                self.x_mm = self.x_mm + value if is_relative else value
            elif axis == "Y":
                self.y_mm = self.y_mm + value if is_relative else value
            else:
                self.z_mm = self.z_mm + value if is_relative else value
        _FEED_RE.search(gcode)  # parsed and ignored; see the docstring
