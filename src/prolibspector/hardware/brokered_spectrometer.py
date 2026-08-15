"""Acquisition module that proxies all hardware I/O to a subprocess.

Some vendor SDKs cannot be recovered in-process. When a blocking read is
cancelled — a trigger that never arrives, a USB transfer abandoned mid-flight —
the SDK can leave the device claimed and every later call wedged, with no API
to unwind it. Nothing inside this interpreter can fix that, because the broken
state lives in the loaded DLL.

Running the SDK in a separate process makes it recoverable: the wedge is
contained, and the last resort (terminate and reopen) is available without
taking the application down with it. This client is the near side of that
split. It is vendor-agnostic by construction — the far side is supplied as the
``broker_target`` callable, which owns the SDK and answers a small command
protocol over two queues.

Recovery is deliberately graded rather than trigger-happy:

* ``cancel_pending_read()`` first waits out the grace period. If the read
  returns, the broker is kept and put back into normal trigger mode.
* If it does not, the default is to mark this client disconnected and *leave
  the subprocess alive*. Killing a process while it sits inside vendor SDK code
  can leave the USB device in a worse state than the wedge did.
* ``force_terminate=True`` is the explicit last resort: terminate, then
  reconnect with backoff and restore the integration time and trigger mode the
  caller had configured.
* ``release_orphaned_process()`` reaps a broker left alive by the middle case,
  once the caller has decided it is not coming back.

The client is an acquisition-tier module like
:class:`~prolibspector.hardware.spectrometer.SimulatedBackedModule`: it reports
``ModuleCapabilities`` and is duck-typed by the acquisition layer rather than
implementing the low-level ``SpectrometerBase`` ABC.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import queue
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

import numpy as np

from prolibspector.hardware.spectrometer import (
    DEFAULT_INTEGRATION_TIME_US,
    ModuleCapabilities,
    NoDeviceError,
    SpectrometerError,
    trigger_mode_fields,
)

logger = logging.getLogger(__name__)

RESPONSE_OK = "ok"
RESPONSE_ERROR = "error"
RESPONSE_SPECTRUM = "spectrum"
RESPONSE_CAPABILITIES = "capabilities"
RESPONSE_CANCELLED = "cancelled"

_SUCCESS_STATUSES = frozenset({
    RESPONSE_OK,
    RESPONSE_SPECTRUM,
    RESPONSE_CAPABILITIES,
    RESPONSE_CANCELLED,
})

#: Capability fields carried over the queue. ``normal_trigger_mode`` and
#: ``external_trigger_mode`` are deliberately absent: they are rebuilt from
#: ``trigger_modes`` on arrival so the two can never disagree across the wire.
_CAPABILITY_FIELDS = (
    "model",
    "serial_number",
    "brand",
    "pixel_count",
    "wavelength_min",
    "wavelength_max",
    "max_intensity",
    "integration_time_min_us",
    "integration_time_max_us",
    "trigger_modes",
    "supports_dark_correction",
    "supports_nonlinearity_correction",
    "supports_trigger_delay",
    "trigger_delay_min_us",
    "trigger_delay_max_us",
)


def capabilities_to_mapping(capabilities: ModuleCapabilities) -> dict[str, Any]:
    """Return a queue-safe mapping for a capability snapshot."""
    payload = {}
    for field in _CAPABILITY_FIELDS:
        value = getattr(capabilities, field)
        payload[field] = dict(value) if field == "trigger_modes" else value
    return payload


def capabilities_from_mapping(payload: dict[str, Any] | None) -> ModuleCapabilities:
    """Rehydrate capabilities from a broker response mapping."""
    capabilities = ModuleCapabilities()
    if not payload:
        return capabilities

    for field in _CAPABILITY_FIELDS:
        if field == "trigger_modes" or field not in payload:
            continue
        setattr(capabilities, field, payload[field])

    for field, value in trigger_mode_fields(payload.get("trigger_modes")).items():
        setattr(capabilities, field, value)
    return capabilities


def response(request_id: int, status: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a well-formed broker response. Broker implementations use this."""
    return {"id": request_id, "status": status, "payload": payload or {}}


def error_response(request_id: int, exc: BaseException) -> dict[str, Any]:
    """Build an error response that survives the process boundary.

    The exception type travels as a name rather than a pickled instance, so a
    broker can raise an SDK-specific error without the client needing to import
    the SDK to unpickle it.
    """
    return {
        "id": request_id,
        "status": RESPONSE_ERROR,
        "error_type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }


class BrokeredSpectrometerClient:
    """Acquisition module whose device work happens in a broker subprocess."""

    def __init__(
        self,
        *,
        broker_target: Callable[..., None],
        broker_args: tuple[Any, ...] = (),
        connect_timeout_s: float = 20.0,
        command_timeout_s: float = 10.0,
        cancel_reconnect_delay_s: float = 0.75,
        cancel_reconnect_attempts: int = 3,
    ):
        # "spawn" rather than "fork": the broker must start from a clean
        # interpreter, or it inherits a copy of an already-wedged SDK.
        self._ctx = mp.get_context("spawn")
        self._broker_target = broker_target
        self._broker_args = tuple(broker_args)
        self._connect_timeout_s = float(connect_timeout_s)
        self._command_timeout_s = float(command_timeout_s)
        self._cancel_reconnect_delay_s = max(0.0, float(cancel_reconnect_delay_s))
        self._cancel_reconnect_attempts = max(1, int(cancel_reconnect_attempts))

        self._state_lock = threading.RLock()
        # Serializes commands, and doubles as the "is a read in flight?" signal
        # that cancel_pending_read() waits on.
        self._command_lock = threading.Lock()
        self._process = None
        self._request_queue = None
        self._response_queue = None
        self._next_request_id = 0

        self._is_connected = False
        self._device_index = 0
        self._capabilities = ModuleCapabilities()
        self._integration_time_us = DEFAULT_INTEGRATION_TIME_US
        self._current_trigger_mode = 0
        self._trigger_delay_us = 0.0
        self._model = "N/A"
        self._serial_number = "N/A"
        self._wavelengths: np.ndarray | None = None
        self._device_info: dict[str, Any] = {}
        self._dll_path: str | None = None
        self._dll_version: str = ""

    # ── Identity and capabilities ────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        with self._state_lock:
            return bool(
                self._is_connected
                and self._process is not None
                and self._process.is_alive()
            )

    @property
    def capabilities(self) -> ModuleCapabilities:
        return self._capabilities

    @property
    def integration_time_us(self) -> int:
        return self._integration_time_us

    @property
    def current_trigger_mode(self) -> int:
        return self._current_trigger_mode

    @property
    def trigger_delay_us(self) -> float:
        return self._trigger_delay_us

    @property
    def model(self) -> str:
        return self._model

    @property
    def serial_number(self) -> str:
        return self._serial_number

    @property
    def device_info(self) -> dict[str, Any]:
        """Backend-reported identity and optics strings, when the broker sends them."""
        return dict(self._device_info)

    @property
    def dll_path(self) -> str | None:
        return self._dll_path

    @property
    def dll_version(self) -> str:
        return self._dll_version

    @property
    def broker_pid(self) -> int | None:
        """Broker process id, for diagnostics and for tests that assert restarts."""
        with self._state_lock:
            return None if self._process is None else self._process.pid

    # ── Connection ───────────────────────────────────────────────────────

    def connect(self, device_index: int = 0) -> str:
        self._device_index = int(device_index)
        self._ensure_broker_running()
        try:
            payload = self._round_trip(
                "connect",
                {"device_index": self._device_index},
                timeout_s=self._connect_timeout_s,
            )
            self._apply_connection_payload(payload)
            return payload.get("status") or f"Connected: {self._model} (S/N: {self._serial_number})"
        except Exception:
            # A half-open broker is worse than none: tear it down so the next
            # attempt starts from a clean interpreter.
            self._terminate_broker()
            self._reset_connection_state()
            raise

    def connect_simulated(self, profile_name: str = "Generic") -> str:
        """Refuse: the broker exists to isolate a real SDK, and there is none to isolate."""
        raise SpectrometerError(
            "The brokered spectrometer client only drives real hardware. Use a "
            "simulated module for hardware-free work."
        )

    def disconnect(self) -> None:
        try:
            if self._process_alive():
                # Ask nicely first so the SDK can close the device itself;
                # both steps are best-effort because the broker may be wedged.
                for command, timeout_s in (("disconnect", 2.0), ("shutdown", 1.0)):
                    try:
                        self._round_trip(command, timeout_s=timeout_s)
                    except Exception as exc:
                        logger.debug("Broker %s command failed: %s", command, exc)
        finally:
            self._terminate_broker()
            self._reset_connection_state()

    # ── Configuration ────────────────────────────────────────────────────

    def set_integration_time(self, microseconds: int) -> None:
        self._require_connected()
        payload = self._round_trip(
            "set_integration_time",
            {"microseconds": int(microseconds)},
            timeout_s=self._command_timeout_s,
        )
        self._integration_time_us = int(payload.get("integration_time_us", microseconds))
        if "current_trigger_mode" in payload:
            self._current_trigger_mode = int(payload["current_trigger_mode"])

    def set_trigger_mode(self, mode: int) -> None:
        self._require_connected()
        valid_modes = set(self._capabilities.trigger_modes.values())
        if int(mode) not in valid_modes:
            raise SpectrometerError(
                f"Invalid trigger mode {mode} for {self.model}. "
                f"Supported: {self._capabilities.trigger_modes}"
            )
        payload = self._round_trip(
            "set_trigger_mode",
            {"mode": int(mode)},
            timeout_s=self._command_timeout_s,
        )
        self._current_trigger_mode = int(payload.get("current_trigger_mode", mode))

    def set_trigger_delay(self, microseconds: float) -> None:
        self._require_connected()
        if not self._capabilities.supports_trigger_delay:
            raise SpectrometerError(
                f"{self.model} does not support a programmable trigger delay."
            )
        payload = self._round_trip(
            "set_trigger_delay",
            {"microseconds": float(microseconds)},
            timeout_s=self._command_timeout_s,
        )
        self._trigger_delay_us = float(payload.get("trigger_delay_us", microseconds))

    def stop_acquisition(self) -> bool:
        """Ask the broker to stop the current acquisition.

        The broker handles commands sequentially, so this cannot interrupt a
        read that is already blocking its loop — it is only useful between
        commands. Mid-read recovery goes through ``cancel_pending_read``.
        """
        self._require_connected()
        payload = self._round_trip("stop_acquisition", timeout_s=self._command_timeout_s)
        return bool(payload.get("stopped", False))

    # ── Acquisition ──────────────────────────────────────────────────────

    def get_wavelengths(self) -> np.ndarray:
        self._require_connected()
        if self._wavelengths is not None:
            return self._wavelengths.copy()

        payload = self._round_trip("get_wavelengths", timeout_s=self._command_timeout_s)
        self._wavelengths = np.asarray(payload["wavelengths"], dtype=float)
        return self._wavelengths.copy()

    def get_intensities(
        self,
        correct_dark_counts: bool = False,
        correct_nonlinearity: bool = False,
    ) -> np.ndarray:
        # No timeout: an externally triggered read legitimately blocks until
        # the trigger arrives. Unblocking it is cancel_pending_read()'s job.
        self._require_connected()
        payload = self._round_trip(
            "get_intensities",
            {
                "correct_dark_counts": bool(correct_dark_counts),
                "correct_nonlinearity": bool(correct_nonlinearity),
            },
            timeout_s=None,
        )
        return np.asarray(payload["intensities"], dtype=float)

    def get_spectrum(self) -> tuple[np.ndarray, np.ndarray]:
        return self.get_wavelengths(), self.get_intensities()

    def drain_buffered_frames(self, max_frames: int = 8, time_budget_s: float = 2.5) -> int:
        """Discard stale frames from the broker-side trigger FIFO.

        Spurious trigger edges leave unread frames in the SDK FIFO, and every
        later read then returns a frame one shot staler — which smears a
        mapping grid sideways by one position. Only call this while no capture
        is armed and no shot is in flight.
        """
        self._require_connected()
        payload = self._round_trip(
            "drain_buffered_frames",
            {"max_frames": int(max_frames), "time_budget_s": float(time_budget_s)},
            timeout_s=max(self._command_timeout_s, float(time_budget_s) + 5.0),
        )
        return int(payload.get("drained", 0))

    # ── Recovery ─────────────────────────────────────────────────────────

    def cancel_pending_read(self, timeout_s: float = 1.0, *, force_terminate: bool = False) -> bool:
        """Recover from a read that is blocking the broker. See the module docstring."""
        timeout_s = max(0.0, float(timeout_s))

        # Acquiring the command lock means the read finished on its own.
        if self._command_lock.acquire(timeout=timeout_s):
            self._command_lock.release()
            if not self.is_connected:
                return False
            self.set_trigger_mode(self._capabilities.normal_trigger_mode)
            return True

        device_index = self._device_index
        integration_time_us = self._integration_time_us
        normal_mode = self._capabilities.normal_trigger_mode

        if not force_terminate:
            logger.warning(
                "Spectrometer broker is blocked in a read; leaving the process alive "
                "and marking the client disconnected."
            )
            with self._state_lock:
                self._is_connected = False
            return False

        logger.warning("Terminating the spectrometer broker to cancel a blocked read.")
        self._terminate_broker()
        self._reset_connection_state()

        last_error: Exception | None = None
        for attempt in range(1, self._cancel_reconnect_attempts + 1):
            # Linear backoff: the USB stack needs a moment to release the
            # interface after the process holding it dies.
            delay_s = self._cancel_reconnect_delay_s * attempt
            if delay_s > 0:
                time.sleep(delay_s)

            try:
                self._ensure_broker_running()
                payload = self._round_trip(
                    "connect",
                    {"device_index": device_index},
                    timeout_s=self._connect_timeout_s,
                )
                self._apply_connection_payload(payload)
                self.set_integration_time(integration_time_us)
                self.set_trigger_mode(normal_mode)
                return True
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Spectrometer broker restart attempt %s/%s failed: %s",
                    attempt, self._cancel_reconnect_attempts, exc,
                )
                self._terminate_broker()
                self._reset_connection_state()

        if last_error is not None:
            logger.error("Spectrometer broker restart failed: %s", last_error)
            self._terminate_broker()
            self._reset_connection_state()
        return False

    def release_orphaned_process(self) -> bool:
        """Reap a broker left alive by a cooperative cancel that did not return.

        Refuses to act while the client is connected, so this can be called
        unconditionally on the recovery path without risking a live device.
        """
        with self._state_lock:
            orphaned = (
                not self._is_connected
                and self._process is not None
                and self._process.is_alive()
            )
        if not orphaned:
            return False

        logger.warning("Releasing an orphaned spectrometer broker process.")
        self._terminate_broker()
        self._reset_connection_state()
        return True

    # ── Internals ────────────────────────────────────────────────────────

    def _require_connected(self) -> None:
        if not self.is_connected:
            raise SpectrometerError("Spectrometer not connected.")

    def _apply_connection_payload(self, payload: dict[str, Any]) -> None:
        capabilities = capabilities_from_mapping(payload.get("capabilities"))
        self._capabilities = capabilities
        self._integration_time_us = int(
            payload.get("integration_time_us", self._integration_time_us)
        )
        self._current_trigger_mode = int(
            payload.get("current_trigger_mode", capabilities.normal_trigger_mode)
        )
        self._trigger_delay_us = float(payload.get("trigger_delay_us", 0.0))
        self._model = capabilities.model
        self._serial_number = capabilities.serial_number
        wavelengths = payload.get("wavelengths")
        self._wavelengths = None if wavelengths is None else np.asarray(wavelengths, dtype=float)
        self._device_info = dict(payload.get("device_info") or {})
        self._dll_path = payload.get("dll_path")
        self._dll_version = str(payload.get("dll_version") or "")
        self._is_connected = True

    def _reset_connection_state(self) -> None:
        self._is_connected = False
        self._capabilities = ModuleCapabilities()
        self._current_trigger_mode = 0
        self._trigger_delay_us = 0.0
        self._model = "N/A"
        self._serial_number = "N/A"
        self._wavelengths = None
        self._device_info = {}
        self._dll_path = None
        self._dll_version = ""

    def _next_id(self) -> int:
        with self._state_lock:
            self._next_request_id += 1
            return self._next_request_id

    def _ensure_broker_running(self) -> None:
        with self._state_lock:
            if self._process is not None and self._process.is_alive():
                return

            request_queue = self._ctx.Queue()
            response_queue = self._ctx.Queue()
            process = self._ctx.Process(
                target=self._broker_target,
                args=(request_queue, response_queue, *self._broker_args),
                name="SpectrometerBroker",
                daemon=True,
            )
            process.start()
            self._request_queue = request_queue
            self._response_queue = response_queue
            self._process = process

    def _process_alive(self) -> bool:
        with self._state_lock:
            return self._process is not None and self._process.is_alive()

    def _round_trip(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        if not self._command_lock.acquire(timeout=self._command_timeout_s):
            raise SpectrometerError("Spectrometer broker is busy.")

        try:
            self._ensure_broker_running()
            with self._state_lock:
                process = self._process
                request_queue = self._request_queue
                response_queue = self._response_queue

            if process is None or request_queue is None or response_queue is None:
                raise SpectrometerError("Spectrometer broker is not running.")

            request_id = self._next_id()
            request_queue.put({"id": request_id, "command": command, "payload": payload or {}})

            deadline = None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
            while True:
                try:
                    # Short poll rather than a long blocking get, so a broker
                    # that dies mid-command is noticed instead of waited on.
                    message = response_queue.get(timeout=0.05)
                except queue.Empty:
                    if process is not None and not process.is_alive():
                        raise SpectrometerError(
                            f"Spectrometer broker stopped while handling '{command}'."
                        )
                    if deadline is not None and time.monotonic() >= deadline:
                        raise SpectrometerError(
                            f"Spectrometer broker command timed out: {command}"
                        )
                    continue

                if message.get("id") != request_id:
                    # A late reply to a command that already timed out.
                    logger.debug("Ignoring stale broker response: %s", message)
                    continue

                status = message.get("status")
                if status == RESPONSE_ERROR:
                    self._raise_response_error(message)
                if status in _SUCCESS_STATUSES:
                    return dict(message.get("payload") or {})

                raise SpectrometerError(f"Unexpected broker response status: {status}")
        finally:
            self._command_lock.release()

    def _raise_response_error(self, message: dict[str, Any]) -> None:
        """Re-raise a broker-side failure, preserving the distinction the UI acts on."""
        text = str(message.get("message") or "Spectrometer broker error.")
        logger.debug("Broker traceback:\n%s", message.get("traceback", ""))
        if message.get("error_type") == "NoDeviceError":
            raise NoDeviceError(text)
        raise SpectrometerError(text)

    def _terminate_broker(self) -> None:
        with self._state_lock:
            process = self._process
            self._process = None
            self._request_queue = None
            self._response_queue = None

        if process is None:
            return

        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
        if process.is_alive():
            kill = getattr(process, "kill", None)
            if callable(kill):
                kill()
                process.join(timeout=1.0)
        # Deliberately not process.close(): a worker thread may still hold a
        # reference while unwinding the get_intensities call we just killed.


__all__ = [
    "RESPONSE_CANCELLED",
    "RESPONSE_CAPABILITIES",
    "RESPONSE_ERROR",
    "RESPONSE_OK",
    "RESPONSE_SPECTRUM",
    "BrokeredSpectrometerClient",
    "capabilities_from_mapping",
    "capabilities_to_mapping",
    "error_response",
    "response",
]
