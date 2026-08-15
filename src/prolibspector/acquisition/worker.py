"""Background worker thread for Acquisition Mode data capture.

Handles both continuous live polling and hardware-triggered acquisition states.
"""

# Communicates with the GUI via a thread-safe queue.

import threading
import queue
import time
import numpy as np
import logging
import os
from prolibspector.core.settings import default_save_directory
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, Future, TimeoutError as FutureTimeoutError
from prolibspector.acquisition.plate_autosave import (
    PlateAutosaveConfig,
    PlateRunState,
    append_plate_reproducibility_event,
    sanitize_filename_part,
    unique_output_path,
    save_plate_run_state,
    save_plate_reproducibility_log,
)
from prolibspector.acquisition.corrections import resolve_effective_corrections
from prolibspector.acquisition.spectra_readiness_qc import (
    apply_spectra_qc_to_plate_states,
    load_default_spectra_qc,
    run_high_throughput_spectra_qc,
)

logger = logging.getLogger(__name__)


class AutoSaveError(RuntimeError):
    """Raised when a captured spectrum cannot be safely persisted."""


class AcquisitionMessage:
    """Message types sent from the worker thread to the GUI."""
    SPECTRUM = "spectrum"          # New spectrum data available
    STATUS = "status"              # Status text update
    ERROR = "error"                # An error occurred
    ARMED = "armed"                # Worker is armed and waiting for trigger
    CAPTURED = "captured"          # A triggered capture completed
    STOPPED = "stopped"            # Worker has stopped
    SAVE_COMPLETE = "save_complete"  # Auto-save finished
    TIMING = "timing"                # Timing sample for benchmarking
    IDLE = "idle"                      # Worker returned to idle (buttons should reset)
    PLATE_PROGRESS = "plate_progress"  # High-throughput plate progress changed
    PLATE_COMPLETE = "plate_complete"  # High-throughput plate run finished
    PLATE_DISCARDED = "plate_discarded"  # Last high-throughput shot was discarded
    PLATE_REPAIR_STARTED = "plate_repair_started"  # Specific wells were queued for repair
    PLATE_REPAIR_COMPLETE = "plate_repair_complete"  # Specific-well repair pass finished
    QC_COMPLETE = "qc_complete"  # Automated spectra readiness QC finished
    QC_ERROR = "qc_error"  # Automated spectra readiness QC could not run


class AcquisitionWorker(threading.Thread):
    """
    Background thread that controls spectrometer acquisition.
    
    States:
        IDLE  - Not acquiring. Thread is alive but sleeping.
        LIVE  - Continuous polling (Normal trigger mode). For live preview.
        ARMED - Waiting for external hardware trigger. Blocks until laser fires.
    
    Trigger mode integers are read from ``spec.capabilities`` at runtime
    so they match whatever spectrometer model is connected.
    
    All GUI updates are pushed to a queue.Queue and must be consumed by the
    main thread using root.after() polling.
    """

    # States
    STATE_IDLE = "IDLE"
    STATE_LIVE = "LIVE"
    STATE_ARMED = "ARMED"
    STATE_TEST = "TEST"

    def __init__(self, spectrometer_module):
        """
        Args:
            spectrometer_module: A connected SpectrometerBase instance
                (SpectrometerModule for Ocean Optics, ThorlabsCCSModule for Thorlabs).
        """
        super().__init__(daemon=True, name="AcquisitionWorker")
        self.spec = spectrometer_module
        self.message_queue = queue.Queue()

        # State management
        self._state = self.STATE_IDLE
        self._state_lock = threading.Lock()
        self._hardware_lock = threading.RLock()
        self._trigger_read_active = threading.Event()

        # Control events
        self._stop_event = threading.Event()
        self._state_change_event = threading.Event()

        # Auto-save configuration
        self.auto_save_enabled = True
        self.save_directory = default_save_directory(use_pref=False)
        self.sample_name = "Sample"
        self._shot_index = 0
        self._plate_lock = threading.Lock()
        self._plate_run_state = None
        self._spectra_qc_enabled = False
        self.collect_timing_metrics = False

        # Faster active polling keeps live view and trigger-state feedback snappy.
        self.live_poll_interval = 0.02   # 20 ms between live polls
        self.armed_poll_interval = 0.05  # 50 ms between trigger future checks

        # Averaging
        self.averages = 1  # Number of spectra to average in LIVE mode

        # Correction flags (passed to spectrometer.get_intensities when supported)
        self.correct_dark_counts = True
        self.correct_nonlinearity = True

    # ─── State Management ──────────────────────────────────────────────

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    def _set_state(self, new_state: str):
        with self._state_lock:
            old_state = self._state
            self._state = new_state
        logger.info(f"Worker state: {old_state} → {new_state}")
        self._state_change_event.set()

    @property
    def hardware_read_active(self) -> bool:
        """Return True while a blocking hardware read is still in progress."""
        return self._trigger_read_active.is_set()

    def _set_trigger_mode(self, mode: int):
        with self._hardware_lock:
            self.spec.set_trigger_mode(mode)

    def _correction_kwargs(self) -> dict[str, bool]:
        return self._effective_corrections().to_kwargs()

    def _effective_corrections(self):
        return resolve_effective_corrections(
            getattr(self.spec, "capabilities", None),
            requested_dark_counts=bool(self.correct_dark_counts),
            requested_nonlinearity=bool(self.correct_nonlinearity),
        )

    def _get_wavelengths(self):
        with self._hardware_lock:
            return self.spec.get_wavelengths()

    def _get_intensities(self):
        with self._hardware_lock:
            return self.spec.get_intensities(**self._correction_kwargs())

    def _get_intensities_for_trigger_read(self):
        self._trigger_read_active.set()
        try:
            return self._get_intensities()
        finally:
            self._trigger_read_active.clear()

    def _cancel_pending_trigger_read(self, future: Future) -> bool:
        """Cancel a pending external-trigger read if the backend supports it.

        Returns True when the backend handled trigger-mode recovery itself and
        the caller should skip the normal final trigger reset.
        """
        cancel_pending_read = getattr(self.spec, "cancel_pending_read", None)
        if callable(cancel_pending_read):
            self._send(AcquisitionMessage.STATUS, "Resetting spectrometer...")
            reset_ok = False
            try:
                reset_ok = bool(cancel_pending_read(timeout_s=1.0))
            except Exception as exc:
                logger.exception("Spectrometer reset failed during trigger cancel.")
                self._send(
                    AcquisitionMessage.ERROR,
                    f"Spectrometer reset failed. Reconnect spectrometer. ({exc})",
                )
                return True

            if not reset_ok:
                release_orphaned_process = getattr(self.spec, "release_orphaned_process", None)
                if callable(release_orphaned_process):
                    try:
                        release_orphaned_process()
                    except Exception:
                        logger.exception("Failed to release orphaned spectrometer broker.")

            future_unwound = True
            try:
                future.result(timeout=2.0)
            except FutureTimeoutError:
                future_unwound = False
                logger.error("Cancelled trigger read did not unwind after broker reset.")
                self._send(
                    AcquisitionMessage.ERROR,
                    "Spectrometer reset did not finish cleanly. Reconnect spectrometer.",
                )
            except Exception as exc:
                logger.info("Cancelled trigger read finished with error: %s", exc)

            if reset_ok and future_unwound:
                self._send(AcquisitionMessage.STATUS, "Spectrometer reset complete.")
            elif not reset_ok:
                self._send(
                    AcquisitionMessage.ERROR,
                    "Spectrometer reset failed. Reconnect spectrometer.",
                )
            return True

        self._send(AcquisitionMessage.STATUS, "Returning spectrometer to normal trigger mode...")
        try:
            normal_mode = self.spec.capabilities.normal_trigger_mode
            # Match the pre-broker manual Ocean Optics behavior: this call must
            # bypass _hardware_lock because the trigger-read thread owns that
            # lock while SeaBreeze is blocked waiting for the external edge.
            self.spec.set_trigger_mode(normal_mode)
        except Exception as exc:
            logger.exception("Failed to reset trigger mode during direct trigger cancel.")
            self._send(
                AcquisitionMessage.ERROR,
                f"Trigger cancel failed. Reconnect spectrometer. ({exc})",
            )
            return True

        try:
            future.result(timeout=2.0)
        except FutureTimeoutError:
            logger.warning("Direct trigger read did not unwind after normal trigger reset.")
            self._send(
                AcquisitionMessage.STATUS,
                "Trigger cancelled; pending hardware read is still unwinding.",
            )
            return True
        except Exception as exc:
            logger.info("Cancelled trigger read finished with error: %s", exc)
        return False

    # ─── Control Methods (called from GUI thread) ─────────────────────

    def start_live(self):
        """Switch to LIVE mode (continuous spectrum polling)."""
        if not self.spec.is_connected:
            self._send(AcquisitionMessage.ERROR, "Spectrometer not connected.")
            return
        try:
            normal_mode = self.spec.capabilities.normal_trigger_mode
            self._set_trigger_mode(normal_mode)
        except Exception as e:
            self._send(AcquisitionMessage.ERROR, str(e))
            return
        self._set_state(self.STATE_LIVE)
        self._send(AcquisitionMessage.STATUS, "Live view started")

    def arm_trigger(self):
        """Switch to ARMED mode (wait for external hardware trigger)."""
        if not self.spec.is_connected:
            self._send(AcquisitionMessage.ERROR, "Spectrometer not connected.")
            return

        ext_mode = self.spec.capabilities.external_trigger_mode
        if ext_mode is None:
            self._send(AcquisitionMessage.ERROR,
                       f"{self.spec.model} does not support an external hardware trigger.")
            return

        try:
            self._set_trigger_mode(ext_mode)
        except Exception as e:
            self._send(AcquisitionMessage.ERROR, str(e))
            return
        self._set_state(self.STATE_ARMED)
        self._send(AcquisitionMessage.STATUS, "Armed — waiting for trigger...")
        self._send(AcquisitionMessage.ARMED, None)

    def go_idle(self):
        """Return to IDLE state. Stops live view or disarms trigger."""
        was_waiting_on_trigger = self.state == self.STATE_ARMED and self.hardware_read_active
        self._set_state(self.STATE_IDLE)
        if was_waiting_on_trigger:
            self._send(
                AcquisitionMessage.STATUS,
                "Stop requested; waiting for pending trigger read to finish.",
            )
        else:
            self._send(AcquisitionMessage.STATUS, "Idle")

    def test_trigger(self):
        """Queue a test capture to run on the worker thread.
        Does a normal-mode read pushed through the full capture pipeline
        (plot, shot counter, auto-save) to verify software is working
        before waiting for the real laser pulse."""
        if not self.spec.is_connected:
            self._send(AcquisitionMessage.ERROR, "Spectrometer not connected.")
            return
        self._set_state(self.STATE_TEST)
        self._send(AcquisitionMessage.STATUS, "Running test capture...")

    def stop(self):
        """Signal the worker thread to terminate."""
        self._stop_event.set()
        self._state_change_event.set()  # Wake up if sleeping

    def _wait_for_interruptible_interval(self, interval_s: float):
        """Wait for a short interval while still responding to state changes."""
        if interval_s <= 0:
            return
        self._state_change_event.wait(timeout=interval_s)
        self._state_change_event.clear()

    def reset_shot_index(self):
        """Reset the shot counter (e.g., when sample name changes)."""
        self._shot_index = 0

    def enable_timing_metrics(self, enabled: bool = True):
        """Enable or disable per-shot timing messages for benchmarking."""
        self.collect_timing_metrics = enabled

    def set_spectra_qc_enabled(self, enabled: bool) -> None:
        self._spectra_qc_enabled = bool(enabled)

    def set_plate_autosave_config(self, config):
        """Enable high-throughput plate autosave with a fresh plate run."""
        if isinstance(config, PlateAutosaveConfig):
            plate_config = config
        else:
            plate_config = PlateAutosaveConfig.from_mapping(config)

        with self._plate_lock:
            self._plate_run_state = PlateRunState(plate_config)
            self.collect_timing_metrics = True
            self._persist_plate_state_locked()
            self._persist_plate_reproducibility_log(event="plate_configured")
            payload = self._plate_run_state.progress_payload()

        self._send(AcquisitionMessage.PLATE_PROGRESS, payload)

    def resume_plate_autosave(self, state):
        """Resume a previously saved high-throughput plate run."""
        if isinstance(state, PlateRunState):
            plate_state = state
        else:
            plate_state = PlateRunState.from_mapping(state)

        with self._plate_lock:
            self._plate_run_state = plate_state
            self.collect_timing_metrics = True
            self._persist_plate_state_locked()
            self._persist_plate_reproducibility_log(event="plate_resumed")
            payload = self._plate_run_state.progress_payload()

        self._send(AcquisitionMessage.PLATE_PROGRESS, payload)

    def disable_plate_autosave(self):
        """Disable high-throughput plate autosave."""
        with self._plate_lock:
            self._plate_run_state = None
        self.collect_timing_metrics = False

    def close_plate_run_early(self):
        """Persist the current plate as closed early, then stop plate autosave."""
        with self._plate_lock:
            if self._plate_run_state is None:
                return None

            payload = self._plate_run_state.progress_payload()
            payload["closed_early"] = True
            payload["current_well"] = None
            payload["can_discard"] = False
            self._persist_plate_state_locked(closed_early=True)
            self._persist_plate_reproducibility_log(event="plate_closed_early", closed_early=True)
            self._plate_run_state = None

        return payload

    def discard_last_plate_shot(self):
        """Move the latest plate shot to Discarded and roll progress back."""
        with self._plate_lock:
            if self._plate_run_state is None:
                self._send(AcquisitionMessage.ERROR, "No active plate run to discard from.")
                return

            plate_dir = os.path.join(
                self.save_directory,
                self._plate_run_state.config.safe_plate_name,
            )
            discarded_dir = os.path.join(plate_dir, "Discarded")
            record, payload = self._plate_run_state.discard_last(discarded_dir)
            self._persist_plate_state_locked()
            self._persist_plate_reproducibility_log(event="plate_shot_discarded")

        if record is None:
            self._send(AcquisitionMessage.STATUS, "No plate shot to discard.")
            return

        self._send(AcquisitionMessage.PLATE_DISCARDED, payload)
        self._send(AcquisitionMessage.PLATE_PROGRESS, payload)
        self._send(
            AcquisitionMessage.STATUS,
            f"Discarded {record.well} shot {record.shot_number}; repeat this shot.",
        )

    def start_plate_repair(self, wells):
        """Queue a specific-well repair pass and move the old files aside."""
        selected_wells = [str(well).upper() for well in wells if str(well).strip()]
        if not selected_wells:
            self._send(AcquisitionMessage.ERROR, "Select at least one well to repair.")
            return

        with self._plate_lock:
            if self._plate_run_state is None:
                self._send(AcquisitionMessage.ERROR, "No active plate run to repair.")
                return

            plate_dir = os.path.join(
                self.save_directory,
                self._plate_run_state.config.safe_plate_name,
            )
            discarded_dir = os.path.join(plate_dir, "Discarded")

            try:
                _, payload = self._plate_run_state.start_repair(selected_wells, discarded_dir)
            except (RuntimeError, ValueError) as exc:
                self._send(AcquisitionMessage.ERROR, str(exc))
                return

            self._persist_plate_state_locked()
            self._persist_plate_reproducibility_log(event="plate_repair_started")

        self._send(AcquisitionMessage.PLATE_REPAIR_STARTED, payload)
        self._send(AcquisitionMessage.PLATE_PROGRESS, payload)
        self._send(
            AcquisitionMessage.STATUS,
            f"Repair queued for {', '.join(payload.get('repair_queue', []))}.",
        )

    def queue_qc_repeats(self, failed_wells):
        """Queue selected QC failed/warned wells for repair without acquiring automatically."""
        selected_wells = []
        for item in failed_wells or []:
            if isinstance(item, dict):
                well = item.get("well", "")
            else:
                well = item
            well = str(well).strip().upper()
            if well and well not in selected_wells:
                selected_wells.append(well)
        if not selected_wells:
            self._send(AcquisitionMessage.ERROR, "Select at least one QC well to repeat.")
            return {"queued_wells": 0}

        with self._plate_lock:
            if self._plate_run_state is None:
                self._send(AcquisitionMessage.ERROR, "No active plate run to repair.")
                return {"queued_wells": 0}

            plate_dir = os.path.join(self.save_directory, self._plate_run_state.config.safe_plate_name)
            discarded_dir = os.path.join(plate_dir, "Discarded", "QC")
            try:
                removed, payload = self._plate_run_state.start_repair(selected_wells, discarded_dir)
            except (RuntimeError, ValueError) as exc:
                self._send(AcquisitionMessage.ERROR, str(exc))
                return {"queued_wells": 0}

            queued_wells = len({record.well for record in removed}) or len(payload.get("repair_queue", []))
            self._persist_plate_state_locked()
            self._persist_plate_reproducibility_log(event="qc_repair_queued")

        self._send(AcquisitionMessage.PLATE_REPAIR_STARTED, payload)
        self._send(AcquisitionMessage.PLATE_PROGRESS, payload)
        self._send(
            AcquisitionMessage.STATUS,
            f"Queued {queued_wells} QC repeat well(s). Arm acquisition to repeat them.",
        )
        return {"queued_wells": queued_wells, "targets": queued_wells}

    def run_plate_qc_now(self, *, background: bool = True):
        """Run spectra-readiness QC for the active high-throughput plate."""
        if background:
            threading.Thread(target=self._run_plate_qc, daemon=True, name="PlateQCThread").start()
            return None
        return self._run_plate_qc()

    def _run_plate_qc_if_enabled(self):
        if self._spectra_qc_enabled:
            self._run_plate_qc()

    def _run_plate_qc(self):
        with self._plate_lock:
            plate_state = self._plate_run_state
        if plate_state is None:
            self._send(AcquisitionMessage.QC_ERROR, "No active high-throughput plate is configured.")
            return None

        self._send(AcquisitionMessage.STATUS, "Running spectra readiness QC...")
        load_result = load_default_spectra_qc()
        if not load_result.available or load_result.qc is None:
            self._send(AcquisitionMessage.QC_ERROR, load_result.message)
            return None

        try:
            result = run_high_throughput_spectra_qc(
                self.save_directory,
                plate_state,
                qc=load_result.qc,
            )
        except Exception as exc:
            logger.exception("High-throughput spectra QC failed.")
            self._send(AcquisitionMessage.QC_ERROR, str(exc))
            return None

        with self._plate_lock:
            if self._plate_run_state is not plate_state:
                self._send(AcquisitionMessage.QC_ERROR, "Plate changed before QC results could be applied.")
                return None
            apply_spectra_qc_to_plate_states(result, plate_state)
            self._persist_plate_state_locked()
            self._persist_plate_reproducibility_log(event="spectra_qc_complete")
            payload = plate_state.progress_payload()

        self._send(AcquisitionMessage.PLATE_PROGRESS, payload)
        self._send(AcquisitionMessage.QC_COMPLETE, result.to_mapping())
        return result

    # ─── Thread Main Loop ─────────────────────────────────────────────

    def run(self):
        """Main thread loop — dispatches to state handlers."""
        logger.info("Acquisition worker started.")
        try:
            while not self._stop_event.is_set():
                current_state = self.state

                if current_state == self.STATE_LIVE:
                    self._run_live()
                elif current_state == self.STATE_ARMED:
                    self._run_armed()
                elif current_state == self.STATE_TEST:
                    self._run_test()
                else:
                    # IDLE — wait for a state change event
                    self._state_change_event.wait(timeout=0.5)
                    self._state_change_event.clear()
        except Exception as e:
            logger.error(f"Worker thread exception: {e}")
            self._send(AcquisitionMessage.ERROR, f"Worker error: {e}")
        finally:
            self._send(AcquisitionMessage.STOPPED, None)
            logger.info("Acquisition worker stopped.")

    def _run_live(self):
        """Continuous polling loop for live preview."""
        while self.state == self.STATE_LIVE and not self._stop_event.is_set():
            timing = None
            if self.collect_timing_metrics:
                timing = {
                    "mode": "live",
                    "shot_index": self._shot_index,
                    "cycle_start": time.perf_counter(),
                }
            try:
                with self._hardware_lock:
                    if self.averages > 1:
                        # Average multiple spectra
                        accumulated = None
                        for _ in range(self.averages):
                            intensities = self.spec.get_intensities(**self._correction_kwargs())
                            if accumulated is None:
                                accumulated = intensities.astype(np.float64)
                            else:
                                accumulated += intensities
                        intensities = accumulated / self.averages
                    else:
                        intensities = self.spec.get_intensities(**self._correction_kwargs())
                    if timing is not None:
                        timing["capture_end"] = time.perf_counter()

                    if timing is not None:
                        timing["wavelengths_fetch_start"] = time.perf_counter()
                    wavelengths = self.spec.get_wavelengths()
                    if timing is not None:
                        timing["wavelengths_fetch_end"] = time.perf_counter()
                self._send(AcquisitionMessage.SPECTRUM, (wavelengths, intensities))

            except Exception as e:
                if self.state == self.STATE_LIVE:
                    self._send(AcquisitionMessage.ERROR, f"Live acquisition error: {e}")
                    self.go_idle()
                    return

            # Rate limit while remaining responsive to stop/disarm actions.
            self._wait_for_interruptible_interval(self.live_poll_interval)
            if timing is not None:
                timing["cycle_end"] = time.perf_counter()
                timing["message_sent"] = timing["cycle_end"]
                self._emit_timing_sample(timing)

    def _run_armed(self):
        """Wait for hardware trigger, capture, auto-save, then re-arm.

        Automatically loops: after each successful capture the worker
        re-arms and waits for the next trigger.  The loop runs until the
        user clicks Stop (which sets state to IDLE / sets _stop_event).

        The blocking intensities() call runs in a disposable thread so the GUI
        can remain responsive. If cancellation arrives while a hardware read is
        running, the worker waits for that read before changing hardware state.
        """
        skip_final_trigger_reset = False
        while self.state == self.STATE_ARMED and not self._stop_event.is_set():
            timing = None
            if self.collect_timing_metrics:
                timing = {
                    "mode": "armed",
                    "shot_index": self._shot_index + 1,
                    "trigger_wait_start": time.perf_counter(),
                }
            try:
                self._send(AcquisitionMessage.STATUS,
                           "Armed — waiting for laser trigger…")

                # Ensure external trigger mode is set (needed on re-arm)
                ext_mode = self.spec.capabilities.external_trigger_mode
                if ext_mode is not None and self.spec.current_trigger_mode != ext_mode:
                    self._set_trigger_mode(ext_mode)

                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="TriggerRead")
                future: Future = executor.submit(self._get_intensities_for_trigger_read)
                cancel_requested = False

                try:
                    while not future.done():
                        if self.state != self.STATE_ARMED or self._stop_event.is_set():
                            cancel_requested = True
                            break
                        self._wait_for_interruptible_interval(self.armed_poll_interval)

                    if cancel_requested:
                        skip_final_trigger_reset = self._cancel_pending_trigger_read(future)
                        break
                finally:
                    executor.shutdown(
                        wait=not (cancel_requested and skip_final_trigger_reset),
                        cancel_futures=cancel_requested,
                    )

                # Capture completed
                if timing is not None:
                    timing["trigger_wait_end"] = time.perf_counter()
                intensities = future.result()
                if timing is not None:
                    timing["capture_end"] = time.perf_counter()

                if timing is not None:
                    timing["wavelengths_fetch_start"] = time.perf_counter()
                wavelengths = self._get_wavelengths()
                if timing is not None:
                    timing["wavelengths_fetch_end"] = time.perf_counter()

                self._shot_index += 1
                self._send(AcquisitionMessage.STATUS,
                           f"Captured shot #{self._shot_index}; saving...")

                # Auto-save
                plate_complete = False
                if self.auto_save_enabled:
                    if timing is not None:
                        timing["save_start"] = time.perf_counter()
                    self._auto_save(wavelengths, intensities, consume_plate=True, timing=timing)
                    if timing is not None and "save_end" not in timing:
                        timing["save_end"] = time.perf_counter()
                    if self._plate_mode_enabled():
                        self._persist_plate_reproducibility_log(
                            event="plate_shot_saved",
                            timing=timing,
                        )
                    plate_complete = self._is_plate_complete()
                    if plate_complete:
                        self._send(AcquisitionMessage.STATUS, "Plate complete.")
                elif timing is not None:
                    timing["save_start"] = timing["save_end"] = time.perf_counter()

                self._send(AcquisitionMessage.SPECTRUM, (wavelengths, intensities))
                self._send(AcquisitionMessage.CAPTURED, {
                    "wavelengths": wavelengths,
                    "intensities": intensities,
                    "shot_index": self._shot_index,
                })

                if timing is not None:
                    timing["rearm_start"] = time.perf_counter()
                    self._emit_timing_sample(timing)

                if plate_complete:
                    self._run_plate_qc_if_enabled()
                if plate_complete:
                    break

            except AutoSaveError as e:
                self._send(AcquisitionMessage.ERROR, str(e))
                break
            except Exception as e:
                error_msg = str(e)
                if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                    self._send(AcquisitionMessage.STATUS,
                               "Trigger timed out — re-arming…")
                    continue  # re-arm even after timeout
                else:
                    self._send(AcquisitionMessage.ERROR,
                               f"Trigger capture error: {e}")
                    break  # exit loop on real error

        # Return to idle
        if self.spec.is_connected and not skip_final_trigger_reset:
            try:
                normal_mode = self.spec.capabilities.normal_trigger_mode
                self._set_trigger_mode(normal_mode)
            except Exception:
                pass
        self._set_state(self.STATE_IDLE)
        self._send(AcquisitionMessage.IDLE, None)

    def _run_test(self):
        """One-shot normal-mode read pushed through the full capture pipeline."""
        timing = None
        if self.collect_timing_metrics:
            timing = {
                "mode": "test",
                "shot_index": self._shot_index + 1,
                "trigger_wait_start": time.perf_counter(),
            }
        try:
            # Ensure normal mode for immediate read
            normal_mode = self.spec.capabilities.normal_trigger_mode
            with self._hardware_lock:
                if self.spec.current_trigger_mode != normal_mode:
                    self.spec.set_trigger_mode(normal_mode)

                intensities = self.spec.get_intensities(**self._correction_kwargs())
                if timing is not None:
                    timing["capture_end"] = time.perf_counter()
                if timing is not None:
                    timing["wavelengths_fetch_start"] = time.perf_counter()
                wavelengths = self.spec.get_wavelengths()
                if timing is not None:
                    timing["wavelengths_fetch_end"] = time.perf_counter()

            self._shot_index += 1
            self._send(AcquisitionMessage.STATUS,
                        f"Test capture #{self._shot_index}; saving...")

            plate_complete = False
            if self.auto_save_enabled:
                if timing is not None:
                    timing["save_start"] = time.perf_counter()
                self._auto_save(
                    wavelengths,
                    intensities,
                    consume_plate=self._plate_mode_enabled(),
                    timing=timing,
                )
                if timing is not None and "save_end" not in timing:
                    timing["save_end"] = time.perf_counter()
                if self._plate_mode_enabled():
                    self._persist_plate_reproducibility_log(
                        event="plate_shot_saved",
                        timing=timing,
                    )
                plate_complete = self._is_plate_complete()
                if plate_complete:
                    self._send(AcquisitionMessage.STATUS, "Plate complete.")
            elif timing is not None:
                timing["save_start"] = timing["save_end"] = time.perf_counter()

            self._send(AcquisitionMessage.SPECTRUM, (wavelengths, intensities))
            self._send(AcquisitionMessage.CAPTURED, {
                "wavelengths": wavelengths,
                "intensities": intensities,
                "shot_index": self._shot_index,
            })

            if timing is not None:
                timing["rearm_start"] = time.perf_counter()
                self._emit_timing_sample(timing)

            if plate_complete:
                self._run_plate_qc_if_enabled()

        except AutoSaveError as e:
            self._send(AcquisitionMessage.ERROR, str(e))
        except Exception as e:
            self._send(AcquisitionMessage.ERROR, f"Test trigger failed: {e}")
        finally:
            self._set_state(self.STATE_IDLE)
            self._send(AcquisitionMessage.IDLE, None)

    # ─── Auto-Save ─────────────────────────────────────────────────────

    def _auto_save(
        self,
        wavelengths: np.ndarray,
        intensities: np.ndarray,
        consume_plate: bool = True,
        timing: dict | None = None,
    ):
        """Save the captured spectrum to a timestamped CSV file."""
        try:
            if consume_plate:
                with self._plate_lock:
                    if self._plate_run_state is not None:
                        self._auto_save_plate_locked(wavelengths, intensities, timing=timing)
                        return

            self._auto_save_standard(wavelengths, intensities, timing=timing)

        except Exception as e:
            raise AutoSaveError(f"Auto-save failed: {e}") from e

    def _auto_save_standard(
        self,
        wavelengths: np.ndarray,
        intensities: np.ndarray,
        timing: dict | None = None,
    ):
        os.makedirs(self.save_directory, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        sample_name = sanitize_filename_part(self.sample_name, fallback="Sample")
        filename = f"{sample_name}_{timestamp}_{self._shot_index:03d}.csv"
        filepath = unique_output_path(self.save_directory, filename)

        if timing is not None:
            timing["save_file_path"] = filepath
        self._save_spectrum_file(filepath, wavelengths, intensities)
        if timing is not None:
            timing["save_end"] = time.perf_counter()

        self._send(AcquisitionMessage.SAVE_COMPLETE, filepath)
        logger.info(f"Auto-saved: {filepath}")

    def _auto_save_plate_locked(
        self,
        wavelengths: np.ndarray,
        intensities: np.ndarray,
        timing: dict | None = None,
    ):
        plate_state = self._plate_run_state
        if plate_state is None:
            self._auto_save_standard(wavelengths, intensities, timing=timing)
            return

        assignment = plate_state.next_assignment()
        if assignment is None:
            self._send(AcquisitionMessage.PLATE_COMPLETE, plate_state.progress_payload())
            return

        well, shot_number = assignment
        plate_dir = os.path.join(self.save_directory, plate_state.config.safe_plate_name)
        os.makedirs(plate_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = (
            f"{plate_state.config.safe_plate_name}_{well}_shot{shot_number:02d}_"
            f"{timestamp}_{self._shot_index:03d}.csv"
        )
        filepath = unique_output_path(plate_dir, filename)

        if timing is not None:
            timing["save_file_path"] = filepath
        self._save_spectrum_file(filepath, wavelengths, intensities)
        if timing is not None:
            timing["save_end"] = time.perf_counter()
        repair_active_before_save = plate_state.repair_active
        payload = plate_state.record_saved(filepath, self._shot_index)
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
        logger.info(f"Plate auto-saved: {filepath}")

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
        save_plate_run_state(plate_dir, plate_state, closed_early=closed_early, timing=timing)

    def _persist_plate_reproducibility_log(
        self,
        *,
        event: str | None = None,
        timing: dict | None = None,
        closed_early: bool = False,
    ):
        plate_state = self._plate_run_state
        if plate_state is None:
            return

        plate_dir = os.path.join(self.save_directory, plate_state.config.safe_plate_name)
        save_plate_reproducibility_log(
            self.save_directory,
            plate_state,
            spectrometer_info=self._spectrometer_metadata(),
            timing_sample=timing,
            event=event,
            closed_early=closed_early,
        )

    def _append_plate_reproducibility_event(self, *, event: str, timing: dict | None = None) -> None:
        """O(1) per-shot reproducibility event; snapshots stay run-boundary only."""
        plate_state = self._plate_run_state
        if plate_state is None:
            return
        append_plate_reproducibility_event(
            self.save_directory,
            plate_state,
            event=event,
            timing_sample=timing,
        )

    def _spectrometer_metadata(self) -> dict:
        """Return a reproducibility snapshot of the connected spectrometer."""
        caps = self.spec.capabilities
        corrections = self._effective_corrections()
        metadata = {
            "brand": getattr(caps, "brand", "unknown"),
            "model": getattr(caps, "model", "unknown"),
            "serial_number": getattr(caps, "serial_number", "unknown"),
            "pixel_count": getattr(caps, "pixel_count", None),
            "wavelength_min_nm": getattr(caps, "wavelength_min", None),
            "wavelength_max_nm": getattr(caps, "wavelength_max", None),
            "integration_time_min_us": getattr(caps, "integration_time_min_us", None),
            "integration_time_max_us": getattr(caps, "integration_time_max_us", None),
            "current_integration_time_us": getattr(self.spec, "integration_time_us", None),
            "current_trigger_mode": getattr(self.spec, "current_trigger_mode", None),
            "trigger_modes": dict(getattr(caps, "trigger_modes", {})),
            "supports_dark_correction": getattr(caps, "supports_dark_correction", False),
            "supports_nonlinearity_correction": getattr(caps, "supports_nonlinearity_correction", False),
            "requested_dark_correction": corrections.requested_dark_counts,
            "requested_nonlinearity_correction": corrections.requested_nonlinearity,
            "effective_dark_correction": corrections.correct_dark_counts,
            "effective_nonlinearity_correction": corrections.correct_nonlinearity,
            "correction_mask_reasons": corrections.mask_reasons(),
            "supports_trigger_delay": getattr(caps, "supports_trigger_delay", False),
            "trigger_delay_min_us": getattr(caps, "trigger_delay_min_us", None),
            "trigger_delay_max_us": getattr(caps, "trigger_delay_max_us", None),
            "current_trigger_delay_us": getattr(self.spec, "trigger_delay_us", None),
            "seabreeze_backend": getattr(self.spec, "seabreeze_backend", None),
            "seabreeze_backend_fail_reason": getattr(self.spec, "seabreeze_backend_fail_reason", None),
            "spectrometer_class": type(self.spec).__name__,
        }
        device_info = getattr(self.spec, "device_info", None)
        if device_info:
            metadata["device_info"] = dict(device_info)
        dll_path = getattr(self.spec, "dll_path", None)
        if dll_path:
            metadata["sdk_dll_path"] = str(dll_path)
            # Backends that load a vendor DLL expose dll_identity() so a run
            # manifest can record which binary actually produced the data.
            # Vendors ship the same version number with different builds, so the
            # hash is what settles an argument about whether two runs are
            # comparable. Backends with no DLL simply do not offer the hook.
            identity_provider = getattr(self.spec, "dll_identity", None)
            if callable(identity_provider):
                try:
                    identity = identity_provider()
                    metadata["sdk_dll_sha256"] = identity.get("sha256")
                    metadata["sdk_dll_product_version"] = identity.get("product_version")
                except Exception:
                    logger.debug("Could not read SDK DLL identity for metadata.", exc_info=True)
        dll_version = getattr(self.spec, "dll_version", None)
        if dll_version:
            metadata["sdk_dll_version"] = str(dll_version)
        return metadata

    def _save_spectrum_file(self, filepath: str, wavelengths: np.ndarray, intensities: np.ndarray):
        # Save as tab-delimited (consistent with LIBS data format)
        data = np.column_stack((wavelengths, intensities))
        header = "Wavelength\tIntensity"
        tmp_filepath = f"{filepath}.tmp"
        try:
            np.savetxt(tmp_filepath, data, delimiter='\t', header=header, comments='', fmt='%.6f')
            os.replace(tmp_filepath, filepath)
        except Exception:
            try:
                if os.path.exists(tmp_filepath):
                    os.remove(tmp_filepath)
            except OSError:
                pass
            raise

    @staticmethod
    def _unique_output_path(directory: str, filename: str) -> str:
        return unique_output_path(directory, filename)

    def _emit_timing_sample(self, timing: dict):
        """Send a timing payload to the GUI/benchmark consumer."""
        if not self.collect_timing_metrics:
            return

        payload = dict(timing)
        payload["worker_enqueued_at"] = time.perf_counter()
        self._send(AcquisitionMessage.TIMING, payload)

    def _is_plate_complete(self) -> bool:
        with self._plate_lock:
            return bool(self._plate_run_state and self._plate_run_state.is_complete)

    def _plate_mode_enabled(self) -> bool:
        with self._plate_lock:
            return self._plate_run_state is not None

    # ─── Messaging ─────────────────────────────────────────────────────

    def _send(self, msg_type: str, data):
        """Put a message on the queue for the GUI to consume."""
        self.message_queue.put((msg_type, data))
