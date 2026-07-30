"""Main application window and workflow orchestration for Acquisition Mode."""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import sys
import os
import numpy as np
import queue
import logging
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageTk
from prolibspector.acquisition.sidebar import ACQUISITION_SIDEBAR_WIDTH, create_acquisition_sidebar
from prolibspector.acquisition.plate_autosave import (
    ORDER_COLUMN,
    discover_resumable_plate_runs,
    ORDER_LABELS,
    ORDER_ROW,
    PLATE_FORMATS,
    PlateAutosaveConfig,
    PlateRunState,
)
from prolibspector.acquisition.corrections import resolve_effective_corrections

# Import matplotlib BEFORE any Tk root is created, so the TkAgg backend
# initialises cleanly (avoids deadlock when a prior Tk root was destroyed).
import matplotlib  # noqa: F401  (side-effect import: backend init order)
import matplotlib.pyplot as plt  # noqa: F401  (side-effect import: backend init order)

logger = logging.getLogger(__name__)

from prolibspector.acquisition.shell import BaseAcquisitionShell
from prolibspector.acquisition.connection_coordinator import (
    ConnectionCoordinator,
    ConnectionPhase,
    DeviceLink,
)
from prolibspector.acquisition.controllers import ManualAcquisitionController
from prolibspector.core.runtime import supports_thorlabs_ccs
from prolibspector.core.inspector import attach_right_inspector
from prolibspector.core.ui_scale import scaled_font, scaled_int, scaled_wrap, ui_scale_for_widget
from prolibspector.core.ui_theme import apply_canvas_theme, get_palette
from prolibspector.core.windowing import apply_window_policy, create_dialog


SPECTROMETER_CONNECTION_DIALOG_PREFERRED = (1040, 190)
SPECTROMETER_CONNECTION_DIALOG_MIN_SIZE = (900, 170)


class AcquisitionViewBase:
    """
    Acquisition Mode application.
    
    Provides:
        - Spectrometer connection via SpectrometerModule (Ocean Optics) or
          ThorlabsCCSModule (CCS100/125/150/175/200) — user picks brand on connect
        - Live spectrum preview (continuous polling)
        - Hardware-triggered single-shot capture (external edge trigger)
        - Auto-save of captured spectra
        - Send captured data to Analysis Mode (in-memory hand-off)
    """

    def __init__(
        self,
        root=None,
        *,
        mode_name: str = "Acquisition",
        window_title: str = "LIBS Acquisition Mode",
    ):
        self.mode_name = mode_name
        self.window_title = window_title

        # ─── State ─────────────────────────────────────────────────────
        self.spectrometer = None   # SpectrometerModule instance
        self.worker = None         # AcquisitionWorker instance

        # Current spectrum data (for save / send to analysis)
        self.current_wavelengths = None
        self.current_intensities = None
        self._highlight_line = None
        self._highlight_after_id = None

        # Data to hand off to Analysis mode
        self._handoff_data = None
        self.plate_autosave_config = None
        self.plate_progress = None
        self.plate_history = []
        self.current_plate_index = None
        self._plate_completion_prompt_pending = False
        self._pending_plate_run_state = None
        self._queue_poll_after_id = None
        self.timing_samples = []
        self.latest_timing_sample = None
        self._pending_spectrum = None
        self._pending_draw_after_id = None
        self._last_plot_draw_at = 0.0
        self.active_queue_poll_ms = 15
        self.idle_queue_poll_ms = 75
        self.live_redraw_interval_ms = 33
        self._requested_mode = None
        self._pending_after_ids = set()
        self._ui_closing = False
        self.connection_coordinator = ConnectionCoordinator(self)
        self.spectrometer_link = DeviceLink()
        self._disconnect_in_progress = False
        self._shutdown_in_progress = False
        self._shutdown_kind = None
        self._shutdown_on_finished = None
        self._worker_stop_after_id = None
        self._spectra_qc_load_result = getattr(self, "_spectra_qc_load_result", None)
        self._last_spectra_qc_result = getattr(self, "_last_spectra_qc_result", None)
        self._qc_tree_rows_by_iid = {}
        if not hasattr(self, "controller"):
            self.controller = ManualAcquisitionController(self)
        # ─── Build UI ──────────────────────────────────────────────────
        self.shell = BaseAcquisitionShell(
            root=root,
            window_title=self.window_title,
            sidebar_width=ACQUISITION_SIDEBAR_WIDTH,
        )
        self._bind_shell_aliases()
        self._highlight_line = self.live_line
        self._build_qc_review_panel()

        self._build_sidebar()
        after_sidebar = getattr(self, "_after_sidebar_created", None)
        if callable(after_sidebar):
            after_sidebar()
        if hasattr(self, "spectra_qc_status_var"):
            self._refresh_spectra_qc_availability()
        self.inspector = attach_right_inspector(
            self.root,
            self,
            mode_name=self.mode_name,
            status_var=self.status_message_var,
            switch_label="Open Analysis",
        )
        self.apply_ui_theme()

        # ─── Window Close ──────────────────────────────────────────────
        self.shell.set_close_handler(self.on_closing)

    def _bind_shell_aliases(self):
        """Expose shell-owned widgets through the legacy app attributes."""
        for name in (
            "root",
            "graph_container",
            "graph_frame",
            "fig",
            "ax",
            "canvas",
            "live_line",
            "plate_overview_frame",
            "plate_history_canvas",
            "plate_history_scrollbar",
            "plate_history_inner",
            "plate_history_window",
        ):
            setattr(self, name, getattr(self.shell, name))

    def _build_sidebar(self):
        create_acquisition_sidebar(self)

    # ═══════════════════════════════════════════════════════════════════
    #  GUI Event Handlers (called by sidebar buttons)
    # ═══════════════════════════════════════════════════════════════════

    def on_connect(self):
        """Connect to the spectrometer — asks the user which brand first,
        or opens diagnostics on failure.

        The actual connection runs in a background thread so the GUI
        stays responsive during USB enumeration and device initialisation."""
        if self._disconnect_in_progress or self._shutdown_in_progress:
            if hasattr(self, "status_message_var"):
                self.status_message_var.set("Wait for the current disconnect to finish.")
            return
        from prolibspector.hardware.spectrometer import (
            SpectrometerModule,
            SpectrometerError, NoDeviceError,
        )
        from prolibspector.hardware.yixist_spectrometer import yixist_supported

        # ── Brand selection dialog ─────────────────────────────────────────
        brand = self._ask_brand()
        if brand is None:
            return  # user cancelled

        if brand == "simulation":
            self.spectrometer = SpectrometerModule()
            try:
                status = self.spectrometer.connect_simulated()
                self._finish_connection(status)
            except Exception as e:
                messagebox.showerror("Simulation Failed", str(e), parent=self.root)
                self.spectrometer = None
            return

        if brand == "thorlabs":
            if not supports_thorlabs_ccs():
                messagebox.showinfo(
                    "Thorlabs CCS Unavailable",
                    "Thorlabs CCS support is currently Windows only.",
                    parent=self.root,
                )
                return
        elif brand == "yixist":
            if not yixist_supported():
                messagebox.showinfo(
                    "YiXist Unavailable",
                    "YiXist/YXSP support is currently Windows x64 only.",
                    parent=self.root,
                )
                return
        self.spectrometer = self.controller.create_spectrometer(brand)

        # Disable the button and show progress while connecting
        self.connect_btn.config(state="disabled")
        self.status_message_var.set("Connecting… please wait.")
        self.root.update_idletasks()
        logger.info("on_connect: submitting background connect…")
        self.spectrometer_link.desired = True
        self.spectrometer_link.phase = ConnectionPhase.CONNECTING
        token = self.connection_coordinator.begin("spectrometer")

        def _on_error(exc):
            if isinstance(exc, (NoDeviceError, SpectrometerError)):
                self._on_connect_failed(str(exc))
            else:
                self._on_connect_failed(f"Unexpected error: {exc}")

        spectrometer = self.spectrometer
        self.connection_coordinator.submit(
            spectrometer.connect,
            token=token,
            on_done=self._finish_connection,
            on_error=_on_error,
            on_stale=lambda _status, spec=spectrometer: spec.disconnect(),
            label="spectrometer connect",
        )

    def _on_connect_failed(self, reason: str):
        """Called on the main thread when background connect fails."""
        self.spectrometer = None
        self.spectrometer_link.desired = False
        self.spectrometer_link.phase = ConnectionPhase.ERROR
        self.spectrometer_link.detail = reason
        self.status_message_var.set("Connection failed.")
        self._refresh_acquisition_controls()
        result = self._open_diagnostic_dialog(auto_reason=reason)
        if result is None:
            return
        self._handle_diagnostic_result(result)

    def _open_diagnostic_dialog(self, auto_reason: str | None = None):
        """Show the diagnostic + device picker dialog.
        Returns the dialog result dict, or None if cancelled."""
        from prolibspector.acquisition.diagnostic_dialog import DiagnosticDialog
        dlg = DiagnosticDialog(self.root, auto_reason=auto_reason)
        return dlg.result

    def _handle_diagnostic_result(self, result: dict):
        """Process the result from the diagnostic dialog.
        Connections are run in a background thread to keep the GUI responsive."""
        from prolibspector.hardware.spectrometer import (
            SpectrometerModule, ThorlabsCCSModule,
        )
        from prolibspector.hardware.yixist_spectrometer import yixist_supported

        action = result.get("action")
        brand = result.get("brand", "ocean_optics")
        if brand == "thorlabs" and not supports_thorlabs_ccs():
            messagebox.showinfo(
                "Thorlabs CCS Unavailable",
                "Thorlabs CCS support is currently Windows only.",
                parent=self.root,
            )
            return
        if brand == "yixist" and not yixist_supported():
            messagebox.showinfo(
                "YiXist Unavailable",
                "YiXist/YXSP support is currently Windows x64 only.",
                parent=self.root,
            )
            return

        if action == "simulate":
            if brand == "thorlabs":
                self.spectrometer = ThorlabsCCSModule()
                status = self.spectrometer.connect_simulated("CCS175")
            else:
                self.spectrometer = SpectrometerModule()
                status = self.spectrometer.connect_simulated()
            self._finish_connection(status)

        elif action == "connect":
            device_index = result.get("device_index", 0)
            if brand == "thorlabs":
                self.spectrometer = ThorlabsCCSModule()
            else:
                self.spectrometer = self.controller.create_spectrometer(brand)

            self.connect_btn.config(state="disabled")
            self.status_message_var.set("Connecting… please wait.")
            self.root.update_idletasks()
            self.spectrometer_link.desired = True
            self.spectrometer_link.phase = ConnectionPhase.CONNECTING
            token = self.connection_coordinator.begin("spectrometer")

            def _fail(exc, index=device_index):
                messagebox.showerror(
                    "Connection Failed",
                    f"Could not connect to device {index}:\n{exc}",
                    parent=self.root,
                )
                self.spectrometer = None
                self.spectrometer_link.phase = ConnectionPhase.ERROR
                self.status_message_var.set("Connection failed.")
                self._refresh_acquisition_controls()

            spectrometer = self.spectrometer
            self.connection_coordinator.submit(
                lambda index=device_index, spec=spectrometer: spec.connect(device_index=index),
                token=token,
                on_done=self._finish_connection,
                on_error=_fail,
                on_stale=lambda _status, spec=spectrometer: spec.disconnect(),
                label="spectrometer connect (diagnostic)",
            )

        elif action == "connect_resource":
            resource = result.get("resource", "")
            self.spectrometer = ThorlabsCCSModule()

            self.connect_btn.config(state="disabled")
            self.status_message_var.set("Connecting… please wait.")
            self.root.update_idletasks()
            self.spectrometer_link.desired = True
            self.spectrometer_link.phase = ConnectionPhase.CONNECTING
            token = self.connection_coordinator.begin("spectrometer")

            def _fail_visa(exc, res=resource):
                messagebox.showerror(
                    "Connection Failed",
                    f"Could not connect to VISA resource:\n{res}\n\n{exc}",
                    parent=self.root,
                )
                self.spectrometer = None
                self.spectrometer_link.phase = ConnectionPhase.ERROR
                self.status_message_var.set("Connection failed.")
                self._refresh_acquisition_controls()

            spectrometer = self.spectrometer
            self.connection_coordinator.submit(
                lambda res=resource, spec=spectrometer: spec.connect_with_resource(res),
                token=token,
                on_done=self._finish_connection,
                on_error=_fail_visa,
                on_stale=lambda _status, spec=spectrometer: spec.disconnect(),
                label="spectrometer connect (VISA)",
            )

    def _finish_connection(self, status: str):
        """Common post-connection setup (UI configuration, worker start)."""
        self.spectrometer_link.desired = True
        self.spectrometer_link.phase = ConnectionPhase.CONNECTED
        self.spectrometer_link.detail = ""
        self.timing_samples = []
        self.latest_timing_sample = None
        caps = self.spectrometer.capabilities
        model = caps.model or "Spectrometer"
        if len(model) > 32:
            model = f"{model[:29]}..."
        simulated = " [SIM]" if "SIMULATED" in status.upper() or caps.brand == "simulated" else ""
        self.connection_status_var.set(f"Connected: {model}{simulated}")
        self.status_message_var.set("Connected successfully.")

        # ── Configure UI for the connected device's capabilities ───
        # Graph axis limits and placeholder
        from prolibspector.acquisition.graph import configure_graph_for_device
        configure_graph_for_device(self.ax, self.canvas, self.live_line, caps)
        self._refresh_graph_settings_title()

        # Integration time range hint
        int_min_ms = caps.integration_time_min_us / 1000.0
        int_max_ms = caps.integration_time_max_us / 1000.0
        self.int_range_var.set(f"Range: {int_min_ms:.2f}–{int_max_ms:.0f} ms")

        # Enable/disable correction checkboxes based on device support
        if hasattr(self, 'dark_check'):
            if not caps.supports_dark_correction:
                self.correct_dark_var.set(False)
            self.dark_check.config(
                state="normal" if caps.supports_dark_correction else "disabled"
            )
        if hasattr(self, 'nl_check'):
            if not caps.supports_nonlinearity_correction:
                self.correct_nl_var.set(False)
            self.nl_check.config(
                state="normal" if caps.supports_nonlinearity_correction else "disabled"
            )

        # Start the worker thread
        self.worker = self._create_worker(self.spectrometer)
        self.worker.correct_dark_counts = bool(self.correct_dark_var.get())
        self.worker.correct_nonlinearity = bool(self.correct_nl_var.get())
        self.worker.auto_save_enabled = self.auto_save_var.get()
        self.worker.save_directory = self.save_dir_var.get()
        self.worker.sample_name = self.sample_name_var.get()
        if hasattr(self.worker, "set_spectra_qc_enabled") and hasattr(self, "spectra_qc_enabled_var"):
            self.worker.set_spectra_qc_enabled(
                bool(self.spectra_qc_enabled_var.get() and self._spectra_qc_available())
            )
        self.worker.start()
        self._refresh_acquisition_controls()
        if self._spectrometer_is_simulated(status):
            self._show_simulated_spectrum_preview()

        # Start polling the message queue
        self._poll_queue()

    def _create_worker(self, spectrometer):
        return self.controller.create_worker(spectrometer)

    def _spectrometer_is_simulated(self, status: str = "") -> bool:
        """Return True when the connected backend is a simulated spectrometer."""
        spec = getattr(self, "spectrometer", None)
        caps = getattr(spec, "capabilities", None)
        brand = str(getattr(caps, "brand", "") or "").strip().lower()
        return brand == "simulated" or "SIMULATED" in str(status or "").upper()

    def _show_simulated_spectrum_preview(self) -> bool:
        """Draw one simulated spectrum without entering live acquisition state."""
        spec = getattr(self, "spectrometer", None)
        if not (spec and getattr(spec, "is_connected", False)):
            return False
        if not self._spectrometer_is_simulated():
            return False
        try:
            caps = spec.capabilities
            normal_mode = getattr(caps, "normal_trigger_mode", None)
            if normal_mode is not None and getattr(spec, "current_trigger_mode", normal_mode) != normal_mode:
                spec.set_trigger_mode(normal_mode)
            wavelengths = spec.get_wavelengths()
            intensities = spec.get_intensities(
                correct_dark_counts=False,
                correct_nonlinearity=False,
            )
        except Exception as exc:
            self.status_message_var.set(f"Simulation connected, but spectrum preview failed: {exc}")
            return False

        self.current_wavelengths = wavelengths
        self.current_intensities = intensities
        self._pending_spectrum = None

        from prolibspector.acquisition.graph import update_spectrum_fast

        update_spectrum_fast(self.ax, self.canvas, self.live_line, wavelengths, intensities)
        self._last_plot_draw_at = time.perf_counter()
        for attr in ("save_spectrum_btn", "send_to_analysis_btn"):
            widget = getattr(self, attr, None)
            if widget is not None:
                try:
                    widget.config(state="normal")
                except tk.TclError:
                    pass
        if hasattr(self, "status_message_var"):
            self.status_message_var.set("Connected successfully. Simulated spectrum preview ready.")
        return True

    def on_diagnose(self):
        """Open the diagnostic dialog on demand (sidebar button)."""
        if self._disconnect_in_progress or self._shutdown_in_progress:
            if hasattr(self, "status_message_var"):
                self.status_message_var.set("Wait for the current disconnect to finish.")
            return
        result = self._open_diagnostic_dialog()
        if result is not None:
            # If already connected, disconnect first
            if self.spectrometer and self.spectrometer.is_connected:
                self.on_disconnect(
                    on_complete=lambda r=result: self._handle_diagnostic_result(r)
                )
                return
            self._handle_diagnostic_result(result)

    def _stop_worker_async(
        self,
        on_stopped,
        *,
        on_timeout=None,
        timeout_s: float = 3.0,
        hard_deadline_s: float = 10.0,
    ):
        """Request worker stop and poll for completion without blocking Tk.

        ``on_stopped`` fires on the Tk thread once the worker thread has
        exited (or immediately when no worker exists). After *timeout_s* a
        delay notice is shown; after *hard_deadline_s* ``on_timeout`` fires
        instead of waiting forever."""
        self._cancel_worker_stop_poll()
        if not self.worker or not self.worker.is_alive():
            self.worker = None
            on_stopped()
            return

        self.worker.stop()
        if hasattr(self, "worker_state_var"):
            self._set_worker_state("State: STOPPING")
        started = time.monotonic()
        delay_notified = False

        def _poll():
            nonlocal delay_notified
            self._worker_stop_after_id = None
            if not self.worker or not self.worker.is_alive():
                self.worker = None
                on_stopped()
                return
            elapsed = time.monotonic() - started
            if elapsed >= hard_deadline_s and on_timeout is not None:
                on_timeout()
                return
            if elapsed >= timeout_s and not delay_notified:
                delay_notified = True
                message = "Disconnect delayed until the pending hardware read finishes."
                logger.warning(message)
                if hasattr(self, "status_message_var"):
                    self.status_message_var.set(message)
            self._worker_stop_after_id = self.root.after(100, _poll)

        self._worker_stop_after_id = self.root.after(50, _poll)

    def _cancel_worker_stop_poll(self):
        if self._worker_stop_after_id is not None:
            try:
                self.root.after_cancel(self._worker_stop_after_id)
            except tk.TclError:
                pass
            self._worker_stop_after_id = None

    def on_disconnect(self, on_complete=None):
        """Disconnect from the spectrometer without blocking the Tk thread.

        Returns True when a disconnect was initiated. ``on_complete`` fires
        on the Tk thread after the hardware has been released."""
        if self._disconnect_in_progress or self._shutdown_in_progress:
            return False
        self._disconnect_in_progress = True
        self._cancel_queue_poll()
        self.spectrometer_link.desired = False
        self.spectrometer_link.phase = ConnectionPhase.DISCONNECTING
        token = self.connection_coordinator.begin("spectrometer")
        if hasattr(self, "status_message_var"):
            self.status_message_var.set("Disconnecting…")
        if hasattr(self, "connect_btn"):
            self.connect_btn.config(state="disabled")
        if hasattr(self, "disconnect_btn"):
            self.disconnect_btn.config(state="disabled")

        def _worker_stopped():
            spectrometer, self.spectrometer = self.spectrometer, None

            def _disconnect_hw():
                if spectrometer is not None:
                    spectrometer.disconnect()

            self.connection_coordinator.submit(
                _disconnect_hw,
                token=token,
                on_done=lambda _result: self._finish_spectrometer_disconnect(on_complete),
                on_error=lambda exc: self._finish_spectrometer_disconnect(on_complete, error=exc),
                label="spectrometer disconnect",
            )

        def _worker_timeout():
            self._disconnect_in_progress = False
            self.spectrometer_link.desired = True
            self.spectrometer_link.phase = ConnectionPhase.CONNECTED
            if self.worker:
                self._queue_poll_after_id = self.root.after(250, self._poll_queue)
            if hasattr(self, "status_message_var"):
                self.status_message_var.set(
                    "Disconnect abandoned: the acquisition worker did not stop."
                )
            self._refresh_acquisition_controls()

        self._stop_worker_async(_worker_stopped, on_timeout=_worker_timeout)
        return True

    def _finish_spectrometer_disconnect(self, on_complete=None, error=None):
        """Tk-thread epilogue once the spectrometer hardware is released."""
        self._disconnect_in_progress = False
        self.spectrometer_link.phase = ConnectionPhase.DISCONNECTED
        self.spectrometer_link.detail = ""
        if error is not None:
            logger.warning("Spectrometer disconnect raised: %s", error)

        self.connection_status_var.set("Disconnected")
        self._set_worker_state("State: IDLE")
        self.status_message_var.set("Disconnected.")

        # Reset buttons
        self.connect_btn.config(state="normal")
        self.disconnect_btn.config(state="disabled")
        self.live_btn.config(state="disabled")
        self._set_arm_button_visual("disabled")
        self.test_trigger_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.apply_int_btn.config(state="disabled")

        # Reset device-specific UI hints
        if hasattr(self, 'int_range_var'):
            self.int_range_var.set("")
        if hasattr(self, 'dark_check'):
            self.dark_check.config(state="normal")
            self.correct_dark_var.set(True)
        if hasattr(self, 'nl_check'):
            self.nl_check.config(state="normal")
            self.correct_nl_var.set(True)
        if hasattr(self, 'discard_plate_shot_btn'):
            self.discard_plate_shot_btn.config(state="disabled")
        if hasattr(self, 'repair_plate_wells_btn'):
            self.repair_plate_wells_btn.config(state="disabled")
        if hasattr(self, 'finish_plate_btn'):
            self.finish_plate_btn.config(state="disabled")
        if hasattr(self, 'plate_progress_var'):
            self.plate_mode_var.set(False)
            self.configure_plate_btn.config(state="disabled")
            self.plate_progress_var.set("")
            self.plate_progress = None
            self.plate_history = []
            self.current_plate_index = None
            self._plate_completion_prompt_pending = False
            self._pending_plate_run_state = None
            self._last_spectra_qc_result = None
            self._hide_plate_overview()
            self._hide_qc_review_panel()
        self.timing_samples = []
        self.latest_timing_sample = None
        self._refresh_acquisition_controls()
        if on_complete is not None:
            on_complete()

    def on_live_view(self):
        """Start live spectrum preview."""
        if self.worker and self.worker.is_alive():
            self.worker.start_live()
            self.live_btn.config(state="disabled")
            self._set_arm_button_visual("disabled")
            self.test_trigger_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self._set_worker_state("State: LIVE")
            self._refresh_acquisition_controls()

    def on_arm_trigger(self):
        """Arm the hardware trigger and wait for laser pulse.
        If loop-arm is enabled, the worker will automatically re-arm
        after each capture until the user clicks Stop."""
        if self._plate_requires_reconfigure():
            messagebox.showinfo(
                "Configure Next Plate",
                "Finish configuring the next plate before starting another capture.",
                parent=self.root,
            )
            return
        if self.worker and self.worker.is_alive():
            if self.worker.state == "ARMED":
                return
            self.worker.arm_trigger()
            self.live_btn.config(state="disabled")
            self._set_arm_button_visual("armed")
            self.test_trigger_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self._set_worker_state("State: ARMED")
            self._refresh_acquisition_controls()

    def on_test_trigger(self):
        """Fire a test capture using normal mode to verify the full pipeline."""
        if self._plate_requires_reconfigure():
            messagebox.showinfo(
                "Configure Next Plate",
                "Finish configuring the next plate before running another test capture.",
                parent=self.root,
            )
            return
        if self.worker and self.worker.is_alive():
            self.worker.test_trigger()
            self.live_btn.config(state="disabled")
            self._set_arm_button_visual("disabled")
            self.test_trigger_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self._set_worker_state("State: TEST")
            self._refresh_acquisition_controls()

    def on_stop(self):
        """Stop acquisition (live view or disarm trigger / loop)."""
        if self.worker:
            self.worker.go_idle()
            self._remove_highlight()
            if getattr(self.worker, "hardware_read_active", False):
                self._set_worker_state("State: STOPPING")
                if self.spectrometer and hasattr(self.spectrometer, "cancel_pending_read"):
                    self.status_message_var.set("Resetting spectrometer...")
                else:
                    self.status_message_var.set("Waiting for pending hardware read to finish.")
            else:
                self.live_btn.config(state="normal")
                self._update_arm_btn_state()
                self.test_trigger_btn.config(state="normal")
                self.stop_btn.config(state="disabled")
                self._set_worker_state("State: IDLE")
            self._refresh_acquisition_controls()

    def on_apply_integration(self):
        """Apply the integration time setting."""
        if not self.spectrometer or not self.spectrometer.is_connected:
            return
        if not self.worker or not self.worker.is_alive():
            messagebox.showinfo(
                "Acquisition Worker Stopped",
                "Disconnect and reconnect the spectrometer before changing acquisition settings.",
                parent=self.root,
            )
            return
        if self.worker and (
            self.worker.state in {"LIVE", "ARMED", "TEST", "DRY_RUN", "AUTOMATED", "LASER_TEST"}
            or getattr(self.worker, "hardware_read_active", False)
        ):
            messagebox.showinfo(
                "Acquisition Running",
                "Stop acquisition before changing integration time.",
                parent=self.root,
            )
            return

        try:
            ms = float(self.integration_var.get())
            if ms <= 0:
                raise ValueError
            us = int(ms * 1000)  # Convert ms to µs
            self.spectrometer.set_integration_time(us)
            self.status_message_var.set(f"Integration time: {ms} ms")
            self._refresh_graph_settings_title()
        except ValueError:
            messagebox.showwarning(
                "Invalid Input",
                "Please enter a positive number for integration time.",
                parent=self.root,
            )
        except Exception as e:
            messagebox.showerror("Error", str(e), parent=self.root)

    def on_averages_changed(self):
        """Update the number of averages in the worker."""
        if self.worker:
            try:
                self.worker.averages = max(1, int(self.averages_var.get()))
            except ValueError:
                pass
            else:
                self._refresh_graph_settings_title()

    def _refresh_graph_settings_title(self):
        """Show the active integration time and averages in the graph title."""
        if not (self.spectrometer and self.spectrometer.is_connected):
            return
        try:
            model = self.spectrometer.capabilities.model or "Spectrometer"
        except Exception:
            model = "Spectrometer"
        if len(model) > 32:
            model = f"{model[:29]}..."
        parts = []
        try:
            parts.append(f"{float(self.integration_var.get()):g} ms")
        except (ValueError, tk.TclError, AttributeError):
            pass
        try:
            averages = int(self.averages_var.get())
            if averages > 1:
                parts.append(f"{averages} avg")
        except (ValueError, tk.TclError, AttributeError):
            pass
        suffix = f"  ·  {' × '.join(parts)}" if parts else ""
        try:
            self.ax.set_title(f"Live Spectrum — {model}{suffix}")
            self.canvas.draw_idle()
        except Exception:
            logger.debug("Could not refresh graph settings title.", exc_info=True)

    def on_corrections_changed(self):
        """Update dark count and nonlinearity correction flags in the worker."""
        if self.worker:
            self.worker.correct_dark_counts = self.correct_dark_var.get()
            self.worker.correct_nonlinearity = self.correct_nl_var.get()

    def on_auto_save_toggle(self):
        """Toggle auto-save on/off."""
        if self.plate_mode_var.get() and not self.auto_save_var.get():
            messagebox.showinfo(
                "Plate Mode Uses Auto-Save",
                "High-throughput plate mode needs auto-save so each trigger can advance the plate map.",
                parent=self.root,
            )
            self.auto_save_var.set(True)
        if self.worker:
            self.worker.auto_save_enabled = self.auto_save_var.get()

    def on_plate_mode_toggle(self):
        """Enable or disable high-throughput plate autosave."""
        if self._is_acquisition_busy():
            messagebox.showinfo(
                "Acquisition Running",
                "Stop acquisition before changing high-throughput plate mode.",
                parent=self.root,
            )
            self.plate_mode_var.set(self.plate_progress is not None)
            return

        if self.plate_mode_var.get():
            self.auto_save_var.set(True)
            self.on_auto_save_toggle()
            self.configure_plate_btn.config(state="normal")
            if not self.on_configure_plate():
                self.plate_mode_var.set(False)
                self.configure_plate_btn.config(state="disabled")
                self.plate_progress_var.set("")
                self.plate_history = []
                self.current_plate_index = None
                self._hide_plate_overview()
        else:
            if self.worker:
                self.worker.disable_plate_autosave()
            self.configure_plate_btn.config(state="disabled")
            self.discard_plate_shot_btn.config(state="disabled")
            self.repair_plate_wells_btn.config(state="disabled")
            self.finish_plate_btn.config(state="disabled")
            self.plate_progress_var.set("")
            self.plate_progress = None
            self.plate_history = []
            self.current_plate_index = None
            self._plate_completion_prompt_pending = False
            self._pending_plate_run_state = None
            self._last_spectra_qc_result = None
            self._hide_plate_overview()
            self._hide_qc_review_panel()

    def on_configure_plate(self):
        """Open the high-throughput plate settings dialog."""
        if self._is_acquisition_busy():
            messagebox.showinfo(
                "Acquisition Running",
                "Stop acquisition before changing plate settings.",
                parent=self.root,
            )
            return False

        selection = self._ask_plate_settings()
        if selection is None:
            return False
        if isinstance(selection, PlateRunState):
            self._apply_resumed_plate_state(selection)
        else:
            self._apply_plate_config(selection)
        return True

    def on_discard_last_plate_shot(self):
        """Discard the latest saved high-throughput plate shot."""
        if self.worker:
            self.worker.discard_last_plate_shot()

    def on_repair_plate_wells(self):
        """Pause the current plate run, select wells, and queue a repair pass."""
        if (
            not self.worker
            or not self.plate_progress
            or self._plate_payload_finished(self.plate_progress)
            or self.plate_progress.get("repair_active")
        ):
            return

        resume_state = self._pause_plate_worker_for_modal()
        selected_wells = None
        try:
            selected_wells = self._ask_plate_repair_wells()
        finally:
            if not selected_wells:
                self._resume_plate_worker_mode(resume_state)

        if not selected_wells:
            return

        self.worker.start_plate_repair(selected_wells)
        self._resume_plate_worker_mode(resume_state)

    def on_finish_plate_early(self):
        """Close the current plate early and prompt for the next one."""
        if not self.plate_progress or self._plate_payload_finished(self.plate_progress):
            return
        if self.plate_progress.get("repair_active"):
            messagebox.showinfo(
                "Repair In Progress",
                "Finish the current repair pass before closing the plate early.",
                parent=self.root,
            )
            return

        if self._is_acquisition_busy():
            messagebox.showinfo(
                "Acquisition Running",
                "Stop acquisition before finishing the current plate early.",
                parent=self.root,
            )
            return

        if not messagebox.askyesno(
            "Finish Plate Early",
            "This will mark the remaining wells as skipped and let you configure the next plate.\n\nContinue?",
            parent=self.root,
        ):
            return

        payload = dict(self.plate_progress)
        payload["closed_early"] = True
        payload["current_well"] = None
        payload["can_discard"] = False
        self._update_plate_progress(payload)
        self.status_message_var.set("Plate closed early.")
        if self.worker:
            self.worker.close_plate_run_early()
        self._pending_plate_run_state = None
        self._prompt_for_next_plate()

    def _pause_plate_worker_for_modal(self):
        """Pause live/armed acquisition before opening a modal plate action."""
        if not self.worker:
            return None

        previous_state = self.worker.state
        if previous_state in ("LIVE", "ARMED", "TEST"):
            self.on_stop()
            return previous_state
        return None

    def _resume_plate_worker_mode(self, previous_state):
        """Restore the worker mode that was active before a modal plate action."""
        if previous_state == "ARMED":
            self.on_arm_trigger()
        elif previous_state == "LIVE":
            self.on_live_view()

    def _format_well_list(self, wells, max_items=8):
        ordered = [well for well in self.plate_autosave_config.ordered_wells if well in set(wells)] if self.plate_autosave_config else list(wells)
        if len(ordered) <= max_items:
            return ", ".join(ordered)
        return f"{', '.join(ordered[:max_items])}, +{len(ordered) - max_items} more"

    def _ask_plate_repair_wells(self):
        """Let the user click wells to repair on the current plate map."""
        if not self.plate_progress:
            return None

        repairable_wells = {
            well for well, count in self.plate_progress["shots_by_well"].items()
            if count > 0
        }
        if not repairable_wells:
            messagebox.showinfo(
                "No Saved Wells",
                "There are no saved wells available to repair yet.",
                parent=self.root,
            )
            return None

        result = {"selection": None}
        selected_wells: set[str] = set()

        dlg = create_dialog(self.root, "Repair Plate Wells", modal=True, resizable=True)
        scale = ui_scale_for_widget(dlg)

        outer = ttk.Frame(dlg, padding=14)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(
            outer,
            text="Select wells to repair",
            font=scaled_font(13, "bold", scale=scale),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        ttk.Label(
            outer,
            text="Click any saved well to re-shoot it. The current files for those wells will move to Discarded, then the plate run will temporarily revisit them before resuming the normal order.",
            style="Status.TLabel",
            wraplength=scaled_wrap(760, scale),
        ).grid(row=1, column=0, sticky="ew", pady=(0, 10))

        preview = ttk.LabelFrame(outer, text="Plate Repair Map", padding=10)
        preview.grid(row=2, column=0, sticky="nsew")
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(0, weight=1)

        canvas = tk.Canvas(
            preview,
            width=700,
            height=430,
            bg=get_palette()["canvas_bg"],
            highlightthickness=1,
            highlightbackground=get_palette()["canvas_border"],
        )
        canvas.grid(row=0, column=0, sticky="nsew")

        selected_var = tk.StringVar(value="Selected wells: none")
        ttk.Label(
            outer,
            textvariable=selected_var,
            style="StatusValue.TLabel",
            wraplength=scaled_wrap(760, scale),
        ).grid(row=3, column=0, sticky="ew", pady=(10, 0))

        def _refresh_summary():
            if selected_wells:
                selected_var.set(f"Selected wells: {self._format_well_list(selected_wells)}")
            else:
                selected_var.set("Selected wells: none")

        def _redraw():
            payload = dict(self.plate_progress)
            self._draw_plate_payload(
                canvas,
                payload,
                preview=False,
                selected_wells=selected_wells,
                disabled_wells=set(payload["shots_by_well"]) - repairable_wells,
            )

        def _on_click(event):
            well = self._well_from_canvas_point(
                self.plate_progress,
                event.x,
                event.y,
                preview=False,
                canvas_width=canvas.winfo_width(),
                canvas_height=canvas.winfo_height(),
            )
            if not well or well not in repairable_wells:
                return
            if well in selected_wells:
                selected_wells.remove(well)
            else:
                selected_wells.add(well)
            _refresh_summary()
            _redraw()

        def _confirm():
            if not selected_wells:
                messagebox.showinfo(
                    "Select Wells",
                    "Choose at least one saved well to repair.",
                    parent=dlg,
                )
                return
            ordered = [
                well for well in self.plate_autosave_config.ordered_wells
                if well in selected_wells
            ]
            result["selection"] = ordered
            dlg.destroy()

        canvas.bind("<Button-1>", _on_click)
        canvas.bind("<Configure>", lambda _event: _redraw())

        button_bar = ttk.Frame(outer)
        button_bar.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(button_bar, text="Cancel", width=12, command=dlg.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_bar, text="Repair Selected Wells", width=22, command=_confirm).pack(side=tk.RIGHT)

        _refresh_summary()
        _redraw()
        apply_window_policy(
            dlg,
            parent=self.root,
            preferred=(860, 660),
            min_size=(720, 540),
            modal=True,
            resizable=True,
        )
        dlg.wait_window()
        return result["selection"]

    def on_sample_name_changed(self):
        """Update the sample name in the worker and reset shot index."""
        if self.worker:
            self.worker.sample_name = self.sample_name_var.get()
            self.worker.reset_shot_index()
            self.shot_count_var.set("Shots: 0")

    def on_browse_save_dir(self):
        """Open a directory chooser for the auto-save location."""
        directory = filedialog.askdirectory(
            title="Select Save Directory",
            initialdir=self.save_dir_var.get()
        )
        if directory:
            self.save_dir_var.set(directory)
            if self.worker:
                self.worker.save_directory = directory
            self._persist_ui_prefs()

    _UI_PREF_VARS = (
        ("last_save_dir", "save_dir_var"),
        ("last_sample_name", "sample_name_var"),
        ("last_integration_ms", "integration_var"),
        ("last_laser_baud", "laser_baud_var"),
        ("last_jog_step", "jog_step_var"),
        ("last_jog_feed", "jog_feed_var"),
        ("last_laser_profile", "laser_profile_var"),
        ("last_run_type", "automation_run_type_var"),
    )

    def _persist_ui_prefs(self):
        """Remember last-used acquisition choices for the next session."""
        from prolibspector.core.settings import save_ui_prefs

        prefs = {}
        for key, attr in self._UI_PREF_VARS:
            var = getattr(self, attr, None)
            if var is None:
                continue
            try:
                value = str(var.get()).strip()
            except tk.TclError:
                continue
            if value:
                prefs[key] = value
        try:
            save_ui_prefs(prefs)
        except Exception:
            logger.debug("Could not persist UI preferences.", exc_info=True)

    @contextmanager
    def _busy_cursor(self):
        """Show a wait cursor around a short synchronous operation."""
        try:
            self.root.config(cursor="watch")
            self.root.update_idletasks()
        except tk.TclError:
            pass
        try:
            yield
        finally:
            try:
                self.root.config(cursor="")
            except tk.TclError:
                pass

    def on_save_spectrum(self):
        """Manually save the current spectrum to a user-chosen file."""
        if self.current_wavelengths is None or self.current_intensities is None:
            messagebox.showinfo("No Data", "No spectrum data to save.", parent=self.root)
            return

        filetypes = [("CSV files", "*.csv"), ("Text files", "*.txt"), ("All files", "*.*")]
        file_path = filedialog.asksaveasfilename(
            title="Save Spectrum",
            filetypes=filetypes,
            defaultextension=".csv"
        )
        if file_path:
            try:
                data = np.column_stack((self.current_wavelengths, self.current_intensities))
                np.savetxt(file_path, data, delimiter='\t',
                           header="Wavelength\tIntensity", comments='', fmt='%.6f')
                messagebox.showinfo("Saved", f"Spectrum saved to:\n{file_path}", parent=self.root)
            except Exception as e:
                messagebox.showerror("Save Error", str(e), parent=self.root)

    def on_send_to_analysis(self):
        """Store the current spectrum for hand-off to Analysis mode, then close."""
        if self._shutdown_in_progress:
            return
        if self.current_wavelengths is None or self.current_intensities is None:
            messagebox.showinfo("No Data", "No spectrum data to send.", parent=self.root)
            return

        result = messagebox.askyesno(
            "Send to Analysis",
            "This will close Acquisition Mode and open Analysis Mode "
            "with the current spectrum loaded.\n\nContinue?",
            parent=self.root,
        )
        if result:
            self._handoff_data = {
                "wavelengths": self.current_wavelengths.copy(),
                "intensities": self.current_intensities.copy()
            }
            if not self._cleanup_and_quit():
                self._handoff_data = None

    # ═══════════════════════════════════════════════════════════════════
    #  Helpers
    # ═══════════════════════════════════════════════════════════════════

    def _build_qc_review_panel(self):
        scale = ui_scale_for_widget(self.root)
        self.qc_review_frame = ttk.LabelFrame(
            self.plate_overview_frame,
            text="QC Results",
            padding=(8, 6),
        )
        self.qc_review_frame.columnconfigure(0, weight=1)
        self.qc_review_frame.columnconfigure(1, weight=0)
        self.qc_review_frame.rowconfigure(1, weight=1)

        self.qc_review_summary_var = tk.StringVar(value="No spectra QC results yet.")
        ttk.Label(
            self.qc_review_frame,
            textvariable=self.qc_review_summary_var,
            style="StatusValue.TLabel",
        ).grid(row=0, column=0, sticky="w")

        button_bar = ttk.Frame(self.qc_review_frame)
        button_bar.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.qc_open_csv_btn = ttk.Button(
            button_bar,
            text="Open CSV",
            command=self.on_open_spectra_qc_csv,
            state="disabled",
        )
        self.qc_open_csv_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.qc_open_report_btn = ttk.Button(
            button_bar,
            text="Open Report",
            command=self.on_open_spectra_qc_report,
            state="disabled",
        )
        self.qc_open_report_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.qc_run_btn = ttk.Button(
            button_bar,
            text="Run/Rerun QC",
            command=self.on_run_plate_qc,
            state="disabled",
        )
        self.qc_run_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.qc_queue_repeats_btn = ttk.Button(
            button_bar,
            text="Queue Selected Repeats",
            command=self.on_queue_qc_repeats,
            state="disabled",
        )
        self.qc_queue_repeats_btn.pack(side=tk.LEFT)

        columns = ("plate", "well", "status", "score", "reasons")
        self.qc_results_tree_frame = ttk.Frame(self.qc_review_frame)
        self.qc_results_tree_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        self.qc_results_tree_frame.columnconfigure(0, weight=1)
        self.qc_results_tree_frame.rowconfigure(0, weight=1)
        self.qc_results_tree = ttk.Treeview(
            self.qc_results_tree_frame,
            columns=columns,
            show="headings",
            height=4,
            selectmode="extended",
        )
        self.qc_results_scrollbar = ttk.Scrollbar(
            self.qc_results_tree_frame,
            orient=tk.VERTICAL,
            command=self.qc_results_tree.yview,
        )
        self.qc_results_tree.configure(yscrollcommand=self.qc_results_scrollbar.set)
        self.qc_results_tree.heading("plate", text="Plate")
        self.qc_results_tree.heading("well", text="Well")
        self.qc_results_tree.heading("status", text="Status")
        self.qc_results_tree.heading("score", text="Score")
        self.qc_results_tree.heading("reasons", text="Reasons")
        self.qc_results_tree.column("plate", width=90, anchor="w", stretch=False)
        self.qc_results_tree.column("well", width=60, anchor="center", stretch=False)
        self.qc_results_tree.column("status", width=70, anchor="center", stretch=False)
        self.qc_results_tree.column("score", width=70, anchor="e", stretch=False)
        self.qc_results_tree.column("reasons", width=420, anchor="w", stretch=True)
        self.qc_results_tree.grid(row=0, column=0, sticky="nsew")
        self.qc_results_scrollbar.grid(row=0, column=1, sticky="ns")
        self.qc_results_tree.bind("<<TreeviewSelect>>", lambda _event: self._refresh_qc_controls())

        self.qc_review_paths_var = tk.StringVar(value="")
        ttk.Label(
            self.qc_review_frame,
            textvariable=self.qc_review_paths_var,
            style="Status.TLabel",
            wraplength=scaled_wrap(980, scale),
        ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

    def _refresh_spectra_qc_availability(self, *, refresh_controls: bool = True):
        from prolibspector.acquisition.spectra_readiness_qc import load_default_spectra_qc

        result = load_default_spectra_qc()
        self._spectra_qc_load_result = result
        if hasattr(self, "spectra_qc_status_var"):
            if result.available:
                self.spectra_qc_status_var.set("Spectra QC calibration ready.")
            else:
                if hasattr(self, "spectra_qc_enabled_var"):
                    self.spectra_qc_enabled_var.set(False)
                self.spectra_qc_status_var.set(result.message)
        if refresh_controls:
            self._refresh_acquisition_controls()
        return result

    def _spectra_qc_available(self) -> bool:
        result = self._spectra_qc_load_result
        if result is None:
            result = self._refresh_spectra_qc_availability(refresh_controls=False)
        return bool(result and result.available)

    def on_spectra_qc_toggle(self):
        enabled = bool(getattr(self, "spectra_qc_enabled_var", tk.BooleanVar(value=False)).get())
        available = self._spectra_qc_available()
        if enabled and not available:
            self.spectra_qc_enabled_var.set(False)
            message = (
                self._spectra_qc_load_result.message
                if self._spectra_qc_load_result
                else "Spectra QC calibration unavailable."
            )
            self.status_message_var.set(message)
            enabled = False
        if self.worker and hasattr(self.worker, "set_spectra_qc_enabled"):
            self.worker.set_spectra_qc_enabled(enabled and available)
        self._refresh_acquisition_controls()

    def on_run_plate_qc(self):
        if not self.worker or not hasattr(self.worker, "run_plate_qc_now"):
            messagebox.showinfo("Spectra QC", "Connect the spectrometer before running spectra QC.", parent=self.root)
            return
        if not self._spectra_qc_available():
            message = (
                self._spectra_qc_load_result.message
                if self._spectra_qc_load_result
                else "Spectra QC calibration unavailable."
            )
            messagebox.showinfo("Spectra QC", message, parent=self.root)
            return
        self.status_message_var.set("Running spectra readiness QC...")
        self.worker.run_plate_qc_now(background=True)
        self._refresh_acquisition_controls()

    def _handle_qc_complete(self, data):
        return self.controller.handle_qc_complete(data)

    def _handle_qc_complete_impl(self, data):
        result = dict(data or {})
        self._last_spectra_qc_result = result
        pass_count = int(result.get("pass_count") or 0)
        warn_count = int(result.get("warn_count") or 0)
        fail_count = int(result.get("fail_count") or 0)
        summary = f"QC complete: {pass_count} pass, {warn_count} warn, {fail_count} fail."
        if hasattr(self, "spectra_qc_status_var"):
            self.spectra_qc_status_var.set(summary)
        self.status_message_var.set(summary)
        self._populate_qc_review(result)
        self._show_qc_review_panel()
        self._refresh_acquisition_controls()

    def _handle_qc_error(self, data):
        return self.controller.handle_qc_error(data)

    def _handle_qc_error_impl(self, data):
        message = str(data or "Spectra QC could not run.")
        if hasattr(self, "spectra_qc_status_var"):
            self.spectra_qc_status_var.set(message)
        self.status_message_var.set(f"Spectra QC unavailable: {message}")
        self._refresh_acquisition_controls()

    def _populate_qc_review(self, result: dict):
        if not hasattr(self, "qc_results_tree"):
            return
        tree = self.qc_results_tree
        for iid in tree.get_children():
            tree.delete(iid)
        self._qc_tree_rows_by_iid = {}

        rows = list(result.get("rows") or [])
        pass_count = int(result.get("pass_count") or 0)
        warn_count = int(result.get("warn_count") or 0)
        fail_count = int(result.get("fail_count") or 0)
        non_pass_rows = []
        for row in rows:
            status = str(row.get("gate_status") or row.get("status") or "").lower()
            if status != "pass":
                non_pass_rows.append(row)

        self.qc_review_summary_var.set(
            f"{pass_count} pass, {warn_count} warn, {fail_count} fail; {len(rows)} well(s) scored."
        )
        csv_path = result.get("csv_path") or ""
        pdf_path = result.get("pdf_path") or ""
        self.qc_review_paths_var.set(f"CSV: {csv_path or 'not written'}    Report: {pdf_path or 'not written'}")

        default_selection = []
        for index, row in enumerate(non_pass_rows):
            status = str(row.get("gate_status") or row.get("status") or "warn").lower()
            score = row.get("readiness_score")
            try:
                score_text = f"{float(score):.1f}"
            except (TypeError, ValueError):
                score_text = ""
            reasons = row.get("top_failure_reasons") or row.get("failure_reasons") or row.get("reasons") or ""
            if isinstance(reasons, (list, tuple)):
                reasons = "; ".join(str(item) for item in reasons if str(item).strip())
            plate_label = row.get("plate_name") or f"P{int(row.get('plate_index') or 1):02d}"
            iid = f"qc-{index}"
            tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    plate_label,
                    str(row.get("well") or "").upper(),
                    status.upper(),
                    score_text,
                    str(reasons),
                ),
                tags=(status,),
            )
            self._qc_tree_rows_by_iid[iid] = dict(row)
            if status == "fail":
                default_selection.append(iid)
        if default_selection:
            tree.selection_set(default_selection)

    def _show_qc_review_panel(self):
        if not hasattr(self, "qc_review_frame"):
            return
        self.plate_overview_frame.configure(height=560)
        self.plate_history_canvas.configure(height=390)
        if not self.qc_review_frame.winfo_ismapped():
            self.qc_review_frame.pack(fill=tk.X, pady=(6, 0))

    def _hide_qc_review_panel(self):
        if hasattr(self, "qc_review_frame"):
            self.qc_review_frame.pack_forget()
        if hasattr(self, "plate_overview_frame"):
            self.plate_overview_frame.configure(height=440)

    def _selected_qc_repeat_rows(self) -> list[dict]:
        if not hasattr(self, "qc_results_tree"):
            return []
        rows = []
        for iid in self.qc_results_tree.selection():
            row = self._qc_tree_rows_by_iid.get(iid)
            if row:
                rows.append(dict(row))
        return rows

    def on_queue_qc_repeats(self):
        selected = self._selected_qc_repeat_rows()
        if not selected:
            messagebox.showinfo("Queue QC Repeats", "Select at least one non-pass well to repeat.", parent=self.root)
            return
        if not self.worker or not hasattr(self.worker, "queue_qc_repeats"):
            messagebox.showinfo("Queue QC Repeats", "Connect the spectrometer before queueing repeats.", parent=self.root)
            return
        try:
            summary = self.worker.queue_qc_repeats(selected)
        except Exception as exc:
            messagebox.showerror("Queue QC Repeats", str(exc), parent=self.root)
            return
        if getattr(self, "automation_config", None) and hasattr(self.worker, "plate_progress_payloads"):
            self._seed_automation_plate_history(self.automation_config, self.worker.plate_progress_payloads())
        queued = int(summary.get("queued_wells") or summary.get("targets") or 0)
        self.status_message_var.set(f"Queued {queued} QC repeat well(s). Start acquisition when ready.")
        self._refresh_acquisition_controls()

    def _open_spectra_qc_path(self, key: str):
        result = self._last_spectra_qc_result or {}
        path = result.get(key) or ""
        if not path:
            return
        try:
            if os.name == "nt" and hasattr(os, "startfile"):
                os.startfile(path)
            else:
                webbrowser.open(Path(path).resolve().as_uri())
        except Exception as exc:
            messagebox.showerror("Open QC Output", str(exc), parent=self.root)

    def on_open_spectra_qc_csv(self):
        self._open_spectra_qc_path("csv_path")

    def on_open_spectra_qc_report(self):
        self._open_spectra_qc_path("pdf_path")

    def _refresh_qc_controls(self, *, busy: bool | None = None, connected: bool | None = None):
        if busy is None:
            busy = self._is_acquisition_busy()
        if connected is None:
            connected = bool(self.spectrometer and self.spectrometer.is_connected)
        available = bool(self._spectra_qc_load_result and self._spectra_qc_load_result.available)
        if hasattr(self, "spectra_qc_check"):
            self.spectra_qc_check.config(state="normal" if available and not busy else "disabled")
        if hasattr(self, "qc_run_btn"):
            has_saved_plate = bool(self.plate_progress and self.plate_progress.get("saved_shots", 0))
            self.qc_run_btn.config(state="normal" if connected and available and has_saved_plate and not busy else "disabled")
        result = self._last_spectra_qc_result or {}
        csv_path = result.get("csv_path") or ""
        pdf_path = result.get("pdf_path") or ""
        csv_exists = bool(csv_path and os.path.exists(csv_path))
        pdf_exists = bool(pdf_path and os.path.exists(pdf_path))
        if hasattr(self, "qc_open_csv_btn"):
            self.qc_open_csv_btn.config(state="normal" if csv_exists else "disabled")
        if hasattr(self, "qc_open_report_btn"):
            self.qc_open_report_btn.config(state="normal" if pdf_exists else "disabled")
        if hasattr(self, "qc_queue_repeats_btn"):
            can_queue = bool(self._selected_qc_repeat_rows()) and connected and not busy
            self.qc_queue_repeats_btn.config(state="normal" if can_queue else "disabled")

    def _should_defer_plate_completion_prompt_for_qc(self) -> bool:
        result = self._last_spectra_qc_result
        if not isinstance(result, dict):
            return False
        try:
            fail_count = int(result.get("fail_count") or 0)
        except (TypeError, ValueError):
            fail_count = 0
        return fail_count > 0

    def _update_arm_btn_state(self):
        """Keep the Arm Trigger button visually clear across acquisition states."""
        if not hasattr(self, "arm_btn"):
            return

        if not self.spectrometer or not self.spectrometer.is_connected:
            self._set_arm_button_visual("disabled")
            return

        if not self.worker or not self.worker.is_alive():
            self._set_arm_button_visual("disabled")
            return

        if self.worker and getattr(self.worker, "hardware_read_active", False):
            self._set_arm_button_visual("disabled")
            return

        if not self.spectrometer.capabilities.has_external_trigger:
            self._set_arm_button_visual("disabled")
            return

        if self.worker and self.worker.state == "ARMED":
            self._set_arm_button_visual("armed")
            return

        if self.worker and self.worker.state != "IDLE":
            self._set_arm_button_visual("disabled")
            return

        self._set_arm_button_visual("ready")

    def _refresh_acquisition_controls(self):
        """Apply connected/worker state to the main acquisition controls."""
        connected = bool(self.spectrometer and self.spectrometer.is_connected)
        worker_state = self.worker.state if self.worker else "IDLE"
        worker_alive = bool(self.worker and self.worker.is_alive())
        hardware_read_active = bool(self.worker and getattr(self.worker, "hardware_read_active", False))
        busy = connected and worker_alive and (worker_state != "IDLE" or hardware_read_active)
        can_acquire = connected and worker_alive and not busy

        transitioning = self._disconnect_in_progress or self._shutdown_in_progress
        if hasattr(self, "connect_btn"):
            self.connect_btn.config(state="disabled" if connected or transitioning else "normal")
        if hasattr(self, "disconnect_btn"):
            self.disconnect_btn.config(state="normal" if connected and not transitioning else "disabled")
        if hasattr(self, "apply_int_btn"):
            self.apply_int_btn.config(state="normal" if can_acquire else "disabled")
        self._refresh_qc_controls(busy=busy, connected=connected)

        if not connected:
            if hasattr(self, "live_btn"):
                self.live_btn.config(state="disabled")
            if hasattr(self, "test_trigger_btn"):
                self.test_trigger_btn.config(state="disabled")
            if hasattr(self, "stop_btn"):
                self.stop_btn.config(state="disabled")
            self._set_arm_button_visual("disabled")
            return

        if hasattr(self, "live_btn"):
            self.live_btn.config(state="normal" if can_acquire else "disabled")
        if hasattr(self, "test_trigger_btn"):
            self.test_trigger_btn.config(state="normal" if can_acquire else "disabled")
        if hasattr(self, "stop_btn"):
            self.stop_btn.config(state="normal" if busy else "disabled")
        self._update_arm_btn_state()

    def _set_arm_button_visual(self, visual_state):
        """Show whether the trigger is ready, armed, or unavailable."""
        if not hasattr(self, "arm_btn"):
            return

        self._arm_button_visual_state = visual_state
        palette = get_palette()
        if visual_state == "armed":
            self.arm_btn.config(
                text="Armed | Waiting",
                style="Accent.TButton",
                state="normal",
            )
            if hasattr(self, "arm_status_var"):
                self.arm_status_var.set("Armed and waiting for trigger")
            if hasattr(self, "arm_status_chip"):
                self.arm_status_chip.config(bg=palette["qc_pass"], fg=palette["well_text"])
        elif visual_state == "ready":
            self.arm_btn.config(
                text="Arm Trigger",
                style="Accent.TButton",
                state="normal",
            )
            if hasattr(self, "arm_status_var"):
                self.arm_status_var.set("Arm trigger before shooting")
            if hasattr(self, "arm_status_chip"):
                self.arm_status_chip.config(bg=palette["qc_fail"], fg=palette["well_text"])
        else:
            self.arm_btn.config(
                text="Arm Trigger",
                style="Accent.TButton",
                state="disabled",
            )
            if hasattr(self, "arm_status_var"):
                self.arm_status_var.set("Trigger unavailable")
            if hasattr(self, "arm_status_chip"):
                self.arm_status_chip.config(bg=palette["well_disabled"], fg=palette["muted_text"])

    def _set_worker_state(self, state_text):
        """Update the worker state text and color-code armed vs not armed."""
        self.worker_state_var.set(state_text)
        if hasattr(self, "worker_state_label"):
            is_armed = "ARMED" in str(state_text).upper()
            palette = get_palette()
            self.worker_state_label.configure(
                foreground=palette["qc_pass"] if is_armed else palette["qc_fail"]
            )
        self._refresh_window_title(state_text)

    def _refresh_window_title(self, state_text):
        """Reflect the worker state in the window title (taskbar-visible)."""
        state = str(state_text or "").removeprefix("State: ").strip()
        # Strip the activity spinner frames so the title doesn't animate.
        state = state.rstrip("◐◓◑◒ ").strip()
        title = f"{self.window_title} — {state}" if state and state != "IDLE" else self.window_title
        if getattr(self, "_window_title_text", None) == title:
            return
        self._window_title_text = title
        try:
            self.root.title(title)
        except tk.TclError:
            pass

    def _schedule_spectrum_draw(self):
        """Coalesce rapid spectrum updates into a capped redraw cadence."""
        if self._pending_spectrum is None or self._pending_draw_after_id is not None:
            return

        elapsed_ms = (time.perf_counter() - self._last_plot_draw_at) * 1000.0
        delay_ms = 0 if elapsed_ms >= self.live_redraw_interval_ms else int(self.live_redraw_interval_ms - elapsed_ms)
        self._pending_draw_after_id = self.root.after(delay_ms, self._flush_pending_spectrum)

    def _flush_pending_spectrum(self):
        """Draw the latest queued live spectrum."""
        self._pending_draw_after_id = None
        if self._pending_spectrum is None:
            return

        wavelengths, intensities = self._pending_spectrum
        self._pending_spectrum = None

        from prolibspector.acquisition.graph import update_spectrum_fast

        update_spectrum_fast(self.ax, self.canvas, self.live_line, wavelengths, intensities)
        self._last_plot_draw_at = time.perf_counter()

    def _cancel_pending_spectrum_draw(self, *, drop_pending: bool = False):
        """Cancel any scheduled coalesced redraw callback."""
        if self._pending_draw_after_id is not None:
            try:
                self.root.after_cancel(self._pending_draw_after_id)
            except tk.TclError:
                pass
            self._pending_draw_after_id = None
        if drop_pending:
            self._pending_spectrum = None

    def _cancel_canvas_idle_draw(self):
        """Cancel any Matplotlib Tk idle-draw callback before tearing down the UI."""
        if not hasattr(self, "canvas"):
            return
        idle_draw_id = getattr(self.canvas, "_idle_draw_id", None)
        if not idle_draw_id:
            return
        try:
            self.canvas._tkcanvas.after_cancel(idle_draw_id)
        except (AttributeError, tk.TclError):
            pass
        self.canvas._idle_draw_id = None

    def _queue_poll_delay_ms(self, processed_messages: int):
        """Poll faster while acquisition is active and slower when idle."""
        if self.worker is None:
            return self.idle_queue_poll_ms
        if processed_messages > 0 or self._pending_spectrum is not None:
            return self.active_queue_poll_ms
        if self.worker.state in {"LIVE", "ARMED", "TEST"}:
            return self.active_queue_poll_ms
        return self.idle_queue_poll_ms

    def _queue_poll_message_limit(self) -> int | None:
        """Optional per-Tk-tick message cap for modes with heavy UI work."""
        return None

    def _after_queue_poll_batch(self, *, yielded: bool, processed_messages: int) -> None:
        """Hook for subclasses that coalesce expensive redraws across queue batches."""
        return None

    # ═══════════════════════════════════════════════════════════════════
    #  Message Queue Polling (thread-safe GUI updates)
    # ═══════════════════════════════════════════════════════════════════

    def _is_acquisition_busy(self):
        return bool(self.worker and self.worker.state != "IDLE")

    def _plate_payload_finished(self, payload):
        return bool(payload and (payload.get("complete") or payload.get("closed_early")))

    def _plate_requires_reconfigure(self):
        return bool(self.plate_mode_var.get() and self._plate_payload_finished(self.plate_progress))

    def _current_plate_runtime_settings(self):
        """Snapshot the acquisition settings that should be written into plate metadata."""
        averages = 1
        if hasattr(self, "averages_var"):
            try:
                averages = max(1, int(self.averages_var.get()))
            except (TypeError, ValueError):
                averages = 1

        requested_dark = bool(self.correct_dark_var.get()) if hasattr(self, "correct_dark_var") else False
        requested_nl = bool(self.correct_nl_var.get()) if hasattr(self, "correct_nl_var") else False
        corrections = resolve_effective_corrections(
            getattr(getattr(self, "spectrometer", None), "capabilities", None),
            requested_dark_counts=requested_dark,
            requested_nonlinearity=requested_nl,
        )
        return {
            "sample_name": self.sample_name_var.get().strip() or "Sample",
            "integration_time_ms": self.integration_var.get().strip() if hasattr(self, "integration_var") else "",
            "averages": averages,
            "correct_dark_counts": corrections.requested_dark_counts,
            "correct_nonlinearity": corrections.requested_nonlinearity,
            "effective_correct_dark_counts": corrections.correct_dark_counts,
            "effective_correct_nonlinearity": corrections.correct_nonlinearity,
            "correction_mask_reasons": corrections.mask_reasons(),
        }

    def _apply_plate_config(self, config):
        if not isinstance(config, PlateAutosaveConfig):
            config = PlateAutosaveConfig.from_mapping(config)

        self.plate_autosave_config = config
        self.plate_mode_var.set(True)
        self.configure_plate_btn.config(state="normal")
        self.auto_save_var.set(True)
        self.on_auto_save_toggle()
        self._pending_plate_run_state = None
        self._last_spectra_qc_result = None
        self._hide_qc_review_panel()

        state = PlateRunState(config)
        payload = state.progress_payload()
        self._start_plate_history_card(payload)
        self._update_plate_progress(payload)
        if self.worker:
            self.worker.set_plate_autosave_config(config)

    def _apply_resumed_plate_state(self, state):
        if not isinstance(state, PlateRunState):
            raise TypeError("Expected a PlateRunState to resume plate autosave.")

        self.plate_autosave_config = state.config
        self._pending_plate_run_state = state
        self._last_spectra_qc_result = None
        self._hide_qc_review_panel()
        self.plate_mode_var.set(True)
        self.configure_plate_btn.config(state="normal")
        self.auto_save_var.set(True)
        self.on_auto_save_toggle()

        payload = state.progress_payload()
        self.plate_history = [dict(payload)]
        self.current_plate_index = 0
        self._update_plate_progress(payload)

        if self.worker:
            self.worker.resume_plate_autosave(state)

        if payload["current_well"] is None:
            self.status_message_var.set(f"Loaded {state.config.plate_name}, which is already complete.")
        else:
            self.status_message_var.set(
                f"Resumed {state.config.plate_name}. Next position: {payload['current_well']}."
            )

    def _discover_resumable_plate_runs(self):
        return discover_resumable_plate_runs(self.save_dir_var.get())

    def _choose_resumable_plate(self, candidates):
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        result = {"candidate": None}
        dlg = create_dialog(self.root, "Resume Plate", modal=True, resizable=True)
        scale = ui_scale_for_widget(dlg)

        outer = ttk.Frame(dlg, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        ttk.Label(
            outer,
            text="Select a plate folder to resume",
            font=scaled_font(11, "bold", scale=scale),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        columns = ("plate", "progress", "source")
        tree = ttk.Treeview(outer, columns=columns, show="headings", height=10)
        tree.heading("plate", text="Plate")
        tree.heading("progress", text="Progress")
        tree.heading("source", text="Source")
        tree.column("plate", width=220, anchor="w")
        tree.column("progress", width=360, anchor="w")
        tree.column("source", width=120, anchor="w")
        tree.grid(row=1, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)

        for index, candidate in enumerate(candidates):
            payload = candidate["payload"]
            next_well = payload["current_well"] or "complete"
            progress = (
                f"{payload['saved_shots']}/{payload['total_shots']} shots, "
                f"{payload['complete_wells']}/{payload['total_wells']} wells, next {next_well}"
            )
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(candidate["plate_name"], progress, candidate["source_label"]),
            )

        tree.selection_set("0")
        tree.focus("0")

        def _confirm(*_):
            selection = tree.selection()
            if not selection:
                return
            result["candidate"] = candidates[int(selection[0])]
            dlg.destroy()

        button_bar = ttk.Frame(outer)
        button_bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(button_bar, text="Cancel", width=12, command=dlg.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_bar, text="Resume Selected", width=18, command=_confirm).pack(side=tk.RIGHT)

        tree.bind("<Double-1>", _confirm)
        apply_window_policy(
            dlg,
            parent=self.root,
            preferred=(760, 360),
            min_size=(620, 300),
            modal=True,
            resizable=True,
        )
        dlg.wait_window()
        return result["candidate"]

    def _prompt_resume_plate_selection(self):
        candidates = self._discover_resumable_plate_runs()
        if not candidates:
            messagebox.showinfo(
                "No Resumable Plates",
                "No incomplete high-throughput plate folders were found in the current save directory.",
                parent=self.root,
            )
            return None

        candidate = self._choose_resumable_plate(candidates)
        if candidate is None:
            return None

        if not candidate.get("needs_confirmation"):
            return candidate["state"]

        suggested_config = candidate["state"].config
        confirmed = self._ask_plate_settings(
            initial_config=suggested_config,
            dialog_title="Resume Plate Settings",
            action_text="Resume Plate",
            allow_resume=False,
            helper_text=(
                "The files were scanned from disk. Review the inferred settings before resuming."
            ),
        )
        if confirmed is None:
            return None

        try:
            return PlateRunState.from_records(confirmed, candidate["records"])
        except ValueError as exc:
            messagebox.showerror("Resume Failed", str(exc), parent=self.root)
            return None

    def _ask_plate_settings(
        self,
        initial_config=None,
        dialog_title="High-Throughput Plate Settings",
        action_text="Use Plate Settings",
        allow_resume=True,
        helper_text="Files are saved into a subfolder named after the plate.",
    ):
        """Show the modal high-throughput plate settings dialog."""
        current = initial_config or self.plate_autosave_config or PlateAutosaveConfig()
        result = {"selection": None}

        dlg = create_dialog(self.root, dialog_title, modal=True, resizable=True)
        scale = ui_scale_for_widget(dlg)

        main = ttk.Frame(dlg, padding=14)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=0)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        ttk.Label(
            main,
            text=dialog_title,
            font=scaled_font(13, "bold", scale=scale),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        form = ttk.LabelFrame(main, text="Run Settings", padding=10)
        form.grid(row=1, column=0, sticky="nsw", padx=(0, 12))

        plate_type_var = tk.StringVar(value=str(current.plate_type))
        plate_name_var = tk.StringVar(value=current.plate_name)
        shots_var = tk.StringVar(value=str(current.shots_per_well))
        order_var = tk.StringVar(value=current.order_mode)
        laser_wavelength_var = tk.StringVar(
            value="" if current.laser_wavelength_nm is None else str(current.laser_wavelength_nm)
        )
        laser_energy_default = current.laser_energy
        if not laser_energy_default and current.laser_energy_mj is not None:
            laser_energy_default = str(current.laser_energy_mj)
        laser_energy_var = tk.StringVar(value=laser_energy_default)
        laser_hz_var = tk.StringVar(value=current.laser_hz)
        delay_enabled_var = tk.BooleanVar(value=current.delay_enabled)
        delay_ms_var = tk.StringVar(value=current.delay_ms)

        ttk.Label(form, text="Plate type:").grid(row=0, column=0, sticky="w", pady=4)
        plate_combo = ttk.Combobox(
            form,
            textvariable=plate_type_var,
            values=[str(value) for value in PLATE_FORMATS],
            width=12,
            state="readonly",
        )
        plate_combo.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="Plate name:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=plate_name_var, width=20).grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="Shots per well:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(form, from_=1, to=99, textvariable=shots_var, width=8).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(form, text="Order:").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Radiobutton(form, text=ORDER_LABELS[ORDER_ROW], value=ORDER_ROW, variable=order_var).grid(
            row=3, column=1, sticky="w", pady=(4, 1)
        )
        ttk.Radiobutton(form, text=ORDER_LABELS[ORDER_COLUMN], value=ORDER_COLUMN, variable=order_var).grid(
            row=4, column=1, sticky="w", pady=(1, 4)
        )

        ttk.Label(
            form,
            text=helper_text,
            style="Status.TLabel",
            wraplength=scaled_wrap(210, scale),
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))

        metadata_frame = ttk.LabelFrame(form, text="Laser Metadata", padding=8)
        metadata_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        metadata_frame.columnconfigure(1, weight=1)

        ttk.Label(metadata_frame, text="Laser wavelength (nm):", style="Status.TLabel").grid(
            row=0, column=0, sticky="w", pady=3
        )
        ttk.Entry(metadata_frame, textvariable=laser_wavelength_var, width=16).grid(
            row=0, column=1, sticky="ew", pady=3
        )

        ttk.Label(metadata_frame, text="Laser energy:", style="Status.TLabel").grid(
            row=1, column=0, sticky="w", pady=3
        )
        ttk.Entry(metadata_frame, textvariable=laser_energy_var, width=16).grid(
            row=1, column=1, sticky="ew", pady=3
        )

        ttk.Label(metadata_frame, text="Laser frequency (Hz):", style="Status.TLabel").grid(
            row=2, column=0, sticky="w", pady=3
        )
        ttk.Entry(metadata_frame, textvariable=laser_hz_var, width=16).grid(
            row=2, column=1, sticky="ew", pady=3
        )

        ttk.Checkbutton(
            metadata_frame,
            text="Delay enabled",
            variable=delay_enabled_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 3))

        ttk.Label(metadata_frame, text="Delay (ms):", style="Status.TLabel").grid(
            row=4, column=0, sticky="w", pady=3
        )
        delay_ms_entry = ttk.Entry(metadata_frame, textvariable=delay_ms_var, width=16)
        delay_ms_entry.grid(row=4, column=1, sticky="ew", pady=3)

        runtime_settings_var = tk.StringVar()
        runtime_frame = ttk.LabelFrame(form, text="Spectrometer Settings Saved Automatically", padding=8)
        runtime_frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Label(
            runtime_frame,
            textvariable=runtime_settings_var,
            style="Status.TLabel",
            wraplength=scaled_wrap(210, scale),
            justify=tk.LEFT,
        ).grid(row=0, column=0, sticky="w")

        preview_frame = ttk.LabelFrame(main, text="Plate Preview", padding=10)
        preview_frame.grid(row=1, column=1, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        preview_canvas = tk.Canvas(
            preview_frame,
            width=420,
            height=320,
            bg=get_palette()["canvas_bg"],
            highlightthickness=1,
            highlightbackground=get_palette()["canvas_border"],
        )
        preview_canvas.grid(row=0, column=0, sticky="nsew")

        button_bar = ttk.Frame(main)
        button_bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        def _current_dialog_config():
            runtime = self._current_plate_runtime_settings()
            return PlateAutosaveConfig.from_mapping({
                "plate_type": plate_type_var.get(),
                "plate_name": plate_name_var.get(),
                "shots_per_well": shots_var.get(),
                "order_mode": order_var.get(),
                "laser_wavelength_nm": laser_wavelength_var.get(),
                "laser_energy_mj": laser_energy_var.get(),
                "laser_energy": laser_energy_var.get(),
                "laser_hz": laser_hz_var.get(),
                "delay_enabled": delay_enabled_var.get(),
                "delay_ms": delay_ms_var.get(),
                **runtime,
            })

        def _redraw_preview(*_):
            try:
                preview_state = PlateRunState(_current_dialog_config())
                self._draw_plate_payload(preview_canvas, preview_state.progress_payload(), preview=True)
            except Exception:
                preview_canvas.delete("all")
                preview_canvas.create_text(
                    210,
                    160,
                    text="Enter a valid shot count.",
                    fill=get_palette()["error"],
                    font=scaled_font(10, "bold", scale=scale),
                )

            runtime = self._current_plate_runtime_settings()
            runtime_settings_var.set(
                "\n".join([
                    f"Sample name: {runtime['sample_name']}",
                    f"Integration time: {runtime['integration_time_ms'] or 'Unknown'} ms",
                    f"Averages: {runtime['averages']}",
                    (
                        "Dark correction: "
                        f"requested {'On' if runtime['correct_dark_counts'] else 'Off'}, "
                        f"effective {'On' if runtime['effective_correct_dark_counts'] else 'Off'}"
                    ),
                    (
                        "Nonlinearity correction: "
                        f"requested {'On' if runtime['correct_nonlinearity'] else 'Off'}, "
                        f"effective {'On' if runtime['effective_correct_nonlinearity'] else 'Off'}"
                    ),
                ])
            )

            delay_ms_entry.config(state="normal" if delay_enabled_var.get() else "disabled")

        def _apply():
            try:
                config = _current_dialog_config()
            except Exception:
                messagebox.showwarning("Invalid Plate Settings", "Please enter a valid shot count.", parent=dlg)
                return
            result["selection"] = config
            dlg.destroy()

        def _resume():
            selection = self._prompt_resume_plate_selection()
            if selection is None:
                return
            result["selection"] = selection
            dlg.destroy()

        for var in (
            plate_type_var,
            plate_name_var,
            shots_var,
            order_var,
            laser_wavelength_var,
            laser_energy_var,
            laser_hz_var,
            delay_ms_var,
        ):
            var.trace_add("write", _redraw_preview)
        delay_enabled_var.trace_add("write", _redraw_preview)

        ttk.Button(button_bar, text="Cancel", width=12, command=dlg.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_bar, text=action_text, width=18, command=_apply).pack(side=tk.RIGHT)
        if allow_resume:
            ttk.Button(button_bar, text="Resume Existing Plate...", width=22, command=_resume).pack(
                side=tk.LEFT
            )

        dlg.bind("<Return>", lambda _event: _apply())
        _redraw_preview()
        apply_window_policy(
            dlg,
            parent=self.root,
            preferred=(760, 640),
            min_size=(660, 540),
            modal=True,
            resizable=True,
        )
        dlg.wait_window()
        return result["selection"]

    def _update_plate_progress(self, payload):
        self.plate_progress = payload
        if not payload:
            return

        self._update_plate_history_card(payload)
        if payload.get("repair_active"):
            queue_text = ", ".join(payload.get("repair_queue", [])[:3])
            if len(payload.get("repair_queue", [])) > 3:
                queue_text += f", +{len(payload['repair_queue']) - 3} more"
            resume_well = payload.get("repair_resume_well") or "plate completion"
            next_text = f"repairing {queue_text}, then resume {resume_well}"
        elif payload["current_well"] is None:
            next_text = "finished early" if payload.get("closed_early") else "complete"
        else:
            next_text = f"next {payload['current_well']}"

        self.plate_progress_var.set(
            f"{payload['plate_name']} ({payload['plate_type']}-well): "
            f"{payload['complete_wells']}/{payload['total_wells']} wells, {next_text}"
        )
        self._show_plate_overview()
        self._redraw_plate_history()
        finished = self._plate_payload_finished(payload)
        self.discard_plate_shot_btn.config(
            state="normal" if payload.get("can_discard") and not finished else "disabled"
        )
        self.repair_plate_wells_btn.config(
            state="normal"
            if payload.get("saved_shots", 0) and not finished and not payload.get("repair_active")
            else "disabled"
        )
        self.finish_plate_btn.config(
            state="normal"
            if payload.get("saved_shots", 0) and not finished and not payload.get("repair_active")
            else "disabled"
        )

    def _start_plate_history_card(self, payload):
        if self.current_plate_index is None:
            self.plate_history = [dict(payload)]
            self.current_plate_index = 0
            return

        current = self.plate_history[self.current_plate_index]
        if self._plate_payload_finished(current):
            if self.current_plate_index < len(self.plate_history) - 1:
                self.current_plate_index += 1
                self.plate_history[self.current_plate_index] = dict(payload)
            else:
                self.plate_history.append(dict(payload))
                self.current_plate_index = len(self.plate_history) - 1
        else:
            self.plate_history[self.current_plate_index] = dict(payload)

    def _update_plate_history_card(self, payload):
        if self.current_plate_index is None:
            self._start_plate_history_card(payload)
            return
        self.plate_history[self.current_plate_index] = dict(payload)

    def _prompt_for_next_plate(self):
        """Offer to configure the next plate after the current one completes."""
        if not self.plate_autosave_config:
            return
        if self.worker:
            self.worker.disable_plate_autosave()

        if not messagebox.askyesno(
            "Plate Complete",
            "The current plate is complete.\n\nConfigure the next plate now?",
            parent=self.root,
        ):
            self.status_message_var.set("Plate complete. Click Configure Plate to start the next plate.")
            return

        selection = self._ask_plate_settings()
        if selection is None:
            self.status_message_var.set("Plate complete. Click Configure Plate to start the next plate.")
            return

        if isinstance(selection, PlateRunState):
            self._apply_resumed_plate_state(selection)
        else:
            self._apply_plate_config(selection)

    def _show_plate_overview(self):
        if not self.plate_overview_frame.winfo_ismapped():
            self.plate_overview_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
            self.root.update_idletasks()
            self.canvas.draw_idle()

    def _hide_plate_overview(self):
        if hasattr(self, "plate_overview_frame"):
            self.plate_overview_frame.grid_forget()

    def _redraw_plate_history(self, focus_index=None):
        for child in self.plate_history_inner.winfo_children():
            child.destroy()

        for index, payload in enumerate(self.plate_history):
            title = payload.get("plate_label") or f"{payload['plate_name']} - Plate {index + 1}"
            if index == self.current_plate_index and not self._plate_payload_finished(payload):
                title += " (current)"
            elif payload.get("closed_early"):
                title += " (finished early)"
            elif payload.get("complete"):
                title += " (done)"
            elif self.current_plate_index is not None and index > self.current_plate_index:
                title += " (next)"

            card = ttk.LabelFrame(self.plate_history_inner, text=title, padding=(6, 4))
            card.grid(row=0, column=index, sticky="n", padx=(0, 10), pady=2)
            plate_canvas = tk.Canvas(
                card,
                width=520,
                height=360,
                bg=get_palette()["canvas_bg"],
                highlightthickness=1,
                highlightbackground=get_palette()["canvas_border"],
            )
            plate_canvas.pack(fill=tk.BOTH, expand=True)
            self._draw_plate_payload(plate_canvas, payload, preview=False)

        self.plate_history_inner.update_idletasks()
        self.plate_history_canvas.configure(scrollregion=self.plate_history_canvas.bbox("all"))

        if focus_index is None:
            focus_index = self.current_plate_index
        if focus_index is None or not self.plate_history_inner.winfo_children():
            return

        focus_index = max(0, min(focus_index, len(self.plate_history_inner.winfo_children()) - 1))
        target = self.plate_history_inner.winfo_children()[focus_index]
        scroll_region = self.plate_history_canvas.bbox("all")
        if not scroll_region:
            return

        content_width = scroll_region[2] - scroll_region[0]
        visible_width = self.plate_history_canvas.winfo_width()
        if content_width <= visible_width:
            self.plate_history_canvas.xview_moveto(0)
            return
        max_scroll = max(1, content_width - visible_width)
        self.plate_history_canvas.xview_moveto(max(0, min(target.winfo_x() / max_scroll, 1)))

    def _plate_layout_metrics(self, payload, width, height, preview=False, compact=False):
        rows = payload["rows"]
        columns = payload["columns"]
        if compact:
            edge_pad = 8 if min(width, height) < 140 else 10
            label_pad = 18 if min(width, height) < 140 else 24
            left_bound = label_pad
            top_bound = label_pad
            right_bound = width - edge_pad
            bottom_bound = height - edge_pad
        else:
            left_bound = 62 if preview else 58
            top_bound = 58 if preview else 56
            right_bound = width - 20
            bottom_bound = height - 20
        available_w = max(10, right_bound - left_bound)
        available_h = max(10, bottom_bound - top_bound)
        plate_ratio = columns / rows
        if available_w / available_h > plate_ratio:
            grid_h = available_h
            grid_w = grid_h * plate_ratio
        else:
            grid_w = available_w
            grid_h = grid_w / plate_ratio
        left = left_bound + (available_w - grid_w) / 2
        top = top_bound + (available_h - grid_h) / 2
        right = left + grid_w
        bottom = top + grid_h
        cell_w = (right - left) / columns
        cell_h = (bottom - top) / rows
        return {
            "rows": rows,
            "columns": columns,
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "cell_w": cell_w,
            "cell_h": cell_h,
            "min_cell": min(cell_w, cell_h),
        }

    def _well_from_canvas_point(self, payload, x, y, preview=False, canvas_width=None, canvas_height=None, compact=False):
        width = max(1, int(canvas_width or 0))
        height = max(1, int(canvas_height or 0))
        if width <= 1 or height <= 1:
            width = 700 if not preview else 420
            height = 430 if not preview else 320
        metrics = self._plate_layout_metrics(payload, width, height, preview=preview, compact=compact)
        if not (metrics["left"] <= x <= metrics["right"] and metrics["top"] <= y <= metrics["bottom"]):
            return None
        column = int((x - metrics["left"]) / metrics["cell_w"]) + 1
        row = int((y - metrics["top"]) / metrics["cell_h"])
        if row < 0 or row >= metrics["rows"] or column < 1 or column > metrics["columns"]:
            return None
        return f"{chr(65 + row)}{column}"

    def _render_plate_payload_image(
        self,
        payload,
        width: int,
        height: int,
        *,
        preview=False,
        selected_wells=None,
        disabled_wells=None,
        compact=False,
        show_title=True,
        qc_fill_by_well=None,
        qc_outline_by_well=None,
        qc_width_by_well=None,
        show_counts_override=None,
    ):
        palette = get_palette()
        width = max(1, int(width))
        height = max(1, int(height))
        scale = 3
        text_scale = ui_scale_for_widget(self.root)
        rows = payload["rows"]
        columns = payload["columns"]
        shots_per_well = payload["shots_per_well"]
        shots_by_well = payload["shots_by_well"]
        current_well = payload["current_well"]
        dry_run_visited_wells = set(payload.get("dry_run_visited_wells", []) or [])
        dry_run_display = bool(payload.get("dry_run") and dry_run_visited_wells)
        selected_wells = set(selected_wells or [])
        disabled_wells = set(disabled_wells or [])
        repair_queue = list(payload.get("repair_queue", []))
        repaired_wells = set(payload.get("repaired_wells", []))
        qc_by_well = payload.get("qc_by_well", {}) or {}
        qc_fill_by_well = qc_fill_by_well or {}
        qc_outline_by_well = qc_outline_by_well or {}
        qc_width_by_well = qc_width_by_well or {}

        image = Image.new("RGB", (width * scale, height * scale), palette["plate_bg"])
        draw = ImageDraw.Draw(image)

        def px(value):
            return int(round(value * scale))

        def xy(x, y):
            return (px(x), px(y))

        def font(size, bold=False):
            font_name = "segoeuib.ttf" if bold else "segoeui.ttf"
            try:
                return ImageFont.truetype(font_name, max(8, px(size * text_scale)))
            except OSError:
                return ImageFont.load_default()

        def draw_arrow_line(start, end, color):
            draw.line((xy(*start), xy(*end)), fill=color, width=max(1, px(2)))
            sx, sy = start
            ex, ey = end
            if abs(ex - sx) >= abs(ey - sy):
                direction = 1 if ex >= sx else -1
                arrow = [
                    xy(ex, ey),
                    xy(ex - direction * 7, ey - 4),
                    xy(ex - direction * 7, ey + 4),
                ]
            else:
                direction = 1 if ey >= sy else -1
                arrow = [
                    xy(ex, ey),
                    xy(ex - 4, ey - direction * 7),
                    xy(ex + 4, ey - direction * 7),
                ]
            draw.polygon(arrow, fill=color)

        if show_title:
            title = f"{payload['plate_type']}-well - {payload['order_label']}"
            draw.text(xy(10, 12), title, anchor="lt", fill=palette["plate_title"], font=font(11, bold=True))

        metrics = self._plate_layout_metrics(payload, width, height, preview=preview, compact=compact)
        left = metrics["left"]
        top = metrics["top"]
        right = metrics["right"]
        bottom = metrics["bottom"]
        cell_w = metrics["cell_w"]
        cell_h = metrics["cell_h"]
        min_cell = metrics["min_cell"]
        radius = max(2, min_cell * (0.40 if compact else 0.36))
        count_font_size = 10 if compact else (11 if min_cell >= 18 else 9)
        show_counts = min_cell >= (8 if compact else 10) and not dry_run_display
        if show_counts_override is not None:
            show_counts = bool(show_counts_override)
        show_edge_labels = min_cell >= (7 if compact else 8)
        edge_font = font(12 if min_cell >= 20 else 10, bold=True)
        count_font = font(count_font_size, bold=True)

        if not compact:
            if payload["order_mode"] == ORDER_ROW:
                draw_arrow_line((left + cell_w * 0.2, top - 30), (right - cell_w * 0.2, top - 30), palette["plate_arrow"])
            else:
                draw_arrow_line((left - 32, top + cell_h * 0.2), (left - 32, bottom - cell_h * 0.2), palette["plate_arrow"])

        if show_edge_labels:
            for column in range(1, columns + 1):
                cx = left + (column - 0.5) * cell_w
                draw.text(xy(cx, top - 17), str(column), anchor="mm", fill=palette["plate_label"], font=edge_font)
            for row in range(rows):
                cy = top + (row + 0.5) * cell_h
                draw.text(xy(left - 17, cy), chr(65 + row), anchor="mm", fill=palette["plate_label"], font=edge_font)

        for row in range(rows):
            for column in range(1, columns + 1):
                well = f"{chr(65 + row)}{column}"
                count = shots_by_well.get(well, 0)
                cx = left + (column - 0.5) * cell_w
                cy = top + (row + 0.5) * cell_h
                fill = palette["well_empty"]
                outline = palette["well_empty_outline"]
                width_px = 1
                text_fill = palette["well_text"]
                if dry_run_display and well in dry_run_visited_wells:
                    fill = palette["well_done"]
                    outline = palette["well_done_outline"]
                elif count >= shots_per_well:
                    fill = palette["well_done"]
                    outline = palette["well_done_outline"]
                elif count > 0:
                    fill = palette["well_partial"]
                    outline = palette["well_partial_outline"]
                if count >= shots_per_well:
                    qc_state = qc_by_well.get(well)
                    if isinstance(qc_state, dict):
                        status = str(qc_state.get("status", "")).lower()
                        if status == "pass":
                            fill = palette["qc_pass"]
                            outline = palette["qc_pass_outline"]
                        elif status == "warn":
                            fill = palette["qc_warn"]
                            outline = palette["qc_warn_outline"]
                            width_px = 2
                        elif status == "fail":
                            fill = palette["qc_fail"]
                            outline = palette["qc_fail_outline"]
                            width_px = 2
                if well in qc_fill_by_well:
                    fill = qc_fill_by_well[well]
                    outline = qc_outline_by_well.get(well, palette["well_done_outline"])
                    width_px = int(qc_width_by_well.get(well, 1) or 1)
                if well in repair_queue:
                    fill = palette["well_partial"]
                    outline = palette["well_partial_outline"]
                    width_px = 2
                if well in repaired_wells and count >= shots_per_well:
                    fill = palette["well_done"]
                    outline = palette["well_repaired_outline"]
                    width_px = 2
                if well in selected_wells:
                    fill = palette["well_selected"]
                    outline = palette["well_selected_outline"]
                    width_px = 3
                if well in disabled_wells:
                    fill = palette["well_disabled"]
                    outline = palette["well_disabled_outline"]
                    text_fill = palette["well_disabled_text"]
                if well == current_well and well not in repair_queue:
                    outline = palette["current_well"]
                    width_px = 2

                draw.ellipse(
                    (
                        px(cx - radius),
                        px(cy - radius),
                        px(cx + radius),
                        px(cy + radius),
                    ),
                    fill=fill,
                    outline=outline,
                    width=max(1, px(width_px)),
                )
                if show_counts and count > 0:
                    draw.text(xy(cx, cy), str(count), anchor="mm", fill=text_fill, font=count_font)

        resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
        return image.resize((width, height), resample)

    def _draw_plate_payload(self, canvas, payload, preview=False, selected_wells=None, disabled_wells=None):
        canvas.delete("all")
        palette = get_palette()
        apply_canvas_theme(canvas, palette, border=True)

        width = max(canvas.winfo_width(), int(canvas.cget("width") or 1))
        height = max(canvas.winfo_height(), int(canvas.cget("height") or 1))
        image = self._render_plate_payload_image(
            payload,
            width,
            height,
            preview=preview,
            selected_wells=selected_wells,
            disabled_wells=disabled_wells,
        )
        photo = ImageTk.PhotoImage(image)
        canvas.create_image(0, 0, image=photo, anchor=tk.NW)
        canvas._plate_image = photo

    def _poll_queue(self, reschedule: bool = True):
        return self.controller.poll_queue(reschedule=reschedule)

    def _on_worker_error_message(self, data):
        """Hook for subclasses reacting to worker ERROR messages (e.g. laser loss)."""

    def _on_worker_stopped_message(self):
        """Hook for subclasses reacting to worker STOPPED messages (e.g. laser loss)."""

    def _poll_queue_impl(self, reschedule: bool = True):
        """Check the worker's message queue and process all pending messages."""
        if self.worker is None:
            return

        from prolibspector.acquisition.worker import AcquisitionMessage
        from prolibspector.acquisition.graph import highlight_captured_spectrum
        processed_messages = 0
        queue_yielded = False
        message_limit = self._queue_poll_message_limit()
        if message_limit is not None:
            try:
                message_limit = max(1, int(message_limit))
            except (TypeError, ValueError):
                message_limit = None

        try:
            while True:
                if message_limit is not None and processed_messages >= message_limit:
                    queue_yielded = not self.worker.message_queue.empty()
                    break
                msg_type, data = self.worker.message_queue.get_nowait()
                processed_messages += 1

                if msg_type == AcquisitionMessage.SPECTRUM:
                    wavelengths, intensities = data
                    self.current_wavelengths = wavelengths
                    self.current_intensities = intensities
                    self._pending_spectrum = (wavelengths, intensities)
                    self._schedule_spectrum_draw()
                    # Enable save/send buttons now that we have data
                    self.save_spectrum_btn.config(state="normal")
                    self.send_to_analysis_btn.config(state="normal")

                elif msg_type == AcquisitionMessage.STATUS:
                    self.status_message_var.set(str(data))

                elif msg_type == AcquisitionMessage.ERROR:
                    self.status_message_var.set(f"Error: {data}")
                    if "Reconnect spectrometer" in str(data):
                        self.connection_status_var.set("Reconnect spectrometer")
                    self._refresh_acquisition_controls()
                    logger.error(data)
                    self._on_worker_error_message(data)

                elif msg_type == AcquisitionMessage.ARMED:
                    self._set_arm_button_visual("armed")
                    self._set_worker_state("State: ARMED")

                elif msg_type == AcquisitionMessage.CAPTURED:
                    self._cancel_pending_spectrum_draw(drop_pending=True)
                    shot_idx = data["shot_index"]
                    self.current_wavelengths = data["wavelengths"]
                    self.current_intensities = data["intensities"]
                    self.shot_count_var.set(f"Shots: {shot_idx}")
                    # Visual feedback
                    self._cancel_highlight_timer()
                    highlight_captured_spectrum(
                        self.ax, self.canvas, self.live_line, data["wavelengths"],
                        data["intensities"], shot_idx
                    )
                    self._last_plot_draw_at = time.perf_counter()
                    # Remove highlight after 2 seconds
                    self._highlight_after_id = self.root.after(2000, self._remove_highlight)

                    # The worker automatically re-arms after each capture.
                    # While it stays in ARMED state, keep Stop enabled.
                    current_state = self.worker.state if self.worker else "IDLE"
                    if current_state == "ARMED":
                        self._set_arm_button_visual("armed")
                        self._set_worker_state(f"State: ARMED (shot {shot_idx})")
                    elif current_state != "IDLE":
                        self._update_arm_btn_state()
                        self.live_btn.config(state="disabled")
                        self.test_trigger_btn.config(state="disabled")
                        self.stop_btn.config(state="normal")
                        self._set_worker_state(f"State: {current_state} (shot {shot_idx})")
                    else:
                        # Worker returned to idle (error or single-shot test)
                        self.live_btn.config(state="normal")
                        self._update_arm_btn_state()
                        self.test_trigger_btn.config(state="normal")
                        self.stop_btn.config(state="disabled")
                        self._set_worker_state("State: IDLE")

                elif msg_type == AcquisitionMessage.IDLE:
                    self._remove_highlight()
                    # Worker returned to idle — restore button state
                    self.live_btn.config(state="normal")
                    self._update_arm_btn_state()
                    self.test_trigger_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self._set_worker_state("State: IDLE")
                    self._refresh_acquisition_controls()
                    if self._plate_completion_prompt_pending:
                        self._plate_completion_prompt_pending = False
                        if self._should_defer_plate_completion_prompt_for_qc():
                            self.status_message_var.set(
                                "Plate complete. Review failed QC wells before configuring the next plate."
                            )
                        else:
                            self.root.after(0, self._prompt_for_next_plate)

                elif msg_type == AcquisitionMessage.SAVE_COMPLETE:
                    self.status_message_var.set(f"Saved: {os.path.basename(data)}")

                elif msg_type == AcquisitionMessage.TIMING:
                    sample = dict(data)
                    sample["gui_received_at"] = time.perf_counter()
                    if "worker_enqueued_at" in sample:
                        latency_s = sample["gui_received_at"] - sample["worker_enqueued_at"]
                        sample["gui_queue_latency"] = latency_s
                        sample["gui_queue_latency_ms"] = latency_s * 1000.0
                    self.latest_timing_sample = sample
                    self.timing_samples.append(sample)

                elif msg_type == AcquisitionMessage.PLATE_PROGRESS:
                    self._update_plate_progress(data)

                elif msg_type == AcquisitionMessage.PLATE_DISCARDED:
                    self._update_plate_progress(data)
                    discarded = data.get("discarded")
                    if discarded:
                        self.status_message_var.set(f"Discarded: {os.path.basename(discarded)}")

                elif msg_type == AcquisitionMessage.PLATE_REPAIR_STARTED:
                    self._update_plate_progress(data)
                    queued = data.get("repair_queue", [])
                    if queued:
                        self.status_message_var.set(f"Repairing wells: {self._format_well_list(queued)}")

                elif msg_type == AcquisitionMessage.PLATE_REPAIR_COMPLETE:
                    self._update_plate_progress(data)
                    next_well = data.get("current_well") or "plate completion"
                    self.status_message_var.set(f"Repair pass complete. Resuming {next_well}.")

                elif msg_type == AcquisitionMessage.PLATE_COMPLETE:
                    self._update_plate_progress(data)
                    self.status_message_var.set("Plate complete.")
                    self._plate_completion_prompt_pending = True

                elif msg_type == AcquisitionMessage.QC_COMPLETE:
                    if hasattr(self, "_handle_qc_complete"):
                        self._handle_qc_complete(data)
                    else:
                        self.status_message_var.set("Spectra QC complete.")

                elif msg_type == AcquisitionMessage.QC_ERROR:
                    if hasattr(self, "_handle_qc_error"):
                        self._handle_qc_error(data)
                    else:
                        self.status_message_var.set(f"Spectra QC unavailable: {data}")

                elif msg_type == AcquisitionMessage.STOPPED:
                    self._remove_highlight()
                    self._set_arm_button_visual("disabled")
                    self._set_worker_state("State: STOPPED")
                    self._refresh_acquisition_controls()
                    self._on_worker_stopped_message()

        except queue.Empty:
            pass

        self._after_queue_poll_batch(
            yielded=queue_yielded,
            processed_messages=processed_messages,
        )

        # Schedule next poll with a faster cadence while acquisition is active.
        if reschedule and self.worker:
            delay_ms = self._queue_poll_delay_ms(processed_messages)
            if queue_yielded:
                delay_ms = 1
            self._queue_poll_after_id = self.root.after(
                delay_ms,
                self._poll_queue,
            )

    def _cancel_queue_poll(self):
        return self.controller.cancel_queue_poll()

    def _cancel_queue_poll_impl(self):
        """Cancel any pending queue polling callback."""
        if self._queue_poll_after_id is None:
            self._cancel_pending_spectrum_draw(drop_pending=True)
            return
        try:
            self.root.after_cancel(self._queue_poll_after_id)
        except tk.TclError:
            pass
        self._queue_poll_after_id = None
        self._cancel_pending_spectrum_draw(drop_pending=True)

    def _cancel_highlight_timer(self):
        """Cancel any pending highlight clear callback."""
        if self._highlight_after_id is not None:
            try:
                self.root.after_cancel(self._highlight_after_id)
            except tk.TclError:
                pass
            self._highlight_after_id = None

    def _post_to_ui(self, callback):
        """Schedule *callback* on the Tk thread from any thread.

        Safe against teardown: no-op once the UI is closing, and the
        scheduled wrapper re-checks before invoking so a completion that
        raced past the first check is still dropped."""
        if self._ui_closing:
            return
        handle = {"id": None, "fired": False}

        def _invoke():
            handle["fired"] = True
            self._pending_after_ids.discard(handle["id"])
            if self._ui_closing:
                return
            callback()

        try:
            after_id = self.root.after(0, _invoke)
        except (tk.TclError, RuntimeError):
            return
        handle["id"] = after_id
        # When posted from a worker thread while Tk is pumping, _invoke can
        # fire before the id was recorded; don't track an already-fired id.
        if not handle["fired"]:
            self._pending_after_ids.add(after_id)

    def _cancel_pending_after_ids(self):
        """Cancel every callback scheduled through _post_to_ui."""
        pending, self._pending_after_ids = self._pending_after_ids, set()
        for after_id in pending:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass

    def _remove_highlight(self):
        """Restore the live line after a capture highlight."""
        self._cancel_highlight_timer()
        if self._highlight_line:
            from prolibspector.acquisition.graph import clear_highlight
            clear_highlight(self.ax, self.canvas, self._highlight_line)

    # ═══════════════════════════════════════════════════════════════════
    #  Brand selection dialog
    # ═══════════════════════════════════════════════════════════════════

    def _ask_brand(self) -> str | None:
        """
        Show a small dialog asking the user to pick a spectrometer brand.
        
        Returns ``"ocean_optics"``, ``"thorlabs"``, ``"yixist"``, ``"simulation"``,
        or ``None`` if cancelled.
        """
        from prolibspector.hardware.yixist_spectrometer import yixist_supported

        result = {"value": None}

        dlg = create_dialog(self.root, "Select Spectrometer Connection", modal=True, resizable=True)
        scale = ui_scale_for_widget(dlg)

        ttk.Label(dlg, text="Choose a spectrometer connection",
                  font=scaled_font(11, "bold", scale=scale)).pack(pady=(scaled_int(18, scale), scaled_int(10, scale)))

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(pady=4)

        def _pick(brand):
            result["value"] = brand
            dlg.destroy()

        ttk.Button(btn_frame, text="Ocean Optics", width=16,
                   command=lambda: _pick("ocean_optics")).pack(side=tk.LEFT, padx=8)
        thorlabs_text = "Thorlabs CCS" if supports_thorlabs_ccs() else "Thorlabs CCS (Windows only)"
        thorlabs_state = "normal" if supports_thorlabs_ccs() else "disabled"
        ttk.Button(
            btn_frame,
            text=thorlabs_text,
            width=24,
            state=thorlabs_state,
            command=lambda: _pick("thorlabs"),
        ).pack(side=tk.LEFT, padx=8)
        yixist_text = "YiXist / YXSP" if yixist_supported() else "YiXist / YXSP (Windows x64 only)"
        yixist_state = "normal" if yixist_supported() else "disabled"
        ttk.Button(
            btn_frame,
            text=yixist_text,
            width=28,
            state=yixist_state,
            command=lambda: _pick("yixist"),
        ).pack(side=tk.LEFT, padx=8)
        ttk.Button(btn_frame, text="Simulation Mode", width=18,
                   command=lambda: _pick("simulation")).pack(side=tk.LEFT, padx=8)

        ttk.Button(dlg, text="Cancel", width=10,
                   command=dlg.destroy).pack(pady=(16, 0))

        apply_window_policy(
            dlg,
            parent=self.root,
            preferred=SPECTROMETER_CONNECTION_DIALOG_PREFERRED,
            min_size=SPECTROMETER_CONNECTION_DIALOG_MIN_SIZE,
            modal=True,
            resizable=True,
        )
        dlg.wait_window()
        return result["value"]

    # ═══════════════════════════════════════════════════════════════════
    #  Lifecycle
    # ═══════════════════════════════════════════════════════════════════

    def run(self):
        """Start the Tkinter main loop."""
        self.shell.reveal_for_run()
        self.root.mainloop()

    def get_handoff_data(self):
        """Return any spectrum data that should be passed to Analysis mode."""
        return self._handoff_data

    def get_requested_mode(self):
        return self._requested_mode

    def apply_ui_theme(self):
        """Apply the saved UI theme to Acquisition plots and plate canvases."""
        if hasattr(self, "shell"):
            self.shell.apply_theme()
        if hasattr(self, "sidebar_canvas"):
            # The scroll canvas captured the root bg at build time; re-read it
            # so a live theme switch doesn't leave a wrong-colored sidebar.
            try:
                self.sidebar_canvas.configure(bg=self.root.cget("bg"))
            except tk.TclError:
                pass
        try:
            ttk.Style(self.root).configure("Muted.TLabel", foreground=get_palette()["muted_text"])
        except tk.TclError:
            pass
        # Re-derive the palette-based status colors for the new theme.
        if hasattr(self, "arm_status_chip"):
            self._set_arm_button_visual(getattr(self, "_arm_button_visual_state", "disabled"))
        if hasattr(self, "worker_state_var"):
            self._set_worker_state(self.worker_state_var.get())
        if getattr(self, "plate_history", None):
            self._redraw_plate_history()

    def _spectrometer_status_summary(self):
        if not (self.spectrometer and self.spectrometer.is_connected):
            return "No spectrometer connected."

        caps = self.spectrometer.capabilities

        def number(value, default=0.0):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        current_ms = number(getattr(self.spectrometer, "integration_time_us", 0)) / 1000.0
        min_ms = number(getattr(caps, "integration_time_min_us", 0)) / 1000.0
        max_ms = number(getattr(caps, "integration_time_max_us", 0)) / 1000.0
        wl_min = number(getattr(caps, "wavelength_min", 0))
        wl_max = number(getattr(caps, "wavelength_max", 0))
        triggers = []
        if getattr(caps, "normal_trigger_mode", None) is not None:
            triggers.append("normal")
        if getattr(caps, "has_external_trigger", False):
            triggers.append("external")
        trigger_text = ", ".join(triggers) if triggers else "unknown"

        return "\n".join([
            f"{getattr(caps, 'brand', 'unknown')} - {getattr(caps, 'model', 'Spectrometer')}",
            f"S/N: {getattr(caps, 'serial_number', 'N/A')}",
            f"Range: {wl_min:.1f}-{wl_max:.1f} nm",
            f"Pixels: {getattr(caps, 'pixel_count', 0)}",
            f"Integration: {current_ms:.2f} ms ({min_ms:.2f}-{max_ms:.0f} ms)",
            f"Triggers: {trigger_text}",
            f"Dark correction: {'yes' if getattr(caps, 'supports_dark_correction', False) else 'no'}",
            f"Nonlinearity: {'yes' if getattr(caps, 'supports_nonlinearity_correction', False) else 'no'}",
        ])

    def get_status_snapshot(self):
        connection = self.connection_status_var.get() if hasattr(self, "connection_status_var") else "Disconnected"
        worker_state = self.worker_state_var.get() if hasattr(self, "worker_state_var") else "State: IDLE"
        shot_count = self.shot_count_var.get() if hasattr(self, "shot_count_var") else "Shots: 0"
        lines = [connection, worker_state, shot_count]
        if hasattr(self, "plate_progress_var") and self.plate_progress_var.get():
            lines.append(self.plate_progress_var.get())
        return {
            "mode": self.mode_name,
            "summary": "\n".join(lines),
            "spectrometer_summary": self._spectrometer_status_summary(),
        }

    def request_mode_switch(self, target_mode):
        from prolibspector.app.launcher import MODE_CHOOSER

        if target_mode not in ("Analysis", MODE_CHOOSER):
            return
        if self._shutdown_in_progress:
            return
        if self.worker and self.worker.state != "IDLE":
            destination = "Analysis" if target_mode == "Analysis" else "the mode menu"
            result = messagebox.askyesno(
                "Switch Mode",
                f"Acquisition is running. Stop acquisition and open {destination} without sending data?",
                parent=self.root,
            )
            if not result:
                return

        self._handoff_data = None
        self._requested_mode = target_mode
        if not self._cleanup_and_quit():
            self._requested_mode = None

    def _cleanup_and_quit(self):
        """Begin the asynchronous shutdown that exits the mainloop via quit().
        Uses quit() so the shared root stays alive for Analysis mode handoff.
        Returns True when shutdown was initiated (completion is async)."""
        return self._begin_shutdown("quit")

    def _cleanup_and_close(self):
        """Begin the asynchronous shutdown that destroys the window.
        Returns True when shutdown was initiated (completion is async)."""
        return self._begin_shutdown("close")

    def _begin_shutdown(self, kind: str, on_finished=None) -> bool:
        """Serialized teardown: stop worker, release hardware off-thread,
        then tear down Tk. Never joins or does hardware I/O on the Tk thread."""
        if self._shutdown_in_progress:
            if hasattr(self, "status_message_var"):
                self.status_message_var.set("Shutting down…")
            return False
        self._shutdown_in_progress = True
        self._shutdown_kind = kind
        self._shutdown_on_finished = on_finished
        self._persist_ui_prefs()

        self._cancel_queue_poll()
        self._cancel_canvas_idle_draw()
        self._remove_highlight()
        if hasattr(self, "status_message_var"):
            self.status_message_var.set("Shutting down…")
        self.connection_coordinator.invalidate_all()
        token = self.connection_coordinator.begin("shutdown")

        def _worker_stopped():
            captured = self._capture_hardware_for_shutdown()
            self.connection_coordinator.submit(
                lambda: self._shutdown_hardware_blocking(captured),
                token=token,
                on_done=lambda _result: self._finish_shutdown(),
                on_error=lambda exc: self._finish_shutdown(error=exc),
                label="hardware shutdown",
            )
            # If the hardware release hangs (stuck serial/USB I/O), force the
            # teardown to finish rather than leaving the window unclosable.
            try:
                self._shutdown_hw_deadline_id = self.root.after(
                    15000,
                    lambda: self._finish_shutdown(
                        error=TimeoutError("hardware release did not finish within 15 s")
                    ),
                )
            except tk.TclError:
                self._shutdown_hw_deadline_id = None

        def _worker_timeout():
            force = messagebox.askyesno(
                "Shutdown Delayed",
                "The acquisition worker has not stopped.\n\n"
                "Force close anyway? Hardware may be left in an undefined state.\n"
                "Choose No to keep the application open and retry later.",
                parent=self.root,
            )
            if force:
                _worker_stopped()
            else:
                self._abort_shutdown()

        self._stop_worker_async(_worker_stopped, on_timeout=_worker_timeout)
        return True

    def _capture_hardware_for_shutdown(self) -> dict:
        """Detach hardware refs on the Tk thread for executor-side teardown."""
        spectrometer, self.spectrometer = self.spectrometer, None
        if spectrometer is not None:
            self.spectrometer_link.phase = ConnectionPhase.DISCONNECTING
        return {"spectrometer": spectrometer}

    def _shutdown_hardware_blocking(self, captured: dict):
        """Release captured hardware. Runs on the coordinator executor."""
        spectrometer = captured.get("spectrometer")
        if spectrometer is not None:
            spectrometer.disconnect()

    def _finish_shutdown(self, error=None):
        """Tk-thread epilogue: tear down the UI after hardware release."""
        if getattr(self, "_shutdown_finished", False):
            return
        self._shutdown_finished = True
        deadline_id = getattr(self, "_shutdown_hw_deadline_id", None)
        if deadline_id is not None:
            self._shutdown_hw_deadline_id = None
            try:
                self.root.after_cancel(deadline_id)
            except tk.TclError:
                pass
        if error is not None:
            logger.warning("Hardware shutdown raised: %s", error, exc_info=error)
        kind = self._shutdown_kind
        on_finished, self._shutdown_on_finished = self._shutdown_on_finished, None
        self.spectrometer_link.phase = ConnectionPhase.DISCONNECTED

        self._ui_closing = True
        self._cancel_worker_stop_poll()
        self._cancel_pending_after_ids()
        self.connection_coordinator.shutdown()

        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass

        if kind == "quit":
            # Remove all acquisition widgets so the root can be reused
            for widget in self.root.winfo_children():
                widget.destroy()
            self.root.quit()
        else:
            try:
                self.root.destroy()
            except tk.TclError:
                pass
        if on_finished is not None:
            on_finished()

    def _abort_shutdown(self):
        """Keep the application running after a refused/failed shutdown."""
        self._shutdown_in_progress = False
        self._shutdown_kind = None
        self._shutdown_on_finished = None
        self._handoff_data = None
        self._requested_mode = None
        if self.worker:
            self._queue_poll_after_id = self.root.after(250, self._poll_queue)
        if hasattr(self, "status_message_var"):
            self.status_message_var.set(
                "Shutdown cancelled; the acquisition worker is still running."
            )
        self._refresh_acquisition_controls()

    def on_closing(self):
        """Handle window close event."""
        if self._shutdown_in_progress:
            return
        if self.worker and self.worker.state != "IDLE":
            result = messagebox.askyesno(
                "Confirm Exit",
                "Acquisition is in progress. Are you sure you want to exit?"
            )
            if not result:
                return

        self._handoff_data = None  # Don't hand off on close
        self._begin_shutdown("close", on_finished=lambda: sys.exit(0))


class AcquisitionApp(AcquisitionViewBase):
    """Manual acquisition mode view."""

    pass
