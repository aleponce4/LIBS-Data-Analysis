"""Workflow controllers for acquisition views."""

from __future__ import annotations

import logging
import os
from prolibspector.core.settings import default_save_directory
import threading
from datetime import datetime
from tkinter import messagebox
from types import SimpleNamespace

import tkinter as tk

from prolibspector.acquisition.automation import (
    ORIENTATION_NORMAL,
    automation_plan_details,
    load_resume_state_for_config,
)
from prolibspector.acquisition.connection_coordinator import ConnectionPhase
from prolibspector.acquisition.automation_launch import (
    automation_config_with_run_directory,
    plan_automation_dry_run_launch,
    plan_automation_start_launch,
)
from prolibspector.acquisition.automation_mapping import (
    MappingGridConfig,
    load_mapping_resume_state_for_config,
    mapping_plan_details,
)
from prolibspector.acquisition.automated_worker import (
    GRBL_PREP_MOTION,
    RUN_MODE_REAL,
    AutomatedAcquisitionWorker,
    prepare_grbl_controller,
)
from prolibspector.hardware.grbl_laser import (
    GrblLaserController,
    GrblLaserError,
    SimulatedGrblLaserController,
    probe_grbl_controller,
    unlock_and_reprobe_grbl_controller,
)
from prolibspector.hardware.spectrometer import SpectrometerModule


logger = logging.getLogger(__name__)


class _LaserPatternTestSpectrometer:
    """Minimal spectrometer facade for laser-only pattern tests."""

    is_connected = True
    model = "Laser Pattern Test"
    integration_time_us = 0
    current_trigger_mode = 0

    def __init__(self):
        self.capabilities = SimpleNamespace(
            brand="laser_test",
            model=self.model,
            serial_number="N/A",
            pixel_count=0,
            wavelength_min=0.0,
            wavelength_max=0.0,
            integration_time_min_us=0,
            integration_time_max_us=0,
            normal_trigger_mode=0,
            external_trigger_mode=None,
            has_external_trigger=False,
            trigger_modes={"normal": 0},
            supports_dark_correction=False,
            supports_nonlinearity_correction=False,
        )

    def set_trigger_mode(self, _mode: int) -> None:
        raise RuntimeError("Laser pattern test does not use spectrometer trigger modes.")

    def get_intensities(self, **_kwargs):
        raise RuntimeError("Laser pattern test does not read spectrometer intensities.")

    def get_wavelengths(self):
        raise RuntimeError("Laser pattern test does not read spectrometer wavelengths.")


class ManualAcquisitionController:
    """Own workflow operations for the manual acquisition view."""

    def __init__(self, view):
        self.view = view

    def create_spectrometer(self, brand: str):
        """Create the spectrometer adapter for this workflow."""
        if brand == "thorlabs":
            from prolibspector.hardware.spectrometer import ThorlabsCCSModule

            return ThorlabsCCSModule()
        if brand == "yixist":
            from prolibspector.hardware.yixist_spectrometer_broker import (
                YixistSpectrometerBrokerClient,
            )

            return YixistSpectrometerBrokerClient()
        return SpectrometerModule()

    def create_worker(self, spectrometer):
        from prolibspector.acquisition.worker import AcquisitionWorker

        return AcquisitionWorker(spectrometer)

    def poll_queue(self, *, reschedule: bool = True):
        return self.view._poll_queue_impl(reschedule=reschedule)

    def cancel_queue_poll(self):
        return self.view._cancel_queue_poll_impl()

    def handle_qc_complete(self, data):
        return self.view._handle_qc_complete_impl(data)

    def handle_qc_error(self, data):
        return self.view._handle_qc_error_impl(data)


class AutomatedAcquisitionController(ManualAcquisitionController):
    """Own automated acquisition workflow orchestration."""

    def create_worker(self, spectrometer):
        view = self.view
        worker = AutomatedAcquisitionWorker(spectrometer, view.laser)
        setter = getattr(worker, "set_grbl_p_input_nonblocking", None)
        if callable(setter):
            setter(view._grbl_p_input_nonblocking_enabled())
        return worker

    def ensure_laser_pattern_test_worker(self) -> bool:
        view = self.view
        if not view._automation_laser_ready():
            view.status_message_var.set("Connect the GRBL laser.")
            return False
        worker = view.worker
        if worker and worker.is_alive():
            if getattr(worker, "_laser_pattern_test_worker", False):
                return True
            if view.spectrometer and getattr(view.spectrometer, "is_connected", False):
                return True
            if getattr(worker, "state", "IDLE") != "IDLE":
                view.status_message_var.set("Automation is busy.")
                return False
            try:
                worker.stop()
                worker.join(timeout=2.0)
            except Exception:
                logger.debug("Failed to stop stale automation worker before laser pattern test.", exc_info=True)
            if worker.is_alive():
                view.status_message_var.set("Previous automation worker is still stopping.")
                return False
            if view.worker is worker:
                view.worker = None
        elif worker and not getattr(worker, "_laser_pattern_test_worker", False):
            view.worker = None
        view._stop_laser_pattern_test_worker()
        worker = AutomatedAcquisitionWorker(_LaserPatternTestSpectrometer(), view.laser)
        worker._laser_pattern_test_worker = True
        worker.set_grbl_p_input_nonblocking(view._grbl_p_input_nonblocking_enabled())
        worker.correct_dark_counts = bool(view.correct_dark_var.get())
        worker.correct_nonlinearity = bool(view.correct_nl_var.get())
        worker.auto_save_enabled = False
        worker.save_directory = view.save_dir_var.get()
        worker.sample_name = view.sample_name_var.get()
        worker.start()
        view.worker = worker
        view._laser_pattern_test_worker_active = True
        view._poll_queue()
        return True

    def selected_grbl_probe_port_and_baud(self) -> tuple[str, int]:
        view = self.view
        label = view.laser_port_var.get().strip()
        port = view._serial_ports_by_label.get(label, label.split(" - ", 1)[0].strip())
        if not port:
            raise ValueError("Select a laser serial port first.")
        try:
            baudrate = int(view.laser_baud_var.get())
        except ValueError as exc:
            raise ValueError("Enter a valid GRBL baud rate.") from exc
        if baudrate <= 0:
            raise ValueError("Enter a valid GRBL baud rate.")
        return port, baudrate

    def ensure_grbl_port_recovery_idle(self) -> bool:
        view = self.view
        if view._laser_connecting:
            messagebox.showinfo(
                "Laser Connection",
                "Wait for the current laser connection attempt to finish.",
                parent=view.root,
            )
            return False
        if view._laser_utility_busy:
            view.status_message_var.set("Laser utility is running.")
            messagebox.showinfo("Laser Busy", "Wait for the current laser utility action to finish.", parent=view.root)
            return False
        if view._automation_busy():
            messagebox.showinfo("Automation Running", "Stop automation before using controller recovery.", parent=view.root)
            return False
        if view._laser_connected_for_teaching():
            messagebox.showinfo(
                "Laser Connected",
                "Disconnect the laser before probing the selected serial port.",
                parent=view.root,
            )
            return False
        return True

    def on_grbl_recovery_probe(self):
        view = self.view
        if not self.ensure_grbl_port_recovery_idle():
            return
        try:
            port, baudrate = self.selected_grbl_probe_port_and_baud()
        except ValueError as exc:
            messagebox.showwarning("Controller Probe", str(exc), parent=view.root)
            return

        view._laser_utility_busy = True
        view.laser_status_var.set(f"Probing {port} @ {baudrate}...")
        view._refresh_automation_controls()

        def _work():
            result = probe_grbl_controller(port, baudrate=baudrate, timeout_s=1.0)
            try:
                view._post_to_ui(lambda r=result: view._finish_grbl_recovery_probe("Controller probe", r))
            except (RuntimeError, tk.TclError):
                view._laser_utility_busy = False

        threading.Thread(target=_work, daemon=True, name="GrblRecoveryProbeThread").start()

    def on_grbl_unlock_and_recheck(self):
        view = self.view
        if not self.ensure_grbl_port_recovery_idle():
            return
        last_probe = view._last_grbl_recovery_probe
        if last_probe is None or not last_probe.alarm_active:
            messagebox.showinfo(
                "Unlock and Recheck",
                "Run a controller probe that reports Alarm before unlocking.",
                parent=view.root,
            )
            return
        try:
            port, baudrate = self.selected_grbl_probe_port_and_baud()
        except ValueError as exc:
            messagebox.showwarning("Unlock and Recheck", str(exc), parent=view.root)
            return
        if str(port) != str(last_probe.port) or int(baudrate) != int(last_probe.baudrate):
            messagebox.showinfo(
                "Unlock and Recheck",
                "The selected port changed. Probe this port before unlocking.",
                parent=view.root,
            )
            return
        confirmed = messagebox.askyesno(
            "Unlock GRBL Alarm",
            (
                "$X clears the GRBL alarm lock without homing.\n\n"
                "Only continue if the laser is physically safe, the beam path is clear, "
                "and you understand that this does not move the head off a limit switch."
            ),
            parent=view.root,
        )
        if not confirmed:
            view.status_message_var.set("GRBL unlock cancelled.")
            return

        view._laser_utility_busy = True
        view.laser_status_var.set(f"Unlocking {port} and rechecking status...")
        view._refresh_automation_controls()

        def _work():
            result = unlock_and_reprobe_grbl_controller(port, baudrate=baudrate, timeout_s=1.0)
            try:
                view._post_to_ui(lambda r=result: view._finish_grbl_recovery_probe("Unlock and recheck", r))
            except (RuntimeError, tk.TclError):
                view._laser_utility_busy = False

        threading.Thread(target=_work, daemon=True, name="GrblUnlockRecheckThread").start()

    def on_connect_laser(self):
        view = self.view
        if view._laser_connecting:
            return
        if view._laser_utility_busy:
            view.status_message_var.set("Laser utility is running.")
            messagebox.showinfo("Laser Busy", "Wait for the current laser utility action to finish.", parent=view.root)
            return
        if view._automation_busy():
            messagebox.showinfo("Automation Running", "Stop automation before changing the laser connection.", parent=view.root)
            return
        view._prefer_laser_port_for_profile(force=True)
        label = view.laser_port_var.get().strip()
        port = view._serial_ports_by_label.get(label, label.split(" - ", 1)[0].strip())
        if not port:
            messagebox.showinfo("Laser Port", "Select a laser serial port or use Simulate.", parent=view.root)
            return
        try:
            baudrate = int(view.laser_baud_var.get())
        except ValueError:
            messagebox.showwarning("Laser Baud", "Enter a valid baud rate.", parent=view.root)
            return

        view.laser_link.desired = True
        view.laser_link.phase = ConnectionPhase.CONNECTING
        view.laser_status_var.set("Connecting laser (homes after connect)...")
        view._refresh_automation_controls()
        token = view.connection_coordinator.begin("laser")

        def _connect_blocking():
            # Connect + home run as one coordinator op so a connected laser is
            # always homed and verified. The single DeviceOps thread means other
            # device ops queue behind the homing move (up to ~30 s worst case).
            controller = GrblLaserController(port, baudrate=baudrate)
            try:
                status = controller.connect()
                prepare_grbl_controller(
                    controller,
                    intent=GRBL_PREP_MOTION,
                    context="laser connect",
                    force_home=True,
                    send_status=view._set_laser_status_from_worker_thread,
                )
            except Exception as exc:
                connected = controller.is_connected
                try:
                    controller.disconnect()
                except Exception:
                    logger.debug("Failed to disconnect controller after laser connection error.", exc_info=True)
                if connected:
                    raise GrblLaserError(
                        f"Connected to {port}, but homing failed: {exc} "
                        "Check limit switches, then use Probe Port / Unlock and Recheck before reconnecting."
                    ) from exc
                raise
            return controller, f"{status}; homed"

        def _dispose_stale_connect(result):
            # A stale-but-successful connect holds the serial port open;
            # release it so the next attempt does not hit port-in-use.
            controller, _status = result
            controller.disconnect()

        view.connection_coordinator.submit(
            _connect_blocking,
            token=token,
            on_done=lambda result: view._finish_laser_connect(*result),
            on_error=view._finish_laser_connect_failed,
            on_stale=_dispose_stale_connect,
            label="laser connect",
        )

    def on_simulate_laser(self):
        view = self.view
        if view._laser_connecting:
            messagebox.showinfo("Laser Connection", "Wait for the current laser connection attempt to finish.", parent=view.root)
            return
        if view._laser_utility_busy:
            view.status_message_var.set("Laser utility is running.")
            messagebox.showinfo("Laser Busy", "Wait for the current laser utility action to finish.", parent=view.root)
            return
        if view._automation_busy():
            messagebox.showinfo("Automation Running", "Stop automation before changing the laser connection.", parent=view.root)
            return
        controller = SimulatedGrblLaserController()
        status = controller.connect()
        controller.home()
        view._finish_laser_connect(controller, f"{status}; homed")

    def on_disconnect_laser(self):
        view = self.view
        if view._laser_utility_busy:
            view.status_message_var.set("Laser utility is running.")
            messagebox.showinfo("Laser Busy", "Wait for the current laser utility action to finish.", parent=view.root)
            return
        if view._automation_busy():
            messagebox.showinfo("Automation Running", "Stop automation before disconnecting the laser.", parent=view.root)
            return
        view._disconnect_laser_async()

    def configure_worker_for_current_automation(
        self,
        *,
        config=None,
        run_mode: str = RUN_MODE_REAL,
        resume_state=None,
    ) -> bool:
        view = self.view
        config = config or view._active_plan_config()
        if config is None or not view.worker:
            return False
        mapping_mode = isinstance(config, MappingGridConfig)
        configure = getattr(view.worker, "configure_mapping" if mapping_mode else "configure_automation", None)
        if not callable(configure):
            return True
        try:
            view._sync_worker_laser_options()
            view.worker.save_directory = (
                config.run_directory
                or view._automation_run_dir
                or view.save_dir_var.get()
            )
            configure(
                config,
                resume_state=resume_state,
                safety_checklist=view._safety_checklist_mapping(),
                run_mode=run_mode,
            )
        except Exception as exc:
            messagebox.showerror("Automation Plan", f"Worker plan error: {exc}", parent=view.root)
            view._refresh_automation_controls()
            return False
        if mapping_mode:
            if resume_state is not None:
                view._mapping_resume_state = resume_state
            view._seed_mapping_grid_history(config)
        elif hasattr(view.worker, "plate_progress_payloads"):
            view._seed_automation_plate_history(
                view.automation_config or config,
                view.worker.plate_progress_payloads(),
                rebuild_editor=False,
            )
        return True

    def start_worker_for_automation_launch(
        self,
        launch_plan,
        *,
        run_config,
        resume_state=None,
    ) -> bool:
        view = self.view
        if run_config.run_directory:
            if launch_plan.simulation_run:
                view._automation_simulation_run_dir = run_config.run_directory
            elif isinstance(run_config, MappingGridConfig):
                view.mapping_config = run_config
                view._automation_run_dir = run_config.run_directory
            else:
                view.automation_config = run_config
                view._automation_run_dir = run_config.run_directory
        if not self.configure_worker_for_current_automation(
            config=run_config,
            run_mode=launch_plan.run_mode,
            resume_state=resume_state,
        ):
            return False
        if hasattr(view.worker, "set_safety_checklist"):
            view.worker.set_safety_checklist(view._safety_checklist_mapping())
        if hasattr(view.worker, "set_spectra_qc_enabled"):
            view.worker.set_spectra_qc_enabled(False if isinstance(run_config, MappingGridConfig) else launch_plan.spectra_qc_enabled)
        if isinstance(run_config, MappingGridConfig) and hasattr(view.worker, "set_mapping_background_capture"):
            view.worker.set_mapping_background_capture(bool(view.mapping_capture_background_var.get()))
        view.worker.save_directory = run_config.run_directory or view._automation_run_dir or view.save_dir_var.get()
        view._lock_plate_name_editor()
        if not launch_plan.laser_pattern_test:
            view.auto_save_var.set(True)
            view.on_auto_save_toggle()
        if isinstance(run_config, MappingGridConfig):
            if resume_state is not None:
                view._mapping_resume_state = resume_state
            view._seed_mapping_grid_history(run_config)
        elif hasattr(view.worker, "plate_progress_payloads"):
            view._seed_automation_plate_history(
                run_config,
                view.worker.plate_progress_payloads(),
                rebuild_editor=False,
            )
        if launch_plan.laser_pattern_test:
            view.worker.start_laser_pattern_test()
        else:
            view.worker.start_automated_run(simulation=launch_plan.simulation_run)
        view._clear_automation_sidebar_step_override()
        view._refresh_acquisition_controls()
        return True

    def on_resume_automation_plan(self):
        view = self.view
        if not view._active_plan_config() and not view.on_apply_automation_plan():
            return
        config = view._active_plan_config()
        if config is None:
            return
        mapping_mode = isinstance(config, MappingGridConfig)
        if not mapping_mode and not view._ensure_plate_names_applied():
            errors = view._current_plate_name_errors()
            view.automation_plan_var.set(errors[0] if errors else "Name every plate.")
            view._refresh_automation_controls()
            return
        if mapping_mode:
            resume_state = view._mapping_resume_state or load_mapping_resume_state_for_config(
                config.run_directory or view.save_dir_var.get(),
                config,
            )
        else:
            resume_state = view._resume_state or load_resume_state_for_config(
                config.run_directory or view.save_dir_var.get(),
                config,
            )
        if resume_state is None:
            view.automation_plan_var.set("No matching saved mapping state found for this plan." if mapping_mode else "No matching saved automated run state found for this plan.")
            view._refresh_automation_controls()
            return
        configure_name = "configure_mapping" if mapping_mode else "configure_automation"
        if not view.worker or not view.worker.is_alive() or not hasattr(view.worker, configure_name):
            view.automation_plan_var.set("Connect the spectrometer before resuming saved automation state.")
            view._refresh_automation_controls()
            return
        try:
            view._sync_worker_laser_options()
            view.worker.save_directory = config.run_directory or view._automation_run_dir or view.save_dir_var.get()
            getattr(view.worker, configure_name)(
                config,
                resume_state=resume_state,
                safety_checklist=view._safety_checklist_mapping(),
            )
        except Exception as exc:
            view.automation_plan_var.set(f"Resume error: {exc}")
            view._refresh_automation_controls()
            return
        if mapping_mode:
            view._mapping_resume_state = resume_state
            view._seed_mapping_grid_history(config)
        else:
            view._resume_state = resume_state
        if not mapping_mode and hasattr(view.worker, "plate_progress_payloads"):
            view._seed_automation_plate_history(config, view.worker.plate_progress_payloads())
        view.automation_plan_var.set(
            f"Loaded {len(resume_state.completed_targets)} saved target(s). Complete Dry Run if required, then use Resume Run to continue."
        )
        view._refresh_automation_controls()

    def prompt_for_qc_repeats(self, failed_wells: list[dict], result: dict):
        view = self.view
        csv_path = result.get("csv_path", "")
        pdf_path = result.get("pdf_path", "")
        message = (
            "Spectra QC found failed wells.\n\n"
            f"{view._failed_qc_well_text(failed_wells)}\n\n"
            f"CSV: {csv_path}\n"
            f"PDF: {pdf_path}\n\n"
            "Queue failed wells for repeat acquisition? This will move the current failed spectra into the plate discard area. "
            "It will not fire the laser until you choose Resume Run."
        )
        if not messagebox.askyesno("Queue Failed Repeats", message, parent=view.root):
            return
        if not view.worker or not hasattr(view.worker, "queue_qc_repeats"):
            messagebox.showinfo("Queue Failed Repeats", "Connect the spectrometer before queueing repeats.", parent=view.root)
            return
        try:
            summary = view.worker.queue_qc_repeats(failed_wells)
        except Exception as exc:
            messagebox.showerror("Queue Failed Repeats", str(exc), parent=view.root)
            return
        if view.automation_config and hasattr(view.worker, "plate_progress_payloads"):
            view._seed_automation_plate_history(view.automation_config, view.worker.plate_progress_payloads())
        view.status_message_var.set(
            f"Queued {summary.get('queued_wells', 0)} failed well(s). Use Resume Run to repeat them."
        )
        view._refresh_automation_controls()

    def create_fresh_automation_run_directory(self, experiment_name: str) -> str:
        view = self.view
        parent_dir = view.save_dir_var.get().strip() or default_save_directory()
        os.makedirs(parent_dir, exist_ok=True)
        run_dir = view._automation_run_dirs.unique_run_directory(parent_dir, experiment_name)
        os.makedirs(run_dir, exist_ok=True)
        view._automation_run_parent_dir = parent_dir
        view._automation_run_dir = run_dir
        view._automation_run_created_at = datetime.now()
        view._automation_simulation_run_dir = None
        return run_dir

    def on_prepare_simulation(self):
        view = self.view
        if view._laser_utility_busy:
            view.status_message_var.set("Laser utility is running.")
            messagebox.showinfo("Laser Busy", "Wait for the current laser utility action to finish.", parent=view.root)
            return
        if view._automation_busy():
            messagebox.showinfo("Automation Running", "Stop automation before preparing simulation.", parent=view.root)
            return
        if (
            view.spectrometer
            and view.spectrometer.is_connected
            and not view._automation_spectrometer_simulated()
        ) or (
            view.laser
            and getattr(view.laser, "is_connected", False)
            and not view._automation_laser_simulated()
        ):
            view._warn_mixed_automation_devices()
            return
        if not (view.spectrometer and view.spectrometer.is_connected):
            view.spectrometer = SpectrometerModule()
            try:
                status = view.spectrometer.connect_simulated("Generic")
            except Exception as exc:
                messagebox.showerror("Simulation Failed", str(exc), parent=view.root)
                view.spectrometer = None
                return
            view._finish_connection(status)
        if not (view.laser and getattr(view.laser, "is_connected", False)):
            view.on_simulate_laser()

        mapping_mode = bool(getattr(view, "_mapping_mode_selected", lambda: False)())
        if hasattr(view, "automation_preset_var") and not mapping_mode:
            view.automation_preset_var.set(view._custom_automation_preset_label())
        if mapping_mode:
            view.mapping_x_length_var.set("2")
            view.mapping_y_length_var.set("2")
            view.mapping_step_var.set("1")
            view.mapping_shots_var.set("1")
            view.spectra_qc_enabled_var.set(False)
        else:
            model_label = next((label for label in view._plate_models_by_label if "96-well" in label), None)
            if model_label:
                view.plate_model_var.set(model_label)
            view.orientation_var.set(ORIENTATION_NORMAL)
            view.bed_x_min_var.set("0")
            view.bed_x_max_var.set("150")
            view.bed_y_min_var.set("0")
            view.bed_y_max_var.set("100")
            view.plate_gap_x_var.set("8")
            view.plate_gap_y_var.set("8")
            view.max_plates_var.set("1")
            view.auto_shots_var.set("1")
        view.power_percent_var.set("1")
        view.pulse_ms_var.set("1")
        view.settle_ms_var.set("0")
        view.capture_timeout_var.set("4")
        view.safety_cover_var.set(True)
        view.safety_cooling_var.set(True)
        view.safety_exhaust_var.set(True)
        view.safety_grounding_var.set(True)
        view.safety_estop_var.set(True)
        view.safety_material_var.set(True)
        view.unverified_model_ack_var.set(True)
        view._refresh_precheck_dots()
        view.on_apply_automation_plan()
        simulated_spec = getattr(getattr(view.spectrometer, "capabilities", None), "brand", "") == "simulated"
        preview_ok = view._show_simulated_spectrum_preview()
        if preview_ok or not simulated_spec:
            view.status_message_var.set("Simulated mapping run is prepared." if mapping_mode else "Simulated automated run is prepared.")

    def on_run_dry_run(self):
        view = self.view
        if view._laser_utility_busy:
            view._show_automation_launch_block("Laser utility is running.")
            return
        if not view._ensure_automation_plan_and_plate_names():
            return
        state = view._automation_ui_state()
        launch_plan = plan_automation_dry_run_launch(state)
        if not launch_plan.can_start:
            view._show_automation_launch_block(
                launch_plan.block_reason,
                laser_pattern_test=launch_plan.laser_pattern_test,
            )
            return
        if launch_plan.laser_pattern_test and not view._ensure_laser_pattern_test_worker():
            messagebox.showinfo("Laser Pattern Test", "Connect the laser before running the pattern test.", parent=view.root)
            view._refresh_automation_controls()
            return
        if not view.worker or not hasattr(view.worker, "start_dry_run"):
            messagebox.showinfo("Spectrometer Required", "Connect the spectrometer before running automation.", parent=view.root)
            return
        if not self.configure_worker_for_current_automation():
            return
        if hasattr(view.worker, "set_safety_checklist"):
            view.worker.set_safety_checklist(view._safety_checklist_mapping())
        active_config = view._active_plan_config()
        view.worker.save_directory = active_config.run_directory or view._automation_run_dir or view.save_dir_var.get()
        view._lock_plate_name_editor()
        view.worker.start_dry_run()
        view._clear_automation_sidebar_step_override()
        view._refresh_acquisition_controls()

    def on_start_automated(self):
        view = self.view
        worker_state = getattr(view.worker, "state", "IDLE") if view.worker else "IDLE"
        worker_alive = bool(view.worker and view.worker.is_alive())
        if worker_alive and worker_state in {"DRY_RUN", "AUTOMATED", "LASER_TEST"}:
            if view._automation_pause_pending:
                view._refresh_acquisition_controls()
                return
            view._automation_pause_pending = True
            view.status_message_var.set("Pause requested. Waiting for a safe boundary.")
            pause = getattr(view.worker, "request_pause", None)
            if callable(pause):
                pause(
                    source="start-button-toggle",
                    ui_context={
                        "automation_pause_pending": bool(view._automation_pause_pending),
                        "worker_state": worker_state,
                    },
                )
            else:
                view.on_stop()
            view._refresh_acquisition_controls()
            return

        if view._automation_resume_available():
            view.on_resume_automation_run()
            return

        if view._laser_utility_busy:
            view._show_automation_launch_block("Laser utility is running.")
            return
        if not view._ensure_automation_plan_and_plate_names():
            return
        state = view._automation_ui_state()
        resume_run = view._automation_resume_available()
        active_config = view._active_plan_config()
        mapping_mode = isinstance(active_config, MappingGridConfig)
        launch_plan = plan_automation_start_launch(
            active_config,
            state,
            devices_simulated=view._automation_devices_simulated(),
            resume_available=resume_run,
            worker_config=view._current_worker_automation_config() if resume_run else None,
            existing_simulation_run_directory=view._automation_simulation_run_dir,
            spectra_qc_selected=False if mapping_mode else bool(view.spectra_qc_enabled_var.get()),
            spectra_qc_available=False if mapping_mode else view._spectra_qc_available(),
        )
        if not launch_plan.ready:
            view._show_automation_launch_block(
                launch_plan.block_reason,
                simulation_run=launch_plan.simulation_run,
                laser_pattern_test=launch_plan.laser_pattern_test,
            )
            return
        if launch_plan.laser_pattern_test and not view._ensure_laser_pattern_test_worker():
            messagebox.showinfo("Laser Pattern Test", "Connect the laser before running the pattern test.", parent=view.root)
            view._refresh_automation_controls()
            return
        if not view.worker or not hasattr(view.worker, "start_automated_run"):
            messagebox.showinfo("Spectrometer Required", "Connect the spectrometer before running guarded firing.", parent=view.root)
            return
        if launch_plan.needs_simulation_directory:
            try:
                simulation_dir = view._ensure_automation_simulation_run_directory()
            except Exception as exc:
                messagebox.showerror("Run Simulation", str(exc), parent=view.root)
                return
            launch_plan = plan_automation_start_launch(
                active_config,
                state,
                devices_simulated=view._automation_devices_simulated(),
                resume_available=resume_run,
                worker_config=view._current_worker_automation_config() if resume_run else None,
                existing_simulation_run_directory=view._automation_simulation_run_dir,
                new_simulation_run_directory=simulation_dir,
                spectra_qc_selected=False if mapping_mode else bool(view.spectra_qc_enabled_var.get()),
                spectra_qc_available=False if mapping_mode else view._spectra_qc_available(),
            )
        if launch_plan.simulation_directory_to_remember:
            view._automation_simulation_run_dir = launch_plan.simulation_directory_to_remember
        run_config = launch_plan.run_config
        if run_config is None:
            messagebox.showerror("Run Simulation", "Simulation run directory could not be prepared.", parent=view.root)
            return
        self.start_worker_for_automation_launch(launch_plan, run_config=run_config)

    def on_resume_automation_run(self):
        view = self.view
        if view._laser_utility_busy:
            view._show_automation_launch_block("Laser utility is running.")
            return
        if not view._ensure_automation_plan_and_plate_names():
            return
        active_config = view._active_plan_config()
        mapping_mode = isinstance(active_config, MappingGridConfig)
        resume_state = view._matching_resume_state_for_current_plan()
        if resume_state is None:
            if mapping_mode:
                view._mapping_resume_state = load_mapping_resume_state_for_config(
                    active_config.run_directory or view.save_dir_var.get(),
                    active_config,
                )
            else:
                view._resume_state = load_resume_state_for_config(
                    active_config.run_directory or view.save_dir_var.get(),
                    active_config,
                )
            resume_state = view._matching_resume_state_for_current_plan()
        if resume_state is None:
            view.automation_plan_var.set("No resumable progress matches this run plan.")
            view._refresh_automation_controls()
            return
        state = view._automation_ui_state()
        launch_plan = plan_automation_start_launch(
            active_config,
            state,
            devices_simulated=view._automation_devices_simulated(),
            resume_available=True,
            worker_config=resume_state.config,
            existing_simulation_run_directory=view._automation_simulation_run_dir,
            spectra_qc_selected=False if mapping_mode else bool(view.spectra_qc_enabled_var.get()),
            spectra_qc_available=False if mapping_mode else view._spectra_qc_available(),
        )
        if not launch_plan.ready:
            view._show_automation_launch_block(
                launch_plan.block_reason,
                simulation_run=launch_plan.simulation_run,
                laser_pattern_test=launch_plan.laser_pattern_test,
            )
            return
        run_config = launch_plan.run_config or resume_state.config
        if not launch_plan.simulation_run and resume_state.config.run_directory:
            run_config = resume_state.config
        if launch_plan.simulation_directory_to_remember:
            view._automation_simulation_run_dir = launch_plan.simulation_directory_to_remember
        if mapping_mode:
            view._mapping_resume_state = resume_state
        else:
            view._resume_state = resume_state
        self.start_worker_for_automation_launch(
            launch_plan,
            run_config=run_config,
            resume_state=resume_state,
        )

    def on_restart_automation_from_zero(self):
        view = self.view
        if not view._automation_resume_available():
            view._refresh_automation_controls()
            return
        completed, total = view._automation_progress_counts()
        confirmed = messagebox.askyesno(
            "Restart Automated Run",
            (
                f"Restart this run from target 0?\n\n"
                f"Saved progress shows {completed} of {total} target(s) complete. "
                "A new run folder will be created so the paused data is not overwritten."
            ),
            parent=view.root,
        )
        if not confirmed:
            view.status_message_var.set("Restart from 0 cancelled.")
            return
        if view._laser_utility_busy:
            view._show_automation_launch_block("Laser utility is running.")
            return
        if not view._ensure_automation_plan_and_plate_names():
            return
        state = view._automation_ui_state()
        active_config = view._active_plan_config()
        mapping_mode = isinstance(active_config, MappingGridConfig)
        experiment_name = active_config.experiment_name
        fresh_run_dir = self.create_fresh_automation_run_directory(experiment_name)
        fresh_config = automation_config_with_run_directory(active_config, fresh_run_dir)
        if mapping_mode:
            view.mapping_config = fresh_config
            view._latest_plan_details = mapping_plan_details(fresh_config, include_targets=False)
            view._mapping_resume_state = None
            view.mapping_progress = None
        else:
            view.automation_config = fresh_config
            view._latest_plan_details = automation_plan_details(fresh_config)
            view._resume_state = None
        if view.worker is not None:
            if hasattr(view.worker, "automation_state"):
                view.worker.automation_state = None
            if hasattr(view.worker, "mapping_state"):
                view.worker.mapping_state = None
            if hasattr(view.worker, "automation_paused"):
                view.worker.automation_paused = False
        launch_plan = plan_automation_start_launch(
            fresh_config,
            state,
            devices_simulated=view._automation_devices_simulated(),
            resume_available=False,
            worker_config=None,
            existing_simulation_run_directory=None,
            spectra_qc_selected=False if mapping_mode else bool(view.spectra_qc_enabled_var.get()),
            spectra_qc_available=False if mapping_mode else view._spectra_qc_available(),
        )
        if launch_plan.needs_simulation_directory:
            try:
                simulation_dir = view._ensure_automation_simulation_run_directory()
            except Exception as exc:
                messagebox.showerror("Run Simulation", str(exc), parent=view.root)
                return
            launch_plan = plan_automation_start_launch(
                fresh_config,
                state,
                devices_simulated=view._automation_devices_simulated(),
                resume_available=False,
                worker_config=None,
                existing_simulation_run_directory=None,
                new_simulation_run_directory=simulation_dir,
                spectra_qc_selected=False if mapping_mode else bool(view.spectra_qc_enabled_var.get()),
                spectra_qc_available=False if mapping_mode else view._spectra_qc_available(),
            )
        if not launch_plan.ready:
            view._show_automation_launch_block(
                launch_plan.block_reason,
                simulation_run=launch_plan.simulation_run,
                laser_pattern_test=launch_plan.laser_pattern_test,
            )
            return
        run_config = launch_plan.run_config or fresh_config
        if launch_plan.simulation_directory_to_remember:
            view._automation_simulation_run_dir = launch_plan.simulation_directory_to_remember
        self.start_worker_for_automation_launch(launch_plan, run_config=run_config, resume_state=None)
