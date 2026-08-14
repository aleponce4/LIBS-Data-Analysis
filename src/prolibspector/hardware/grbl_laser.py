"""GRBL-compatible laser motion and pulse control.

The Monport/LaserGRBL-style controller is treated as a serial instrument:
commands are newline-delimited G-code strings and successful commands end
with an ``ok`` response.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
import re
import threading
import time
from typing import Callable, Iterable, NoReturn, Sequence

logger = logging.getLogger(__name__)


_FIRE_ENABLE_TIMEOUT_S = 3.0
# GRBL 1.1's RX ring buffer is 128 bytes; keep a small margin so realtime
# bytes and rounding can never overflow it.
STREAM_RX_BUDGET = 120


class GrblLaserError(RuntimeError):
    """Raised when GRBL communication or command execution fails."""


class GrblStreamAborted(GrblLaserError):
    """A streamed program was intentionally stopped (feed hold + soft reset)."""


@dataclass(frozen=True)
class StreamLine:
    """One line of a compiled streamed program.

    ``is_fire`` marks the powered relay stroke; the streamer refuses to send
    it until the caller's fire gate opens (read-armed-before-fire).
    """

    text: str
    is_fire: bool = False
    fire_index: int | None = None


@dataclass
class StreamResult:
    lines_acked: int = 0
    fire_strokes_acked: int = 0
    # Fire lines transmitted to GRBL, acked or not. GRBL acks at parse time
    # and may execute a stroke whose 'ok' never got read back (abort), so
    # never-re-shoot decisions must use the sent count, not the acked count.
    fire_strokes_sent: int = 0
    hold_events: int = 0
    aborted_reason: str | None = None
    completed: bool = False
    fire_ack_monotonic: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class SerialPortInfo:
    device: str
    description: str = ""
    hwid: str = ""

    @property
    def label(self) -> str:
        detail = self.description or self.hwid
        return f"{self.device} - {detail}" if detail else self.device


@dataclass(frozen=True)
class GrblStatus:
    raw: str
    state: str = "unknown"
    x: float | None = None
    y: float | None = None
    z: float | None = None
    active_pins: frozenset[str] = field(default_factory=frozenset)
    coordinate_kind: str = ""
    work_x: float | None = None
    work_y: float | None = None
    work_z: float | None = None
    machine_x: float | None = None
    machine_y: float | None = None
    machine_z: float | None = None
    wco_x: float | None = None
    wco_y: float | None = None
    wco_z: float | None = None

    @property
    def work_position_available(self) -> bool:
        return self.work_x is not None and self.work_y is not None

    @property
    def work_position(self) -> tuple[float | None, float | None, float | None]:
        return self.work_x, self.work_y, self.work_z

    @property
    def machine_position(self) -> tuple[float | None, float | None, float | None]:
        return self.machine_x, self.machine_y, self.machine_z

    @property
    def wco(self) -> tuple[float | None, float | None, float | None]:
        return self.wco_x, self.wco_y, self.wco_z


@dataclass(frozen=True)
class GrblControllerProbeResult:
    port: str
    baudrate: int
    status: GrblStatus | None = None
    identity_lines: tuple[str, ...] = ()
    raw_lines: tuple[str, ...] = ()
    error: str = ""

    @property
    def state_text(self) -> str:
        if self.status is None:
            return "unknown"
        return str(self.status.state or "unknown")

    @property
    def active_pins_text(self) -> str:
        if self.status is None:
            return "none"
        pins = "".join(sorted(self.status.active_pins))
        return pins or "none"

    @property
    def work_coordinates_reliable(self) -> bool:
        return bool(self.status and self.status.work_position_available)

    @property
    def alarm_active(self) -> bool:
        return self.state_text.lower().startswith("alarm")

    @property
    def detected_grbl(self) -> bool:
        return bool(self.status is not None or self.identity_lines)


_COORDINATE_RE = r"(?P<x>-?\d+(?:\.\d+)?),(?P<y>-?\d+(?:\.\d+)?),(?P<z>-?\d+(?:\.\d+)?)"
_POSITION_RE = re.compile(rf"(?P<kind>WPos|MPos):{_COORDINATE_RE}")
_WCO_RE = re.compile(rf"(?:^|\|)WCO:{_COORDINATE_RE}(?:\||$)")
_ACTIVE_PIN_RE = re.compile(r"(?:^|\|)Pn:(?P<pins>[A-Za-z]+)(?:\||$)")
_SETTING_RE = re.compile(r"^\$(?P<key>\d+)=(?P<value>-?\d+(?:\.\d+)?)")


def list_serial_ports() -> list[SerialPortInfo]:
    """Return serial ports visible to pyserial.

    The function returns an empty list when pyserial is not installed so the
    rest of the app can still import and run in simulation mode.
    """
    try:
        from serial.tools import list_ports
    except Exception:
        return []

    ports = []
    for port in list_ports.comports():
        ports.append(
            SerialPortInfo(
                device=str(getattr(port, "device", "")),
                description=str(getattr(port, "description", "") or ""),
                hwid=str(getattr(port, "hwid", "") or ""),
            )
        )
    return ports


def parse_status_line(
    line: str,
    *,
    cached_wco: tuple[float | None, float | None, float | None] | None = None,
) -> GrblStatus:
    """Parse a GRBL status line such as ``<Idle|WPos:1.000,2.000,0.000>``."""
    raw = line.strip()
    if not raw.startswith("<") or not raw.endswith(">"):
        return GrblStatus(raw=raw)

    body = raw[1:-1]
    state = body.split("|", 1)[0] or "unknown"
    pin_match = _ACTIVE_PIN_RE.search(body)
    active_pins = frozenset(pin.upper() for pin in pin_match.group("pins")) if pin_match else frozenset()
    wco_match = _WCO_RE.search(body)
    wco_x = wco_y = wco_z = None
    if wco_match:
        wco_x = float(wco_match.group("x"))
        wco_y = float(wco_match.group("y"))
        wco_z = float(wco_match.group("z"))
    elif cached_wco is not None and all(value is not None for value in cached_wco):
        wco_x = float(cached_wco[0])
        wco_y = float(cached_wco[1])
        wco_z = float(cached_wco[2])
    match = _POSITION_RE.search(body)
    if not match:
        return GrblStatus(
            raw=raw,
            state=state,
            active_pins=active_pins,
            wco_x=wco_x,
            wco_y=wco_y,
            wco_z=wco_z,
        )
    coordinate_kind = match.group("kind")
    x = float(match.group("x"))
    y = float(match.group("y"))
    z = float(match.group("z"))
    work_x = work_y = work_z = None
    machine_x = machine_y = machine_z = None
    if coordinate_kind == "WPos":
        work_x, work_y, work_z = x, y, z
        if wco_x is not None and wco_y is not None and wco_z is not None:
            machine_x = x + wco_x
            machine_y = y + wco_y
            machine_z = z + wco_z
    elif coordinate_kind == "MPos":
        machine_x, machine_y, machine_z = x, y, z
        if wco_x is not None and wco_y is not None and wco_z is not None:
            work_x = x - wco_x
            work_y = y - wco_y
            work_z = z - wco_z
    return GrblStatus(
        raw=raw,
        state=state,
        x=x,
        y=y,
        z=z,
        active_pins=active_pins,
        coordinate_kind=coordinate_kind,
        work_x=work_x,
        work_y=work_y,
        work_z=work_z,
        machine_x=machine_x,
        machine_y=machine_y,
        machine_z=machine_z,
        wco_x=wco_x,
        wco_y=wco_y,
        wco_z=wco_z,
    )


def _probe_serial_factory(serial_factory: Callable[..., object] | None):
    if serial_factory is not None:
        return serial_factory
    try:
        import serial
    except Exception as exc:
        raise GrblLaserError("pyserial is required for real laser probes.") from exc
    return serial.Serial


def _open_probe_serial(
    port: str,
    baudrate: int,
    *,
    timeout_s: float,
    serial_factory: Callable[..., object] | None,
):
    factory = _probe_serial_factory(serial_factory)
    try:
        return factory(
            port,
            int(baudrate),
            timeout=float(timeout_s),
            write_timeout=float(timeout_s),
        )
    except TypeError:
        return factory(port, int(baudrate))


def _decode_probe_line(raw_line) -> str:
    if not raw_line:
        return ""
    if isinstance(raw_line, bytes):
        return raw_line.decode("ascii", errors="replace").strip()
    return str(raw_line).strip()


def _read_probe_line(serial_obj) -> str:
    return _decode_probe_line(serial_obj.readline())


def _write_probe_bytes(serial_obj, data: bytes) -> None:
    serial_obj.write(data)
    flush = getattr(serial_obj, "flush", None)
    if callable(flush):
        flush()


def _wake_probe_controller(serial_obj) -> None:
    _write_probe_bytes(serial_obj, b"\r\n\r\n")
    time.sleep(0.05)
    reset = getattr(serial_obj, "reset_input_buffer", None)
    if callable(reset):
        try:
            reset()
            return
        except Exception:
            logger.debug("GRBL probe input-buffer reset failed; falling back to timed drain.", exc_info=True)
    deadline = time.monotonic() + 0.1
    while time.monotonic() < deadline:
        try:
            if not _read_probe_line(serial_obj):
                break
        except Exception:
            break


def _probe_poll_status(serial_obj, raw_lines: list[str], *, timeout_s: float) -> GrblStatus:
    _write_probe_bytes(serial_obj, b"?")
    deadline = time.monotonic() + max(0.01, float(timeout_s))
    while time.monotonic() < deadline:
        line = _read_probe_line(serial_obj)
        if not line:
            continue
        raw_lines.append(line)
        if line.startswith("<"):
            return parse_status_line(line)
    raise GrblLaserError("No GRBL status response was received during the controller probe.")


def _probe_read_identity(serial_obj, raw_lines: list[str], *, timeout_s: float) -> tuple[str, ...]:
    _write_probe_bytes(serial_obj, b"$I\n")
    identity: list[str] = []
    deadline = time.monotonic() + max(0.01, float(timeout_s))
    while time.monotonic() < deadline:
        line = _read_probe_line(serial_obj)
        if not line:
            continue
        raw_lines.append(line)
        if line.lower() == "ok":
            return tuple(identity)
        if line.startswith("["):
            identity.append(line)
    return tuple(identity)


def _probe_send_command_ok(serial_obj, command: str, raw_lines: list[str], *, timeout_s: float) -> None:
    clean = command.strip()
    if not clean:
        raise GrblLaserError("Cannot send an empty GRBL probe command.")
    _write_probe_bytes(serial_obj, (clean + "\n").encode("ascii"))
    deadline = time.monotonic() + max(0.01, float(timeout_s))
    while time.monotonic() < deadline:
        line = _read_probe_line(serial_obj)
        if not line:
            continue
        raw_lines.append(line)
        lower = line.lower()
        if lower == "ok":
            return
        if lower.startswith("error"):
            raise GrblLaserError(f"GRBL rejected '{clean}': {line}")
        if lower.startswith("alarm"):
            raise GrblLaserError(f"GRBL alarm while running '{clean}': {line}")
    raise GrblLaserError(f"Timed out waiting for GRBL response to '{clean}'.")


def _close_probe_serial(serial_obj) -> None:
    if serial_obj is None:
        return
    close = getattr(serial_obj, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("Failed to close GRBL probe serial port.", exc_info=True)


def probe_grbl_controller(
    port: str,
    baudrate: int = 115200,
    *,
    timeout_s: float = 1.0,
    soft_reset_delay_s: float = 2.0,
    serial_factory: Callable[..., object] | None = None,
) -> GrblControllerProbeResult:
    """Open a serial port and run status/identity diagnostics only.

    If the first status poll gets no answer, one Ctrl-X soft reset is sent and
    the poll retried: MonPort GRBL 1.1f boards can power up into a boot lock
    that ignores '?' until soft reset. Ctrl-X commands no motion or firing.
    """
    raw_lines: list[str] = []
    status: GrblStatus | None = None
    identity_lines: tuple[str, ...] = ()
    error = ""
    serial_obj = None
    try:
        serial_obj = _open_probe_serial(
            port,
            int(baudrate),
            timeout_s=timeout_s,
            serial_factory=serial_factory,
        )
        _wake_probe_controller(serial_obj)
        try:
            status = _probe_poll_status(serial_obj, raw_lines, timeout_s=timeout_s)
        except Exception as first_exc:
            try:
                _write_probe_bytes(serial_obj, b"\x18")
                raw_lines.append("(no response to '?'; sent Ctrl-X soft reset)")
                time.sleep(max(0.0, float(soft_reset_delay_s)))
                status = _probe_poll_status(
                    serial_obj, raw_lines, timeout_s=max(float(timeout_s), 2.0)
                )
            except Exception:
                error = str(first_exc)
        try:
            identity_lines = _probe_read_identity(serial_obj, raw_lines, timeout_s=timeout_s)
        except Exception as exc:
            if not error:
                error = str(exc)
    except Exception as exc:
        error = str(exc)
    finally:
        _close_probe_serial(serial_obj)
    return GrblControllerProbeResult(
        port=str(port),
        baudrate=int(baudrate),
        status=status,
        identity_lines=identity_lines,
        raw_lines=tuple(raw_lines),
        error=error,
    )


def unlock_and_reprobe_grbl_controller(
    port: str,
    baudrate: int = 115200,
    *,
    timeout_s: float = 1.0,
    serial_factory: Callable[..., object] | None = None,
) -> GrblControllerProbeResult:
    """Send an explicit GRBL unlock and immediately poll status."""
    raw_lines: list[str] = []
    status: GrblStatus | None = None
    error = ""
    serial_obj = None
    try:
        serial_obj = _open_probe_serial(
            port,
            int(baudrate),
            timeout_s=timeout_s,
            serial_factory=serial_factory,
        )
        _wake_probe_controller(serial_obj)
        _probe_send_command_ok(serial_obj, "$X", raw_lines, timeout_s=timeout_s)
        status = _probe_poll_status(serial_obj, raw_lines, timeout_s=timeout_s)
    except Exception as exc:
        error = str(exc)
    finally:
        _close_probe_serial(serial_obj)
    return GrblControllerProbeResult(
        port=str(port),
        baudrate=int(baudrate),
        status=status,
        raw_lines=tuple(raw_lines),
        error=error,
    )


class GrblLaserController:
    """Serial controller for GRBL-compatible laser engravers."""

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 115200,
        timeout_s: float = 1.0,
        write_timeout_s: float = 1.0,
        startup_delay_s: float = 2.0,
        serial_factory: Callable[..., object] | None = None,
    ):
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout_s = float(timeout_s)
        self.write_timeout_s = float(write_timeout_s)
        self.startup_delay_s = float(startup_delay_s)
        self._serial_factory = serial_factory
        self._serial = None
        self.last_status = GrblStatus(raw="")
        self.cached_settings: dict[str, float] = {}
        self.command_log: list[str] = []
        self._raw_emergency_sent = False
        self._cached_wco: tuple[float, float, float] | None = None
        self._command_lock = threading.RLock()
        self.reconnect_required = False
        self.reconnect_required_reason = ""
        self.connection_lost = False
        self.connection_lost_reason = ""

    @property
    def is_connected(self) -> bool:
        serial_obj = self._serial
        return bool(serial_obj and getattr(serial_obj, "is_open", True))

    @property
    def is_simulated(self) -> bool:
        """False: this controller drives a real beam.

        Callers gate motion-safety validation and machine-limit checks on this.
        It is a property rather than a class check so that a controller wrapping
        a fake serial port still reports itself as real -- the protocol is being
        exercised for its own sake there, and the safety checks should run.
        """
        return False

    @property
    def position(self) -> tuple[float | None, float | None, float | None]:
        return self.last_status.work_position

    def connect(self) -> str:
        with self._command_lock:
            if self.is_connected:
                return f"Connected: {self.port}"
            self.reconnect_required = False
            self.reconnect_required_reason = ""
            self.connection_lost = False
            self.connection_lost_reason = ""
            self.cached_settings = {}
            self._cached_wco = None

            if self._serial_factory is None:
                try:
                    import serial
                except Exception as exc:
                    raise GrblLaserError("pyserial is required for real laser connections.") from exc

                serial_factory = serial.Serial
            else:
                serial_factory = self._serial_factory

            try:
                self._serial = serial_factory(
                    self.port,
                    self.baudrate,
                    timeout=self.timeout_s,
                    write_timeout=self.write_timeout_s,
                )
            except TypeError:
                self._serial = serial_factory(self.port, self.baudrate)
            except Exception as exc:
                raise GrblLaserError(f"Could not open laser serial port {self.port}: {exc}") from exc

            try:
                if self.startup_delay_s > 0:
                    time.sleep(self.startup_delay_s)
                self._wake_controller()
                # Startup can legitimately be in Alarm, where normal newline
                # G-code such as M5 may be rejected before we can report status.
                # No other transaction owns the stream during connect, so keep
                # this as a locked best-effort safety write and let the status
                # probe below determine whether GRBL is reachable.
                self._write_raw_m5_locked()
                try:
                    status = self.poll_status(timeout_s=max(self.timeout_s, 1.0))
                except GrblLaserError:
                    # MonPort GRBL 1.1f boards can power up into a boot lock
                    # that ignores '?' until a Ctrl-X soft reset. Ctrl-X is
                    # safe here: no motion or firing exists yet at connect.
                    status = self._soft_reset_and_poll_locked()
            except Exception as exc:
                serial_obj = self._serial
                self._serial = None
                try:
                    close = getattr(serial_obj, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    logger.debug("Failed to close laser serial port after connect failure.", exc_info=True)
                if isinstance(exc, GrblLaserError):
                    raise
                raise GrblLaserError(
                    f"Laser serial port {self.port} opened, but no GRBL status response was received."
                ) from exc
            return f"Connected: {self.port} @ {self.baudrate} ({status.state})"

    def disconnect(self) -> None:
        with self._command_lock:
            if self._serial is None:
                return
            try:
                if self.is_connected:
                    if self.reconnect_required:
                        try:
                            self._write_raw_m5_locked()
                        except Exception:
                            logger.debug("Failed to send final raw M5 before disconnect.", exc_info=True)
                    else:
                        self.laser_off()
            finally:
                # A port failure during laser_off may already have released
                # the handle; close only what is still held.
                serial_obj, self._serial = self._serial, None
                if serial_obj is not None:
                    serial_obj.close()

    def _wake_controller(self) -> None:
        serial_obj = self._require_serial()
        try:
            serial_obj.write(b"\r\n\r\n")
            serial_obj.flush()
        except Exception as exc:
            raise GrblLaserError(f"Failed to wake GRBL controller: {exc}") from exc
        time.sleep(0.05)
        self._drain_input()

    def _soft_reset_and_poll_locked(self) -> GrblStatus:
        """Ctrl-X soft reset, wait out the reboot, then re-poll status."""
        serial_obj = self._require_serial()
        try:
            serial_obj.write(b"\x18")
            serial_obj.flush()
        except Exception as exc:
            raise GrblLaserError(f"Failed to soft-reset GRBL controller: {exc}") from exc
        if self.startup_delay_s > 0:
            time.sleep(self.startup_delay_s)
        self._drain_input()
        self._write_raw_m5_locked()
        try:
            return self._poll_status_locked(timeout_s=max(self.timeout_s, 2.0))
        except GrblLaserError as exc:
            raise GrblLaserError(
                f"Laser serial port {self.port} opened, but no GRBL status response "
                "was received (even after a Ctrl-X soft reset)."
            ) from exc

    def _drain_input(self) -> None:
        serial_obj = self._require_serial()
        reset = getattr(serial_obj, "reset_input_buffer", None)
        if callable(reset):
            try:
                reset()
                return
            except Exception:
                logger.debug("GRBL input-buffer reset failed; falling back to timed drain.", exc_info=True)

        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline:
            try:
                line = serial_obj.readline()
            except Exception:
                break
            if not line:
                break

    def _require_serial(self):
        if not self._serial:
            if self.connection_lost:
                raise GrblLaserError(self.connection_lost_reason or "Laser connection lost; reconnect the laser.")
            raise GrblLaserError("Laser is not connected.")
        return self._serial

    def _mark_reconnect_required(self, reason: str) -> None:
        if self.connection_lost:
            # The handle is already released; the desync latch is meaningless
            # on a dead port and its "resync/reconnect in place" wording would
            # contradict the connection-lost message.
            return
        self.reconnect_required = True
        self.reconnect_required_reason = str(reason or "GRBL command stream is unsafe.")

    def _handle_port_failure_locked(self, exc: Exception, context: str) -> NoReturn:
        """Release the serial handle after a port-level I/O failure and raise.

        Called with _command_lock held when a write/read raises OSError
        (pyserial SerialException, including write timeouts: a TX buffer that
        will not drain means the device is gone). Closing the handle promptly
        is what lets Windows re-enumerate the USB-serial adapter when the
        laser is powered back on; holding it forces the user into restart
        roulette. The wording must never contain the substrings that mark an
        error as retryable ("no GRBL status response", "timed out") -- port
        loss must not be retried.
        """
        serial_obj, self._serial = self._serial, None
        try:
            if serial_obj is not None:
                serial_obj.close()
        except Exception:
            logger.debug("Failed to close laser serial port after port failure.", exc_info=True)
        self.connection_lost = True
        self.connection_lost_reason = f"Laser connection lost while {context}: {exc}"
        raise GrblLaserError(
            f"Laser connection lost while {context} ({exc}). "
            "Power the laser back on (or replug USB), then reconnect. "
            "The port may reappear under a different COM number."
        ) from exc

    def _write_raw_m5_locked(self) -> None:
        """Write an unacknowledged M5 while the caller owns the command lock."""
        serial_obj = self._require_serial()
        self.command_log.append("M5")
        try:
            serial_obj.write(b"M5\n")
            serial_obj.flush()
        except OSError as exc:
            self._handle_port_failure_locked(exc, "sending emergency M5")
        self._raw_emergency_sent = True

    def _remember_status_line(self, line: str) -> GrblStatus:
        status = parse_status_line(line, cached_wco=self._cached_wco)
        if status.wco_x is not None and status.wco_y is not None and status.wco_z is not None:
            self._cached_wco = (float(status.wco_x), float(status.wco_y), float(status.wco_z))
        self.last_status = status
        return status

    def _raise_alarm_response(self, command: str, line: str) -> None:
        try:
            self._drain_input()
        except Exception:
            logger.debug("Failed to drain GRBL input after alarm response.", exc_info=True)
        raise GrblLaserError(
            f"GRBL alarm while running '{command}': {line}. "
            "Use Recover/Home after checking limit switches and controller state."
        )

    def send_command(self, command: str, *, timeout_s: float | None = None) -> list[str]:
        """Send one GRBL command and wait for ``ok`` or ``error``."""
        if not command or not command.strip():
            raise GrblLaserError("Cannot send an empty GRBL command.")

        with self._command_lock:
            if self.reconnect_required:
                reason = self.reconnect_required_reason or "a previous GRBL command timed out"
                raise GrblLaserError(f"Reconnect the laser controller before continuing; {reason}")
            serial_obj = self._require_serial()
            clean = command.strip()
            if self._raw_emergency_sent:
                self._raw_emergency_sent = False
                self._drain_input()
            self.command_log.append(clean)
            deadline = time.monotonic() + (self.timeout_s if timeout_s is None else float(timeout_s))
            responses: list[str] = []

            try:
                serial_obj.write((clean + "\n").encode("ascii"))
                serial_obj.flush()
            except OSError as exc:
                self._handle_port_failure_locked(exc, f"sending '{clean}'")
            except Exception as exc:
                raise GrblLaserError(f"Failed to send GRBL command '{clean}': {exc}") from exc

            while time.monotonic() < deadline:
                try:
                    raw_line = serial_obj.readline()
                except OSError as exc:
                    self._handle_port_failure_locked(exc, f"awaiting reply to '{clean}'")
                if not raw_line:
                    continue
                line = raw_line.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                responses.append(line)
                lower = line.lower()
                setting_match = _SETTING_RE.match(line)
                if setting_match:
                    self.cached_settings[setting_match.group("key")] = float(setting_match.group("value"))
                    continue
                if lower == "ok":
                    return responses
                if lower.startswith("alarm"):
                    self._raise_alarm_response(clean, line)
                if lower.startswith("error"):
                    raise GrblLaserError(f"GRBL rejected '{clean}': {line}")
                if line.startswith("<"):
                    self._remember_status_line(line)

            reason = f"Timed out waiting for GRBL response to '{clean}'."
            self._mark_reconnect_required(reason)
            raise GrblLaserError(f"{reason} Reconnect the laser controller before more motion or firing.")

    def stream_commands(self, commands: Iterable[str], *, timeout_s: float | None = None) -> list[list[str]]:
        with self._command_lock:
            return [self.send_command(command, timeout_s=timeout_s) for command in commands]

    def _poll_status_locked(self, *, timeout_s: float = 1.0) -> GrblStatus:
        serial_obj = self._require_serial()
        if self._raw_emergency_sent:
            self._raw_emergency_sent = False
            self._drain_input()
        deadline = time.monotonic() + float(timeout_s)
        try:
            serial_obj.write(b"?")
            serial_obj.flush()
        except OSError as exc:
            self._handle_port_failure_locked(exc, "polling status")
        except Exception as exc:
            raise GrblLaserError(f"Failed to poll GRBL status: {exc}") from exc

        while time.monotonic() < deadline:
            try:
                raw_line = serial_obj.readline()
            except OSError as exc:
                self._handle_port_failure_locked(exc, "reading status")
            if not raw_line:
                continue
            line = raw_line.decode("ascii", errors="replace").strip()
            lower = line.lower()
            if line.startswith("<"):
                return self._remember_status_line(line)
            if lower.startswith("alarm"):
                self._raise_alarm_response("?", line)
        raise GrblLaserError(
            f"Laser serial port {self.port} opened, but no GRBL status response was received."
        )

    def poll_status(self, *, timeout_s: float = 1.0) -> GrblStatus:
        with self._command_lock:
            if self.reconnect_required:
                reason = self.reconnect_required_reason or "a previous GRBL command timed out"
                raise GrblLaserError(f"Reconnect the laser controller before continuing; {reason}")
            return self._poll_status_locked(timeout_s=timeout_s)

    def resync_command_stream(self, *, timeout_s: float = 2.0) -> GrblStatus:
        """Best-effort recovery after a command timeout desynchronizes replies."""
        with self._command_lock:
            try:
                self._write_raw_m5_locked()
            except Exception as exc:
                reason = f"Failed to send raw M5 during GRBL resync: {exc}"
                self._mark_reconnect_required(reason)
                raise GrblLaserError(f"{reason}. Reconnect the laser controller before continuing.") from exc

            self._raw_emergency_sent = False
            try:
                self._drain_input()
                status = self._poll_status_locked(timeout_s=timeout_s)
            except Exception as exc:
                reason = f"GRBL command stream resync failed: {exc}"
                self._mark_reconnect_required(reason)
                raise GrblLaserError(f"{reason}. Reconnect the laser controller before continuing.") from exc

            self.reconnect_required = False
            self.reconnect_required_reason = ""
            return status

    def confirm_laser_off_and_status(self, *, timeout_s: float = 2.0) -> tuple[GrblStatus, bool]:
        """Serialize laser-off cleanup and prove that the GRBL stream still responds."""
        with self._command_lock:
            resync_used = bool(self.reconnect_required)
            if not resync_used:
                try:
                    self.send_command("M5", timeout_s=timeout_s)
                    status = self._poll_status_locked(timeout_s=timeout_s)
                except Exception:
                    resync_used = True
                else:
                    self.reconnect_required = False
                    self.reconnect_required_reason = ""
                    return status, False

            try:
                status = self.resync_command_stream(timeout_s=timeout_s)
            except Exception as exc:
                reason = f"Failed to confirm laser-off and recover GRBL status: {exc}"
                self._mark_reconnect_required(reason)
                raise GrblLaserError(f"{reason}. Reconnect the laser controller before continuing.") from exc
            return status, resync_used

    def wait_until_idle(self, *, timeout_s: float = 60.0, poll_interval_s: float = 0.1) -> GrblStatus:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        interval = max(0.01, float(poll_interval_s))
        last_status = self.last_status
        last_error: Exception | None = None
        while time.monotonic() <= deadline:
            try:
                status = self.poll_status(timeout_s=min(self.timeout_s, max(0.05, interval)))
            except GrblLaserError as exc:
                if "no GRBL status response" not in str(exc):
                    raise
                last_error = exc
                time.sleep(interval)
                continue
            last_status = status
            last_error = None
            state = (status.state or "").lower()
            if state == "idle":
                return status
            if state.startswith(("alarm", "door", "hold")):
                raise GrblLaserError(f"GRBL controller is not ready for firing: {status.raw or status.state}")
            time.sleep(interval)
        if last_error is not None:
            detail = last_status.raw or last_status.state if last_status is not None else "unknown"
            raise GrblLaserError(
                f"Timed out waiting for GRBL idle state after transient status failures. "
                f"Last status: {detail}. Last error: {last_error}"
            ) from last_error
        raise GrblLaserError(f"Timed out waiting for GRBL idle state. Last status: {last_status.raw or last_status.state}")

    def unlock(self) -> None:
        self.send_command("$X")

    def home(self) -> None:
        self.send_command("$H", timeout_s=max(self.timeout_s, 30.0))

    def read_grbl_settings(self, keys: set[str] | None = None) -> dict[str, float]:
        self.send_command("$$", timeout_s=max(self.timeout_s, 3.0))
        if keys is None:
            return dict(self.cached_settings)
        selected = {str(item) for item in keys}
        return {
            str(key): value
            for key, value in self.cached_settings.items()
            if str(key) in selected
        }

    def ensure_work_position(self, *, timeout_s: float = 2.0) -> tuple[float, float, float | None]:
        status = self.last_status
        if not status.work_position_available:
            status = self.poll_status(timeout_s=timeout_s)
        if not status.work_position_available:
            kind = status.coordinate_kind or "none"
            raise GrblLaserError(
                f"Reliable GRBL work coordinates are unavailable from {kind}; home the controller before motion."
            )
        return float(status.work_x), float(status.work_y), status.work_z

    def move_to(self, x_mm: float, y_mm: float, *, feed_mm_min: float | None = None) -> None:
        self.ensure_work_position()
        commands = ["G90"]
        move_code = "G1" if feed_mm_min is not None else "G0"
        move = f"{move_code} X{float(x_mm):.3f} Y{float(y_mm):.3f}"
        if feed_mm_min is not None:
            move = f"{move} F{float(feed_mm_min):.1f}"
        commands.append(move)
        self.stream_commands(commands)
        self.last_status = GrblStatus(
            raw="",
            state="Run",
            x=float(x_mm),
            y=float(y_mm),
            z=self.last_status.work_z,
            coordinate_kind="WPos",
            work_x=float(x_mm),
            work_y=float(y_mm),
            work_z=self.last_status.work_z,
        )

    def jog(self, dx_mm: float = 0.0, dy_mm: float = 0.0, *, feed_mm_min: float = 1000.0) -> None:
        self.ensure_work_position()
        self.send_command(f"$J=G91 X{float(dx_mm):.3f} Y{float(dy_mm):.3f} F{float(feed_mm_min):.1f}")
        x = self.last_status.work_x
        y = self.last_status.work_y
        z = self.last_status.work_z
        self.last_status = GrblStatus(
            raw="",
            state="Jog",
            x=None if x is None else x + float(dx_mm),
            y=None if y is None else y + float(dy_mm),
            z=z,
            coordinate_kind="WPos",
            work_x=None if x is None else x + float(dx_mm),
            work_y=None if y is None else y + float(dy_mm),
            work_z=z,
        )

    def laser_off(self) -> None:
        self.send_command("M5")

    def feed_hold(self) -> None:
        """Best-effort GRBL real-time feed hold for stopping motion."""
        serial_obj = self._serial
        if serial_obj is None:
            return
        self.command_log.append("!")
        try:
            serial_obj.write(b"!")
            serial_obj.flush()
        except Exception:
            logger.exception("Failed to send GRBL feed hold command.")

    def cycle_start(self) -> None:
        """Best-effort GRBL real-time cycle start / resume after a feed hold."""
        serial_obj = self._serial
        if serial_obj is None:
            return
        self.command_log.append("~")
        try:
            serial_obj.write(b"~")
            serial_obj.flush()
        except Exception:
            logger.exception("Failed to send GRBL cycle start command.")

    def stream_program(
        self,
        lines: Sequence[StreamLine],
        *,
        fire_gate: Callable[[int | None], bool] | None = None,
        should_abort: Callable[[], str | None] | None = None,
        on_progress: Callable[[GrblStatus], None] | None = None,
        status_interval_s: float = 0.05,
        ack_timeout_s: float = 5.0,
        rx_budget: int = STREAM_RX_BUDGET,
    ) -> StreamResult:
        """Stream a compiled program with character-counting flow control.

        One unified read loop owns the port for the whole program and routes
        every reply itself (``ok`` acks, ``error``/``ALARM``, ``<...>`` status
        reports) — interleaving ``poll_status`` with outstanding acks would
        silently eat them. ``?`` is injected as a realtime byte (no RX cost)
        every ``status_interval_s`` for progress and pin monitoring.

        A line with ``is_fire`` is only sent once ``fire_gate(fire_index)``
        returns True: withholding it starves the planner, which decelerates
        and stops at the stroke start with the beam off ($32=1 laser mode) —
        this is the read-armed-before-fire valve, no feed hold needed.

        Failure semantics match ``send_command``: an ack timeout latches
        ``reconnect_required`` after a best-effort raw M5; ``should_abort``
        triggers feed hold + soft reset (position retained) and raises
        ``GrblStreamAborted``.
        """
        with self._command_lock:
            if self.reconnect_required:
                reason = self.reconnect_required_reason or "a previous GRBL command timed out"
                raise GrblLaserError(f"Reconnect the laser controller before continuing; {reason}")
            serial_obj = self._require_serial()
            if self._raw_emergency_sent:
                self._raw_emergency_sent = False
                self._drain_input()

            result = StreamResult()
            pending: deque[StreamLine] = deque()
            pending_cost = 0
            send_index = 0
            hold_active = False
            gate_hold_started = 0.0
            last_status_poll = 0.0
            ack_deadline: float | None = None
            original_timeout = getattr(serial_obj, "timeout", None)
            rx_buffer = bytearray()

            def _read_reply_lines() -> list[str]:
                # The short serial timeout acts as an inter-byte timeout, so a
                # single readline() can return a fragment of 'ok' and desync
                # the ack ledger; assemble complete lines ourselves instead.
                if b"\n" not in rx_buffer:
                    try:
                        waiting = int(getattr(serial_obj, "in_waiting", 0) or 0)
                        chunk = serial_obj.read(max(1, waiting))
                    except OSError as exc:
                        self._handle_port_failure_locked(exc, "reading stream replies")
                    if chunk:
                        rx_buffer.extend(chunk)
                replies: list[str] = []
                while b"\n" in rx_buffer:
                    raw, _, rest = bytes(rx_buffer).partition(b"\n")
                    rx_buffer[:] = rest
                    text = raw.decode("ascii", errors="replace").strip()
                    if text:
                        replies.append(text)
                return replies

            try:
                # Short readline slices keep the gate/abort checks responsive;
                # restored in finally.
                try:
                    serial_obj.timeout = max(0.005, min(0.02, float(status_interval_s)))
                except Exception:
                    logger.debug("Could not shorten serial timeout for streaming.", exc_info=True)

                while send_index < len(lines) or pending:
                    now = time.monotonic()
                    if should_abort is not None:
                        abort_reason = should_abort()
                        if abort_reason:
                            result.aborted_reason = str(abort_reason)
                            self._abort_stream_locked(str(abort_reason))

                    if now - last_status_poll >= float(status_interval_s):
                        try:
                            serial_obj.write(b"?")
                            serial_obj.flush()
                        except OSError as exc:
                            self._handle_port_failure_locked(exc, "polling status during stream")
                        last_status_poll = now

                    while send_index < len(lines):
                        line = lines[send_index]
                        cost = len(line.text) + 1
                        # Gate before budget: a budget-blocked iteration must
                        # still observe an open gate and clear hold_active, or
                        # a later re-closed gate would compare against a stale
                        # gate_hold_started and time out instantly.
                        if line.is_fire and fire_gate is not None:
                            if not fire_gate(line.fire_index):
                                if not hold_active:
                                    hold_active = True
                                    result.hold_events += 1
                                    gate_hold_started = now
                                elif should_abort is None and now - gate_hold_started > float(ack_timeout_s):
                                    # Without a should_abort callback nothing
                                    # else can break a never-opening gate;
                                    # mirror the simulated controller's timeout
                                    # instead of spinning forever.
                                    result.aborted_reason = "fire gate never opened"
                                    self._abort_stream_locked(result.aborted_reason)
                                break
                            hold_active = False
                        if pending_cost + cost > int(rx_budget):
                            break
                        try:
                            serial_obj.write((line.text + "\n").encode("ascii"))
                            serial_obj.flush()
                        except OSError as exc:
                            self._handle_port_failure_locked(exc, f"streaming '{line.text}'")
                        self.command_log.append(line.text)
                        if line.is_fire:
                            result.fire_strokes_sent += 1
                        pending.append(line)
                        pending_cost += cost
                        if ack_deadline is None:
                            ack_deadline = time.monotonic() + float(ack_timeout_s)
                        send_index += 1

                    for text in _read_reply_lines():
                        lower = text.lower()
                        if lower == "ok":
                            if pending:
                                acked = pending.popleft()
                                pending_cost -= len(acked.text) + 1
                                result.lines_acked += 1
                                if acked.is_fire:
                                    result.fire_strokes_acked += 1
                                    result.fire_ack_monotonic.append(time.perf_counter())
                                ack_deadline = time.monotonic() + float(ack_timeout_s) if pending else None
                        elif lower.startswith("alarm"):
                            self._raise_alarm_response("<stream>", text)
                        elif lower.startswith("error"):
                            offending = pending[0].text if pending else "<stream>"
                            result.aborted_reason = f"GRBL rejected '{offending}': {text}"
                            self._abort_stream_locked(result.aborted_reason)
                        elif text.startswith("<"):
                            status = self._remember_status_line(text)
                            if on_progress is not None:
                                on_progress(status)

                    if pending and ack_deadline is not None and time.monotonic() > ack_deadline:
                        reason = (
                            f"Timed out waiting for GRBL ack while streaming (unacknowledged: '{pending[0].text}')."
                        )
                        try:
                            self._write_raw_m5_locked()
                        except Exception:
                            logger.debug("Failed to send raw M5 after stream ack timeout.", exc_info=True)
                        self._mark_reconnect_required(reason)
                        raise GrblLaserError(
                            f"{reason} Reconnect the laser controller before more motion or firing."
                        )

                result.completed = True
                return result
            except GrblStreamAborted as exc:
                # Partial counts (sent/acked strokes so far) let the caller
                # reconcile frames against strokes after an abort.
                exc.result = result
                raise
            except GrblLaserError as exc:
                # Ack-timeout / alarm / port-failure paths already put the
                # controller in a safe, latched state; still expose the
                # partial counts so the caller can mark sent strokes missing.
                exc.result = result
                raise
            except Exception as exc:
                # A callback (fire_gate/should_abort/on_progress) or internal
                # bug must not exit with queued motion still executing and M3
                # modal: run the standard abort so the planner is cleared and
                # the beam is deterministically off, then surface the original
                # error. Abort failures latch reconnect_required themselves.
                result.aborted_reason = f"internal stream failure: {exc}"
                try:
                    self._abort_stream_locked(result.aborted_reason)
                except GrblStreamAborted:
                    pass
                except Exception:
                    logger.exception("Stream abort cleanup failed after an internal stream failure.")
                exc.result = result  # type: ignore[attr-defined]
                raise
            finally:
                try:
                    serial_obj.timeout = original_timeout
                except Exception:
                    logger.debug("Could not restore serial timeout after streaming.", exc_info=True)

    def _abort_stream_locked(self, reason: str) -> NoReturn:
        """Feed hold, wait for the hold to complete, soft reset, confirm status.

        The standard GRBL streaming abort: motion decelerates on '!', the
        Ctrl-X reset clears the planner while retaining position, and the
        status probe proves the controller is back at a responsive Idle. Any
        anomaly in this sequence latches ``reconnect_required``.
        """
        serial_obj = self._require_serial()
        try:
            self.command_log.append("!")
            try:
                serial_obj.write(b"!")
                serial_obj.flush()
            except OSError as exc:
                self._handle_port_failure_locked(exc, "sending feed hold during stream abort")
            deadline = time.monotonic() + 2.0
            hold_confirmed = False
            hold_buffer = bytearray()
            while time.monotonic() < deadline and not hold_confirmed:
                try:
                    serial_obj.write(b"?")
                    serial_obj.flush()
                    waiting = int(getattr(serial_obj, "in_waiting", 0) or 0)
                    chunk = serial_obj.read(max(1, waiting))
                except OSError as exc:
                    self._handle_port_failure_locked(exc, "confirming feed hold during stream abort")
                if chunk:
                    hold_buffer.extend(chunk)
                while b"\n" in hold_buffer:
                    raw, _, rest = bytes(hold_buffer).partition(b"\n")
                    hold_buffer[:] = rest
                    text = raw.decode("ascii", errors="replace").strip()
                    if not text.startswith("<"):
                        continue
                    state = (self._remember_status_line(text).state or "").lower()
                    if state.startswith("hold:0") or state.startswith("idle"):
                        hold_confirmed = True
                        break
            if not hold_confirmed:
                # Soft-resetting mid-deceleration can lose steps; the position
                # can no longer be trusted, so force a reconnect (which homes).
                self._soft_reset_and_poll_locked()
                raise GrblLaserError(
                    "Feed hold was not confirmed before the stream-abort soft reset; "
                    "machine position is untrusted."
                )
            status = self._soft_reset_and_poll_locked()
            state = (status.state or "").lower()
            # 'alarm' is expected here: with homing enabled, GRBL boots into
            # Alarm after any soft reset. Position is retained; the next real
            # motion command surfaces the unlock requirement loudly.
            if not state.startswith(("idle", "alarm")):
                raise GrblLaserError(f"Controller state after stream abort is {status.state or 'unknown'}.")
        except GrblStreamAborted:
            raise
        except GrblLaserError:
            self._mark_reconnect_required(f"Stream abort recovery failed: {reason}")
            raise
        except Exception as exc:
            self._mark_reconnect_required(f"Stream abort recovery failed: {exc}")
            raise GrblLaserError(f"Stream abort recovery failed: {exc}") from exc
        raise GrblStreamAborted(reason)

    def emergency_laser_off(self) -> None:
        """Compatibility wrapper for serialized, best-effort laser-off cleanup."""
        if self._serial is None:
            return
        try:
            self.confirm_laser_off_and_status()
        except Exception:
            logger.exception("Failed to confirm emergency GRBL laser-off state.")

    def fire_pulse(
        self,
        *,
        s_value: int,
        pulse_ms: float,
        should_stop: Callable[[], bool] | None = None,
        poll_interval_s: float = 0.005,
    ) -> None:
        dwell_s = max(0.0, float(pulse_ms) / 1000.0)
        pulse_error = None
        resync_succeeded = False
        try:
            self.send_command(f"M3 S{int(s_value)}", timeout_s=max(self.timeout_s, _FIRE_ENABLE_TIMEOUT_S))
            deadline = time.monotonic() + dwell_s
            interval = max(0.001, min(0.05, float(poll_interval_s)))
            while time.monotonic() < deadline:
                if callable(should_stop) and should_stop():
                    raise GrblLaserError("Laser pulse interrupted before requested duration completed.")
                time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        except Exception as exc:
            pulse_error = exc
        finally:
            try:
                self.laser_off()
            except Exception as exc:
                try:
                    _status, resync_used = self.confirm_laser_off_and_status()
                except Exception as resync_exc:
                    self._mark_reconnect_required(f"Failed to confirm GRBL M5 after pulse: {exc}")
                    if pulse_error is None:
                        raise GrblLaserError(
                            "Failed to confirm laser-off after pulse. Reconnect the laser controller before continuing."
                        ) from resync_exc
                    logger.debug(
                        "Failed to confirm GRBL M5 after pulse error and resync failed: %s",
                        resync_exc,
                        exc_info=True,
                    )
                else:
                    resync_succeeded = bool(resync_used)
                    if pulse_error is None:
                        raise GrblLaserError(
                            "Failed to confirm laser-off after pulse, but the GRBL command stream was resynchronized."
                        ) from exc
                    logger.debug("Failed to confirm GRBL M5 after pulse error; command stream resynchronized.")

        if pulse_error is not None:
            if resync_succeeded and not self.reconnect_required:
                detail = str(pulse_error).replace(
                    " Reconnect the laser controller before more motion or firing.",
                    "",
                )
                raise GrblLaserError(
                    f"GRBL firing command failed: {detail} Laser-off was sent and the GRBL command stream "
                    "was resynchronized; check controller status before another pulse."
                ) from pulse_error
            raise pulse_error

    def fire_motion_pulse(
        self,
        *,
        end_x_mm: float,
        end_y_mm: float,
        s_value: int,
        pulse_ms: float,
        feed_mm_min: float,
        should_stop: Callable[[], bool] | None = None,
        poll_interval_s: float = 0.005,
    ) -> None:
        """Fire by queuing a powered XY stroke, for motion-gated laser outputs."""
        self.ensure_work_position()
        pulse_error = None
        m5_queued = False
        resync_succeeded = False
        try:
            self.send_command("G90")
            self.send_command(f"M3 S{int(s_value)}", timeout_s=max(self.timeout_s, _FIRE_ENABLE_TIMEOUT_S))
            move = (
                f"G1 X{float(end_x_mm):.3f} Y{float(end_y_mm):.3f} "
                f"S{int(s_value)} F{float(feed_mm_min):.1f}"
            )
            self.send_command(move, timeout_s=max(self.timeout_s, float(pulse_ms) / 1000.0 + 1.0))
            self.send_command("M5")
            m5_queued = True
            self.last_status = GrblStatus(
                raw="",
                state="Run",
                x=float(end_x_mm),
                y=float(end_y_mm),
                z=self.last_status.work_z,
                coordinate_kind="WPos",
                work_x=float(end_x_mm),
                work_y=float(end_y_mm),
                work_z=self.last_status.work_z,
            )
            deadline = time.monotonic() + max(0.0, float(pulse_ms) / 1000.0)
            interval = max(0.001, min(0.05, float(poll_interval_s)))
            while time.monotonic() < deadline:
                if callable(should_stop) and should_stop():
                    raise GrblLaserError("Motion-coupled laser pulse interrupted before requested stroke completed.")
                time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            self.wait_until_idle(
                timeout_s=max(self.timeout_s, max(0.0, float(pulse_ms) / 1000.0) + 5.0),
                poll_interval_s=max(0.01, interval),
            )
        except Exception as exc:
            pulse_error = exc
            if m5_queued:
                self.emergency_laser_off()
        finally:
            if not m5_queued:
                try:
                    self.laser_off()
                except Exception as exc:
                    try:
                        _status, resync_used = self.confirm_laser_off_and_status()
                    except Exception as resync_exc:
                        self._mark_reconnect_required(f"Failed to confirm GRBL M5 after motion-coupled pulse: {exc}")
                        if pulse_error is None:
                            raise GrblLaserError(
                                "Failed to confirm laser-off after motion-coupled pulse. "
                                "Reconnect the laser controller before continuing."
                            ) from resync_exc
                        logger.debug(
                            "Failed to confirm GRBL M5 after motion-coupled pulse error and resync failed: %s",
                            resync_exc,
                            exc_info=True,
                        )
                    else:
                        resync_succeeded = bool(resync_used)
                        if pulse_error is None:
                            raise GrblLaserError(
                                "Failed to confirm laser-off after motion-coupled pulse, but the GRBL command stream "
                                "was resynchronized."
                            ) from exc
                        logger.debug(
                            "Failed to confirm GRBL M5 after motion-coupled pulse error; command stream resynchronized."
                        )

        if pulse_error is not None:
            if resync_succeeded and not self.reconnect_required:
                detail = str(pulse_error).replace(
                    " Reconnect the laser controller before more motion or firing.",
                    "",
                )
                raise GrblLaserError(
                    f"GRBL motion-coupled firing failed: {detail} Laser-off was sent and the GRBL command stream "
                    "was resynchronized; check controller status before another pulse."
                ) from pulse_error
            raise pulse_error


class SimulatedGrblLaserController:
    """In-process GRBL-like controller for development and tests."""

    def __init__(self, port: str = "SIMULATED-GRBL", *, baudrate: int = 115200):
        self.port = port
        self.baudrate = int(baudrate)
        self.command_log: list[str] = []
        self.last_status = parse_status_line("<Idle|WPos:0.000,0.000,0.000>")
        self.cached_settings: dict[str, float] = {"30": 1000.0, "32": 1.0, "130": 300.0, "131": 200.0}
        self.reconnect_required = False
        self.reconnect_required_reason = ""
        self.connection_lost = False
        self.connection_lost_reason = ""
        self._connected = False
        self.pulse_count = 0
        # Streaming simulation: 0.0 executes instantly (tests), 1.0 models
        # real per-line motion durations. on_fire_stroke lets a simulated
        # spectrometer synthesize one frame per stroke.
        self.stream_time_scale = 0.0
        self.on_fire_stroke: Callable[[int | None], None] | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_simulated(self) -> bool:
        """True: no beam, no serial port, nothing to crash into."""
        return True

    @property
    def position(self) -> tuple[float | None, float | None, float | None]:
        return self.last_status.work_position

    def connect(self) -> str:
        self._connected = True
        return "Connected: simulated GRBL"

    def disconnect(self) -> None:
        if self._connected:
            self.emergency_laser_off()
        self._connected = False

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise GrblLaserError("Simulated laser is not connected.")

    def send_command(self, command: str, *, timeout_s: float | None = None) -> list[str]:
        self._ensure_connected()
        clean = command.strip()
        self.command_log.append(clean)
        upper = clean.upper()
        if upper.startswith("G0") or upper.startswith("G1"):
            self._update_position_from_command(clean)
        elif upper.startswith("$J="):
            self._update_jog_from_command(clean)
        elif upper == "$$":
            return [f"${key}={value:g}" for key, value in sorted(self.cached_settings.items())] + ["ok"]
        elif upper == "$H":
            self.last_status = parse_status_line("<Idle|WPos:0.000,0.000,0.000>")
        elif upper.startswith("M3"):
            self.pulse_count += 1
        return ["ok"]

    def stream_commands(self, commands: Iterable[str], *, timeout_s: float | None = None) -> list[list[str]]:
        return [self.send_command(command, timeout_s=timeout_s) for command in commands]

    def _update_position_from_command(self, command: str) -> None:
        x = self.last_status.work_x or 0.0
        y = self.last_status.work_y or 0.0
        for part in command.split():
            if part.startswith("X"):
                x = float(part[1:])
            elif part.startswith("Y"):
                y = float(part[1:])
        self.last_status = parse_status_line(f"<Idle|WPos:{x:.3f},{y:.3f},0.000>")

    def _update_jog_from_command(self, command: str) -> None:
        dx = 0.0
        dy = 0.0
        for part in command.replace("$J=", "").split():
            if part.startswith("X"):
                dx = float(part[1:])
            elif part.startswith("Y"):
                dy = float(part[1:])
        x = (self.last_status.work_x or 0.0) + dx
        y = (self.last_status.work_y or 0.0) + dy
        self.last_status = parse_status_line(f"<Idle|WPos:{x:.3f},{y:.3f},0.000>")

    def poll_status(self, *, timeout_s: float = 1.0) -> GrblStatus:
        self._ensure_connected()
        return self.last_status

    def wait_until_idle(self, *, timeout_s: float = 60.0, poll_interval_s: float = 0.1) -> GrblStatus:
        self._ensure_connected()
        return self.last_status

    def unlock(self) -> None:
        self.send_command("$X")

    def home(self) -> None:
        self.send_command("$H")
        self.last_status = parse_status_line("<Idle|WPos:0.000,0.000,0.000>")

    def read_grbl_settings(self, keys: set[str] | None = None) -> dict[str, float]:
        self._ensure_connected()
        if keys is None:
            return dict(self.cached_settings)
        selected = {str(key) for key in keys}
        return {key: value for key, value in self.cached_settings.items() if key in selected}

    def resync_command_stream(self, *, timeout_s: float = 2.0) -> GrblStatus:
        self._ensure_connected()
        self.emergency_laser_off()
        self.reconnect_required = False
        self.reconnect_required_reason = ""
        return self.last_status

    def confirm_laser_off_and_status(self, *, timeout_s: float = 2.0) -> tuple[GrblStatus, bool]:
        self._ensure_connected()
        resync_used = bool(self.reconnect_required)
        self.send_command("M5")
        self.reconnect_required = False
        self.reconnect_required_reason = ""
        return self.last_status, resync_used

    def ensure_work_position(self, *, timeout_s: float = 2.0) -> tuple[float, float, float | None]:
        self._ensure_connected()
        if not self.last_status.work_position_available:
            raise GrblLaserError("Reliable simulated work coordinates are unavailable.")
        return float(self.last_status.work_x), float(self.last_status.work_y), self.last_status.work_z

    def move_to(self, x_mm: float, y_mm: float, *, feed_mm_min: float | None = None) -> None:
        self.ensure_work_position()
        move_code = "G1" if feed_mm_min is not None else "G0"
        command = f"{move_code} X{float(x_mm):.3f} Y{float(y_mm):.3f}"
        if feed_mm_min is not None:
            command = f"{command} F{float(feed_mm_min):.1f}"
        self.stream_commands(["G90", command])

    def jog(self, dx_mm: float = 0.0, dy_mm: float = 0.0, *, feed_mm_min: float = 1000.0) -> None:
        self.ensure_work_position()
        self.send_command(f"$J=G91 X{float(dx_mm):.3f} Y{float(dy_mm):.3f} F{float(feed_mm_min):.1f}")

    def laser_off(self) -> None:
        self.send_command("M5")

    def feed_hold(self) -> None:
        self.command_log.append("!")

    def cycle_start(self) -> None:
        self.command_log.append("~")

    def stream_program(
        self,
        lines: Sequence[StreamLine],
        *,
        fire_gate: Callable[[int | None], bool] | None = None,
        should_abort: Callable[[], str | None] | None = None,
        on_progress: Callable[[GrblStatus], None] | None = None,
        status_interval_s: float = 0.05,
        ack_timeout_s: float = 5.0,
        rx_budget: int = STREAM_RX_BUDGET,
    ) -> StreamResult:
        """Time-modeled simulation of the streamed-program path."""
        self._ensure_connected()
        if self.reconnect_required:
            reason = self.reconnect_required_reason or "a previous GRBL command timed out"
            raise GrblLaserError(f"Reconnect the laser controller before continuing; {reason}")
        result = StreamResult()

        def _check_abort() -> None:
            if should_abort is None:
                return
            reason = should_abort()
            if reason:
                self.command_log.append("!")
                self.command_log.append("\x18")
                result.aborted_reason = str(reason)
                raise GrblStreamAborted(str(reason))

        try:
            for line in lines:
                _check_abort()
                if line.is_fire and fire_gate is not None and not fire_gate(line.fire_index):
                    result.hold_events += 1
                    gate_deadline = time.monotonic() + float(ack_timeout_s)
                    while not fire_gate(line.fire_index):
                        _check_abort()
                        if time.monotonic() > gate_deadline:
                            self.reconnect_required = True
                            self.reconnect_required_reason = "Simulated stream fire gate never opened."
                            gate_exc = GrblLaserError("Simulated stream fire gate never opened.")
                            gate_exc.result = result  # type: ignore[attr-defined]
                            raise gate_exc
                        time.sleep(0.001)
                previous = self.last_status
                self.command_log.append(line.text)
                if line.is_fire:
                    result.fire_strokes_sent += 1
                upper = line.text.upper()
                if upper.startswith(("G0", "G1")):
                    self._update_position_from_command(line.text)
                elif upper.startswith("M3"):
                    self.pulse_count += 1
                if self.stream_time_scale > 0:
                    duration = self._simulated_line_duration_s(line.text, previous)
                    if duration > 0:
                        time.sleep(duration * float(self.stream_time_scale))
                result.lines_acked += 1
                if line.is_fire:
                    result.fire_strokes_acked += 1
                    result.fire_ack_monotonic.append(time.perf_counter())
                    if callable(self.on_fire_stroke):
                        self.on_fire_stroke(line.fire_index)
                if on_progress is not None:
                    on_progress(self.last_status)
        except GrblStreamAborted as exc:
            exc.result = result
            raise
        except Exception as exc:
            # Parity with the real controller: every stream exception carries
            # the partial counts so callers can reconcile strokes vs frames.
            self.command_log.append("!")
            self.command_log.append("\x18")
            exc.result = result  # type: ignore[attr-defined]
            raise
        result.completed = True
        return result

    def _simulated_line_duration_s(self, command: str, previous: GrblStatus) -> float:
        upper = command.upper()
        if not upper.startswith(("G0", "G1")):
            return 0.0
        x = previous.work_x or 0.0
        y = previous.work_y or 0.0
        feed_mm_min = 1200.0
        target_x, target_y = x, y
        for part in command.split():
            if part.startswith("X"):
                target_x = float(part[1:])
            elif part.startswith("Y"):
                target_y = float(part[1:])
            elif part.startswith("F"):
                try:
                    feed_mm_min = max(1.0, float(part[1:]))
                except ValueError:
                    pass
        distance = ((target_x - x) ** 2 + (target_y - y) ** 2) ** 0.5
        return distance / (feed_mm_min / 60.0)

    def emergency_laser_off(self) -> None:
        if self._connected:
            self.confirm_laser_off_and_status()

    def fire_pulse(
        self,
        *,
        s_value: int,
        pulse_ms: float,
        should_stop: Callable[[], bool] | None = None,
        poll_interval_s: float = 0.005,
    ) -> None:
        self.send_command(f"M3 S{int(s_value)}")
        deadline = time.monotonic() + max(0.0, float(pulse_ms) / 1000.0)
        interval = max(0.001, min(0.05, float(poll_interval_s)))
        while time.monotonic() < deadline:
            if callable(should_stop) and should_stop():
                self.laser_off()
                raise GrblLaserError("Laser pulse interrupted before requested duration completed.")
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        self.laser_off()

    def fire_motion_pulse(
        self,
        *,
        end_x_mm: float,
        end_y_mm: float,
        s_value: int,
        pulse_ms: float,
        feed_mm_min: float,
        should_stop: Callable[[], bool] | None = None,
        poll_interval_s: float = 0.005,
    ) -> None:
        self.ensure_work_position()
        self.send_command("G90")
        self.send_command(f"M3 S{int(s_value)}")
        self.send_command(
            f"G1 X{float(end_x_mm):.3f} Y{float(end_y_mm):.3f} "
            f"S{int(s_value)} F{float(feed_mm_min):.1f}"
        )
        deadline = time.monotonic() + max(0.0, float(pulse_ms) / 1000.0)
        interval = max(0.001, min(0.05, float(poll_interval_s)))
        while time.monotonic() < deadline:
            if callable(should_stop) and should_stop():
                self.laser_off()
                raise GrblLaserError("Motion-coupled laser pulse interrupted before requested stroke completed.")
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        self.laser_off()
        self.wait_until_idle(
            timeout_s=max(1.0, max(0.0, float(pulse_ms) / 1000.0) + 1.0),
            poll_interval_s=max(0.01, interval),
        )
