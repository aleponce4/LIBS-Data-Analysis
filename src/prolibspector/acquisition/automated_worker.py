"""Automated acquisition worker combining GRBL motion with spectrometer capture."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import replace
from datetime import datetime
from functools import lru_cache
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import threading
import time

import numpy as np

from prolibspector.acquisition.automation import (
    AutomatedRunState,
    AutomationRunConfig,
    AutomationTarget,
    LASER_PROFILE_MONPORT_NDYAG_RELAY,
    automation_plan_details,
    automation_targets,
    effective_bed_area,
    load_motion_safety_limits,
    load_resume_state_for_config,
    plan_plate_slots,
    representative_dry_run_targets,
    relay_stroke_endpoint,
    relay_stroke_feed_mm_min,
    save_automation_run_state,
    validate_motion_safety_points,
)
from prolibspector.acquisition.automation_persistence import (
    qc_error_manifest,
    qc_success_manifest,
    qc_unavailable_manifest,
    save_worker_automation_manifest,
    save_worker_automation_summary,
    save_worker_run_metadata,
)
from prolibspector.acquisition.automation_mapping import (
    DEFAULT_MAPPING_MOVE_SPEED_MM_MIN,
    MAPPING_RUN_TYPE,
    MappingGridConfig,
    MappingGridTarget,
    MappingRunState,
    MappingStreamSegment,
    aggregate_background_frames,
    append_mapping_index_row,
    append_mapping_timing_row,
    compile_mapping_stream_segments,
    completed_mapping_targets_from_index,
    missing_mapping_targets_from_index,
    effective_mapping_move_speed_mm_min,
    mapping_background_dir,
    mapping_background_metadata_path,
    mapping_background_reference_path,
    mapping_bounds,
    mapping_grid_shape,
    mapping_grid_targets,
    mapping_column_delay_us,
    mapping_paired_setting,
    mapping_progress_payload,
    mapping_row_band_integration_time_ms,
    mapping_timing_row,
    mapping_relay_stroke_endpoint,
    mapping_relay_stroke_feed_mm_min,
    mapping_spectrum_filename,
    mapping_target_count,
    representative_mapping_dry_run_targets,
    save_mapping_manifest,
    save_mapping_run_state,
    save_mapping_summary,
)
from prolibspector.acquisition.mapping_save_writer import MappingSaveWriter
from prolibspector.acquisition.mapping_spectrum_store import (
    MappingSpectrumStore,
    binary_row_index_for_target,
)
from prolibspector.acquisition.plate_autosave import (
    PlateAutosaveConfig,
    PlateRunState,
    append_plate_timing_row,
    load_plate_run_state,
    plate_timing_row,
    records_from_plate_folder,
    save_plate_run_state,
)
from prolibspector.acquisition.spectra_readiness_qc import (
    apply_spectra_qc_to_plate_states,
    load_default_spectra_qc,
    run_automated_spectra_qc,
)
from prolibspector.acquisition.worker import AcquisitionMessage, AcquisitionWorker, AutoSaveError
from prolibspector.hardware.grbl_laser import GrblStreamAborted
from prolibspector.hardware.spectrometer import apply_simulated_trigger_delay_response


logger = logging.getLogger(__name__)


RUN_MODE_REAL = "real"
RUN_MODE_SIMULATION = "simulation"
MAPPING_STATE_WRITE_BATCH_SIZE = 100
# Plate runs are much smaller than maps, and the per-shot plate-state JSON in
# _auto_save still persists every shot (resume backfills from it), so a small
# batch keeps the automation-state JSON+manifest cost bounded without risk.
PLATE_STATE_WRITE_BATCH_SIZE = 25
MAPPING_IDLE_POLL_INTERVAL_S = 0.02
# Repeat shots at the same grid point only make a 0.2 mm return move after the
# relay stroke, so the mechanical excitation is far below a full step move.
MAPPING_BURST_SETTLE_MS = 25.0
# The armed-wait loop only decides when the worker notices the finished read;
# 5 ms keeps up with streaming-adjacent cadences without busy-waiting.
MAPPING_ARMED_POLL_INTERVAL_S = 0.005
# The burst return move must land back on the grid point before firing again.
MAPPING_BURST_POSITION_TOLERANCE_MM = 0.01
# One in-run alert once this many stale trigger frames were drained: at that
# rate the trigger line needs electrical attention, not just software cleanup.
MAPPING_TRIGGER_DRAIN_ALERT_FRAMES = 10
# A hardware run whose first shots are ALL exactly zero has no light path
# (laser not emitting / fiber disconnected) — abort instead of darkening
# the whole grid.
DARK_RUN_STARTUP_SHOT_LIMIT = 5
MAPPING_IDLE_STATUS_TIMEOUT_S = 0.05
RUN_MODE_LASER_TEST = "laser_test"
#: Resolved relative to this module, not to the repository root: the fixture
#: ships *inside* the package, unlike the assets beside ``src/`` that
#: ``resource_path`` is for. Module-relative also survives a frozen build, where
#: the ``src/`` layer is flattened away.
_SIMULATION_REFERENCE_SPECTRUM_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "automated_simulation_reference_spectrum.tsv"
)
_SIMULATION_REFERENCE_ADC_MAX = 65535.0
_DEMO_SIMULATION_QC_PROFILES = {
    (1, "B7"): "low_signal",
    (1, "F3"): "overexposed_flat",
    (1, "C10"): "unstable_baseline",
}
_GRBL_BLOCKING_STATES = ("alarm", "door", "hold")
_GRBL_PROBE_PIN = "P"
_GRBL_LIMIT_PINS = frozenset({"X", "Y", "Z"})
_GRBL_CONTROL_PINS = frozenset({"D", "H", "R", "S"})
GRBL_PREP_MOTION = "motion"
GRBL_PREP_FIRING = "firing"
STREAM_FRAME_STALL_PREFIX = "stream_frame_stall:"


class _MappingStreamReadLoop:
    """Owns frame acquisition for one streamed segment.

    Hardware mode: a dedicated thread arms external-trigger reads
    back-to-back; each real laser pulse produces one frame. Simulation mode:
    no thread — the simulated controller's per-stroke hook synthesizes the
    frame synchronously.

    ``fire_gate(k)`` is the read-armed-before-fire valve: the streamer may
    send fire stroke k only once at least k frames have arrived and (in
    hardware mode) the read thread has entered its blocking read. The active
    flag is raised just before the SDK read arms, so a few-ms window remains
    where a pulse lands early; the device-side trigger FIFO still captures
    that frame, and the segment barrier reconciles any resulting mismatch.
    """

    def __init__(self, worker, segment: MappingStreamSegment, *, simulation_mode: bool):
        self._worker = worker
        self._segment = segment
        self._simulation_mode = simulation_mode
        self.frames_expected = len(segment.strokes)
        self.frames: list[tuple[float, object]] = []
        self.read_error: Exception | None = None
        self._frames_lock = threading.Lock()
        self._frame_event = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wavelengths = None
        self._started_at = time.monotonic()
        self._last_activity = self._started_at

    @property
    def frames_received(self) -> int:
        return len(self.frames)

    @property
    def wavelengths(self):
        """Axis fetched once at start(); barrier code must use this instead of
        a fresh hardware call, which would deadlock if the read thread leaked
        holding the hardware lock."""
        return self._wavelengths

    def stall_reason(self, timeout_s: float) -> str | None:
        """A stall means the laser may be firing without matched frames.

        Distinct from a user stop: the segment is discarded (mismatch policy)
        and acquisition continues with the next segment.

        The timeout is measured from whichever is later: the last frame, or
        the compiled schedule's fire time for the next expected frame. The
        schedule term keeps long approach/return travels (which can exceed
        the capture timeout at low move speeds) from false-stalling; there is
        deliberately no early-out on frames_received >= frames_expected — a
        spurious extra frame (trigger-FIFO ghost) can satisfy that count while
        the fire gate is still waiting, and the stall timer is the only thing
        that breaks such a segment out of the stream.
        """
        if self.read_error is not None:
            return f"{STREAM_FRAME_STALL_PREFIX} read error: {self.read_error}"
        strokes = self._segment.strokes
        next_index = min(self.frames_received, len(strokes) - 1)
        if next_index <= 0:
            # First frame: due after the (possibly long) approach travel.
            next_due = self._started_at + float(strokes[0].scheduled_fire_offset_s)
        else:
            # Later frames: due one inter-stroke interval after the last real
            # frame. Anchoring to _last_activity (not the absolute schedule)
            # absorbs gate-hold drift, which delays fires but not the plan.
            delta = float(strokes[next_index].scheduled_fire_offset_s) - float(
                strokes[next_index - 1].scheduled_fire_offset_s
            )
            next_due = self._last_activity + max(0.0, delta)
        reference = max(self._last_activity, next_due)
        if time.monotonic() - reference > max(1.0, float(timeout_s)):
            return f"{STREAM_FRAME_STALL_PREFIX} no frame within {timeout_s:g}s"
        return None

    def start(self) -> None:
        self._wavelengths = self._worker._get_wavelengths()
        self._started_at = time.monotonic()
        self._last_activity = self._started_at
        if self._simulation_mode:
            return
        self._thread = threading.Thread(target=self._loop, name="MappingStreamRead", daemon=True)
        self._thread.start()

    def fire_gate(self, fire_index) -> bool:
        if self.read_error is not None:
            return False
        # `<` rather than `!=`: a spurious extra frame (frames > k) must not
        # close the gate forever — over-count resolves at the segment barrier
        # as a frame/stroke mismatch and the segment is discarded.
        if self.frames_received < int(fire_index or 0):
            return False
        if self._simulation_mode:
            return True
        return self._worker.hardware_read_active

    def notify_simulated_stroke(self, fire_index) -> None:
        target = self._segment.strokes[int(fire_index or 0)].target
        intensities = self._worker._get_intensities()
        intensities = self._worker._reference_simulation_intensities(
            self._wavelengths, intensities, target, trigger_delay_us=None, integration_us=None
        )
        self._record_frame(intensities)

    def wait_for_frames(self, *, timeout_s: float, expected: int | None = None) -> bool:
        expected = self.frames_expected if expected is None else int(expected)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while self.frames_received < expected:
            if self.read_error is not None:
                return False
            if self._simulation_mode:
                return self.frames_received >= expected
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._frame_event.wait(timeout=min(0.05, remaining))
            self._frame_event.clear()
        return True

    def shutdown(self, *, timeout_s: float = 12.0) -> bool:
        """Stop the read thread; returns True when it actually exited.

        A read blocked in the SDK gives up on its own within its internal
        retry window (< the join timeout), so the first join normally
        succeeds with the spectrometer still connected — which is what lets
        a discarded segment continue with the next one. Deliberately no
        cooperative cancel_pending_read here: against a genuinely blocked
        read it cannot acquire the broker command lock and its failure
        branch marks the client disconnected, silently killing the rest of
        the run. If the SDK read never returns (wedged hardware), the broker
        process is released to unwind it — the spectrometer then needs a
        reconnect. Returning False means the thread leaked holding the
        hardware lock; the caller must not make further hardware calls on
        this worker.
        """
        self._stop.set()
        if self._thread is None:
            return True
        self._thread.join(timeout=max(0.0, float(timeout_s)))
        if not self._thread.is_alive():
            return True
        # Wedged SDK read: a zero-grace cancel cannot reach the broker (the
        # blocked read holds its command lock) but its failure branch marks
        # the client disconnected — the precondition release_orphaned_process
        # needs before it will terminate the broker and unwind the read.
        cancel = getattr(self._worker.spec, "cancel_pending_read", None)
        if callable(cancel):
            try:
                cancel(timeout_s=0.5)
            except Exception:
                logger.debug("cancel_pending_read failed while orphaning a wedged read.", exc_info=True)
        release = getattr(self._worker.spec, "release_orphaned_process", None)
        if callable(release):
            logger.warning("Stream read did not unwind; releasing the spectrometer broker process.")
            try:
                release()
            except Exception:
                logger.exception("Failed to release orphaned spectrometer broker after stream read stall.")
        self._thread.join(timeout=5.0)
        return not self._thread.is_alive()

    def _record_frame(self, intensities) -> None:
        with self._frames_lock:
            self.frames.append((time.perf_counter(), intensities))
        self._last_activity = time.monotonic()
        self._frame_event.set()
        # Live display: SPECTRUM must immediately precede CAPTURED from one
        # thread; persistence happens later at the segment barrier.
        self._worker._send(AcquisitionMessage.SPECTRUM, (self._wavelengths, intensities))
        self._worker._send(
            AcquisitionMessage.CAPTURED,
            {
                "wavelengths": self._wavelengths,
                "intensities": intensities,
                "shot_index": self._worker._shot_index + self.frames_received,
            },
        )

    def _loop(self) -> None:
        while not self._stop.is_set() and self.frames_received < self.frames_expected:
            try:
                intensities = self._worker._get_intensities_for_trigger_read()
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.read_error = exc
                self._frame_event.set()
                return
            if self._stop.is_set():
                return
            self._record_frame(intensities)


class AutomationStopped(RuntimeError):
    """Raised internally when the user stops automation before a shot completes."""


class AutomationPaused(RuntimeError):
    """Raised internally when the operator requests a safe pause."""


def _grbl_status_state(status) -> str:
    return str(getattr(status, "state", "") or "").lower()


def _grbl_active_pins(status) -> frozenset[str]:
    pins = getattr(status, "active_pins", frozenset()) or frozenset()
    return frozenset(str(pin).upper() for pin in pins)


def _grbl_command_lock_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "error:9" in text or "error: 9" in text or "alarm" in text or "locked" in text


def _grbl_status_text(status) -> str:
    return str(getattr(status, "raw", "") or getattr(status, "state", "") or "unknown")


def _default_simulation_reference_spectrum_path() -> str:
    return str(_SIMULATION_REFERENCE_SPECTRUM_PATH)


@lru_cache(maxsize=4)
def _load_simulation_reference_spectrum(path: str | None = None) -> tuple[np.ndarray, np.ndarray]:
    spectrum_path = Path(path or _default_simulation_reference_spectrum_path())
    data = np.loadtxt(spectrum_path, delimiter="\t", skiprows=1)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    if data.shape[1] < 2:
        raise ValueError(f"Reference simulation spectrum must have at least two columns: {spectrum_path}")
    wavelengths = np.asarray(data[:, 0], dtype=float)
    intensities = np.asarray(data[:, 1], dtype=float)
    finite = np.isfinite(wavelengths) & np.isfinite(intensities)
    if not finite.any():
        raise ValueError(f"Reference simulation spectrum has no numeric rows: {spectrum_path}")
    wavelengths = wavelengths[finite]
    intensities = intensities[finite]
    order = np.argsort(wavelengths)
    wavelengths = wavelengths[order]
    intensities = intensities[order]
    wavelengths.setflags(write=False)
    intensities.setflags(write=False)
    return wavelengths, intensities


def _simulation_target_seed(target, *, salt: str = "spectrum") -> int:
    plate_index = getattr(target, "plate_index", 1)
    well = getattr(target, "well", getattr(target, "point_key", "P001"))
    shot_number = getattr(target, "shot_number", 1)
    key = f"{salt}:{int(plate_index)}:{str(well).upper()}:{int(shot_number)}"
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest[:4], "little", signed=False)


def _coerce_simulation_max_intensity(max_intensity) -> float:
    try:
        value = float(max_intensity)
    except (TypeError, ValueError):
        value = _SIMULATION_REFERENCE_ADC_MAX
    if not np.isfinite(value) or value <= 0.0:
        value = _SIMULATION_REFERENCE_ADC_MAX
    return value


def _simulation_value_bounds(max_intensity: float) -> tuple[float, float]:
    max_intensity = _coerce_simulation_max_intensity(max_intensity)
    return -max_intensity * 0.03, max_intensity


def _reference_based_simulation_spectrum(
    wavelengths,
    target,
    *,
    max_intensity: float = _SIMULATION_REFERENCE_ADC_MAX,
    reference_path: str | None = None,
) -> np.ndarray:
    target_wavelengths = np.asarray(wavelengths, dtype=float)
    if target_wavelengths.size == 0:
        return np.asarray([], dtype=float)

    reference_wavelengths, reference_intensities = _load_simulation_reference_spectrum(reference_path)
    max_intensity = _coerce_simulation_max_intensity(max_intensity)
    scale = max_intensity / _SIMULATION_REFERENCE_ADC_MAX
    base = np.interp(
        target_wavelengths,
        reference_wavelengths,
        reference_intensities,
        left=float(reference_intensities[0]),
        right=float(reference_intensities[-1]),
    ) * scale

    finite_base = base[np.isfinite(base)]
    if finite_base.size == 0:
        return np.zeros_like(target_wavelengths, dtype=float)

    rng = np.random.default_rng(_simulation_target_seed(target))
    values = base * rng.uniform(0.94, 1.0)

    axis = np.linspace(-0.5, 0.5, target_wavelengths.size)
    values += rng.normal(0.0, max_intensity * 0.00003)
    values += axis * rng.normal(0.0, max_intensity * 0.00015)

    peak_threshold = float(np.nanpercentile(base, 99.0))
    peak_candidates = np.flatnonzero(base >= peak_threshold)
    if peak_candidates.size:
        chosen = rng.choice(peak_candidates, size=min(3, peak_candidates.size), replace=False)
        for index in np.atleast_1d(chosen):
            center = float(target_wavelengths[int(index)])
            width = rng.uniform(0.18, 0.85)
            height = float(base[int(index)]) * rng.normal(0.0, 0.01)
            values += height * np.exp(-0.5 * ((target_wavelengths - center) / width) ** 2)

    noise_sd = max(max_intensity * 0.00008, 1e-9)
    values += rng.normal(0.0, noise_sd, target_wavelengths.size)
    finite_values = values[np.isfinite(values)]
    if finite_values.size:
        peak = float(np.nanmax(finite_values))
        headroom = max_intensity * 0.975
        if peak > headroom > 0.0:
            values *= headroom / peak
    lower, upper = _simulation_value_bounds(max_intensity)
    return np.clip(values, lower, upper)


def _grbl_blocking_pins(status, *, intent: str, allow_p_input_for_firing: bool) -> list[str]:
    pins = _grbl_active_pins(status)
    blocked = set(_GRBL_CONTROL_PINS.intersection(pins))
    blocked.update(_GRBL_LIMIT_PINS.intersection(pins))
    if intent == GRBL_PREP_FIRING and _GRBL_PROBE_PIN in pins and not allow_p_input_for_firing:
        blocked.add(_GRBL_PROBE_PIN)
    return sorted(blocked)


def _send_grbl_status(send_status, message: str) -> None:
    if callable(send_status):
        send_status(message)


def _resync_grbl_command_stream(laser, *, context: str, send_status=None) -> None:
    resync = getattr(laser, "resync_command_stream", None)
    if not callable(resync):
        raise RuntimeError(f"Reconnect the laser controller before {context}; command-stream recovery is unavailable.")
    _send_grbl_status(send_status, "Recovering GRBL command stream after a timeout.")
    try:
        resync()
    except Exception as exc:
        raise RuntimeError(f"Reconnect the laser controller before {context}; command-stream recovery failed: {exc}") from exc
    _send_grbl_status(send_status, "GRBL command stream recovered.")


def _try_emergency_laser_off(laser) -> None:
    off = getattr(laser, "emergency_laser_off", None)
    if callable(off):
        try:
            off()
        except Exception:
            logger.debug("Emergency laser-off failed during GRBL preparation.", exc_info=True)


def _check_grbl_preparation_cancelled(laser, *, context: str, should_stop=None) -> None:
    if callable(should_stop) and should_stop():
        _try_emergency_laser_off(laser)
        raise AutomationStopped(f"{context.capitalize()} stopped during laser controller preparation.")


def _grbl_status_poll_transient_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "no grbl status response" in text
        or "timed out waiting" in text
        or ("timed out" in text and "status" in text)
    )


def _poll_grbl_status(
    laser,
    *,
    timeout_s: float = 2.0,
    attempts: int = 1,
    retry_delay_s: float = 0.25,
    send_status=None,
):
    poll_status = getattr(laser, "poll_status", None)
    if not callable(poll_status):
        raise RuntimeError("GRBL status polling is unavailable for this laser controller.")
    total_attempts = max(1, int(attempts))
    last_exc: Exception | None = None
    for attempt in range(total_attempts):
        try:
            status = poll_status(timeout_s=timeout_s)
        except Exception as exc:
            last_exc = exc
            if (
                attempt + 1 < total_attempts
                and _grbl_status_poll_transient_error(exc)
                # A lost port is never transient; retrying a dead handle only
                # delays the connection-lost report.
                and not getattr(laser, "connection_lost", False)
            ):
                if attempt == 0:
                    _send_grbl_status(send_status, "Waiting for GRBL status after controller recovery.")
                time.sleep(max(0.0, float(retry_delay_s)))
                continue
            _try_emergency_laser_off(laser)
            raise RuntimeError(f"Could not read GRBL status: {exc}") from exc
        if status is not None:
            return status
        last_exc = RuntimeError("no status was returned")
        if attempt + 1 < total_attempts:
            if attempt == 0:
                _send_grbl_status(send_status, "Waiting for GRBL status after controller recovery.")
            time.sleep(max(0.0, float(retry_delay_s)))
            continue
        _try_emergency_laser_off(laser)
        raise RuntimeError("Could not read GRBL status: no status was returned.")
    _try_emergency_laser_off(laser)
    raise RuntimeError(f"Could not read GRBL status: {last_exc}")


def _home_grbl_controller_once(laser, *, context: str, send_status=None, should_stop=None) -> None:
    _send_grbl_status(send_status, "Preparing laser controller: homing required.")
    home = getattr(laser, "home", None)
    unlock = getattr(laser, "unlock", None)
    if not callable(home):
        raise RuntimeError(f"GRBL homing is unavailable before {context}. Use Home first.")

    _check_grbl_preparation_cancelled(laser, context=context, should_stop=should_stop)
    try:
        home()
    except Exception as exc:
        if not _grbl_command_lock_error(exc) or not callable(unlock):
            if _grbl_command_lock_error(exc):
                raise RuntimeError(f"Controller is locked; homing/unlock recovery failed before {context}: {exc}") from exc
            raise RuntimeError(f"GRBL homing failed before {context}: {exc}") from exc

        _send_grbl_status(send_status, "Preparing laser controller: unlock required before homing.")
        try:
            unlock()
        except Exception as unlock_exc:
            raise RuntimeError(
                f"Controller is locked; homing/unlock recovery failed before {context}: {unlock_exc}"
            ) from unlock_exc
        _check_grbl_preparation_cancelled(laser, context=context, should_stop=should_stop)
        try:
            home()
        except Exception as home_exc:
            raise RuntimeError(
                f"Controller is locked; homing/unlock recovery failed before {context}: {home_exc}"
            ) from home_exc

    _check_grbl_preparation_cancelled(laser, context=context, should_stop=should_stop)


def _verify_grbl_ready_modal_commands(laser) -> None:
    send_command = getattr(laser, "send_command", None)
    if not callable(send_command):
        raise RuntimeError("GRBL command verification is unavailable for this laser controller.")
    for command in ("G90", "G21", "M5"):
        send_command(command)


def _ensure_grbl_work_position(laser, *, context: str):
    ensure_work_position = getattr(laser, "ensure_work_position", None)
    if callable(ensure_work_position):
        try:
            ensure_work_position(timeout_s=2.0)
        except TypeError:
            try:
                ensure_work_position()
            except Exception as exc:
                _try_emergency_laser_off(laser)
                raise RuntimeError(f"Reliable GRBL work coordinates are unavailable before {context}: {exc}") from exc
        except Exception as exc:
            _try_emergency_laser_off(laser)
            raise RuntimeError(f"Reliable GRBL work coordinates are unavailable before {context}: {exc}") from exc
        return getattr(laser, "last_status", None)

    status = getattr(laser, "last_status", None)
    if not bool(getattr(status, "work_position_available", False)):
        _try_emergency_laser_off(laser)
        raise RuntimeError(f"Reliable GRBL work coordinates are unavailable before {context}; home the laser controller.")
    return status


def _wait_grbl_idle_if_needed(laser, status, *, context: str):
    state = _grbl_status_state(status)
    if state in {"", "idle"}:
        return status
    if state.startswith(_GRBL_BLOCKING_STATES):
        _try_emergency_laser_off(laser)
        raise RuntimeError(f"GRBL controller is not ready: {_grbl_status_text(status)}")

    wait_until_idle = getattr(laser, "wait_until_idle", None)
    if callable(wait_until_idle):
        try:
            status = wait_until_idle(timeout_s=10.0, poll_interval_s=0.5)
        except Exception as exc:
            _try_emergency_laser_off(laser)
            raise RuntimeError(f"GRBL controller did not become idle before {context}: {exc}") from exc
        state = _grbl_status_state(status)

    if state != "idle":
        _try_emergency_laser_off(laser)
        raise RuntimeError(f"GRBL controller is not idle before {context}: {_grbl_status_text(status)}")
    return status


def _check_grbl_pins_for_intent(
    laser,
    status,
    *,
    intent: str,
    context: str,
    allow_p_input_for_firing: bool,
    send_status=None,
) -> None:
    pins = _grbl_active_pins(status)
    control_pins = sorted(_GRBL_CONTROL_PINS.intersection(pins))
    if control_pins:
        _try_emergency_laser_off(laser)
        joined = ", ".join(control_pins)
        raise RuntimeError(
            f"GRBL controller input(s) active ({joined}); clear door/hold/reset/cycle-start state before {context}."
        )

    limit_pins = sorted(_GRBL_LIMIT_PINS.intersection(pins))
    if limit_pins:
        _try_emergency_laser_off(laser)
        joined = ", ".join(limit_pins)
        raise RuntimeError(
            f"GRBL limit input(s) active ({joined}); home failed or a limit switch is still triggered before {context}."
        )

    if _GRBL_PROBE_PIN not in pins:
        return
    if intent == GRBL_PREP_MOTION:
        _send_grbl_status(send_status, "P input active; motion can continue because P is not treated as a motion interlock.")
        return
    if allow_p_input_for_firing:
        _send_grbl_status(send_status, "P input active; firing allowed by the selected laser profile or machine-profile override.")
        return
    _try_emergency_laser_off(laser)
    raise RuntimeError(
        "P input active; firing is blocked. Only enable the P-input override if this Monport controller always reports P "
        "during normal safe operation and hardware testing confirms it is not a cover, water, or other safety interlock."
    )


def prepare_grbl_controller(
    laser,
    *,
    intent: str,
    context: str,
    allow_p_input_for_firing: bool = False,
    force_home: bool = False,
    send_status=None,
    should_stop=None,
):
    """Prepare GRBL for either motion-only work or guarded firing."""
    if intent not in {GRBL_PREP_MOTION, GRBL_PREP_FIRING}:
        raise ValueError(f"Unknown GRBL preparation intent: {intent}")

    _send_grbl_status(send_status, "Preparing laser controller.")
    recovery_used = False
    resync_used = False
    if bool(getattr(laser, "reconnect_required", False)):
        _resync_grbl_command_stream(laser, context=context, send_status=send_status)
        resync_used = True
    if force_home:
        status = _poll_grbl_status(laser, attempts=3, retry_delay_s=0.5, send_status=send_status)
        state = _grbl_status_state(status)
        if state.startswith(("door", "hold")):
            _try_emergency_laser_off(laser)
            raise RuntimeError(f"GRBL controller is not ready before {context}: {_grbl_status_text(status)}")
        _home_grbl_controller_once(laser, context=context, send_status=send_status, should_stop=should_stop)
        recovery_used = True

    while True:
        _check_grbl_preparation_cancelled(laser, context=context, should_stop=should_stop)
        status = _poll_grbl_status(laser, attempts=4, retry_delay_s=0.5, send_status=send_status)
        state = _grbl_status_state(status)

        if state.startswith("alarm"):
            if recovery_used:
                _try_emergency_laser_off(laser)
                raise RuntimeError(f"GRBL remained in alarm after automatic homing: {_grbl_status_text(status)}")
            _home_grbl_controller_once(laser, context=context, send_status=send_status, should_stop=should_stop)
            recovery_used = True
            continue

        status = _wait_grbl_idle_if_needed(laser, status, context=context)
        _check_grbl_pins_for_intent(
            laser,
            status,
            intent=intent,
            context=context,
            allow_p_input_for_firing=allow_p_input_for_firing,
            send_status=send_status,
        )

        try:
            _verify_grbl_ready_modal_commands(laser)
        except Exception as exc:
            if _grbl_command_lock_error(exc) and not recovery_used:
                _home_grbl_controller_once(laser, context=context, send_status=send_status, should_stop=should_stop)
                recovery_used = True
                continue
            if bool(getattr(laser, "reconnect_required", False)) and not resync_used:
                _resync_grbl_command_stream(laser, context=context, send_status=send_status)
                resync_used = True
                continue
            _try_emergency_laser_off(laser)
            if _grbl_command_lock_error(exc):
                raise RuntimeError(f"Controller is locked; homing/unlock recovery failed before {context}: {exc}") from exc
            raise RuntimeError(f"GRBL preparation failed while verifying safe modal commands: {exc}") from exc

        try:
            _ensure_grbl_work_position(laser, context=context)
        except Exception:
            if recovery_used:
                raise
            _home_grbl_controller_once(laser, context=context, send_status=send_status, should_stop=should_stop)
            recovery_used = True
            continue
        return status


def verify_grbl_firing_interlock(
    laser,
    *,
    context: str,
    allow_p_input_for_firing: bool = False,
    send_status=None,
):
    """Poll GRBL immediately before M3 without retrying, homing, or unlocking."""
    status = _poll_grbl_status(laser, timeout_s=1.0)
    status = _wait_grbl_idle_if_needed(laser, status, context=context)
    _check_grbl_pins_for_intent(
        laser,
        status,
        intent=GRBL_PREP_FIRING,
        context=context,
        allow_p_input_for_firing=allow_p_input_for_firing,
        send_status=send_status,
    )
    return status


class AutomatedAcquisitionWorker(AcquisitionWorker):
    """Worker that moves a GRBL laser through a plate plan and captures spectra."""

    STATE_DRY_RUN = "DRY_RUN"
    STATE_AUTOMATED = "AUTOMATED"
    STATE_LASER_TEST = "LASER_TEST"

    def __init__(self, spectrometer_module, laser_controller=None):
        super().__init__(spectrometer_module)
        self.name = "AutomatedAcquisitionWorker"
        self.laser = laser_controller
        self.automation_config: AutomationRunConfig | None = None
        self.automation_state: AutomatedRunState | None = None
        self.mapping_config: MappingGridConfig | None = None
        self.mapping_state: MappingRunState | None = None
        self.completed_dry_run_signature: str | None = None
        self._automation_lock = threading.RLock()
        self._plate_states: dict[int, PlateRunState] = {}
        self._active_plate_index: int | None = None
        self._safety_checklist: dict[str, bool] = {}
        self._spectra_qc_enabled = False
        self._last_qc_manifest: dict | None = None
        self._qc_repeat_targets: list[AutomationTarget] = []
        self._pause_requested = threading.Event()
        self._pause_provenance: dict | None = None
        self._pause_events: list[dict] = []
        self._last_laser_interruption: dict | None = None
        self._laser_interruption_finalized = False
        self._automation_run_mode = RUN_MODE_REAL
        self.automation_paused = False
        self.grbl_p_input_nonblocking = False
        self._mapping_completed_since_state_write = 0
        self._mapping_effective_move_speed_mm_min: float | None = None
        self._mapping_trigger_executor_creations = 0
        self._mapping_stale_frames_drained = 0
        self._mapping_trigger_drain_events: list[dict] = []
        self._mapping_trigger_drain_alert_sent = False
        self._mapping_trigger_drain_unsupported = False
        self._mapping_spectrum_store: MappingSpectrumStore | None = None
        self._mapping_save_writer: MappingSaveWriter | None = None
        # Guards mapping_state.completed_targets between the acquisition
        # thread and the save-writer thread (set copies/sorts iterate it).
        self._mapping_state_lock = threading.Lock()
        self._mapping_background_capture_enabled = True
        self._mapping_background_info: dict | None = None
        self._last_requested_trigger_delay_us: float | None = None
        self._run_light_seen = False
        self._run_dark_startup_frames = 0
        self._last_mapping_cycle_end: float | None = None
        self._mapping_stream_stats: dict | None = None
        self._stream_pin_abort_reason: str | None = None
        self._last_plate_cycle_end: float | None = None
        self._plate_completed_since_state_write = 0
        self._plate_last_started_well: tuple[int, str] | None = None

    def set_laser_controller(self, laser_controller) -> None:
        with self._automation_lock:
            self.laser = laser_controller

    def set_grbl_p_input_nonblocking(self, enabled: bool) -> None:
        with self._automation_lock:
            self.grbl_p_input_nonblocking = bool(enabled)

    def _active_motion_config(self):
        return self.mapping_config or self.automation_config

    def _effective_mapping_move_speed_mm_min(self, laser=None) -> float:
        if self._mapping_effective_move_speed_mm_min is not None:
            return self._mapping_effective_move_speed_mm_min
        config = self.mapping_config
        if config is None:
            return DEFAULT_MAPPING_MOVE_SPEED_MM_MIN
        settings = getattr(laser or self.laser, "cached_settings", {}) or {}
        self._mapping_effective_move_speed_mm_min = effective_mapping_move_speed_mm_min(config.move_speed_mm_min, settings)
        return self._mapping_effective_move_speed_mm_min

    def _relay_profile_enabled(self, config=None) -> bool:
        config = config or self._active_motion_config()
        return bool(config and config.laser_profile == LASER_PROFILE_MONPORT_NDYAG_RELAY)

    def _allow_p_input_for_config(self, config=None) -> bool:
        return bool(self.grbl_p_input_nonblocking or self._relay_profile_enabled(config))

    def _p_input_policy_context(self, config=None) -> str:
        config = config or self._active_motion_config()
        profile = config.laser_profile if config is not None else ""
        if self._relay_profile_enabled(config):
            policy = "allowed by Monport relay profile"
        elif self.grbl_p_input_nonblocking:
            policy = "allowed by manual machine-profile override"
        else:
            policy = "blocked for guarded firing"
        return (
            f" Active laser profile: {profile or 'unknown'}; P-input policy: {policy}. "
            "For this Monport relay setup, select the Monport Nd:YAG relay profile, apply the plan, "
            "and rerun dry run before guarded acquisition."
        )

    def _apply_relay_controller_s_max(self, controller_s_max: float, *, context: str) -> None:
        config = self._active_motion_config()
        if config is None or not self._relay_profile_enabled(config):
            return
        effective_s_max = max(1, int(round(float(controller_s_max))))
        if effective_s_max == int(config.laser_s_max):
            return
        updated = replace(config, laser_s_max=effective_s_max)
        if isinstance(updated, MappingGridConfig):
            self.mapping_config = updated
            if self.mapping_state is not None:
                self.mapping_state.config = updated
        else:
            self.automation_config = updated
        if self.automation_state is not None and isinstance(updated, AutomationRunConfig):
            self.automation_state.config = updated
            for plate_index, plate_state in self._plate_states.items():
                plate_state.config = self._plate_config_for_slot(updated, plate_index)
        self._send(
            AcquisitionMessage.STATUS,
            (
                f"Controller $30 is {effective_s_max:g}; using S{updated.laser_s_value:g} "
                f"as the Monport relay 100% trigger scale before {context}."
            ),
        )
        self._persist_run_metadata()

    def _fire_laser_for_target(
        self,
        config,
        target,
        laser,
        *,
        state_name: str,
    ) -> None:
        if self._relay_profile_enabled(config):
            fire_motion = getattr(laser, "fire_motion_pulse", None)
            if not callable(fire_motion):
                raise RuntimeError("Motion-coupled relay firing is unavailable for this laser controller.")
            if isinstance(config, MappingGridConfig):
                end_x, end_y = mapping_relay_stroke_endpoint(config, target)
                feed = mapping_relay_stroke_feed_mm_min(config, target)
            else:
                end_x, end_y = relay_stroke_endpoint(config, target)
                feed = relay_stroke_feed_mm_min(config, target)
            fire_motion(
                end_x_mm=end_x,
                end_y_mm=end_y,
                s_value=config.laser_s_value,
                pulse_ms=config.pulse_ms,
                feed_mm_min=feed,
                should_stop=lambda: self._laser_pulse_cancel_requested(state_name),
            )
            return
        laser.fire_pulse(
            s_value=config.laser_s_value,
            pulse_ms=config.pulse_ms,
            should_stop=lambda: self._laser_pulse_cancel_requested(state_name),
        )

    def _ensure_laser_off_before_motion(self, laser) -> None:
        off = getattr(laser, "laser_off", None)
        if callable(off):
            off()
            return
        fallback = getattr(laser, "emergency_laser_off", None)
        if callable(fallback):
            fallback()

    def set_safety_checklist(self, checklist: dict[str, bool] | None) -> None:
        with self._automation_lock:
            self._safety_checklist = dict(checklist or {})
        self._persist_run_metadata()

    def set_spectra_qc_enabled(self, enabled: bool) -> None:
        with self._automation_lock:
            self._spectra_qc_enabled = bool(enabled)

    def set_mapping_background_capture(self, enabled: bool) -> None:
        """Enable/disable the laser-off background capture before a mapping run."""
        with self._automation_lock:
            self._mapping_background_capture_enabled = bool(enabled)

    def request_pause(self, source: str = "unspecified", *, ui_context: dict | None = None) -> None:
        """Ask the worker to stop at the next safe motion/firing boundary.

        ``source`` records who asked (``toolbar``, ``start-button-toggle``,
        ``worker``, ``stop``, ``test-simulation``, ...) so unexpected pauses can
        be traced back to their trigger.
        """
        mapping_state = self.mapping_state
        provenance = {
            "requested_at": datetime.now().isoformat(timespec="milliseconds"),
            "source": str(source),
            "run_type": MAPPING_RUN_TYPE if self.mapping_config is not None else "plate",
            "worker_state": self.state,
            "stop_event_set": self._stop_event.is_set(),
            "automation_paused_before_request": bool(self.automation_paused),
            "active_target": mapping_state.active_target if mapping_state is not None else None,
            "completed_count": len(mapping_state.completed_targets) if mapping_state is not None else None,
            "ui_context": dict(ui_context or {}),
        }
        self._pause_provenance = provenance
        logger.info("Pause requested: %s", provenance)
        self._pause_requested.set()
        self._state_change_event.set()
        self._send(AcquisitionMessage.STATUS, "Pause requested. Waiting for a safe boundary.")

    def _finalize_pause_event(self, final_cleanup_context: str) -> None:
        """Close out the current pause with cleanup context and safety snapshots.

        The finished event is appended to ``_pause_events`` (persisted in the
        mapping manifest and summary) and the pending provenance is cleared so
        the next pause starts a fresh record.
        """
        provenance = self._pause_provenance or {
            "requested_at": datetime.now().isoformat(timespec="milliseconds"),
            "source": "unknown_worker_observed",
            "run_type": MAPPING_RUN_TYPE if self.mapping_config is not None else "plate",
        }
        event = dict(provenance)
        event["final_cleanup_context"] = str(final_cleanup_context)
        event["finalized_at"] = datetime.now().isoformat(timespec="milliseconds")
        event["completed_count_at_pause"] = (
            len(self.mapping_state.completed_targets) if self.mapping_state is not None else None
        )
        event["laser_safety_last_interruption"] = dict(self._last_laser_interruption or {})
        # Use the controller's cached status only — never issue a new serial
        # round trip while finalizing a pause.
        status = getattr(self.laser, "last_status", None) if self.laser is not None else None
        if status is not None:
            event["grbl_status"] = getattr(status, "raw", "") or str(getattr(status, "state", "") or "")
        self._pause_events.append(event)
        self._pause_provenance = None
        logger.info("Pause event finalized: %s", event)

    def _record_pause_boundary(self, boundary: str) -> None:
        """Record the worker boundary where a pause request was first observed.

        Only the first boundary is kept. If the pause flag was set without
        ``request_pause`` being called, a provenance record is created with
        ``source="unknown_worker_observed"`` — that on its own is a useful
        diagnostic for stray pauses.
        """
        provenance = self._pause_provenance
        if provenance is None:
            provenance = {
                "requested_at": datetime.now().isoformat(timespec="milliseconds"),
                "source": "unknown_worker_observed",
                "run_type": MAPPING_RUN_TYPE if self.mapping_config is not None else "plate",
                "worker_state": self.state,
                "stop_event_set": self._stop_event.is_set(),
            }
            self._pause_provenance = provenance
        if "first_boundary" not in provenance:
            provenance["first_boundary"] = str(boundary)
            logger.info("Pause first observed at worker boundary: %s", boundary)

    def run_plate_qc_now(self, *, background: bool = True):
        """Run spectra-readiness QC across the current automated run."""
        if background:
            threading.Thread(
                target=lambda: self._run_spectra_qc_if_enabled(force=True),
                daemon=True,
                name="AutomatedQCThread",
            ).start()
            return None
        return self._run_spectra_qc_if_enabled(force=True)

    def load_resume_state_for_config(self, config: AutomationRunConfig) -> AutomatedRunState | None:
        return load_resume_state_for_config(self.save_directory, config)

    def _simulation_devices_ready(self) -> bool:
        caps = getattr(self.spec, "capabilities", None)
        spectrometer_simulated = bool(getattr(caps, "is_simulated", False)) or str(
            getattr(caps, "brand", "")
        ).lower() == "simulated"
        laser_simulated = self.laser is not None and bool(getattr(self.laser, "is_simulated", False))
        return spectrometer_simulated and laser_simulated

    def _metadata_run_mode(self) -> str | None:
        return RUN_MODE_SIMULATION if self._automation_run_mode == RUN_MODE_SIMULATION else None

    def configure_automation(
        self,
        config: AutomationRunConfig,
        *,
        resume_state: AutomatedRunState | None = None,
        safety_checklist: dict[str, bool] | None = None,
        run_mode: str = RUN_MODE_REAL,
    ) -> dict:
        slots = plan_plate_slots(config)
        if not slots:
            raise ValueError("No plates fit inside the taught bed area with the selected model and gaps.")
        run_mode = RUN_MODE_SIMULATION if run_mode == RUN_MODE_SIMULATION else RUN_MODE_REAL

        with self._automation_lock:
            self._run_light_seen = False
            self._run_dark_startup_frames = 0
            if config.run_directory:
                self.save_directory = config.run_directory
            if safety_checklist is not None:
                self._safety_checklist = dict(safety_checklist)
            previous_motion_signature = self.automation_config.dry_run_signature() if self.automation_config else None
            previous_identity_signature = self.automation_config.run_identity_signature() if self.automation_config else None
            previous_run_directory = self.automation_config.run_directory if self.automation_config else None
            new_motion_signature = config.dry_run_signature()
            new_identity_signature = config.run_identity_signature()
            if resume_state is not None and resume_state.config.run_identity_signature() != new_identity_signature:
                raise ValueError("Saved automated run state does not match the current plate plan.")
            reset_run = (
                resume_state is not None
                or previous_motion_signature != new_motion_signature
                or previous_identity_signature != new_identity_signature
                or self._automation_run_mode != run_mode
                or (
                    run_mode == RUN_MODE_SIMULATION
                    and previous_run_directory != config.run_directory
                )
                or not self._plate_states
            )
            self._automation_run_mode = run_mode
            self.automation_config = config
            self.mapping_config = None
            self.mapping_state = None
            if reset_run:
                self.automation_state = resume_state or AutomatedRunState(config=config)
                self.automation_state.config = config
                self.automation_paused = bool(
                    resume_state is not None
                    and resume_state.completed_targets
                    and len(resume_state.completed_targets) < len(automation_targets(config))
                )
                self.completed_dry_run_signature = (
                    new_motion_signature
                    if self.automation_state.dry_run_signature == new_motion_signature
                    or self.completed_dry_run_signature == new_motion_signature
                    else None
                )
                self._last_qc_manifest = None
                load_existing_plate_states = resume_state is not None
                self._plate_states = {
                    slot.index: self._plate_state_for_slot(
                        config,
                        slot.index,
                        load_existing=load_existing_plate_states,
                    )
                    for slot in slots
                }
                self._rebuild_plate_progress_from_completed()
                self._rebuild_qc_repeat_targets_from_plate_states()
                self._active_plate_index = None
            elif self.automation_state is None:
                self.automation_state = AutomatedRunState(config=config)
            else:
                self.automation_state.config = config
            self.collect_timing_metrics = True
            active_index = self._active_plate_index or slots[0].index
            payload = self._progress_payload_for_plate(active_index)

        self._send(AcquisitionMessage.PLATE_PROGRESS, payload)
        self._persist_automation_state(event="configured")
        self._persist_run_metadata()
        return {
            "plates": len(slots),
            "targets": len(automation_targets(config)),
            "dry_run_signature": new_motion_signature,
            "run_identity_signature": new_identity_signature,
            "resumed_targets": len(resume_state.completed_targets) if resume_state else 0,
        }

    def configure_mapping(
        self,
        config: MappingGridConfig,
        *,
        resume_state: MappingRunState | None = None,
        safety_checklist: dict[str, bool] | None = None,
        run_mode: str = RUN_MODE_REAL,
    ) -> dict:
        target_count = mapping_target_count(config)
        if target_count <= 0:
            raise ValueError("Mapping grid has no targets.")
        point_count = target_count // max(1, int(config.shots_per_point))
        run_mode = RUN_MODE_SIMULATION if run_mode == RUN_MODE_SIMULATION else RUN_MODE_REAL

        with self._automation_lock:
            self._run_light_seen = False
            self._run_dark_startup_frames = 0
            if config.run_directory:
                self.save_directory = config.run_directory
            if safety_checklist is not None:
                self._safety_checklist = dict(safety_checklist)
            previous_motion_signature = self.mapping_config.dry_run_signature() if self.mapping_config else None
            previous_identity_signature = self.mapping_config.run_identity_signature() if self.mapping_config else None
            previous_run_directory = self.mapping_config.run_directory if self.mapping_config else None
            new_motion_signature = config.dry_run_signature()
            new_identity_signature = config.run_identity_signature()
            if resume_state is not None and resume_state.config.run_identity_signature() != new_identity_signature:
                raise ValueError("Saved mapping state does not match the current mapping plan.")
            reset_run = (
                resume_state is not None
                or self.automation_config is not None
                or previous_motion_signature != new_motion_signature
                or previous_identity_signature != new_identity_signature
                or self._automation_run_mode != run_mode
                or (run_mode == RUN_MODE_SIMULATION and previous_run_directory != config.run_directory)
                or self.mapping_state is None
            )
            self._automation_run_mode = run_mode
            self.mapping_config = config
            self.automation_config = None
            self.automation_state = None
            self._plate_states = {}
            self._plate_run_state = None
            self._active_plate_index = None
            self._qc_repeat_targets = []
            self._last_qc_manifest = None
            if reset_run:
                self.mapping_state = resume_state or MappingRunState(config=config)
                self.mapping_state.config = config
                self.mapping_state.completed_targets.update(
                    completed_mapping_targets_from_index(config.run_directory or self.save_directory, config)
                )
                self.mapping_state.missing_targets.update(
                    missing_mapping_targets_from_index(config.run_directory or self.save_directory, config)
                )
                self.automation_paused = bool(
                    self.mapping_state.completed_targets
                    and len(self.mapping_state.completed_targets) < target_count
                )
                self.completed_dry_run_signature = (
                    new_motion_signature
                    if self.mapping_state.dry_run_signature == new_motion_signature
                    or self.completed_dry_run_signature == new_motion_signature
                    else None
                )
            elif self.mapping_state is None:
                self.mapping_state = MappingRunState(config=config)
            else:
                self.mapping_state.config = config
            self.collect_timing_metrics = True
            self._mapping_completed_since_state_write = 0
            self._mapping_effective_move_speed_mm_min = None
            self._mapping_spectrum_store = None
            if reset_run and resume_state is None:
                self._pause_events = []
                self._pause_provenance = None
                self._mapping_background_info = None
                # Run-scoped like _pause_events: pause/resume within a session
                # keeps accumulating so the persisted summary carries the full
                # drain history needed for an exact post-hoc re-index.
                self._mapping_stale_frames_drained = 0
                self._mapping_trigger_drain_events = []
                self._mapping_trigger_drain_alert_sent = False
                self._mapping_trigger_drain_unsupported = False
            payload = mapping_progress_payload(config, self.mapping_state, include_static_plan=True)

        self._send(AcquisitionMessage.PLATE_PROGRESS, payload)
        self._persist_mapping_state(event="configured")
        self._persist_run_metadata()
        return {
            "run_type": MAPPING_RUN_TYPE,
            "points": point_count,
            "targets": target_count,
            "dry_run_signature": new_motion_signature,
            "run_identity_signature": new_identity_signature,
            "resumed_targets": len(resume_state.completed_targets) if resume_state else 0,
        }

    def _rebuild_plate_progress_from_completed(self) -> None:
        state = self.automation_state
        config = self.automation_config
        if state is None or config is None:
            return
        for target in automation_targets(config):
            if target.key not in state.completed_targets:
                continue
            plate_state = self._plate_states.get(target.plate_index)
            if plate_state is None or target.well not in plate_state.shots_by_well:
                continue
            if target.well in set(plate_state.repair_queue):
                continue
            plate_state.shots_by_well[target.well] = max(
                plate_state.shots_by_well.get(target.well, 0),
                min(target.shot_number, config.shots_per_well),
            )

    def _plate_state_for_slot(
        self,
        config: AutomationRunConfig,
        plate_index: int,
        *,
        load_existing: bool = False,
    ) -> PlateRunState:
        expected = self._plate_config_for_slot(config, plate_index)
        if not load_existing:
            return PlateRunState(expected)
        plate_dir = os.path.join(self.save_directory, config.plate_folder_name(plate_index))
        loaded_state = load_plate_run_state(plate_dir)
        if loaded_state is not None:
            if (
                loaded_state.config.plate_type == expected.plate_type
                and loaded_state.config.shots_per_well == expected.shots_per_well
                and loaded_state.config.order_mode == expected.order_mode
                and loaded_state.config.rows == expected.rows
                and loaded_state.config.columns == expected.columns
            ):
                return loaded_state

        # No usable state file. Rebuild from the spectra actually on disk rather
        # than starting empty, or a resumed run re-shoots wells it already
        # burned - which on a real sample destroys the material it would need to
        # shoot again.
        try:
            scanned = records_from_plate_folder(plate_dir)
            if scanned:
                return PlateRunState.from_records(expected, scanned)
        except ValueError:
            logger.warning(
                "Could not rebuild plate %s from its saved files; starting empty.",
                plate_index,
                exc_info=True,
            )
        return PlateRunState(expected)

    def _rebuild_qc_repeat_targets_from_plate_states(self) -> None:
        config = self.automation_config
        if config is None:
            self._qc_repeat_targets = []
            return

        queued_targets: list[AutomationTarget] = []
        for target in automation_targets(config):
            plate_state = self._plate_states.get(target.plate_index)
            if plate_state is None or target.well not in set(plate_state.repair_queue):
                continue
            queued_targets.append(target)
            if self.automation_state is not None:
                self.automation_state.completed_targets.discard(target.key)

        self._qc_repeat_targets = queued_targets

    def _plate_config_for_slot(self, config: AutomationRunConfig, plate_index: int) -> PlateAutosaveConfig:
        plate_name = config.plate_folder_name(plate_index)
        integration_us = getattr(self.spec, "integration_time_us", "")
        try:
            integration_ms = f"{float(integration_us) / 1000.0:g}"
        except (TypeError, ValueError):
            integration_ms = ""
        corrections = self._effective_corrections()
        return PlateAutosaveConfig(
            plate_type=config.plate_model.well_count,
            plate_name=plate_name,
            shots_per_well=config.shots_per_well,
            order_mode=config.order_mode,
            laser_wavelength_nm=None,
            laser_energy_mj=None,
            laser_energy=f"{config.laser_power_percent:g}% / S{config.laser_s_value}",
            laser_hz="",
            delay_enabled=config.settle_ms > 0,
            delay_ms=f"{config.settle_ms:g}",
            # Spectrometer and correction provenance travels in the runtime
            # extras bucket rather than as dataclass fields. These key names are
            # a contract with app.py's reproducibility panel.
            runtime={
                "sample_name": config.experiment_name,
                "integration_time_ms": integration_ms,
                "averages": self.averages,
                "correct_dark_counts": corrections.requested_dark_counts,
                "correct_nonlinearity": corrections.requested_nonlinearity,
                "effective_correct_dark_counts": corrections.correct_dark_counts,
                "effective_correct_nonlinearity": corrections.correct_nonlinearity,
                "correction_mask_reasons": corrections.mask_reasons(),
            },
        )

    def _progress_payload_for_plate(self, plate_index: int) -> dict:
        payload = self._plate_states[plate_index].progress_payload()
        total = len(self._plate_states)
        payload["plate_index"] = plate_index
        payload["automated_plate_count"] = total
        if self.automation_config is not None:
            payload["plate_label"] = self.automation_config.plate_display_label(plate_index)
            payload["plate_display_name"] = self.automation_config.plate_display_name(plate_index)
            payload["experiment_name"] = self.automation_config.experiment_name
            payload["column_integration_times_ms"] = list(self.automation_config.column_integration_times_ms)
            payload["column_delays_us"] = list(self.automation_config.column_delays_us)
            payload["plate_integration_times_ms"] = list(self.automation_config.plate_integration_times_ms)
        return payload

    def plate_progress_payloads(self) -> list[dict]:
        """Return one progress payload per planned automated plate."""
        with self._automation_lock:
            payloads = []
            for plate_index in sorted(self._plate_states):
                payloads.append(self._progress_payload_for_plate(plate_index))
            return payloads

    def _plate_index_for_state(self, plate_state: PlateRunState) -> int:
        for plate_index, state in self._plate_states.items():
            if state is plate_state:
                return plate_index
        if self._active_plate_index is not None:
            return self._active_plate_index
        return 1

    def _auto_save_plate_locked(
        self,
        wavelengths,
        intensities,
        timing: dict | None = None,
    ):
        plate_state = self._plate_run_state
        if plate_state is None or self.automation_config is None:
            super()._auto_save_plate_locked(wavelengths, intensities, timing=timing)
            return

        assignment = plate_state.next_assignment()
        if assignment is None:
            self._send(AcquisitionMessage.PLATE_COMPLETE, plate_state.progress_payload())
            return

        well, shot_number = assignment
        plate_index = self._plate_index_for_state(plate_state)
        plate_dir = os.path.join(self.save_directory, self.automation_config.plate_folder_name(plate_index))
        os.makedirs(plate_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = (
            f"{self.automation_config.plate_file_prefix(plate_index)}_{well}_shot{shot_number:02d}_"
            f"{timestamp}_{self._shot_index:03d}.csv"
        )
        filepath = self._unique_output_path(plate_dir, filename)

        if timing is not None:
            timing["save_file_path"] = filepath
        self._save_spectrum_file(filepath, wavelengths, intensities)
        if timing is not None:
            timing["save_end"] = time.perf_counter()
        repair_active_before_save = plate_state.repair_active
        integration_time_ms = None
        integration_time_us = None
        trigger_delay_us = None
        if timing is not None:
            integration_time_ms = timing.get("integration_time_ms")
            integration_time_us = timing.get("integration_time_us")
            trigger_delay_us = timing.get("trigger_delay_us")
        payload = plate_state.record_saved(
            filepath,
            self._shot_index,
            integration_time_ms=integration_time_ms,
            integration_time_us=integration_time_us,
            trigger_delay_us=trigger_delay_us,
        )
        payload["plate_index"] = plate_index
        payload["automated_plate_count"] = len(self._plate_states)
        payload["plate_label"] = self.automation_config.plate_display_label(plate_index)
        payload["plate_display_name"] = self.automation_config.plate_display_name(plate_index)
        payload["experiment_name"] = self.automation_config.experiment_name
        payload["column_integration_times_ms"] = list(self.automation_config.column_integration_times_ms)
        payload["column_delays_us"] = list(self.automation_config.column_delays_us)
        payload["plate_integration_times_ms"] = list(self.automation_config.plate_integration_times_ms)
        repair_completed = repair_active_before_save and not plate_state.repair_active
        if timing is not None:
            timing["plate_state_write_start"] = time.perf_counter()
        self._persist_plate_state_locked(timing=timing)
        if timing is not None and "plate_state_write_end" not in timing:
            timing["plate_state_write_end"] = time.perf_counter()

        self._send(AcquisitionMessage.SAVE_COMPLETE, filepath)
        self._send(AcquisitionMessage.PLATE_PROGRESS, payload)
        if repair_completed:
            self._persist_plate_reproducibility_log(event="plate_repair_completed")
            self._send(AcquisitionMessage.PLATE_REPAIR_COMPLETE, payload)
        if payload["complete"]:
            self._send(AcquisitionMessage.PLATE_COMPLETE, payload)
        logger.info("Automated plate auto-saved: %s", filepath)

    def _plate_state_extra_metadata(self) -> dict:
        if self.automation_config is None:
            return {}
        extra: dict = {}
        if self.automation_config.column_integration_times_ms:
            extra["column_integration_times_ms"] = list(self.automation_config.column_integration_times_ms)
        if self.automation_config.column_delays_us:
            extra["column_delays_us"] = list(self.automation_config.column_delays_us)
        if self.automation_config.plate_integration_times_ms:
            extra["plate_integration_times_ms"] = list(self.automation_config.plate_integration_times_ms)
        return extra

    def _persist_plate_state_locked(
        self,
        *,
        closed_early: bool = False,
        timing: dict | None = None,
    ):
        plate_state = self._plate_run_state
        if plate_state is None:
            return

        plate_dir = os.path.join(self.save_directory, plate_state.config.safe_plate_name)
        save_plate_run_state(
            plate_dir,
            plate_state,
            closed_early=closed_early,
            timing=timing,
            extra=self._plate_state_extra_metadata(),
        )

    def start_dry_run(self) -> None:
        if not self._ready_for_motion():
            return
        active_config = self._active_motion_config()
        if active_config is None:
            self._send(AcquisitionMessage.ERROR, "Configure an automated plan first.")
            return
        self._pause_requested.clear()
        if isinstance(active_config, MappingGridConfig):
            self._persist_mapping_state(event="dry_run_started")
        else:
            self._persist_automation_state(event="dry_run_started")
        self._set_state(self.STATE_DRY_RUN)
        self._send(AcquisitionMessage.STATUS, "Dry run started. Checking representative positions with laser output off.")

    def start_automated_run(self, *, simulation: bool = False) -> None:
        if not self._ready_for_motion():
            return
        active_config = self._active_motion_config()
        if active_config is None:
            self._send(AcquisitionMessage.ERROR, "Configure an automated plan first.")
            return
        requested_mode = RUN_MODE_SIMULATION if simulation else RUN_MODE_REAL
        if simulation and not self._simulation_devices_ready():
            self._send(AcquisitionMessage.ERROR, "Full simulation requires both the spectrometer and laser to be simulated.")
            return
        signature = active_config.dry_run_signature()
        if self.completed_dry_run_signature != signature:
            self._send(AcquisitionMessage.ERROR, "Run a dry run after the latest automation settings change.")
            return
        ext_mode = self.spec.capabilities.external_trigger_mode
        if ext_mode is None and not simulation:
            self._send(
                AcquisitionMessage.ERROR,
                f"{self.spec.model} does not support an external hardware trigger.",
            )
            return
        self._automation_run_mode = requested_mode
        self._pause_requested.clear()
        self.auto_save_enabled = True
        self.automation_paused = False
        if isinstance(active_config, MappingGridConfig):
            self._send(
                AcquisitionMessage.PLATE_PROGRESS,
                mapping_progress_payload(active_config, self.mapping_state, include_static_plan=True),
            )
            self._persist_mapping_state(event="simulated_run_started" if simulation else "mapping_run_started")
        else:
            for payload in self.plate_progress_payloads():
                payload["dry_run"] = False
                self._send(AcquisitionMessage.PLATE_PROGRESS, payload)
            self._persist_automation_state(
                event="simulated_run_started" if simulation else "automated_run_started"
            )
        self._set_state(self.STATE_AUTOMATED)
        if simulation:
            self._send(AcquisitionMessage.STATUS, "Simulated acquisition started.")
        elif isinstance(active_config, MappingGridConfig):
            self._send(AcquisitionMessage.STATUS, "2D mapping acquisition started.")
        elif self._qc_repeat_targets:
            self._send(AcquisitionMessage.STATUS, "QC repeat acquisition started.")
        else:
            self._send(AcquisitionMessage.STATUS, "Automated acquisition started.")

    def start_laser_pattern_test(self) -> None:
        if not self._ready_for_motion():
            return
        if self.automation_config is None:
            self._send(AcquisitionMessage.ERROR, "Configure an automated plate plan first.")
            return
        signature = self.automation_config.dry_run_signature()
        if self.completed_dry_run_signature != signature:
            self._send(AcquisitionMessage.ERROR, "Run a dry run after the latest automation settings change.")
            return
        self._automation_run_mode = RUN_MODE_LASER_TEST
        self._pause_requested.clear()
        self.auto_save_enabled = False
        self.automation_paused = False
        targets = automation_targets(self.automation_config)
        for plate_index in sorted(self._plate_states):
            payload = self._laser_test_progress_payload(
                plate_index,
                {},
                current_well=None,
                fired_positions=0,
                total_positions=len(targets),
            )
            payload["dry_run"] = False
            self._send(AcquisitionMessage.PLATE_PROGRESS, payload)
        self._set_state(self.STATE_LASER_TEST)
        self._send(
            AcquisitionMessage.STATUS,
            "Laser pattern test started. Spectrometer trigger and spectrum saving are disabled.",
        )

    def _dry_run_progress_payload(
        self,
        plate_index: int,
        dry_visited_by_plate: dict[int, set[str]],
        dry_position_counts_by_plate: dict[int, int],
        *,
        current_well: str | None,
        visited_positions: int,
        total_positions: int,
    ) -> dict:
        payload = self._progress_payload_for_plate(plate_index)
        visited = sorted(dry_visited_by_plate.get(plate_index, set()))
        plate_targets = [
            target
            for target in representative_dry_run_targets(self.automation_config)
            if target.plate_index == plate_index
        ] if self.automation_config is not None else []
        plate_total = len(plate_targets)
        plate_visited_positions = int(dry_position_counts_by_plate.get(plate_index, len(visited)))
        payload["shots_by_well"] = {}
        payload["complete_wells"] = len(visited)
        payload["total_wells"] = plate_total or payload["total_wells"]
        payload["complete"] = bool(plate_total and plate_visited_positions >= plate_total)
        payload["saved_shots"] = 0
        payload["current_well"] = None if payload["complete"] else current_well
        payload["can_discard"] = False
        payload["dry_run"] = True
        payload["dry_run_visited_wells"] = visited
        payload["dry_run_visited_positions"] = int(visited_positions)
        payload["dry_run_total_positions"] = int(total_positions)
        payload["dry_run_plate_visited_positions"] = plate_visited_positions
        payload["dry_run_plate_total_positions"] = plate_total
        return payload

    def _laser_test_progress_payload(
        self,
        plate_index: int,
        fired_by_plate: dict[int, dict[str, int]],
        *,
        current_well: str | None,
        fired_positions: int,
        total_positions: int,
    ) -> dict:
        payload = self._progress_payload_for_plate(plate_index)
        plate_counts = dict(fired_by_plate.get(plate_index, {}))
        plate_targets = [
            target
            for target in automation_targets(self.automation_config)
            if target.plate_index == plate_index
        ] if self.automation_config is not None else []
        plate_total_positions = len(plate_targets)
        complete_wells = sum(
            1
            for well, count in plate_counts.items()
            if self.automation_config is not None and count >= self.automation_config.shots_per_well
        )
        payload["shots_by_well"] = {}
        payload["complete_wells"] = complete_wells
        payload["saved_shots"] = 0
        payload["current_well"] = None if fired_positions >= total_positions else current_well
        payload["can_discard"] = False
        payload["laser_test"] = True
        payload["laser_test_fired_by_well"] = plate_counts
        payload["laser_test_fired_positions"] = int(fired_positions)
        payload["laser_test_total_positions"] = int(total_positions)
        payload["laser_test_plate_fired_positions"] = sum(plate_counts.values())
        payload["laser_test_plate_total_positions"] = int(plate_total_positions)
        payload["complete"] = bool(plate_total_positions and sum(plate_counts.values()) >= plate_total_positions)
        return payload

    def queue_qc_repeats(self, failed_wells: list[dict] | tuple[dict, ...]) -> dict:
        """Move accepted failed-well spectra aside and queue those wells for a manual restart."""
        config = self.automation_config
        if config is None:
            raise RuntimeError("Configure an automated plate plan before queueing QC repeats.")
        if self.state != self.STATE_IDLE:
            raise RuntimeError("Stop automated acquisition before queueing QC repeats.")

        requested: dict[int, set[str]] = {}
        for item in failed_wells or []:
            try:
                plate_index = int(item.get("plate_index"))
            except (AttributeError, TypeError, ValueError):
                continue
            well = str(item.get("well", "")).strip().upper()
            if plate_index in self._plate_states and well:
                requested.setdefault(plate_index, set()).add(well)

        if not requested:
            raise ValueError("No failed wells were selected for repeat acquisition.")

        all_targets = automation_targets(config)
        repeat_targets = [
            target
            for target in all_targets
            if target.plate_index in requested and target.well in requested[target.plate_index]
        ]
        if not repeat_targets:
            raise ValueError("Selected failed wells are not part of the current automated plan.")

        queued_wells = 0
        with self._automation_lock:
            for plate_index in sorted(requested):
                self._set_active_plate(plate_index)
                state = self._plate_states[plate_index]
                selected_wells = [well for well in state.config.ordered_wells if well in requested[plate_index]]
                if not selected_wells:
                    continue
                plate_dir = os.path.join(self.save_directory, state.config.safe_plate_name)
                discarded_dir = os.path.join(plate_dir, "Discarded", "QC")
                removed, payload = state.start_repair(selected_wells, discarded_dir)
                queued_wells += len({record.well for record in removed}) or len(selected_wells)
                payload.update(self._progress_payload_for_plate(plate_index))
                self._persist_plate_state_locked()
                self._persist_plate_reproducibility_log(event="qc_repair_queued")
                self._send(AcquisitionMessage.PLATE_REPAIR_STARTED, payload)
                self._send(AcquisitionMessage.PLATE_PROGRESS, payload)

            if self.automation_state is not None:
                for target in repeat_targets:
                    self.automation_state.completed_targets.discard(target.key)
                self.automation_state.abort_reason = None
                self.automation_state.active_target = None
            self._qc_repeat_targets = repeat_targets
            self._persist_automation_state(event="qc_repeats_queued")

        self._send(
            AcquisitionMessage.STATUS,
            f"Queued {queued_wells} failed well(s) for repeat. Start guarded firing when ready.",
        )
        return {"queued_wells": queued_wells, "targets": len(repeat_targets)}

    def _ready_for_motion(self) -> bool:
        if not self.spec.is_connected:
            self._send(AcquisitionMessage.ERROR, "Spectrometer not connected.")
            return False
        if self.laser is None or not getattr(self.laser, "is_connected", False):
            self._send(AcquisitionMessage.ERROR, "GRBL laser is not connected.")
            return False
        return True

    def _grbl_preparation_cancelled(self) -> bool:
        return (
            self.state not in {self.STATE_DRY_RUN, self.STATE_AUTOMATED, self.STATE_LASER_TEST}
            or self._stop_event.is_set()
        )

    def _active_run_targets_for_validation(self, config) -> tuple[list[tuple[float, float]], list[tuple[str, float, float]]]:
        travel_points: list[tuple[float, float]] = []
        motion_safety_points: list[tuple[str, float, float]] = []
        if isinstance(config, MappingGridConfig):
            for target in representative_mapping_dry_run_targets(config):
                travel_points.append((float(target.x_mm), float(target.y_mm)))
                motion_safety_points.append((f"Mapping {target.point_key}", float(target.x_mm), float(target.y_mm)))
                if self._relay_profile_enabled(config) and config.relay_stroke_mm > 0:
                    end_x, end_y = mapping_relay_stroke_endpoint(config, target)
                    travel_points.append((float(end_x), float(end_y)))
                    motion_safety_points.append((f"Mapping {target.point_key} relay endpoint", float(end_x), float(end_y)))
            return travel_points, motion_safety_points
        for target in automation_targets(config):
            travel_points.append((float(target.x_mm), float(target.y_mm)))
            motion_safety_points.append((f"Plate {target.plate_index} {target.well}", float(target.x_mm), float(target.y_mm)))
            if self._relay_profile_enabled(config) and config.relay_stroke_mm > 0:
                end_x, end_y = relay_stroke_endpoint(config, target)
                travel_points.append((float(end_x), float(end_y)))
                motion_safety_points.append((f"Plate {target.plate_index} {target.well} relay endpoint", float(end_x), float(end_y)))
        return travel_points, motion_safety_points

    def _validate_laser_controller_settings_for_run(self, laser, *, context: str, motion_only: bool) -> None:
        config = self._active_motion_config()
        if config is None:
            return
        read_settings = getattr(laser, "read_grbl_settings", None)
        keys = {"30", "130", "131"}
        if isinstance(config, MappingGridConfig):
            keys.update({"110", "111"})
        relay_profile = self._relay_profile_enabled(config)
        if relay_profile:
            keys.add("32")
        settings = {}
        try:
            if callable(read_settings):
                settings = read_settings(keys)
        except Exception as exc:
            if bool(getattr(laser, "reconnect_required", False)):
                try:
                    _resync_grbl_command_stream(
                        laser,
                        context=context,
                        send_status=lambda message: self._send(AcquisitionMessage.STATUS, message),
                    )
                except Exception as resync_exc:
                    raise RuntimeError(f"Laser controller requires reconnect before {context}: {exc}") from resync_exc
            self._send(AcquisitionMessage.STATUS, f"Controller settings unavailable before {context}; continuing.")

        s_max = settings.get("30")
        if s_max is not None and abs(float(s_max) - float(config.laser_s_max)) > 1e-6:
            if relay_profile:
                if motion_only:
                    self._send(
                        AcquisitionMessage.STATUS,
                        (
                            f"Controller $30 ({float(s_max):g}) differs from configured S max "
                            f"({config.laser_s_max}); relay firing will use controller $30 before firing."
                        ),
                    )
                else:
                    self._apply_relay_controller_s_max(float(s_max), context=context)
                    config = self._active_motion_config() or config
            elif motion_only:
                self._send(
                    AcquisitionMessage.STATUS,
                    (
                        f"Controller $30 ({float(s_max):g}) does not match configured S max "
                        f"({config.laser_s_max}); dry-run motion can continue, but fix this before firing."
                    ),
                )
            else:
                raise RuntimeError(
                    f"Controller $30 ({float(s_max):g}) does not match configured S max ({config.laser_s_max})."
                )

        if isinstance(config, MappingGridConfig):
            self._mapping_effective_move_speed_mm_min = effective_mapping_move_speed_mm_min(
                config.move_speed_mm_min,
                settings,
            )

        if relay_profile:
            laser_mode = settings.get("32")
            if laser_mode is None:
                self._send(
                    AcquisitionMessage.STATUS,
                    "Controller $32 laser mode setting unavailable; continuing, but relay firing expects $32=1.",
                )
            elif int(round(float(laser_mode))) != 1:
                if motion_only:
                    self._send(
                        AcquisitionMessage.STATUS,
                        f"Controller $32 ({float(laser_mode):g}) is not laser mode; dry-run motion can continue, but set $32=1 before relay firing.",
                    )
                else:
                    raise RuntimeError(
                        f"Controller $32 ({float(laser_mode):g}) is not laser mode. Set $32=1 before Monport relay firing."
                    )

        if isinstance(config, MappingGridConfig):
            bed_x_min, bed_y_min, bed_x_max, bed_y_max = mapping_bounds(config)
        else:
            bed = effective_bed_area(config)
            bed_x_min = bed.x_min_mm
            bed_x_max = bed.x_max_mm
            bed_y_min = bed.y_min_mm
            bed_y_max = bed.y_max_mm
        travel_points, motion_safety_points = self._active_run_targets_for_validation(config)
        # Anything that does not explicitly declare itself simulated is treated as a
        # real beam, so motion-safety validation and machine-limit checks still run.
        simulated_laser = bool(getattr(laser, "is_simulated", False))
        if not simulated_laser:
            validate_motion_safety_points(
                motion_safety_points,
                limits=load_motion_safety_limits(),
                grbl_settings=settings,
            )
        x_limit = None if simulated_laser else settings.get("130")
        if x_limit is not None:
            limit = float(x_limit)
            if travel_points:
                min_x = min(point[0] for point in travel_points)
                max_x = max(point[0] for point in travel_points)
                if min_x < -1e-6 or max_x > limit + 1e-6:
                    raise RuntimeError(
                        f"Calibrated target X range ({min_x:g} to {max_x:g} mm) exceeds controller $130 travel ({limit:g} mm)."
                    )
                if bed_x_min < -1e-6 or bed_x_max > limit + 1e-6:
                    self._send(
                        AcquisitionMessage.STATUS,
                        (
                            f"Plan outline X range ({bed_x_min:g} to {bed_x_max:g} mm) extends outside "
                            f"controller $130 travel ({limit:g} mm); continuing because planned targets are inside travel."
                        ),
                    )
            elif bed_x_min < -1e-6 or bed_x_max > limit + 1e-6:
                raise RuntimeError(
                    f"Plan X range ({bed_x_min:g} to {bed_x_max:g} mm) exceeds controller $130 travel ({limit:g} mm)."
                )
        y_limit = None if simulated_laser else settings.get("131")
        if y_limit is not None:
            limit = float(y_limit)
            if travel_points:
                min_y = min(point[1] for point in travel_points)
                max_y = max(point[1] for point in travel_points)
                if min_y < -1e-6 or max_y > limit + 1e-6:
                    raise RuntimeError(
                        f"Calibrated target Y range ({min_y:g} to {max_y:g} mm) exceeds controller $131 travel ({limit:g} mm)."
                    )
                if bed_y_min < -1e-6 or bed_y_max > limit + 1e-6:
                    self._send(
                        AcquisitionMessage.STATUS,
                        (
                            f"Plan outline Y range ({bed_y_min:g} to {bed_y_max:g} mm) extends outside "
                            f"controller $131 travel ({limit:g} mm); continuing because planned targets are inside travel."
                        ),
                    )
            elif bed_y_min < -1e-6 or bed_y_max > limit + 1e-6:
                raise RuntimeError(
                    f"Plan Y range ({bed_y_min:g} to {bed_y_max:g} mm) exceeds controller $131 travel ({limit:g} mm)."
                )

    def _prepare_laser_for_run(self, laser, *, motion_only: bool, context: str) -> None:
        try:
            prepare_grbl_controller(
                laser,
                intent=GRBL_PREP_MOTION if motion_only else GRBL_PREP_FIRING,
                context=context,
                allow_p_input_for_firing=self._allow_p_input_for_config(self.automation_config),
                force_home=False,
                send_status=lambda message: self._send(AcquisitionMessage.STATUS, message),
                should_stop=self._grbl_preparation_cancelled,
            )
        except RuntimeError as exc:
            if "P input active" in str(exc) and not motion_only:
                raise RuntimeError(f"{exc}{self._p_input_policy_context(self.automation_config)}") from exc
            raise
        self._validate_laser_controller_settings_for_run(laser, context=context, motion_only=motion_only)

    def _verify_guarded_fire_interlock(self, laser, *, context: str) -> None:
        try:
            verify_grbl_firing_interlock(
                laser,
                context=context,
                allow_p_input_for_firing=self._allow_p_input_for_config(self.automation_config),
                send_status=lambda message: self._send(AcquisitionMessage.STATUS, message),
            )
        except RuntimeError as exc:
            if "P input active" in str(exc):
                raise RuntimeError(f"{exc}{self._p_input_policy_context(self.automation_config)}") from exc
            raise

    def go_idle(self):
        self._pause_requested.clear()
        super().go_idle()

    def stop(self):
        self._pause_requested.clear()
        super().stop()

    def run(self):
        logger.info("Automated acquisition worker started.")
        try:
            while not self._stop_event.is_set():
                current_state = self.state
                if current_state == self.STATE_LIVE:
                    self._run_live()
                elif current_state == self.STATE_ARMED:
                    self._run_armed()
                elif current_state == self.STATE_TEST:
                    self._run_test()
                elif current_state == self.STATE_DRY_RUN:
                    if self.mapping_config is not None:
                        self._run_mapping_dry_run()
                    else:
                        self._run_dry_run()
                elif current_state == self.STATE_AUTOMATED:
                    if self.mapping_config is not None:
                        self._run_mapping()
                    else:
                        self._run_automated()
                elif current_state == self.STATE_LASER_TEST:
                    self._run_laser_pattern_test()
                else:
                    self._state_change_event.wait(timeout=0.5)
                    self._state_change_event.clear()
        except Exception as exc:
            logger.exception("Automated worker thread exception.")
            self._send(AcquisitionMessage.ERROR, f"Automated worker error: {exc}")
        finally:
            self._finalize_laser_interruption(context="worker_shutdown")
            self._send(AcquisitionMessage.STOPPED, None)
            logger.info("Automated acquisition worker stopped.")

    def _run_dry_run(self) -> None:
        self._laser_interruption_finalized = False
        config = self.automation_config
        laser = self.laser
        if config is None or laser is None:
            self._send(AcquisitionMessage.ERROR, "Automated dry run is not configured.")
            self._set_state(self.STATE_IDLE)
            self._send(AcquisitionMessage.IDLE, None)
            return

        targets = representative_dry_run_targets(config)
        dry_visited_by_plate: dict[int, set[str]] = {}
        dry_position_counts_by_plate: dict[int, int] = {}
        paused = False
        try:
            self._prepare_laser_for_run(laser, motion_only=True, context="dry run")
            for index, target in enumerate(targets, start=1):
                if self._pause_requested.is_set():
                    paused = True
                    self._send(AcquisitionMessage.STATUS, "Dry run paused before the next move.")
                    break
                if self.state != self.STATE_DRY_RUN or self._stop_event.is_set():
                    self._send(AcquisitionMessage.STATUS, "Dry run stopped.")
                    break
                offset_suffix = f" ({target.shot_offset_label})" if target.shot_offset_label else ""
                self._send(
                    AcquisitionMessage.STATUS,
                    f"Dry run {index}/{len(targets)}: plate {target.plate_index} {target.well}{offset_suffix}",
                )
                motion_timeout_s = self._motion_timeout_s(laser, config, target)
                laser.move_to(target.x_mm, target.y_mm, feed_mm_min=config.move_speed_mm_min)
                self._wait_for_laser_idle(laser, timeout_s=motion_timeout_s, state_name=self.STATE_DRY_RUN)
                if self._relay_profile_enabled(config):
                    end_x, end_y = relay_stroke_endpoint(config, target)
                    stroke_target = AutomationTarget(
                        target.plate_index,
                        target.well,
                        target.shot_number,
                        end_x,
                        end_y,
                        target.shot_offset_label,
                        target.shot_offset_dx_mm,
                        target.shot_offset_dy_mm,
                    )
                    stroke_timeout_s = self._motion_timeout_s(laser, config, stroke_target)
                    laser.move_to(end_x, end_y, feed_mm_min=config.move_speed_mm_min)
                    self._wait_for_laser_idle(laser, timeout_s=stroke_timeout_s, state_name=self.STATE_DRY_RUN)
                dry_visited_by_plate.setdefault(target.plate_index, set()).add(target.well)
                dry_position_counts_by_plate[target.plate_index] = (
                    dry_position_counts_by_plate.get(target.plate_index, 0) + 1
                )
                self._send(
                    AcquisitionMessage.PLATE_PROGRESS,
                    self._dry_run_progress_payload(
                        target.plate_index,
                        dry_visited_by_plate,
                        dry_position_counts_by_plate,
                        current_well=target.well,
                        visited_positions=index,
                        total_positions=len(targets),
                    ),
                )
                if self._pause_requested.is_set():
                    paused = True
                    self._send(AcquisitionMessage.STATUS, "Dry run paused after reaching a safe position.")
                    break
            else:
                self.completed_dry_run_signature = config.dry_run_signature()
                if self.automation_state is not None:
                    self.automation_state.abort_reason = None
                    self.automation_state.active_target = None
                    self.automation_state.dry_run_signature = self.completed_dry_run_signature
                    self._persist_automation_state(event="dry_run_complete")
                self._send(AcquisitionMessage.STATUS, "Dry run complete. Full run is enabled.")
        except AutomationPaused as exc:
            paused = True
            self._send(AcquisitionMessage.STATUS, str(exc) or "Dry run paused.")
        except Exception as exc:
            self.completed_dry_run_signature = None
            if self.automation_state is not None:
                self.automation_state.record_abort(f"Dry run failed: {exc}")
                self._persist_automation_state(event="dry_run_failed")
            self._send(AcquisitionMessage.ERROR, f"Dry run failed: {exc}")
        finally:
            self._finalize_laser_interruption(context="dry_run_paused" if paused else "dry_run_finished")
            if paused and self.automation_state is not None:
                self.automation_state.abort_reason = None
                self.automation_state.active_target = None
                self._persist_automation_state(event="dry_run_paused")
            if paused:
                self._pause_requested.clear()
            self._set_state(self.STATE_IDLE)
            self._send(AcquisitionMessage.IDLE, None)

    def _mapping_progress_payload(
        self,
        *,
        current_target: MappingGridTarget | None = None,
        completed_target: MappingGridTarget | None = None,
        dry_run: bool = False,
        dry_run_visited_positions: int = 0,
        dry_run_total_positions: int = 0,
        include_static_plan: bool = False,
    ) -> dict:
        config = self.mapping_config
        if config is None:
            return {}
        with self._mapping_state_lock:
            payload = mapping_progress_payload(
                config,
                self.mapping_state,
                current_target=current_target,
                completed_target=completed_target,
                dry_run=dry_run,
                dry_run_visited_positions=dry_run_visited_positions,
                dry_run_total_positions=dry_run_total_positions,
                include_static_plan=include_static_plan,
            )
        started_at = getattr(self, "_mapping_run_started_at", None)
        if payload and started_at is not None and not dry_run:
            completed_now = (
                len(self.mapping_state.completed_targets) if self.mapping_state is not None else 0
            )
            payload["run_elapsed_s"] = max(0.0, time.perf_counter() - started_at)
            payload["completed_since_start"] = max(
                0, completed_now - int(getattr(self, "_mapping_completed_at_run_start", 0) or 0)
            )
        return payload

    def _run_mapping_dry_run(self) -> None:
        self._laser_interruption_finalized = False
        config = self.mapping_config
        laser = self.laser
        if config is None or laser is None:
            self._send(AcquisitionMessage.ERROR, "Mapping dry run is not configured.")
            self._set_state(self.STATE_IDLE)
            self._send(AcquisitionMessage.IDLE, None)
            return

        targets = representative_mapping_dry_run_targets(config)
        paused = False
        try:
            self._prepare_laser_for_run(laser, motion_only=True, context="mapping dry run")
            move_speed_mm_min = self._effective_mapping_move_speed_mm_min(laser)
            for index, target in enumerate(targets, start=1):
                if self._pause_requested.is_set():
                    paused = True
                    self._send(AcquisitionMessage.STATUS, "Mapping dry run paused before the next move.")
                    break
                if self.state != self.STATE_DRY_RUN or self._stop_event.is_set():
                    self._send(AcquisitionMessage.STATUS, "Mapping dry run stopped.")
                    break
                self._send(
                    AcquisitionMessage.STATUS,
                    (
                        f"Mapping dry run {index}/{len(targets)}: "
                        f"R{target.row_index:03d} C{target.column_index:03d} "
                        f"at X{target.x_mm:.3f} Y{target.y_mm:.3f}."
                    ),
                )
                motion_timeout_s = self._motion_timeout_s(laser, config, target)
                laser.move_to(target.x_mm, target.y_mm, feed_mm_min=move_speed_mm_min)
                self._wait_for_laser_idle(
                    laser,
                    timeout_s=motion_timeout_s,
                    state_name=self.STATE_DRY_RUN,
                    poll_interval_s=MAPPING_IDLE_POLL_INTERVAL_S,
                    status_timeout_s=MAPPING_IDLE_STATUS_TIMEOUT_S,
                )
                if self._relay_profile_enabled(config):
                    end_x, end_y = mapping_relay_stroke_endpoint(config, target)
                    stroke_target = MappingGridTarget(
                        target.row_index,
                        target.column_index,
                        target.shot_number,
                        end_x,
                        end_y,
                    )
                    stroke_timeout_s = self._motion_timeout_s(laser, config, stroke_target)
                    laser.move_to(end_x, end_y, feed_mm_min=move_speed_mm_min)
                    self._wait_for_laser_idle(
                        laser,
                        timeout_s=stroke_timeout_s,
                        state_name=self.STATE_DRY_RUN,
                        poll_interval_s=MAPPING_IDLE_POLL_INTERVAL_S,
                        status_timeout_s=MAPPING_IDLE_STATUS_TIMEOUT_S,
                    )
                self._send(
                    AcquisitionMessage.PLATE_PROGRESS,
                    self._mapping_progress_payload(
                        current_target=target,
                        dry_run=True,
                        dry_run_visited_positions=index,
                        dry_run_total_positions=len(targets),
                    ),
                )
                if self._pause_requested.is_set():
                    paused = True
                    self._send(AcquisitionMessage.STATUS, "Mapping dry run paused after reaching a safe position.")
                    break
            else:
                self.completed_dry_run_signature = config.dry_run_signature()
                if self.mapping_state is not None:
                    self.mapping_state.abort_reason = None
                    self.mapping_state.active_target = None
                    self.mapping_state.dry_run_signature = self.completed_dry_run_signature
                    self._persist_mapping_state(event="dry_run_complete")
                self._send(AcquisitionMessage.STATUS, "Mapping dry run complete. Full run is enabled.")
        except AutomationPaused as exc:
            paused = True
            self._send(AcquisitionMessage.STATUS, str(exc) or "Mapping dry run paused.")
        except Exception as exc:
            self.completed_dry_run_signature = None
            if self.mapping_state is not None:
                self.mapping_state.record_abort(f"Mapping dry run failed: {exc}")
                self._persist_mapping_state(event="dry_run_failed")
            self._send(AcquisitionMessage.ERROR, f"Mapping dry run failed: {exc}")
        finally:
            self._finalize_laser_interruption(context="mapping_dry_run_paused" if paused else "mapping_dry_run_finished")
            if paused and self.mapping_state is not None:
                self.mapping_state.abort_reason = None
                self.mapping_state.active_target = None
                self._persist_mapping_state(event="dry_run_paused")
            if paused:
                self._pause_requested.clear()
            self._set_state(self.STATE_IDLE)
            self._send(AcquisitionMessage.IDLE, None)

    def _drain_spectrometer_trigger_backlog(self, context: str) -> None:
        """Discard stale frames from the spectrometer's trigger FIFO.

        Spurious extra trigger edges (relay bounce, flash-lamp EMI) leave
        unread frames buffered in the SDK; every later armed read then
        returns a frame one shot staler, which shears mapping grids sideways
        and shifts plate well assignments (diagnosed on the 2026-07-26 coin
        runs; see docs/trigger_fifo_desync.md). Draining is only safe while no
        capture is armed and no shot is in flight, so callers invoke it at run
        starts and row/segment boundaries. The terminating empty read costs
        ~1 s in hardware-trigger mode; outside that mode the backend no-ops.
        """
        if self._mapping_trigger_drain_unsupported:
            return
        spec = self.spec
        if spec is None or not spec.is_connected:
            return
        ext_mode = spec.capabilities.external_trigger_mode
        if ext_mode is None:
            return
        try:
            if spec.current_trigger_mode != ext_mode:
                self._set_trigger_mode(ext_mode)
            drained = spec.drain_buffered_frames()
        except Exception as exc:
            # One warning per run: a backend/broker that cannot drain will fail
            # identically at every boundary, and the run must not slow or spam
            # for a protection it cannot have.
            self._mapping_trigger_drain_unsupported = True
            logger.warning(
                "Trigger-FIFO drain unavailable (%s at %s); continuing without "
                "desync protection for this run.",
                exc,
                context,
            )
            return
        if drained:
            self._mapping_stale_frames_drained += drained
            # Persisted with the run summary: each entry is the exact frame lag
            # that accumulated since the previous drain, so an affected run can
            # be re-indexed exactly without shape priors.
            self._mapping_trigger_drain_events.append(
                {
                    "context": context,
                    "drained": int(drained),
                    "total": int(self._mapping_stale_frames_drained),
                    "recorded_at": datetime.now().isoformat(timespec="milliseconds"),
                }
            )
            logger.warning(
                "Discarded %d stale trigger frame(s) at %s (%d total this run). "
                "The spectrometer trigger line is producing extra edges; check for "
                "relay bounce or EMI.",
                drained,
                context,
                self._mapping_stale_frames_drained,
            )
            self._send(
                AcquisitionMessage.STATUS,
                f"Discarded {drained} stale trigger frame(s) at {context}; "
                "map alignment protected.",
            )
            if (
                self._mapping_stale_frames_drained >= MAPPING_TRIGGER_DRAIN_ALERT_FRAMES
                and not self._mapping_trigger_drain_alert_sent
            ):
                self._mapping_trigger_drain_alert_sent = True
                self._send(
                    AcquisitionMessage.STATUS,
                    (
                        f"WARNING: {self._mapping_stale_frames_drained} stale trigger frames "
                        "discarded this run. The trigger line is misfiring often — check the "
                        "trigger wiring/debounce and consider slower repeat-shot spacing "
                        "(rapid bursts correlate with these extra edges)."
                    ),
                )

    def _run_mapping(self) -> None:
        self._laser_interruption_finalized = False
        config = self.mapping_config
        laser = self.laser
        if config is None or laser is None:
            self._send(AcquisitionMessage.ERROR, "Mapping acquisition is not configured.")
            self._set_state(self.STATE_IDLE)
            self._send(AcquisitionMessage.IDLE, None)
            return

        simulation_mode = self._automation_run_mode == RUN_MODE_SIMULATION
        targets = mapping_grid_targets(config)
        completed_all = False
        stopped_early = False
        paused = False
        abort_reason_pending: str | None = None
        trigger_executor: ThreadPoolExecutor | None = None
        try:
            self._prepare_laser_for_run(
                laser,
                motion_only=simulation_mode,
                context="simulated mapping" if simulation_mode else "mapping guarded firing",
            )
            config = self.mapping_config or config
            self._mapping_effective_move_speed_mm_min = self._effective_mapping_move_speed_mm_min(laser)
            # Live-rate ETA baseline: rate is measured per session so resumed
            # runs do not count previously completed targets as this session's.
            self._mapping_run_started_at = time.perf_counter()
            self._last_mapping_cycle_end = None
            self._mapping_stream_stats = None
            self._mapping_completed_at_run_start = (
                len(self.mapping_state.completed_targets) if self.mapping_state is not None else 0
            )
            self._capture_mapping_background_reference(laser, simulation_mode=simulation_mode)
            if not simulation_mode:
                trigger_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="MappingTriggerRead")
                self._mapping_trigger_executor_creations += 1
                self._drain_spectrometer_trigger_backlog("run start")
            self._mapping_save_writer = MappingSaveWriter()
            if config.streaming_enabled:
                completed_all, stopped_early, paused = self._run_mapping_streamed_loop(
                    config, targets, laser, simulation_mode=simulation_mode
                )
            else:
                completed_all, stopped_early, paused = self._run_mapping_sequential_loop(
                    config, targets, laser, simulation_mode=simulation_mode, trigger_executor=trigger_executor
                )
            if completed_all:
                self._send(
                    AcquisitionMessage.STATUS,
                    "Simulated mapping acquisition complete." if simulation_mode else "2D mapping acquisition complete.",
                )
        except AutomationStopped as exc:
            abort_reason_pending = str(exc)
            if self.mapping_state is not None:
                self.mapping_state.record_abort(str(exc))
                self._persist_mapping_state(event="simulated_mapping_stopped" if simulation_mode else "mapping_stopped")
            self._send(AcquisitionMessage.STATUS, "Mapping acquisition stopped.")
        except AutomationPaused as exc:
            paused = True
            self._send(AcquisitionMessage.STATUS, str(exc) or "Mapping acquisition paused.")
        except Exception as exc:
            abort_reason_pending = str(exc)
            if self.mapping_state is not None:
                self.mapping_state.record_abort(str(exc))
                self._persist_mapping_state(event="simulated_mapping_error" if simulation_mode else "mapping_error")
            self._send(
                AcquisitionMessage.ERROR,
                (
                    f"Simulated mapping stopped: {exc}."
                    if simulation_mode
                    else f"Mapping acquisition stopped: {exc}. Use the physical e-stop if software stop does not halt the laser."
                ),
            )
        finally:
            if paused:
                interruption_context = "simulated_mapping_paused" if simulation_mode else "mapping_paused"
            elif stopped_early:
                interruption_context = "simulated_mapping_stopped" if simulation_mode else "mapping_stopped"
            elif completed_all:
                interruption_context = "simulated_mapping_complete" if simulation_mode else "mapping_complete"
            else:
                interruption_context = "simulated_mapping_interrupted" if simulation_mode else "mapping_interrupted"
            self._finalize_laser_interruption(context=interruption_context)
            # Drain the save writer before any terminal state/summary write so
            # persisted state and the on-disk store agree.
            save_writer = self._mapping_save_writer
            self._mapping_save_writer = None
            if save_writer is not None:
                close_error = save_writer.close()
                if close_error is not None:
                    if completed_all:
                        # Never persist a clean completion over unsaved shots;
                        # resume re-shoots whatever the writer dropped.
                        completed_all = False
                    self._send(
                        AcquisitionMessage.ERROR,
                        f"Mapping save writer did not finish cleanly: {close_error}. "
                        "Unsaved shots stay unrecorded and will be re-shot on resume.",
                    )
            if paused and self.mapping_state is not None:
                self.automation_paused = True
                self.mapping_state.abort_reason = None
                self.mapping_state.active_target = None
                self._finalize_pause_event(interruption_context)
                self._persist_mapping_state(event="simulated_mapping_paused" if simulation_mode else "mapping_paused")
            elif stopped_early and self.mapping_state is not None:
                self.automation_paused = False
                self.mapping_state.record_abort("Mapping acquisition stopped by user.")
                self._persist_mapping_state(event="simulated_mapping_stopped" if simulation_mode else "mapping_stopped")
            elif completed_all and self.mapping_state is not None:
                self.automation_paused = False
                self.mapping_state.abort_reason = None
                self.mapping_state.active_target = None
                self._persist_mapping_state(event="simulated_mapping_complete" if simulation_mode else "mapping_complete")
            elif abort_reason_pending and self.mapping_state is not None:
                # The abort was recorded before the writer drained; batched
                # completions during the drain clear abort_reason, and drained
                # completions themselves need a terminal persist. Re-record
                # (only if the drain actually cleared it, to avoid duplicate
                # abort events) and persist now that the writer is closed.
                if self.mapping_state.abort_reason != abort_reason_pending:
                    self.mapping_state.record_abort(abort_reason_pending)
                self._persist_mapping_state(event="simulated_mapping_interrupted" if simulation_mode else "mapping_interrupted")
            # Close (and therefore flush) the store before the summary: the
            # summary reads written_mask.npy from disk and must see the final
            # deferred mask bits, including on error paths that skip the
            # terminal state persists above. Guarded: a close-time disk error
            # must not skip the summary, idle transition, or trigger restore.
            if self._mapping_spectrum_store is not None:
                try:
                    self._mapping_spectrum_store.close()
                except Exception as exc:
                    logger.exception("Failed to close the mapping spectrum store.")
                    self._send(
                        AcquisitionMessage.ERROR,
                        f"Mapping spectrum store did not close cleanly: {exc}. "
                        "Recently captured shots may be re-shot on resume.",
                    )
                self._mapping_spectrum_store = None
            self._persist_mapping_summary()
            if paused:
                self._pause_requested.clear()
            if self.spec.is_connected:
                if self.hardware_read_active:
                    self._send(
                        AcquisitionMessage.ERROR,
                        "Spectrometer read is still active. Reconnect spectrometer before continuing.",
                    )
                else:
                    try:
                        self._set_trigger_mode(self.spec.capabilities.normal_trigger_mode)
                    except Exception:
                        logger.warning("Failed to restore normal trigger mode after run.", exc_info=True)
                        self._send(
                            AcquisitionMessage.ERROR,
                            "Failed to restore normal trigger mode after the run. "
                            "Reconnect the spectrometer before the next capture.",
                        )
                    self._clear_sweep_trigger_delay()
            self._set_state(self.STATE_IDLE)
            self._send(AcquisitionMessage.IDLE, None)
            if trigger_executor is not None:
                trigger_executor.shutdown(wait=False, cancel_futures=True)

    def _run_mapping_sequential_loop(
        self,
        config: MappingGridConfig,
        targets: list[MappingGridTarget],
        laser,
        *,
        simulation_mode: bool,
        trigger_executor: ThreadPoolExecutor | None,
    ) -> tuple[bool, bool, bool]:
        """Per-target stop-and-confirm loop; returns (completed_all, stopped_early, paused)."""
        # Burst tracking: repeat shots at a point this session just made are
        # follow-ups (0.2 mm return move, reduced settle). Skipped targets
        # never set the tracker, so the first shot actually executed at a
        # point -- including resume entering mid-point -- gets the full
        # move/idle/settle prologue.
        last_completed_point_key: str | None = None
        previous_row_index: int | None = None
        for target in targets:
            if self._pause_requested.is_set():
                self._record_pause_boundary("before_next_move")
                self._send(AcquisitionMessage.STATUS, "Mapping acquisition paused before the next move.")
                return False, False, True
            if self.state != self.STATE_AUTOMATED or self._stop_event.is_set():
                self._send(AcquisitionMessage.STATUS, "Mapping acquisition stopped.")
                return False, True, False
            writer_failure = self._mapping_save_writer.failure if self._mapping_save_writer is not None else None
            if writer_failure is not None:
                raise RuntimeError(f"Mapping save writer failed: {writer_failure}") from writer_failure
            if self.mapping_state and self.mapping_state.is_target_done(target.key):
                continue
            if (
                not simulation_mode
                and previous_row_index is not None
                and target.row_index != previous_row_index
            ):
                # Row boundary: no shot is in flight, so anything in the
                # trigger FIFO is a stale frame that would desync every
                # later capture by one shot.
                self._drain_spectrometer_trigger_backlog(f"row R{target.row_index:03d} start")
            self._run_mapping_target(
                config,
                target,
                laser,
                trigger_executor=trigger_executor,
                burst_follow_up=(last_completed_point_key == target.point_key),
            )
            last_completed_point_key = target.point_key
            previous_row_index = target.row_index
        return True, False, False

    def _run_mapping_streamed_loop(
        self,
        config: MappingGridConfig,
        targets: list[MappingGridTarget],
        laser,
        *,
        simulation_mode: bool,
    ) -> tuple[bool, bool, bool]:
        """Streamed-row loop: one continuous GRBL job per segment.

        Returns (completed_all, stopped_early, paused). Pause is honored at
        segment boundaries only; stop and interlock pins abort the running
        segment via feed hold + soft reset.
        """
        remaining = [
            target
            for target in targets
            if not (self.mapping_state and self.mapping_state.is_target_done(target.key))
        ]
        if not remaining:
            return True, False, False

        ext_mode = self.spec.capabilities.external_trigger_mode
        if ext_mode is None and not simulation_mode:
            raise RuntimeError(f"{self.spec.model} does not support external trigger capture.")
        trigger_mode = self.spec.capabilities.normal_trigger_mode if simulation_mode else ext_mode
        if trigger_mode is not None and self.spec.current_trigger_mode != trigger_mode:
            self._set_trigger_mode(trigger_mode)

        head_x, head_y, _head_z = getattr(laser, "position", (None, None, None))
        if head_x is None or head_y is None:
            head_x, head_y = remaining[0].x_mm, remaining[0].y_mm
        segments = compile_mapping_stream_segments(
            config,
            remaining,
            cadence_ms=config.stream_cadence_ms,
            start_x_mm=float(head_x),
            start_y_mm=float(head_y),
            s_value=self._mapping_laser_s_value(config),
            max_travel_feed_mm_min=self._effective_mapping_move_speed_mm_min(laser),
        )
        stream_stats = {
            "cadence_ms": float(config.stream_cadence_ms),
            "segments_total": len(segments),
            "segments_completed": 0,
            "segments_discarded": 0,
            "hold_events": 0,
            "frame_mismatches": 0,
        }
        self._mapping_stream_stats = stream_stats

        for segment_index, segment in enumerate(segments):
            if self._pause_requested.is_set():
                self._record_pause_boundary("before_next_segment")
                self._send(AcquisitionMessage.STATUS, "Mapping acquisition paused before the next streamed segment.")
                return False, False, True
            if self.state != self.STATE_AUTOMATED or self._stop_event.is_set():
                self._send(AcquisitionMessage.STATUS, "Mapping acquisition stopped.")
                return False, True, False
            writer_failure = self._mapping_save_writer.failure if self._mapping_save_writer is not None else None
            if writer_failure is not None:
                raise RuntimeError(f"Mapping save writer failed: {writer_failure}") from writer_failure
            if not simulation_mode:
                self._drain_spectrometer_trigger_backlog(f"streamed segment {segment_index + 1} start")
                # Defense-in-depth: anything that reset trigger mode between
                # segments (e.g. broker recovery paths) is corrected at the
                # boundary, where no read is armed. Normally a no-op.
                if trigger_mode is not None and self.spec.current_trigger_mode != trigger_mode:
                    self._set_trigger_mode(trigger_mode)
                self._verify_guarded_fire_interlock(laser, context="before streamed segment")
            first_target = segment.strokes[0].target
            self._send(
                AcquisitionMessage.STATUS,
                (
                    f"Streaming segment {segment_index + 1}/{len(segments)} "
                    f"({len(segment.strokes)} strokes from R{first_target.row_index:03d} C{first_target.column_index:03d})."
                ),
            )
            self._send(AcquisitionMessage.PLATE_PROGRESS, self._mapping_progress_payload(current_target=first_target))
            self._run_mapping_stream_segment(
                config, segment, laser, segment_index=segment_index, simulation_mode=simulation_mode
            )
        return True, False, False

    def _mapping_laser_s_value(self, config: MappingGridConfig) -> int:
        return int(config.laser_s_value)

    def _stream_should_abort(self) -> str | None:
        if self.state != self.STATE_AUTOMATED or self._stop_event.is_set():
            return "Mapping acquisition stopped."
        return self._stream_pin_abort_reason

    def _stream_pin_monitor(self, status) -> None:
        pins = _grbl_active_pins(status)
        blocking = set(_GRBL_CONTROL_PINS.intersection(pins)) | set(_GRBL_LIMIT_PINS.intersection(pins))
        if not self._allow_p_input_for_config(self.automation_config) and _GRBL_PROBE_PIN in pins:
            blocking.add(_GRBL_PROBE_PIN)
        if blocking and self._stream_pin_abort_reason is None:
            self._stream_pin_abort_reason = (
                f"GRBL interlock input(s) active mid-segment ({', '.join(sorted(blocking))})."
            )

    def _run_mapping_stream_segment(
        self,
        config: MappingGridConfig,
        segment: MappingStreamSegment,
        laser,
        *,
        segment_index: int,
        simulation_mode: bool,
    ) -> None:
        stream_stats = self._mapping_stream_stats or {}
        self._stream_pin_abort_reason = None
        read_loop = _MappingStreamReadLoop(self, segment, simulation_mode=simulation_mode)

        def stream_should_abort() -> str | None:
            reason = self._stream_should_abort()
            if reason:
                return reason
            return read_loop.stall_reason(config.capture_timeout_s)

        previous_hook = getattr(laser, "on_fire_stroke", None)
        if simulation_mode and hasattr(laser, "on_fire_stroke"):
            laser.on_fire_stroke = read_loop.notify_simulated_stroke
        try:
            read_loop.start()
        except Exception:
            if simulation_mode and hasattr(laser, "on_fire_stroke"):
                laser.on_fire_stroke = previous_hook
            raise
        result = None
        aborted: GrblStreamAborted | None = None
        stream_error: Exception | None = None
        shutdown_clean: bool | None = None
        try:
            try:
                result = laser.stream_program(
                    segment.lines,
                    fire_gate=read_loop.fire_gate,
                    should_abort=stream_should_abort,
                    on_progress=self._stream_pin_monitor,
                )
            except GrblStreamAborted as exc:
                aborted = exc
                result = getattr(exc, "result", None)
            except Exception as exc:
                # Ack timeout / ALARM / port failure / callback bug: the
                # controller aborted the stream and latched itself safe;
                # reconcile below so fired strokes are still marked missing
                # before the error propagates.
                stream_error = exc
                result = getattr(exc, "result", None)
            finally:
                if simulation_mode and hasattr(laser, "on_fire_stroke"):
                    laser.on_fire_stroke = previous_hook

            stalled = aborted is not None and STREAM_FRAME_STALL_PREFIX in str(aborted)
            strokes_acked = int(getattr(result, "fire_strokes_acked", 0) or 0)
            # GRBL acks at parse time; a stroke can execute without its 'ok'
            # ever being read back. Never-re-shoot decisions use the sent count.
            strokes_sent = int(getattr(result, "fire_strokes_sent", strokes_acked) or 0)
            if result is not None:
                stream_stats["hold_events"] = int(stream_stats.get("hold_events", 0)) + int(result.hold_events)

            frames_ok = read_loop.wait_for_frames(timeout_s=config.capture_timeout_s, expected=strokes_acked)
            shutdown_clean = read_loop.shutdown()

            if frames_ok and read_loop.frames_received == strokes_acked and not stalled:
                # Frames arrive in stroke order, so acked strokes and their
                # frames are a verified matched prefix even when later strokes
                # were sent without their ok being read back (abort mid-ack).
                self._persist_stream_segment_frames(
                    config, segment, read_loop, segment_index=segment_index, result=result
                )
                if strokes_sent > strokes_acked:
                    # Sent-but-unacked tail: execution unknown, never re-shoot.
                    stream_stats["frame_mismatches"] = int(stream_stats.get("frame_mismatches", 0)) + 1
                    stream_stats["segments_partial"] = int(stream_stats.get("segments_partial", 0)) + 1
                    self._discard_stream_segment(
                        config, segment, read_loop, strokes_fired=strokes_sent, first_stroke=strokes_acked
                    )
                elif aborted is None and stream_error is None:
                    stream_stats["segments_completed"] = int(stream_stats.get("segments_completed", 0)) + 1
            else:
                # Frame/stroke mismatch or stall: order-based alignment is broken
                # for this segment, so every fired-but-unmatched target is
                # permanently marked missing (no re-shooting ablated craters).
                # Unfired targets stay unrecorded and are re-shot on resume.
                stream_stats["frame_mismatches"] = int(stream_stats.get("frame_mismatches", 0)) + 1
                stream_stats["segments_discarded"] = int(stream_stats.get("segments_discarded", 0)) + 1
                self._discard_stream_segment(config, segment, read_loop, strokes_fired=strokes_sent)
        finally:
            # Any exception path above (frame wait, persistence, discard) must
            # still stop the read thread, or it leaks holding the hardware lock.
            if shutdown_clean is None:
                read_loop.shutdown()

        if shutdown_clean is False:
            raise RuntimeError(
                "Spectrometer trigger read could not be cancelled after the streamed "
                "segment; reconnect the spectrometer before continuing."
            ) from stream_error
        if not simulation_mode and not bool(getattr(self.spec, "is_connected", True)):
            # The read thread exited only because the broker was released;
            # continuing would fail on the next hardware call with a
            # misleading error. State is already persisted; resume re-shoots
            # only unfired targets.
            raise RuntimeError(
                "Spectrometer was reset while recovering the streamed segment; "
                "reconnect the spectrometer, then resume the run."
            ) from stream_error
        if stream_error is not None:
            raise stream_error
        if aborted is not None and not stalled:
            if self._pause_requested.is_set():
                self._finalize_laser_interruption(context="mapping_paused")
                raise AutomationPaused("Mapping acquisition paused during streamed segment.")
            self._finalize_laser_interruption(context="mapping_stopped")
            raise AutomationStopped(str(aborted) or "Mapping acquisition stopped.")

    def _persist_stream_segment_frames(
        self,
        config: MappingGridConfig,
        segment: MappingStreamSegment,
        read_loop: _MappingStreamReadLoop,
        *,
        segment_index: int,
        result,
    ) -> None:
        # Axis cached by the read loop at start(); a fresh hardware call here
        # could block behind a leaked read thread.
        wavelengths = read_loop.wavelengths
        fire_acks = list(getattr(result, "fire_ack_monotonic", []) or [])
        previous_frame_at: float | None = None
        for k, (frame_at, intensities) in enumerate(read_loop.frames):
            stroke = segment.strokes[k]
            self._check_run_startup_light(intensities)
            self._shot_index += 1
            timing = {
                "mode": self._automation_run_mode,
                "run_type": MAPPING_RUN_TYPE,
                "shot_index": self._shot_index,
                "target_key": stroke.target.key,
                "point_key": stroke.target.point_key,
                "row_index": stroke.target.row_index,
                "column_index": stroke.target.column_index,
                "shot_number": stroke.target.shot_number,
                "effective_move_speed_mm_min": stroke.travel_feed_mm_min,
                "stream_segment_index": segment_index,
                "scheduled_fire_offset_ms": stroke.scheduled_fire_offset_s * 1000.0,
                "hold_events": int(getattr(result, "hold_events", 0) or 0),
                "cycle_start": previous_frame_at if previous_frame_at is not None else frame_at,
                "cycle_end": frame_at,
            }
            if previous_frame_at is not None:
                timing["frame_interval_ms"] = (frame_at - previous_frame_at) * 1000.0
            if k < len(fire_acks):
                timing["trigger_wait_start"] = fire_acks[k]
                timing["trigger_wait_end"] = frame_at
            previous_frame_at = frame_at
            job = self._build_mapping_save_job(config, stroke.target, wavelengths, intensities, timing=timing)
            writer = self._mapping_save_writer
            if writer is not None:
                writer.submit(job)
            else:
                job()

    def _discard_stream_segment(
        self,
        config: MappingGridConfig,
        segment: MappingStreamSegment,
        read_loop: _MappingStreamReadLoop,
        *,
        strokes_fired: int,
        first_stroke: int = 0,
    ) -> None:
        detail = (
            f"strokes fired {strokes_fired}, frames received {read_loop.frames_received}"
            + (f", matched prefix kept: {first_stroke}" if first_stroke else "")
            + (f", read error: {read_loop.read_error}" if read_loop.read_error else "")
        )
        logger.warning("Streamed segment frame mismatch; discarding strokes (%s).", detail)
        self._send(
            AcquisitionMessage.ERROR,
            (
                f"Streamed segment frame mismatch ({detail}). Fired shots are marked "
                "missing and will not be re-shot; acquisition continues with the next segment."
            ),
        )
        # Only strokes that were actually transmitted can have ablated the
        # surface; later strokes never fired and stay eligible for re-shoot.
        # first_stroke skips a verified matched prefix that was persisted.
        for stroke in segment.strokes[max(0, int(first_stroke)) : max(0, int(strokes_fired))]:
            if self.mapping_state is not None:
                if stroke.target.key in self.mapping_state.completed_targets:
                    continue
                with self._mapping_state_lock:
                    self.mapping_state.record_missing(stroke.target.key)
            try:
                self._append_mapping_index_row_locked(
                    self.save_directory,
                    {
                        "target_key": stroke.target.key,
                        "point_key": stroke.target.point_key,
                        "row_index": stroke.target.row_index,
                        "column_index": stroke.target.column_index,
                        "x_mm": f"{stroke.target.x_mm:.6f}",
                        "y_mm": f"{stroke.target.y_mm:.6f}",
                        "shot_number": stroke.target.shot_number,
                        "shot_index": "",
                        "filename": "",
                        "filepath": "",
                        "captured_at": datetime.now().isoformat(timespec="microseconds"),
                        "save_ms": "",
                        "integration_time_us": getattr(self.spec, "integration_time_us", ""),
                        "trigger_delay_us": "",
                        "storage_kind": "binary_npy",
                        "store_dir": "_mapping_spectrum_store",
                        "binary_row_index": "",
                        "storage_uri": "",
                        "binary_written": "false",
                        "shot_quality_state": "",
                        "exclude_reason": "stream_frame_mismatch",
                    },
                )
            except Exception:
                logger.warning("Failed to append missing-target index row.", exc_info=True)
        self._persist_mapping_state(event="stream_segment_discarded")

    MAPPING_BACKGROUND_FRAME_COUNT = 5

    def _capture_mapping_background_reference(self, laser, *, simulation_mode: bool) -> None:
        """Capture a laser-off electronic background reference before the grid.

        Uses normal-trigger-mode reads with the run's integration time and
        corrections (an external-trigger read would block forever with no laser
        pulse). Failures are recorded and the run continues — raw mapping
        spectra are unaffected and analysis simply has no reference to offer.
        """
        if not self._mapping_background_capture_enabled or not self.save_directory:
            return
        try:
            reference_path = mapping_background_reference_path(self.save_directory)
            metadata_path = mapping_background_metadata_path(self.save_directory)
            if os.path.isfile(reference_path):
                # Resumed run: reuse the existing reference instead of recapturing.
                try:
                    with open(metadata_path, "r", encoding="utf-8") as handle:
                        self._mapping_background_info = json.load(handle)
                except (OSError, json.JSONDecodeError, ValueError):
                    self._mapping_background_info = {
                        "reference_path": os.path.basename(reference_path),
                    }
                self._send(AcquisitionMessage.STATUS, "Reusing the existing background reference for this run.")
                return

            self._send(
                AcquisitionMessage.STATUS,
                f"Capturing laser-off background reference ({self.MAPPING_BACKGROUND_FRAME_COUNT} frames)...",
            )
            self._ensure_laser_off_before_motion(laser)
            normal_mode = self.spec.capabilities.normal_trigger_mode
            if normal_mode is not None and self.spec.current_trigger_mode != normal_mode:
                self._set_trigger_mode(normal_mode)
            wavelengths = np.asarray(self._get_wavelengths(), dtype=float)
            frames = [
                np.asarray(self._get_intensities(), dtype=float)
                for _ in range(self.MAPPING_BACKGROUND_FRAME_COUNT)
            ]
            reference = aggregate_background_frames([frame.tolist() for frame in frames], method="median")

            background_dir = mapping_background_dir(self.save_directory)
            os.makedirs(background_dir, exist_ok=True)
            frame_paths = []
            for index, frame in enumerate(frames, start=1):
                frame_path = os.path.join(background_dir, f"background_frame_{index:02d}.csv")
                self._save_spectrum_file(frame_path, wavelengths, frame)
                frame_paths.append(os.path.relpath(frame_path, self.save_directory))
            self._save_spectrum_file(reference_path, wavelengths, np.asarray(reference, dtype=float))

            corrections = self._effective_corrections().to_kwargs()
            info = {
                "reference_path": os.path.basename(reference_path),
                "frame_paths": frame_paths,
                "frame_count": len(frames),
                "aggregation_method": "median",
                "captured_at": datetime.now().isoformat(timespec="milliseconds"),
                "integration_time_us": getattr(self.spec, "integration_time_us", None),
                "correct_dark_counts": bool(corrections.get("correct_dark_counts", False)),
                "correct_nonlinearity": bool(corrections.get("correct_nonlinearity", False)),
                "trigger_mode_used": "normal",
                "laser_state": "simulated" if simulation_mode else "off",
                "wavelength_count": int(wavelengths.size),
                "wavelength_min_nm": float(wavelengths.min()) if wavelengths.size else None,
                "wavelength_max_nm": float(wavelengths.max()) if wavelengths.size else None,
                "spectrometer": self._spectrometer_metadata(),
            }
            tmp_path = f"{metadata_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(info, handle, indent=2, sort_keys=True)
            os.replace(tmp_path, metadata_path)
            self._mapping_background_info = info
            self._persist_mapping_state(event="background_reference_captured")
            self._send(
                AcquisitionMessage.STATUS,
                "Background reference captured (median of "
                f"{len(frames)} laser-off frames).",
            )
        except Exception as exc:
            logger.warning("Background reference capture failed; continuing run.", exc_info=True)
            self._mapping_background_info = {
                "error": str(exc),
                "captured_at": datetime.now().isoformat(timespec="milliseconds"),
            }
            self._send(
                AcquisitionMessage.STATUS,
                f"Background reference capture failed; continuing without it: {exc}",
            )

    def _run_mapping_target(
        self,
        config: MappingGridConfig,
        target: MappingGridTarget,
        laser,
        *,
        trigger_executor: ThreadPoolExecutor | None = None,
        burst_follow_up: bool = False,
    ) -> None:
        simulation_mode = self._automation_run_mode == RUN_MODE_SIMULATION
        move_speed_mm_min = self._effective_mapping_move_speed_mm_min(laser)
        timing = {
            "mode": self._automation_run_mode,
            "run_type": MAPPING_RUN_TYPE,
            "shot_index": self._shot_index + 1,
            "target_key": target.key,
            "point_key": target.point_key,
            "row_index": target.row_index,
            "column_index": target.column_index,
            "shot_number": target.shot_number,
            "target_x_mm": target.x_mm,
            "target_y_mm": target.y_mm,
            "requested_move_speed_mm_min": config.move_speed_mm_min,
            "effective_move_speed_mm_min": move_speed_mm_min,
            "previous_cycle_end": self._last_mapping_cycle_end,
            "cycle_start": time.perf_counter(),
            "burst_shot": 1 if burst_follow_up else 0,
        }
        self._apply_mapping_target_integration_time(config, target, timing)
        self._apply_target_trigger_delay(config, target, timing)
        if self.mapping_state is not None:
            self.mapping_state.record_started(target)

        self._send(
            AcquisitionMessage.STATUS,
            (
                f"Moving to mapping R{target.row_index:03d} C{target.column_index:03d} "
                f"shot {target.shot_number}."
            ),
        )
        self._send(AcquisitionMessage.PLATE_PROGRESS, self._mapping_progress_payload(current_target=target))
        if not burst_follow_up:
            self._ensure_laser_off_before_motion(laser)
        motion_timeout_s = self._motion_timeout_s(laser, config, target)
        timing["motion_start"] = time.perf_counter()
        laser.move_to(target.x_mm, target.y_mm, feed_mm_min=move_speed_mm_min)
        timing["motion_command_end"] = time.perf_counter()
        timing["idle_wait_start"] = time.perf_counter()
        # Burst follow-ups only return 0.2 mm from the relay stroke endpoint and
        # the previous fire already confirmed M5 + Idle; one status poll usually
        # suffices, with the full idle wait as fallback.
        if not burst_follow_up or not self._burst_return_verified(laser, target):
            self._wait_for_laser_idle(
                laser,
                timeout_s=motion_timeout_s,
                state_name=self.STATE_AUTOMATED,
                poll_interval_s=MAPPING_IDLE_POLL_INTERVAL_S,
                status_timeout_s=MAPPING_IDLE_STATUS_TIMEOUT_S,
            )
        timing["idle_wait_end"] = time.perf_counter()
        timing["motion_end"] = timing["idle_wait_end"]
        timing["settle_start"] = time.perf_counter()
        settle_ms = min(float(config.settle_ms), MAPPING_BURST_SETTLE_MS) if burst_follow_up else float(config.settle_ms)
        self._wait_for_interruptible_interval(settle_ms / 1000.0)
        timing["settle_end"] = time.perf_counter()
        if self._pause_requested.is_set():
            self._record_pause_boundary("before_firing")
            self._finalize_laser_interruption(context="mapping_paused_before_firing")
            raise AutomationPaused("Mapping acquisition paused before firing.")
        self._raise_if_automation_stopped("Mapping acquisition stopped before firing.")
        # The single guarded-fire interlock poll runs immediately before M3,
        # inside the armed context below; a second pre-arm poll added only a
        # redundant status round trip per shot.
        timing["interlock_start"] = timing["interlock_end"] = time.perf_counter()

        ext_mode = self.spec.capabilities.external_trigger_mode
        if ext_mode is None and not simulation_mode:
            raise RuntimeError(f"{self.spec.model} does not support external trigger capture.")
        trigger_mode = self.spec.capabilities.normal_trigger_mode if simulation_mode else ext_mode
        if trigger_mode is not None and self.spec.current_trigger_mode != trigger_mode:
            self._set_trigger_mode(trigger_mode)

        self._send(AcquisitionMessage.ARMED, None)
        self._send(
            AcquisitionMessage.STATUS,
            (
                f"Simulating mapping R{target.row_index:03d} C{target.column_index:03d} shot {target.shot_number}."
                if simulation_mode
                else f"Armed mapping R{target.row_index:03d} C{target.column_index:03d}; firing guarded pulse."
            ),
        )

        timing["trigger_wait_start"] = time.perf_counter()
        if simulation_mode:
            if self._pause_requested.is_set():
                self._record_pause_boundary("before_firing")
                self._finalize_laser_interruption(context="simulated_mapping_paused_before_firing")
                raise AutomationPaused("Simulated mapping paused before firing.")
            if self._automation_cancelled():
                self._finalize_laser_interruption(context="simulated_mapping_stopped_before_capture")
                raise AutomationStopped("Simulated mapping stopped before capture.")
            try:
                self._fire_laser_for_target(config, target, laser, state_name=self.STATE_AUTOMATED)
            except Exception as exc:
                self._finalize_laser_interruption(context="simulated_mapping_firing_interrupted")
                if self._pause_requested.is_set():
                    self._record_pause_boundary("during_firing")
                    raise AutomationPaused("Simulated mapping paused during firing.") from exc
                if self._automation_cancelled():
                    raise AutomationStopped("Simulated mapping stopped during firing.") from exc
                raise
            intensities = self._get_intensities()
        else:
            executor = trigger_executor
            own_executor = executor is None
            if executor is None:
                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="MappingTriggerRead")
                self._mapping_trigger_executor_creations += 1
            future: Future = executor.submit(self._get_intensities_for_trigger_read)
            cancel_requested = False
            skip_final_trigger_reset = False
            try:
                if self._pause_requested.is_set():
                    cancel_requested = True
                    self._record_pause_boundary("before_firing")
                    self._finalize_laser_interruption(context="mapping_paused_before_firing")
                    skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                    raise AutomationPaused("Mapping acquisition paused before firing.")
                if self._automation_cancelled():
                    cancel_requested = True
                    self._finalize_laser_interruption(context="mapping_stopped_before_firing")
                    skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                    raise AutomationStopped("Mapping acquisition stopped before firing.")
                try:
                    timing["interlock_start"] = time.perf_counter()
                    self._verify_guarded_fire_interlock(laser, context="before mapping firing")
                    timing["interlock_end"] = time.perf_counter()
                    self._fire_laser_for_target(config, target, laser, state_name=self.STATE_AUTOMATED)
                except Exception as exc:
                    self._finalize_laser_interruption(context="mapping_firing_interrupted")
                    if self._pause_requested.is_set():
                        cancel_requested = True
                        self._record_pause_boundary("during_firing")
                        skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                        raise AutomationPaused("Mapping acquisition paused during firing.") from exc
                    if self._automation_cancelled():
                        cancel_requested = True
                        skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                        raise AutomationStopped("Mapping acquisition stopped during firing.") from exc
                    raise RuntimeError(
                        f"GRBL firing command failed or interlock blocked mapping capture: {exc}. "
                        "No retry was attempted; verify laser interlocks and controller response."
                    ) from exc
                deadline = time.monotonic() + config.capture_timeout_s
                while not future.done():
                    if self.state != self.STATE_AUTOMATED or self._stop_event.is_set():
                        cancel_requested = True
                        skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                        raise AutomationStopped("Mapping acquisition stopped.")
                    remaining_s = deadline - time.monotonic()
                    if remaining_s <= 0:
                        cancel_requested = True
                        raise TimeoutError(
                            f"Capture timed out after {config.capture_timeout_s:g}s at mapping {target.point_key}."
                        )
                    self._wait_for_interruptible_interval(min(MAPPING_ARMED_POLL_INTERVAL_S, remaining_s))
                if future.done() and time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Capture timed out after {config.capture_timeout_s:g}s at mapping {target.point_key}."
                    )
            except AutomationPaused:
                # Fallback boundary: only recorded if no earlier site tagged one.
                self._record_pause_boundary("trigger_wait")
                self._finalize_laser_interruption(context="mapping_paused")
                raise
            except AutomationStopped:
                self._finalize_laser_interruption(context="mapping_stopped")
                raise
            except Exception:
                self._finalize_laser_interruption(context="mapping_error")
                if not future.done():
                    skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                raise
            finally:
                if own_executor:
                    executor.shutdown(wait=not (cancel_requested and skip_final_trigger_reset), cancel_futures=cancel_requested)
            intensities = future.result()

        timing["trigger_wait_end"] = time.perf_counter()
        timing["capture_end"] = time.perf_counter()
        timing["wavelengths_fetch_start"] = time.perf_counter()
        wavelengths = self._get_wavelengths()
        timing["wavelengths_fetch_end"] = time.perf_counter()
        if simulation_mode:
            intensities = self._reference_simulation_intensities(
                wavelengths,
                intensities,
                target,
                trigger_delay_us=timing.get("trigger_delay_us"),
                integration_us=timing.get("integration_time_us"),
            )

        self._check_run_startup_light(intensities)
        self._shot_index += 1
        self._send(
            AcquisitionMessage.STATUS,
            (
                f"Captured mapping R{target.row_index:03d} C{target.column_index:03d} "
                f"shot {target.shot_number}; saving."
            ),
        )
        timing["save_enqueued"] = time.perf_counter()
        save_job = self._build_mapping_save_job(config, target, wavelengths, intensities, timing=timing)
        timing["cycle_end"] = time.perf_counter()
        timing["message_sent"] = timing["cycle_end"]
        self._last_mapping_cycle_end = timing["cycle_end"]
        # Snapshot before handing `timing` to the writer thread, which appends
        # the save/state fields on its own schedule.
        self.latest_timing_sample = dict(timing)
        writer = self._mapping_save_writer
        if writer is not None:
            writer.submit(save_job)
        else:
            save_job()
        self._send(AcquisitionMessage.SPECTRUM, (wavelengths, intensities))
        self._send(AcquisitionMessage.CAPTURED, {
            "wavelengths": wavelengths,
            "intensities": intensities,
            "shot_index": self._shot_index,
        })

    def _auto_save_mapping_target(
        self,
        config: MappingGridConfig,
        target: MappingGridTarget,
        wavelengths,
        intensities,
        *,
        timing: dict | None = None,
    ) -> str:
        """Synchronous save (no completion record); the run path uses the writer."""
        job = self._build_mapping_save_job(
            config, target, wavelengths, intensities, timing=timing, record_completion=False
        )
        return job()

    def _build_mapping_save_job(
        self,
        config: MappingGridConfig,
        target: MappingGridTarget,
        wavelengths,
        intensities,
        *,
        timing: dict | None = None,
        record_completion: bool = True,
    ):
        """Capture every worker-state-dependent value at enqueue time.

        The returned job reads nothing from live worker state that the
        acquisition thread advances afterwards (shot index, spectrometer
        settings), so it is safe to run on the save-writer thread.
        """
        captured_at = datetime.now()
        timestamp = captured_at.strftime("%Y%m%d_%H%M%S_%f")
        filename = mapping_spectrum_filename(
            config,
            target,
            timestamp=timestamp,
            shot_index=self._shot_index,
        )
        columns = mapping_grid_shape(config)[1]
        binary_row_index = binary_row_index_for_target(
            row_index=target.row_index,
            column_index=target.column_index,
            shot_number=target.shot_number,
            columns=columns,
            shots_per_point=config.shots_per_point,
        )
        integration_time_us = timing.get("integration_time_us") if timing is not None else None
        if integration_time_us is None:
            integration_time_us = getattr(self.spec, "integration_time_us", "")
        trigger_delay_us = timing.get("trigger_delay_us") if timing is not None else None
        if trigger_delay_us is None and config.column_delays_us:
            trigger_delay_us = getattr(self.spec, "trigger_delay_us", "")
        shot_index = self._shot_index
        save_directory = self.save_directory
        wavelengths_arr = np.asarray(wavelengths)
        intensities_arr = np.array(intensities, copy=True)

        def job() -> str:
            return self._execute_mapping_save_job(
                config=config,
                target=target,
                wavelengths=wavelengths_arr,
                intensities=intensities_arr,
                timing=timing,
                captured_at=captured_at,
                filename=filename,
                binary_row_index=binary_row_index,
                integration_time_us=integration_time_us,
                trigger_delay_us=trigger_delay_us,
                shot_index=shot_index,
                save_directory=save_directory,
                record_completion=record_completion,
            )

        return job

    def _execute_mapping_save_job(
        self,
        *,
        config: MappingGridConfig,
        target: MappingGridTarget,
        wavelengths,
        intensities,
        timing: dict | None,
        captured_at,
        filename: str,
        binary_row_index: int,
        integration_time_us,
        trigger_delay_us,
        shot_index: int,
        save_directory: str,
        record_completion: bool,
    ) -> str:
        """Persist one shot; runs on the save-writer thread during runs.

        Ordering is the resume-correctness contract: bytes first, then the
        completion record, then (batched, after a store flush) the state JSON —
        state never claims a shot whose data is not durable.
        """
        if timing is not None:
            timing["save_start"] = time.perf_counter()
            timing["save_file_path"] = filename
        os.makedirs(save_directory, exist_ok=True)
        filepath = ""
        store = self._mapping_binary_store(config)
        storage_uri = store.write_shot(binary_row_index, wavelengths, intensities)
        if config.write_csv_files:
            filepath = self._unique_output_path(save_directory, filename)
            if timing is not None:
                timing["save_file_path"] = filepath
            self._save_spectrum_file(filepath, wavelengths, intensities)
        if timing is not None:
            timing["save_end"] = time.perf_counter()
        save_ms = ""
        if timing is not None and timing.get("save_start") is not None and timing.get("save_end") is not None:
            save_ms = (float(timing["save_end"]) - float(timing["save_start"])) * 1000.0
        self._append_mapping_index_row_locked(
            save_directory,
            {
                "target_key": target.key,
                "point_key": target.point_key,
                "row_index": target.row_index,
                "column_index": target.column_index,
                "x_mm": f"{target.x_mm:.6f}",
                "y_mm": f"{target.y_mm:.6f}",
                "shot_number": target.shot_number,
                "shot_index": shot_index,
                "filename": os.path.basename(filepath) if filepath else filename,
                "filepath": filepath,
                "captured_at": captured_at.isoformat(timespec="microseconds"),
                "save_ms": "" if save_ms == "" else f"{save_ms:.3f}",
                "integration_time_us": integration_time_us,
                "trigger_delay_us": "" if trigger_delay_us is None else trigger_delay_us,
                "storage_kind": "binary_npy",
                "store_dir": "_mapping_spectrum_store",
                "binary_row_index": binary_row_index,
                "storage_uri": storage_uri,
                "binary_written": "true",
                "shot_quality_state": "",
                "exclude_reason": "",
            },
        )
        self._send(AcquisitionMessage.SAVE_COMPLETE, filepath or storage_uri)
        logger.info("Mapping auto-saved: %s", filepath or storage_uri)

        if record_completion and self.mapping_state is not None:
            with self._mapping_state_lock:
                self.mapping_state.record_completed(target)
            self._mapping_completed_since_state_write += 1
            if self._mapping_completed_since_state_write >= MAPPING_STATE_WRITE_BATCH_SIZE:
                if timing is not None:
                    timing["state_write_start"] = time.perf_counter()
                # Bytes must be durable before the state JSON claims them.
                store.flush()
                self._persist_mapping_state(event="target_batch_completed")
                if timing is not None:
                    timing["state_write_end"] = time.perf_counter()
                self._mapping_completed_since_state_write = 0
            self._send(
                AcquisitionMessage.PLATE_PROGRESS,
                self._mapping_progress_payload(current_target=target, completed_target=target),
            )

        if record_completion and timing is not None:
            timing.setdefault("state_write_start", time.perf_counter())
            timing.setdefault("state_write_end", timing["state_write_start"])
            if save_directory:
                try:
                    append_mapping_timing_row(save_directory, mapping_timing_row(timing))
                except Exception:
                    logger.warning("Failed to append mapping timing sample row.", exc_info=True)
            self._emit_timing_sample(timing)
        return filepath or storage_uri

    def _append_mapping_index_row_locked(self, save_directory: str, row: dict) -> None:
        """Serialize index-CSV appends: they come from both the worker thread
        (discarded stream segments) and the save-writer thread, and concurrent
        appends through separate handles can interleave rows or duplicate the
        header."""
        with self._mapping_state_lock:
            append_mapping_index_row(save_directory, row)

    def _mapping_binary_store(self, config: MappingGridConfig) -> MappingSpectrumStore:
        target_count = mapping_target_count(config)
        if self._mapping_spectrum_store is None or self._mapping_spectrum_store.run_directory != Path(self.save_directory):
            self._mapping_spectrum_store = MappingSpectrumStore.open(
                self.save_directory,
                targets_total=target_count,
                config_summary=config.to_mapping(),
            )
        return self._mapping_spectrum_store

    def _run_automated(self) -> None:
        self._laser_interruption_finalized = False
        config = self.automation_config
        laser = self.laser
        if config is None or laser is None:
            self._send(AcquisitionMessage.ERROR, "Automated acquisition is not configured.")
            self._set_state(self.STATE_IDLE)
            self._send(AcquisitionMessage.IDLE, None)
            return

        simulation_mode = self._automation_run_mode == RUN_MODE_SIMULATION
        repeat_mode = bool(self._qc_repeat_targets) and not simulation_mode
        targets = list(self._qc_repeat_targets) if repeat_mode else automation_targets(config)
        completed_all = False
        stopped_early = False
        paused = False
        backfilled_completed_targets = False
        trigger_executor: ThreadPoolExecutor | None = None
        try:
            self._prepare_laser_for_run(
                laser,
                motion_only=simulation_mode,
                context="simulated acquisition" if simulation_mode else "guarded firing",
            )
            config = self.automation_config or config
            if not simulation_mode:
                trigger_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="AutoTriggerRead")
                # Stale trigger frames shift well assignments exactly like they
                # shear mapping grids; clear leftovers before the first well.
                # New run => give a previously latched-off drain another chance.
                self._mapping_trigger_drain_unsupported = False
                self._drain_spectrometer_trigger_backlog("plate run start")
            self._last_plate_cycle_end = None
            self._plate_completed_since_state_write = 0
            self._plate_last_started_well = None
            for target in targets:
                if self._pause_requested.is_set():
                    paused = True
                    self._send(AcquisitionMessage.STATUS, "Automated acquisition paused before the next move.")
                    break
                if self.state != self.STATE_AUTOMATED or self._stop_event.is_set():
                    self._send(AcquisitionMessage.STATUS, "Automated acquisition stopped.")
                    stopped_early = True
                    break
                if not repeat_mode and self.automation_state and target.key in self.automation_state.completed_targets:
                    continue

                self._set_active_plate(target.plate_index)
                plate_state = self._plate_states[target.plate_index]
                if plate_state.shots_by_well.get(target.well, 0) >= target.shot_number:
                    if (
                        not repeat_mode
                        and self.automation_state is not None
                        and target.key not in self.automation_state.completed_targets
                        and target.well not in set(plate_state.repair_queue)
                    ):
                        self.automation_state.completed_targets.add(target.key)
                        backfilled_completed_targets = True
                    continue

                self._run_automated_target(config, target, laser, trigger_executor=trigger_executor)
                if self._plate_states[target.plate_index].is_complete:
                    self._send(
                        AcquisitionMessage.STATUS,
                        f"Plate {target.plate_index} complete.",
                    )
            else:
                completed_all = True
                if simulation_mode:
                    self._send(AcquisitionMessage.STATUS, "Simulated acquisition complete.")
                elif repeat_mode:
                    self._send(AcquisitionMessage.STATUS, "QC repeat acquisition complete.")
                else:
                    self._send(AcquisitionMessage.STATUS, "Automated acquisition complete.")
        except AutomationStopped as exc:
            if self.automation_state is not None:
                self.automation_state.record_abort(str(exc))
                self._persist_automation_state(event="simulated_run_stopped" if simulation_mode else "automated_run_stopped")
            self._send(AcquisitionMessage.STATUS, "Simulated acquisition stopped." if simulation_mode else "Automated acquisition stopped.")
        except AutomationPaused as exc:
            paused = True
            self._send(AcquisitionMessage.STATUS, str(exc) or "Automated acquisition paused.")
        except Exception as exc:
            if self.automation_state is not None:
                self.automation_state.record_abort(str(exc))
                self._persist_automation_state(event="simulated_run_error" if simulation_mode else "automated_run_error")
            self._send(
                AcquisitionMessage.ERROR,
                (
                    f"Simulated acquisition stopped: {exc}."
                    if simulation_mode
                    else f"Automated acquisition stopped: {exc}. Use the physical e-stop if software stop does not halt the laser."
                ),
            )
        finally:
            if paused:
                interruption_context = "simulated_run_paused" if simulation_mode else "automated_run_paused"
            elif stopped_early:
                interruption_context = "simulated_run_stopped" if simulation_mode else "automated_run_stopped"
            elif completed_all:
                interruption_context = "simulated_run_complete" if simulation_mode else "automated_run_complete"
            else:
                interruption_context = "simulated_run_interrupted" if simulation_mode else "automated_run_interrupted"
            self._finalize_laser_interruption(context=interruption_context)
            if backfilled_completed_targets and self.automation_state is not None:
                self._persist_automation_state(event="target_state_backfilled")
            if paused and self.automation_state is not None:
                self.automation_paused = True
                self.automation_state.abort_reason = None
                self.automation_state.active_target = None
                self._persist_automation_state(event="simulated_run_paused" if simulation_mode else "automated_run_paused")
            elif stopped_early and self.automation_state is not None:
                self.automation_paused = False
                self.automation_state.record_abort("Automated acquisition stopped by user.")
                self._persist_automation_state(event="simulated_run_stopped" if simulation_mode else "automated_run_stopped")
            elif completed_all and self.automation_state is not None:
                self.automation_paused = False
                self.automation_state.abort_reason = None
                self.automation_state.active_target = None
                self._persist_automation_state(event="simulated_run_complete" if simulation_mode else "automated_run_complete")
            if completed_all and repeat_mode:
                self._qc_repeat_targets = []
            self._persist_automation_summary()
            if completed_all and not stopped_early:
                self._run_spectra_qc_if_enabled(force=simulation_mode)
            if paused:
                self._pause_requested.clear()
            if self.spec.is_connected:
                if self.hardware_read_active:
                    self._send(
                        AcquisitionMessage.ERROR,
                        "Spectrometer read is still active. Reconnect spectrometer before continuing.",
                    )
                else:
                    try:
                        self._set_trigger_mode(self.spec.capabilities.normal_trigger_mode)
                    except Exception:
                        logger.warning("Failed to restore normal trigger mode after run.", exc_info=True)
                        self._send(
                            AcquisitionMessage.ERROR,
                            "Failed to restore normal trigger mode after the run. "
                            "Reconnect the spectrometer before the next capture.",
                        )
                    self._clear_sweep_trigger_delay()
            self._set_state(self.STATE_IDLE)
            self._send(AcquisitionMessage.IDLE, None)
            if trigger_executor is not None:
                trigger_executor.shutdown(wait=False, cancel_futures=True)

    def _run_laser_pattern_test(self) -> None:
        self._laser_interruption_finalized = False
        config = self.automation_config
        laser = self.laser
        if config is None or laser is None:
            self._send(AcquisitionMessage.ERROR, "Laser pattern test is not configured.")
            self._set_state(self.STATE_IDLE)
            self._send(AcquisitionMessage.IDLE, None)
            return

        targets = automation_targets(config)
        completed_all = False
        stopped_early = False
        paused = False
        fired_by_plate: dict[int, dict[str, int]] = {}
        fired_positions = 0
        try:
            self._prepare_laser_for_run(laser, motion_only=False, context="laser pattern test")
            config = self.automation_config or config
            for target in targets:
                if self._pause_requested.is_set():
                    paused = True
                    self._send(AcquisitionMessage.STATUS, "Laser pattern test paused before the next move.")
                    break
                if self.state != self.STATE_LASER_TEST or self._stop_event.is_set():
                    self._send(AcquisitionMessage.STATUS, "Laser pattern test stopped.")
                    stopped_early = True
                    break
                self._run_laser_pattern_target(config, target, laser)
                fired_positions += 1
                plate_counts = fired_by_plate.setdefault(target.plate_index, {})
                plate_counts[target.well] = plate_counts.get(target.well, 0) + 1
                self._send(
                    AcquisitionMessage.PLATE_PROGRESS,
                    self._laser_test_progress_payload(
                        target.plate_index,
                        fired_by_plate,
                        current_well=target.well,
                        fired_positions=fired_positions,
                        total_positions=len(targets),
                    ),
                )
            else:
                completed_all = True
                self._send(AcquisitionMessage.STATUS, "Laser pattern test complete.")
        except AutomationStopped as exc:
            self._send(AcquisitionMessage.STATUS, str(exc) or "Laser pattern test stopped.")
        except AutomationPaused as exc:
            paused = True
            self._send(AcquisitionMessage.STATUS, str(exc) or "Laser pattern test paused.")
        except Exception as exc:
            self._send(
                AcquisitionMessage.ERROR,
                f"Laser pattern test stopped: {exc}. Use the physical e-stop if software stop does not halt the laser.",
            )
        finally:
            if paused:
                interruption_context = "laser_pattern_test_paused"
            elif stopped_early:
                interruption_context = "laser_pattern_test_stopped"
            elif completed_all:
                interruption_context = "laser_pattern_test_complete"
            else:
                interruption_context = "laser_pattern_test_interrupted"
            self._finalize_laser_interruption(context=interruption_context)
            if paused:
                self.automation_paused = False
                self._pause_requested.clear()
            if completed_all:
                self.automation_paused = False
            if stopped_early:
                self.automation_paused = False
            self._set_state(self.STATE_IDLE)
            self._send(AcquisitionMessage.IDLE, None)

    def _run_laser_pattern_target(self, config: AutomationRunConfig, target: AutomationTarget, laser) -> None:
        self._send(
            AcquisitionMessage.STATUS,
            (
                f"Laser test moving to plate {target.plate_index} {target.well} "
                f"shot {target.shot_number} at X{target.x_mm:.3f} Y{target.y_mm:.3f}."
            ),
        )
        self._ensure_laser_off_before_motion(laser)
        motion_timeout_s = self._motion_timeout_s(laser, config, target)
        laser.move_to(target.x_mm, target.y_mm, feed_mm_min=config.move_speed_mm_min)
        self._wait_for_laser_idle(laser, timeout_s=motion_timeout_s, state_name=self.STATE_LASER_TEST)
        self._wait_for_interruptible_interval(config.settle_ms / 1000.0)
        if self._pause_requested.is_set():
            self._finalize_laser_interruption(context="laser_pattern_test_paused_before_firing")
            raise AutomationPaused("Laser pattern test paused before firing.")
        if self.state != self.STATE_LASER_TEST or self._stop_event.is_set():
            self._finalize_laser_interruption(context="laser_pattern_test_stopped_before_firing")
            raise AutomationStopped("Laser pattern test stopped before firing.")
        try:
            self._verify_guarded_fire_interlock(laser, context="before laser pattern test firing")
            self._fire_laser_for_target(config, target, laser, state_name=self.STATE_LASER_TEST)
        except Exception as exc:
            self._finalize_laser_interruption(context="laser_pattern_test_firing_interrupted")
            if self._pause_requested.is_set():
                raise AutomationPaused("Laser pattern test paused during firing.") from exc
            if self.state != self.STATE_LASER_TEST or self._stop_event.is_set():
                raise AutomationStopped("Laser pattern test stopped during firing.") from exc
            raise RuntimeError(
                f"GRBL firing command failed or interlock blocked laser pattern test firing: {exc}. "
                "No retry was attempted; verify laser interlocks and controller response."
            ) from exc
        self._send(
            AcquisitionMessage.STATUS,
            (
                f"Laser test fired plate {target.plate_index} {target.well} "
                f"shot {target.shot_number} at X{target.x_mm:.3f} Y{target.y_mm:.3f}."
            ),
        )

    def _apply_demo_simulation_qc_profile(self, wavelengths, intensities, target: AutomationTarget):
        if self._automation_run_mode != RUN_MODE_SIMULATION:
            return intensities
        profile = _DEMO_SIMULATION_QC_PROFILES.get((int(target.plate_index), str(target.well).upper()))
        if profile is None:
            return intensities

        values = np.asarray(intensities, dtype=float).copy()
        if values.size == 0:
            return values
        finite_values = values[np.isfinite(values)]
        if finite_values.size == 0:
            return np.zeros_like(values)
        max_intensity = _coerce_simulation_max_intensity(
            getattr(getattr(self.spec, "capabilities", None), "max_intensity", _SIMULATION_REFERENCE_ADC_MAX)
        )
        baseline = float(np.nanmedian(finite_values))
        rng = np.random.default_rng(_simulation_target_seed(target, salt=f"qc:{profile}"))
        lower, upper = _simulation_value_bounds(max_intensity)

        if profile == "low_signal":
            noise = rng.normal(0.0, max_intensity * 0.00035, values.size)
            floor = rng.uniform(0.0, max_intensity * 0.0008)
            trend = np.linspace(0.0, max_intensity * 0.0006, values.size)
            return np.clip((values * 0.012) + noise + floor + trend, lower, upper)

        if profile == "overexposed_flat":
            plateau = rng.uniform(max_intensity * 0.82, max_intensity * 0.92)
            gradient = np.linspace(-1.0, 1.0, values.size) * rng.uniform(0.0, max_intensity * 0.015)
            noise = rng.normal(0.0, max_intensity * 0.004, values.size)
            return np.clip(plateau + (values * 0.015) + gradient + noise, 0.0, upper)

        if profile == "unstable_baseline":
            phase = np.linspace(0.0, 10.0 * math.pi, values.size, endpoint=False) + rng.uniform(0.0, 2.0 * math.pi)
            ripple_amplitude = max(max_intensity * 0.08, baseline * 0.6, max_intensity * 0.0008)
            ripple = np.sin(phase) * ripple_amplitude
            if values.size < 8:
                alternating = np.where(np.arange(values.size) % 2 == 0, 1.0, -1.0)
                ripple += alternating * ripple_amplitude
            drift = np.linspace(-max_intensity * 0.06, max(max_intensity * 0.28, baseline, max_intensity * 0.001), values.size)
            noise = rng.normal(0.0, max_intensity * 0.012, values.size)
            return np.clip(values + max_intensity * 0.12 + ripple + drift + noise, lower, upper)

        return values

    def _column_integration_time_ms(self, config: AutomationRunConfig, target: AutomationTarget) -> float | None:
        schedule = tuple(config.column_integration_times_ms or ())
        if not schedule:
            return None
        well = str(target.well or "").strip().upper()
        digits = "".join(ch for ch in well[1:] if ch.isdigit())
        if not digits:
            raise RuntimeError(f"Cannot determine integration sweep column from well '{target.well}'.")
        column = int(digits)
        if column < 1 or column > len(schedule):
            raise RuntimeError(
                f"Integration sweep has {len(schedule)} column setting(s), but target {target.well} is column {column}."
            )
        return float(schedule[column - 1])

    def _plate_integration_time_ms(self, config: AutomationRunConfig, target: AutomationTarget) -> float | None:
        schedule = tuple(getattr(config, "plate_integration_times_ms", ()) or ())
        if not schedule:
            return None
        plate_index = int(getattr(target, "plate_index", 0))
        if plate_index < 1 or plate_index > len(schedule):
            raise RuntimeError(
                f"Per-plate integration sweep has {len(schedule)} value(s), "
                f"but target {target.well} is on plate {plate_index}."
            )
        return float(schedule[plate_index - 1])

    def _set_sweep_integration_time(self, integration_ms: float, timing: dict, label: str) -> None:
        integration_us = int(round(float(integration_ms) * 1000.0))
        caps = getattr(self.spec, "capabilities", None)
        min_us = getattr(caps, "integration_time_min_us", None)
        max_us = getattr(caps, "integration_time_max_us", None)
        if min_us is not None and integration_us < int(min_us):
            raise RuntimeError(
                f"Integration sweep value {integration_ms:g} ms for {label} is below "
                f"the spectrometer minimum {int(min_us) / 1000.0:g} ms."
            )
        if max_us is not None and integration_us > int(max_us):
            raise RuntimeError(
                f"Integration sweep value {integration_ms:g} ms for {label} exceeds "
                f"the spectrometer maximum {int(max_us) / 1000.0:g} ms."
            )
        current_us = getattr(self.spec, "integration_time_us", None)
        timing["integration_time_ms"] = float(integration_ms)
        timing["integration_time_us"] = integration_us
        if current_us is not None and int(current_us) == integration_us:
            return
        setter = getattr(self.spec, "set_integration_time", None)
        if not callable(setter):
            raise RuntimeError("Connected spectrometer does not support integration-time changes.")
        normal_mode = getattr(caps, "normal_trigger_mode", None)
        if normal_mode is not None and getattr(self.spec, "current_trigger_mode", None) != normal_mode:
            self._set_trigger_mode(normal_mode)
        setter(integration_us)
        timing["integration_time_changed"] = True

    def _apply_target_integration_time(
        self,
        config: AutomationRunConfig,
        target: AutomationTarget,
        timing: dict,
    ) -> None:
        integration_ms = self._plate_integration_time_ms(config, target)
        if integration_ms is None:
            integration_ms = self._column_integration_time_ms(config, target)
        if integration_ms is None:
            return
        self._set_sweep_integration_time(integration_ms, timing, str(target.well))

    def _apply_mapping_target_integration_time(
        self,
        config: MappingGridConfig,
        target: MappingGridTarget,
        timing: dict,
    ) -> None:
        pair = mapping_paired_setting(config, target.row_index, target.column_index)
        if pair is not None:
            integration_ms = float(pair[1])
        else:
            integration_ms = mapping_row_band_integration_time_ms(config, target.row_index)
        if integration_ms is None:
            return
        self._set_sweep_integration_time(integration_ms, timing, target.point_key)

    def _sweep_column_for_target(self, target) -> int:
        """Sweep column index for either a plate well or a mapping grid target."""
        column_index = getattr(target, "column_index", None)
        if column_index is not None:
            return int(column_index)
        well = str(getattr(target, "well", "") or "").strip().upper()
        digits = "".join(ch for ch in well[1:] if ch.isdigit())
        if not digits:
            raise RuntimeError(f"Cannot determine sweep column from well '{well}'.")
        return int(digits)

    @staticmethod
    def _sweep_target_label(target) -> str:
        well = getattr(target, "well", None)
        if well:
            return str(well)
        return str(getattr(target, "point_key", "target"))

    def _column_delay_us(self, config, target) -> float | None:
        if isinstance(config, MappingGridConfig) and config.paired_settings:
            pair = mapping_paired_setting(config, target.row_index, target.column_index)
            return float(pair[0]) if pair is not None else None
        schedule = tuple(getattr(config, "column_delays_us", ()) or ())
        if not schedule:
            return None
        if isinstance(config, MappingGridConfig):
            try:
                return mapping_column_delay_us(config, self._sweep_column_for_target(target))
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
        column = self._sweep_column_for_target(target)
        if column < 1 or column > len(schedule):
            raise RuntimeError(
                f"Trigger-delay sweep has {len(schedule)} column setting(s), "
                f"but target {self._sweep_target_label(target)} is column {column}."
            )
        return float(schedule[column - 1])

    def _clear_sweep_trigger_delay(self) -> None:
        """Disable the trigger delay after a run so it cannot leak into
        normal captures or a later run without a delay schedule."""
        self._last_requested_trigger_delay_us = None
        setter = getattr(self.spec, "set_trigger_delay", None)
        caps = getattr(self.spec, "capabilities", None)
        current = getattr(self.spec, "trigger_delay_us", 0.0) or 0.0
        if not callable(setter) or not getattr(caps, "supports_trigger_delay", False) or current <= 0:
            return
        try:
            setter(0.0)
        except Exception:
            logger.warning("Failed to clear trigger delay after run.", exc_info=True)
            self._send(
                AcquisitionMessage.ERROR,
                f"Failed to clear the sweep trigger delay ({current:g} us may still be active). "
                "Reconnect the spectrometer before the next external-trigger capture.",
            )

    def _apply_target_trigger_delay(self, config, target, timing: dict) -> None:
        delay_us = self._column_delay_us(config, target)
        if delay_us is None:
            return
        timing["trigger_delay_us"] = float(delay_us)
        caps = getattr(self.spec, "capabilities", None)
        supported = bool(getattr(caps, "supports_trigger_delay", False))
        setter = getattr(self.spec, "set_trigger_delay", None)
        if not supported or not callable(setter):
            if self._automation_run_mode == RUN_MODE_SIMULATION:
                # Simulation still records the planned delay; the simulated
                # spectrum applies its effect downstream.
                return
            raise RuntimeError(
                "Trigger-delay sweep requires a spectrometer with a programmable "
                f"trigger delay; {getattr(self.spec, 'model', 'the connected spectrometer')} does not support one."
            )
        min_us = float(getattr(caps, "trigger_delay_min_us", 0.0) or 0.0)
        max_us = float(getattr(caps, "trigger_delay_max_us", 0.0) or 0.0)
        if delay_us < min_us:
            raise RuntimeError(
                f"Trigger-delay value {delay_us:g} us for {self._sweep_target_label(target)} is below "
                f"the spectrometer minimum {min_us:g} us."
            )
        if max_us and delay_us > max_us:
            raise RuntimeError(
                f"Trigger-delay value {delay_us:g} us for {self._sweep_target_label(target)} exceeds "
                f"the spectrometer maximum {max_us:g} us."
            )
        current = getattr(self.spec, "trigger_delay_us", None)
        if current is not None and (
            float(current) == float(delay_us)
            or (
                self._last_requested_trigger_delay_us is not None
                and float(delay_us) == self._last_requested_trigger_delay_us
            )
        ):
            # The hardware already holds this value (exactly, or as the
            # quantized result of the same schedule value) — record the
            # applied state and skip the redundant call.
            self._last_requested_trigger_delay_us = float(delay_us)
            timing["trigger_delay_us"] = float(current)
            return
        setter(delay_us)
        self._last_requested_trigger_delay_us = float(delay_us)
        applied = getattr(self.spec, "trigger_delay_us", None)
        if applied is not None:
            # The SDK quantizes to 10 ns and enforces a 100 ns enabled minimum;
            # persist what the hardware actually holds, not what was asked for.
            timing["trigger_delay_us"] = float(applied)
        timing["trigger_delay_changed"] = True

    def _reference_simulation_intensities(
        self,
        wavelengths,
        fallback_intensities,
        target,
        *,
        trigger_delay_us: float | None = None,
        integration_us: float | None = None,
    ) -> np.ndarray:
        max_intensity = getattr(getattr(self.spec, "capabilities", None), "max_intensity", _SIMULATION_REFERENCE_ADC_MAX)
        try:
            intensities = _reference_based_simulation_spectrum(
                wavelengths,
                target,
                max_intensity=max_intensity,
            )
        except Exception as exc:
            logger.warning("Failed to build reference-based simulated spectrum; using backend simulation: %s", exc)
            return np.asarray(fallback_intensities, dtype=float)
        if trigger_delay_us is not None:
            intensities = apply_simulated_trigger_delay_response(
                wavelengths,
                intensities,
                float(trigger_delay_us),
                integration_us=integration_us,
                max_intensity=_coerce_simulation_max_intensity(max_intensity),
                rng=np.random.default_rng(_simulation_target_seed(target, salt="delay")),
            )
        return intensities

    def _run_automated_target(
        self,
        config: AutomationRunConfig,
        target: AutomationTarget,
        laser,
        *,
        trigger_executor: ThreadPoolExecutor | None = None,
    ) -> None:
        simulation_mode = self._automation_run_mode == RUN_MODE_SIMULATION
        timing = {
            "mode": self._automation_run_mode,
            "shot_index": self._shot_index + 1,
            "plate_index": target.plate_index,
            "well": target.well,
            "shot_number": target.shot_number,
            "target_x_mm": target.x_mm,
            "target_y_mm": target.y_mm,
            "shot_offset_label": target.shot_offset_label,
            "shot_offset_dx_mm": target.shot_offset_dx_mm,
            "shot_offset_dy_mm": target.shot_offset_dy_mm,
            "previous_cycle_end": self._last_plate_cycle_end,
            "cycle_start": time.perf_counter(),
        }
        self._apply_target_integration_time(config, target, timing)
        self._apply_target_trigger_delay(config, target, timing)
        if self.automation_state is not None:
            self.automation_state.record_started(target)
            # Per-shot disk persistence collapsed to per-well: crash diagnosis
            # keeps well granularity while the JSON+manifest write leaves the
            # per-shot path.
            well_key = (target.plate_index, target.well)
            if well_key != self._plate_last_started_well:
                self._persist_automation_state(event="target_started")
                self._plate_last_started_well = well_key

        self._send(
            AcquisitionMessage.STATUS,
            f"Moving to plate {target.plate_index} {target.well} shot {target.shot_number}.",
        )
        self._ensure_laser_off_before_motion(laser)
        motion_timeout_s = self._motion_timeout_s(laser, config, target)
        timing["motion_start"] = time.perf_counter()
        laser.move_to(target.x_mm, target.y_mm, feed_mm_min=config.move_speed_mm_min)
        timing["motion_command_end"] = time.perf_counter()
        timing["idle_wait_start"] = time.perf_counter()
        self._wait_for_laser_idle(
            laser,
            timeout_s=motion_timeout_s,
            state_name=self.STATE_AUTOMATED,
            poll_interval_s=MAPPING_IDLE_POLL_INTERVAL_S,
            status_timeout_s=MAPPING_IDLE_STATUS_TIMEOUT_S,
        )
        timing["idle_wait_end"] = time.perf_counter()
        timing["settle_start"] = time.perf_counter()
        self._wait_for_interruptible_interval(config.settle_ms / 1000.0)
        timing["settle_end"] = time.perf_counter()
        if self._pause_requested.is_set():
            self._finalize_laser_interruption(context="automated_run_paused_before_firing")
            raise AutomationPaused("Automated acquisition paused before firing.")
        self._raise_if_automation_stopped("Automated acquisition stopped before firing.")
        timing["interlock_start"] = timing["interlock_end"] = time.perf_counter()
        if not simulation_mode:
            self._verify_guarded_fire_interlock(laser, context="before arming")
            timing["interlock_end"] = time.perf_counter()

        ext_mode = self.spec.capabilities.external_trigger_mode
        if ext_mode is None and not simulation_mode:
            raise RuntimeError(f"{self.spec.model} does not support external trigger capture.")
        trigger_mode = self.spec.capabilities.normal_trigger_mode if simulation_mode else ext_mode
        if trigger_mode is not None and self.spec.current_trigger_mode != trigger_mode:
            self._set_trigger_mode(trigger_mode)

        self._send(AcquisitionMessage.ARMED, None)
        self._send(
            AcquisitionMessage.STATUS,
            (
                f"Simulating plate {target.plate_index} {target.well} shot {target.shot_number}."
                if simulation_mode
                else f"Armed plate {target.plate_index} {target.well}; firing guarded pulse."
            ),
        )

        timing["trigger_wait_start"] = time.perf_counter()
        if simulation_mode:
            if self._pause_requested.is_set():
                self._finalize_laser_interruption(context="simulated_run_paused_before_firing")
                raise AutomationPaused("Simulated acquisition paused before firing.")
            if self._automation_cancelled():
                self._finalize_laser_interruption(context="simulated_run_stopped_before_capture")
                raise AutomationStopped("Simulated acquisition stopped before capture.")
            try:
                self._fire_laser_for_target(config, target, laser, state_name=self.STATE_AUTOMATED)
            except Exception as exc:
                self._finalize_laser_interruption(context="simulated_run_firing_interrupted")
                if self._pause_requested.is_set():
                    raise AutomationPaused("Simulated acquisition paused during firing.") from exc
                if self._automation_cancelled():
                    raise AutomationStopped("Simulated acquisition stopped during firing.") from exc
                raise
            intensities = self._get_intensities()
        else:
            executor = trigger_executor
            own_executor = executor is None
            if executor is None:
                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="AutoTriggerRead")
            future: Future = executor.submit(self._get_intensities_for_trigger_read)
            cancel_requested = False
            skip_final_trigger_reset = False

            try:
                if self._pause_requested.is_set():
                    cancel_requested = True
                    self._finalize_laser_interruption(context="automated_run_paused_before_firing")
                    skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                    raise AutomationPaused("Automated acquisition paused before firing.")
                if self._automation_cancelled():
                    cancel_requested = True
                    self._finalize_laser_interruption(context="automated_run_stopped_before_firing")
                    skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                    raise AutomationStopped("Automated acquisition stopped before firing.")
                try:
                    self._verify_guarded_fire_interlock(laser, context="before firing")
                    self._fire_laser_for_target(config, target, laser, state_name=self.STATE_AUTOMATED)
                except Exception as exc:
                    self._finalize_laser_interruption(context="automated_run_firing_interrupted")
                    if self._pause_requested.is_set():
                        cancel_requested = True
                        skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                        raise AutomationPaused("Automated acquisition paused during firing.") from exc
                    if self._automation_cancelled():
                        cancel_requested = True
                        skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                        raise AutomationStopped("Automated acquisition stopped during firing.") from exc
                    raise RuntimeError(
                        f"GRBL firing command failed or interlock blocked firing before capture: {exc}. "
                        "No retry was attempted; verify laser interlocks and controller response."
                    ) from exc
                deadline = time.monotonic() + config.capture_timeout_s
                while not future.done():
                    if self.state != self.STATE_AUTOMATED or self._stop_event.is_set():
                        cancel_requested = True
                        skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                        raise AutomationStopped("Automated acquisition stopped.")
                    remaining_s = deadline - time.monotonic()
                    if remaining_s <= 0:
                        cancel_requested = True
                        raise TimeoutError(
                            f"Capture timed out after {config.capture_timeout_s:g}s at plate {target.plate_index} {target.well}."
                        )
                    self._wait_for_interruptible_interval(min(self.armed_poll_interval, remaining_s))
                if future.done() and time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Capture timed out after {config.capture_timeout_s:g}s at plate {target.plate_index} {target.well}."
                    )
            except AutomationPaused:
                self._finalize_laser_interruption(context="automated_run_paused")
                raise
            except AutomationStopped:
                self._finalize_laser_interruption(context="automated_run_stopped")
                raise
            except Exception:
                self._finalize_laser_interruption(context="automated_run_error")
                if not future.done():
                    skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                raise
            finally:
                if own_executor:
                    executor.shutdown(wait=not (cancel_requested and skip_final_trigger_reset), cancel_futures=cancel_requested)

            intensities = future.result()
        timing["trigger_wait_end"] = time.perf_counter()
        timing["capture_end"] = time.perf_counter()
        timing["wavelengths_fetch_start"] = time.perf_counter()
        wavelengths = self._get_wavelengths()
        timing["wavelengths_fetch_end"] = time.perf_counter()
        if simulation_mode:
            intensities = self._reference_simulation_intensities(
                wavelengths,
                intensities,
                target,
                trigger_delay_us=timing.get("trigger_delay_us"),
                integration_us=timing.get("integration_time_us"),
            )
            intensities = self._apply_demo_simulation_qc_profile(wavelengths, intensities, target)

        self._check_run_startup_light(intensities)
        self._shot_index += 1
        self._send(
            AcquisitionMessage.STATUS,
            f"Captured plate {target.plate_index} {target.well} shot {target.shot_number}; saving.",
        )
        timing["save_start"] = time.perf_counter()
        try:
            self._auto_save(wavelengths, intensities, consume_plate=True, timing=timing)
        except AutoSaveError:
            raise
        timing.setdefault("save_end", time.perf_counter())

        if self.automation_state is not None:
            self.automation_state.record_completed(target)
            self._plate_completed_since_state_write += 1
            if self._plate_completed_since_state_write >= PLATE_STATE_WRITE_BATCH_SIZE:
                timing["state_write_start"] = time.perf_counter()
                self._persist_automation_state(event="target_batch_completed")
                timing["state_write_end"] = time.perf_counter()
                self._plate_completed_since_state_write = 0
        # Per-shot reproducibility events append one JSONL line instead of
        # rewriting the whole snapshot JSON (which grew O(n^2) over a run).
        self._append_plate_reproducibility_event(event="automated_plate_shot_saved", timing=timing)

        self._send(AcquisitionMessage.SPECTRUM, (wavelengths, intensities))
        self._send(AcquisitionMessage.CAPTURED, {
            "wavelengths": wavelengths,
            "intensities": intensities,
            "shot_index": self._shot_index,
        })
        timing.setdefault("state_write_start", time.perf_counter())
        timing.setdefault("state_write_end", timing["state_write_start"])
        timing["cycle_end"] = time.perf_counter()
        timing["message_sent"] = timing["cycle_end"]
        self._last_plate_cycle_end = timing["cycle_end"]
        if self.collect_timing_metrics and self.save_directory:
            try:
                append_plate_timing_row(self.save_directory, plate_timing_row(timing))
            except Exception:
                logger.warning("Failed to append plate timing sample row.", exc_info=True)
        self._emit_timing_sample(timing)

    def _motion_timeout_s(self, laser, config: AutomationRunConfig, target: AutomationTarget) -> float:
        try:
            x, y, _z = getattr(laser, "position", (None, None, None))
        except Exception:
            return 60.0
        if x is None or y is None:
            return 60.0
        try:
            distance_mm = math.hypot(float(target.x_mm) - float(x), float(target.y_mm) - float(y))
            if isinstance(config, MappingGridConfig):
                feed_mm_min = self._effective_mapping_move_speed_mm_min(laser)
            else:
                feed_mm_min = max(1.0, float(config.move_speed_mm_min))
        except (TypeError, ValueError):
            return 60.0
        return max(10.0, (distance_mm / (feed_mm_min / 60.0)) + 5.0)

    def _wait_for_laser_idle(
        self,
        laser,
        *,
        timeout_s: float,
        state_name: str,
        poll_interval_s: float | None = None,
        status_timeout_s: float | None = None,
    ) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        last_status = None
        pause_seen = False
        normal_timeout = 0.5 if status_timeout_s is None else max(0.01, float(status_timeout_s))
        pause_timeout = 0.2 if status_timeout_s is None else max(0.01, min(0.2, float(status_timeout_s)))
        interval = 0.1 if poll_interval_s is None else max(0.0, float(poll_interval_s))
        while time.monotonic() <= deadline:
            if self.state != state_name or self._stop_event.is_set():
                self._finalize_laser_interruption(context="laser_motion_stopped")
                raise AutomationStopped("Automated acquisition stopped while waiting for laser motion to finish.")
            if self._pause_requested.is_set() and not pause_seen:
                pause_seen = True
                self._record_pause_boundary("motion_idle_wait")
                self._send(
                    AcquisitionMessage.STATUS,
                    "Pause requested. Waiting for laser motion to reach a safe idle state.",
                )
            status = laser.poll_status(timeout_s=pause_timeout if pause_seen else normal_timeout)
            last_status = status
            state = (getattr(status, "state", "") or "").lower()
            if state == "idle":
                pins = _grbl_active_pins(status)
                control_pins = sorted(_GRBL_CONTROL_PINS.intersection(pins))
                if control_pins:
                    self._finalize_laser_interruption(context="laser_motion_control_pin_block")
                    joined = ", ".join(control_pins)
                    raise RuntimeError(f"GRBL controller input(s) active after motion ({joined}); clear controller state.")
                limit_pins = sorted(_GRBL_LIMIT_PINS.intersection(pins))
                if limit_pins:
                    self._finalize_laser_interruption(context="laser_motion_limit_pin_block")
                    joined = ", ".join(limit_pins)
                    raise RuntimeError(f"GRBL limit input(s) active after motion ({joined}); check homing and limit switches.")
                if pause_seen:
                    raise AutomationPaused("Paused after laser motion reached a safe idle state.")
                return
            if state.startswith(("alarm", "door", "hold")):
                self._finalize_laser_interruption(context="laser_motion_not_ready")
                raise RuntimeError(f"GRBL controller is not ready for firing: {getattr(status, 'raw', '') or status.state}")
            self._wait_for_interruptible_interval(interval)
        raw = getattr(last_status, "raw", "") if last_status is not None else ""
        self._finalize_laser_interruption(context="laser_motion_timeout")
        raise TimeoutError(f"Timed out waiting for GRBL idle state after motion. Last status: {raw or 'unknown'}")

    def _burst_return_verified(self, laser, target: MappingGridTarget) -> bool:
        """One cheap status poll after the 0.2 mm burst return move.

        Returns True only when the controller is already Idle back on the grid
        point; any other outcome falls back to the full idle wait.
        """
        try:
            status = laser.poll_status(timeout_s=MAPPING_IDLE_STATUS_TIMEOUT_S)
        except Exception:
            return False
        if status is None or (getattr(status, "state", "") or "").strip().lower() != "idle":
            return False
        work_x = getattr(status, "work_x", None)
        work_y = getattr(status, "work_y", None)
        if work_x is None or work_y is None:
            return False
        return (
            abs(float(work_x) - float(target.x_mm)) <= MAPPING_BURST_POSITION_TOLERANCE_MM
            and abs(float(work_y) - float(target.y_mm)) <= MAPPING_BURST_POSITION_TOLERANCE_MM
        )

    def _automation_cancelled(self) -> bool:
        return self.state != self.STATE_AUTOMATED or self._stop_event.is_set()

    def _laser_pulse_cancel_requested(self, expected_state: str) -> bool:
        return self._pause_requested.is_set() or self.state != expected_state or self._stop_event.is_set()

    def _raise_if_automation_stopped(self, message: str = "Automated acquisition stopped.") -> None:
        if self._automation_cancelled():
            raise AutomationStopped(message)

    def _set_active_plate(self, plate_index: int) -> None:
        with self._plate_lock:
            if plate_index not in self._plate_states:
                raise RuntimeError(f"Unknown automated plate index {plate_index}.")
            if self._active_plate_index == plate_index and self._plate_run_state is self._plate_states[plate_index]:
                return
            self._active_plate_index = plate_index
            self._plate_run_state = self._plate_states[plate_index]
            self._persist_plate_state_locked()
            payload = self._progress_payload_for_plate(plate_index)
        self._send(AcquisitionMessage.PLATE_PROGRESS, payload)

    def _laser_command_transcript(self) -> list[str]:
        if self.laser is None:
            return []
        return list(getattr(self.laser, "command_log", []) or [])

    def _finalize_laser_interruption(self, *, context: str) -> bool:
        laser = self.laser
        if self._laser_interruption_finalized:
            return not bool(getattr(laser, "reconnect_required", False)) if laser is not None else True
        if laser is None:
            self._last_laser_interruption = {
                "context": context,
                "outcome": "unavailable",
                "final_status": "",
                "resync_used": False,
                "reconnect_required": False,
                "failure_reason": "No laser controller is attached.",
            }
            self._laser_interruption_finalized = True
            return True

        status = None
        resync_used = False
        failure_reason = ""
        outcome = "confirmed"
        try:
            confirm = getattr(laser, "confirm_laser_off_and_status", None)
            if callable(confirm):
                status, resync_used = confirm(timeout_s=2.0)
            else:
                off = getattr(laser, "emergency_laser_off", None)
                if callable(off):
                    off()
                status = getattr(laser, "last_status", None)
        except Exception as exc:
            outcome = "reconnect_required"
            failure_reason = str(exc)
            self._send(
                AcquisitionMessage.ERROR,
                f"Laser-off/status confirmation failed; reconnect laser before resuming: {exc}",
            )
            logger.warning("Failed to confirm laser-off/status during %s: %s", context, exc)

        reconnect_required = bool(getattr(laser, "reconnect_required", False)) or outcome == "reconnect_required"
        if reconnect_required and outcome == "confirmed":
            outcome = "reconnect_required"
            failure_reason = str(getattr(laser, "reconnect_required_reason", "") or "")

        self._last_laser_interruption = {
            "context": str(context),
            "outcome": outcome,
            "final_status": _grbl_status_text(status) if status is not None else "",
            "resync_used": bool(resync_used),
            "reconnect_required": reconnect_required,
            "failure_reason": failure_reason,
        }
        self._laser_interruption_finalized = True
        return not reconnect_required

    def _laser_safety_metadata(self) -> dict:
        laser = self.laser
        if laser is None:
            return {}
        status = getattr(laser, "last_status", None)
        active_pins = sorted(_grbl_active_pins(status)) if status is not None else []
        active_config = self._active_motion_config()
        allow_p_input = self._allow_p_input_for_config(active_config)
        blocking_motion = _grbl_blocking_pins(
            status,
            intent=GRBL_PREP_MOTION,
            allow_p_input_for_firing=allow_p_input,
        ) if status is not None else []
        blocking_firing = _grbl_blocking_pins(
            status,
            intent=GRBL_PREP_FIRING,
            allow_p_input_for_firing=allow_p_input,
        ) if status is not None else []
        settings = getattr(laser, "cached_settings", {}) or {}
        return {
            "laser_profile": active_config.laser_profile if active_config is not None else "",
            "grbl_p_input_nonblocking": bool(allow_p_input),
            "manual_grbl_p_input_nonblocking": bool(self.grbl_p_input_nonblocking),
            "last_grbl_status": _grbl_status_text(status) if status is not None else "",
            "active_pins": active_pins,
            "blocking_pins_motion": blocking_motion,
            "blocking_pins_firing": blocking_firing,
            "coordinate_kind": getattr(status, "coordinate_kind", "") if status is not None else "",
            "work_position": list(getattr(status, "work_position", (None, None, None))) if status is not None else [],
            "machine_position": list(getattr(status, "machine_position", (None, None, None))) if status is not None else [],
            "wco": list(getattr(status, "wco", (None, None, None))) if status is not None else [],
            "controller_settings": dict(settings),
            "reconnect_required": bool(getattr(laser, "reconnect_required", False)),
            "reconnect_required_reason": str(getattr(laser, "reconnect_required_reason", "") or ""),
            "last_interruption": dict(self._last_laser_interruption or {}),
        }

    def _check_run_startup_light(self, intensities) -> None:
        """Abort early when a run's first captures deliver no light at all.

        All-zero frames are legitimate mid-sweep (a window past plasma death
        on a zero-clamped detector), so only the START of a run is watched:
        once any capture contains light the check disarms for the rest of
        the run. Consecutive dark frames from shot 1 mean no light reaches
        the spectrometer at all — laser not emitting or collection fiber
        disconnected (observed 2026-07-26: 120/120 all-zero frames saved
        without a warning).
        """
        if self._run_light_seen or self._automation_run_mode == RUN_MODE_SIMULATION:
            return
        try:
            has_light = bool(np.nanmax(np.asarray(intensities, dtype=float)) > 0)
        except (TypeError, ValueError):
            return
        if has_light:
            self._run_light_seen = True
            self._run_dark_startup_frames = 0
            return
        self._run_dark_startup_frames += 1
        if self._run_dark_startup_frames >= DARK_RUN_STARTUP_SHOT_LIMIT:
            raise RuntimeError(
                f"The first {self._run_dark_startup_frames} captures returned no light at all "
                "(every pixel zero, including the laser-scatter band). Check that the laser is "
                "emitting and the collection fiber is connected, then restart the run."
            )

    def _run_directory_assigned(self) -> bool:
        """True once the active config carries a per-run directory.

        Plan previews configure the worker with ``run_directory=""`` —
        persisting run-level files then would drop them into the PARENT
        data folder (the folder run directories are created in), which is
        exactly how stray manifests ended up in ``~/LIBS_Data``. Run files
        are only written once a run directory exists.
        """
        config = self.automation_config or self.mapping_config
        return bool(str(getattr(config, "run_directory", "") or "").strip())

    def _persist_run_metadata(self) -> None:
        if not self._run_directory_assigned():
            return
        config = self._active_motion_config()
        if config is None:
            return
        if isinstance(config, MappingGridConfig):
            self._persist_mapping_manifest(event="metadata_updated")
            return
        try:
            save_worker_run_metadata(
                self.save_directory,
                config=config,
                spectrometer_info=self._spectrometer_metadata(),
                safety_checklist=self._safety_checklist,
                laser_safety=self._laser_safety_metadata(),
                run_mode=self._metadata_run_mode(),
            )
        except Exception as exc:
            logger.warning("Failed to persist automated run metadata in %s: %s", self.save_directory, exc)

    def _persist_automation_state(self, *, event: str | None = None) -> None:
        if not self._run_directory_assigned():
            return
        state = self.automation_state
        if state is None:
            return
        try:
            save_automation_run_state(self.save_directory, state)
            self._persist_automation_manifest(event=event)
        except Exception as exc:
            logger.warning("Failed to persist automated acquisition state in %s: %s", self.save_directory, exc)

    def _persist_automation_manifest(self, *, event: str | None = None) -> None:
        if not self._run_directory_assigned():
            return
        config = self.automation_config
        state = self.automation_state
        if config is None:
            return
        try:
            save_worker_automation_manifest(
                self.save_directory,
                config=config,
                state=state,
                completed_dry_run_signature=self.completed_dry_run_signature,
                safety_checklist=self._safety_checklist,
                command_transcript=self._laser_command_transcript(),
                qc=self._last_qc_manifest,
                laser_safety=self._laser_safety_metadata(),
                event=event,
                run_mode=self._metadata_run_mode(),
            )
        except Exception as exc:
            logger.warning("Failed to persist automated acquisition manifest in %s: %s", self.save_directory, exc)

    def _persist_automation_summary(self) -> None:
        if not self._run_directory_assigned():
            return
        config = self.automation_config
        state = self.automation_state
        if config is None or state is None:
            return
        try:
            save_worker_automation_summary(
                self.save_directory,
                config=config,
                state=state,
                latest_timing_sample=getattr(self, "latest_timing_sample", None),
                run_mode=self._metadata_run_mode(),
            )
        except Exception as exc:
            logger.warning("Failed to persist automated acquisition summary in %s: %s", self.save_directory, exc)

    def _persist_mapping_state(self, *, event: str | None = None) -> None:
        if not self._run_directory_assigned():
            return
        state = self.mapping_state
        if state is None:
            return
        try:
            with self._mapping_state_lock:
                # Durability order: flush the spectrum store first (making
                # intensities and mask bits durable) and only then write the
                # state JSON that claims those targets — resume unions state
                # with the mask and never subtracts. The flush is called
                # directly (not only via the manifest, which swallows its own
                # errors) so a failed flush skips the state claim entirely.
                store = self._mapping_spectrum_store
                if store is not None:
                    store.flush()
                self._persist_mapping_manifest(event=event)
                save_mapping_run_state(self.save_directory, state)
        except Exception as exc:
            logger.warning("Failed to persist mapping acquisition state in %s: %s", self.save_directory, exc)

    def _persist_mapping_manifest(self, *, event: str | None = None) -> None:
        if not self._run_directory_assigned():
            return
        config = self.mapping_config
        if config is None:
            return
        try:
            if self._mapping_spectrum_store is not None:
                self._mapping_spectrum_store.write_manifest(force=True)
            save_mapping_manifest(
                self.save_directory,
                config=config,
                state=self.mapping_state,
                completed_dry_run_signature=self.completed_dry_run_signature,
                safety_checklist=self._safety_checklist,
                command_transcript=self._laser_command_transcript(),
                laser_safety=self._laser_safety_metadata(),
                spectrometer_info=self._spectrometer_metadata(),
                event=event,
                run_mode=self._metadata_run_mode(),
                effective_move_speed_mm_min=self._mapping_effective_move_speed_mm_min,
                pause_events=self._pause_events,
                background_reference=self._mapping_background_info,
            )
        except Exception as exc:
            logger.warning("Failed to persist mapping acquisition manifest in %s: %s", self.save_directory, exc)

    def _persist_mapping_summary(self) -> None:
        if not self._run_directory_assigned():
            return
        config = self.mapping_config
        state = self.mapping_state
        if config is None or state is None:
            return
        try:
            timing_summary: dict = {}
            if getattr(self, "latest_timing_sample", None):
                timing_summary["latest"] = self.latest_timing_sample
            observed = self._mapping_observed_timing()
            if observed is not None:
                timing_summary["observed"] = observed
            # Zero means a verified-clean trigger FIFO; nonzero events carry the
            # exact per-boundary frame lag needed to re-index an affected run.
            timing_summary["trigger_fifo"] = {
                "stale_frames_drained_total": int(self._mapping_stale_frames_drained),
                "drain_events": list(self._mapping_trigger_drain_events),
            }
            if self._mapping_stream_stats is not None:
                timing_summary["stream"] = dict(self._mapping_stream_stats)
            save_mapping_summary(
                self.save_directory,
                config=config,
                state=state,
                timing_summary=timing_summary,
                run_mode=self._metadata_run_mode(),
                pause_events=self._pause_events,
                background_reference=self._mapping_background_info,
            )
        except Exception as exc:
            logger.warning("Failed to persist mapping acquisition summary in %s: %s", self.save_directory, exc)

    def _mapping_observed_timing(self) -> dict | None:
        """Average per-shot cycle time observed this session, for ETA seeding."""
        started_at = getattr(self, "_mapping_run_started_at", None)
        state = self.mapping_state
        if started_at is None or state is None:
            return None
        completed = max(
            0,
            len(state.completed_targets) - int(getattr(self, "_mapping_completed_at_run_start", 0) or 0),
        )
        if completed <= 0:
            return None
        elapsed_s = max(0.0, time.perf_counter() - started_at)
        if elapsed_s <= 0:
            return None
        return {
            "completed_shots": completed,
            "elapsed_s": round(elapsed_s, 3),
            "avg_cycle_s": round(elapsed_s / completed, 4),
            "run_mode": self._metadata_run_mode(),
        }

    def _spectrometer_metadata(self) -> dict:
        metadata = super()._spectrometer_metadata()
        if self.mapping_config is not None:
            metadata["mapping"] = {
                "experiment_name": self.mapping_config.experiment_name,
                "run_directory": self.mapping_config.run_directory,
                "origin_x_mm": self.mapping_config.origin_x_mm,
                "origin_y_mm": self.mapping_config.origin_y_mm,
                "x_length_mm": self.mapping_config.x_length_mm,
                "y_length_mm": self.mapping_config.y_length_mm,
                "step_mm": self.mapping_config.step_mm,
                "shots_per_point": self.mapping_config.shots_per_point,
                "laser_power_percent": self.mapping_config.laser_power_percent,
                "laser_s_value": self.mapping_config.laser_s_value,
                "pulse_ms": self.mapping_config.pulse_ms,
                "move_speed_mm_min": self.mapping_config.move_speed_mm_min,
                "settle_ms": self.mapping_config.settle_ms,
                "column_delays_us": list(self.mapping_config.column_delays_us),
                "row_integration_times_ms": list(self.mapping_config.row_integration_times_ms),
            }
        elif self.automation_config is not None:
            metadata["automation"] = {
                "experiment_name": self.automation_config.experiment_name,
                "plate_names_by_index": dict(self.automation_config.plate_names_by_index),
                "run_directory": self.automation_config.run_directory,
                "plate_model": self.automation_config.plate_model.to_mapping(),
                "orientation": self.automation_config.orientation,
                "laser_power_percent": self.automation_config.laser_power_percent,
                "laser_s_value": self.automation_config.laser_s_value,
                "pulse_ms": self.automation_config.pulse_ms,
                "move_speed_mm_min": self.automation_config.move_speed_mm_min,
                "settle_ms": self.automation_config.settle_ms,
                "column_integration_times_ms": list(self.automation_config.column_integration_times_ms),
                "column_delays_us": list(self.automation_config.column_delays_us),
                "plate_integration_times_ms": list(self.automation_config.plate_integration_times_ms),
            }
        if self.laser is not None:
            metadata["laser_controller"] = {
                "class": type(self.laser).__name__,
                "port": getattr(self.laser, "port", ""),
                "baudrate": getattr(self.laser, "baudrate", None),
            }
        return metadata

    def _run_spectra_qc_if_enabled(self, *, force: bool = False):
        if not (force or self._spectra_qc_enabled):
            return None
        config = self.automation_config
        if config is None:
            self._send(AcquisitionMessage.QC_ERROR, "No automated plate plan is configured.")
            return None
        self._send(AcquisitionMessage.STATUS, "Running spectra readiness QC...")
        load_result = load_default_spectra_qc()
        if not load_result.available or load_result.qc is None:
            self._last_qc_manifest = qc_unavailable_manifest(load_result)
            self._persist_automation_manifest(event="spectra_qc_unavailable")
            self._send(AcquisitionMessage.QC_ERROR, load_result.message)
            return None
        try:
            result = run_automated_spectra_qc(
                self.save_directory,
                config,
                automation_plan_details(config),
                qc=load_result.qc,
            )
        except Exception as exc:
            self._last_qc_manifest = qc_error_manifest(exc)
            self._persist_automation_manifest(event="spectra_qc_error")
            self._send(AcquisitionMessage.QC_ERROR, str(exc))
            return None

        progress_payloads = []
        with self._automation_lock:
            apply_spectra_qc_to_plate_states(result, self._plate_states)
            for plate_index in sorted(self._plate_states):
                self._active_plate_index = plate_index
                self._plate_run_state = self._plate_states[plate_index]
                self._persist_plate_state_locked()
                self._persist_plate_reproducibility_log(event="spectra_qc_complete")
                payload = self._progress_payload_for_plate(plate_index)
                progress_payloads.append(payload)
        self._last_qc_manifest = qc_success_manifest(result)
        self._persist_automation_manifest(event="spectra_qc_complete")
        for payload in progress_payloads:
            self._send(AcquisitionMessage.PLATE_PROGRESS, payload)
        self._send(AcquisitionMessage.QC_COMPLETE, result.to_mapping())
        return result
