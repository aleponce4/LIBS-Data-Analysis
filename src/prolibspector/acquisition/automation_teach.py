"""Laser teach, jog, guarded test pulse, and GRBL recovery utilities.

Verbatim extraction from automated_app.py (module split): teach-action
threading, laser status polling, jog/manual moves, guarded test pulse,
motion-safety limits, taught corners, and the GRBL recovery probe. Methods
run on the AutomatedAcquisitionApp instance via mixin inheritance, so
``self`` is the app.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime
import logging
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from prolibspector.acquisition.automation import (
    LASER_PROFILE_MONPORT_NDYAG_RELAY,
    AutomationRunConfig,
    AutomationTarget,
    MotionSafetyLimits,
    automation_targets,
    bed_area_from_corners,
    load_motion_safety_limits,
    plan_plate_slots,
    save_motion_safety_limits,
    target_xy_for_slot,
    validate_motion_safety_points,
)
from prolibspector.acquisition.automated_worker import (
    GRBL_PREP_FIRING,
    GRBL_PREP_MOTION,
    prepare_grbl_controller,
    verify_grbl_firing_interlock,
)
from prolibspector.acquisition.automation_ui_laser_report import (
    grbl_status_active_pins,
    grbl_status_line_for_report,
)
from prolibspector.acquisition.corrections import resolve_effective_corrections
from prolibspector.core.windowing import create_dialog
from prolibspector.hardware.grbl_laser import GrblControllerProbeResult

_STABLE_WORK_POSITION_TOLERANCE_MM = 0.02
_STABLE_WORK_POSITION_TIMEOUT_S = 5.0
_STABLE_WORK_POSITION_SETTLE_S = 0.05

logger = logging.getLogger(__name__)


class AutomationTeachMixin:
    def _laser_connected_for_teaching(self) -> bool:
        return bool(self.laser and getattr(self.laser, "is_connected", False))

    def _refresh_laser_position(self, status=None):
        if status is None and self.laser is not None:
            x, y, _z = getattr(self.laser, "position", (None, None, None))
        elif status is not None:
            x, y = getattr(status, "work_x", None), getattr(status, "work_y", None)
            if x is None or y is None:
                x, y = getattr(status, "x", None), getattr(status, "y", None)
        else:
            x = y = None
        if x is None or y is None:
            self.laser_position_var.set("Position: unknown")
        else:
            self.laser_position_var.set(f"Position: X{x:.3f} Y{y:.3f}")

    def _poll_laser_status(self, *, timeout_s: float = 1.0):
        try:
            return self.laser.poll_status(timeout_s=timeout_s)
        except TypeError:
            return self.laser.poll_status()

    def _wait_laser_until_idle(self, *, timeout_s: float):
        wait = getattr(self.laser, "wait_until_idle", None)
        if callable(wait):
            try:
                return wait(timeout_s=timeout_s)
            except TypeError:
                return wait()
        return None

    def _work_xy_from_status(self, status) -> tuple[float, float]:
        x, y = getattr(status, "work_x", None), getattr(status, "work_y", None)
        if x is None or y is None:
            raise ValueError("Reliable GRBL work position is unavailable; recover/home the laser before teaching.")
        return float(x), float(y)

    def _require_teach_status_idle(self, status) -> None:
        state = str(getattr(status, "state", "") or "").strip()
        lower = state.lower()
        if lower.startswith(("alarm", "door", "hold")):
            raw = str(getattr(status, "raw", "") or state)
            raise ValueError(f"GRBL controller is not ready for teaching: {raw}")
        if lower != "idle":
            raw = str(getattr(status, "raw", "") or state or "unknown")
            raise ValueError(f"GRBL controller is not idle ({raw}); wait for motion to finish and retry.")

    def _stable_laser_work_position(
        self,
        *,
        require_idle: bool = True,
        verify_stable: bool = True,
        timeout_s: float = _STABLE_WORK_POSITION_TIMEOUT_S,
        tolerance_mm: float = _STABLE_WORK_POSITION_TOLERANCE_MM,
    ) -> tuple[float, float, object]:
        if not self._laser_connected_for_teaching():
            raise ValueError("Laser is not connected.")
        if require_idle:
            self._wait_laser_until_idle(timeout_s=timeout_s)
        try:
            first_status = self._poll_laser_status(timeout_s=timeout_s)
        except Exception as exc:
            raise ValueError("Reliable GRBL work position is unavailable; recover/home the laser before teaching.") from exc
        if require_idle:
            self._require_teach_status_idle(first_status)
        first_x, first_y = self._work_xy_from_status(first_status)
        if not verify_stable:
            return first_x, first_y, first_status

        time.sleep(_STABLE_WORK_POSITION_SETTLE_S)
        try:
            second_status = self._poll_laser_status(timeout_s=timeout_s)
        except Exception as exc:
            raise ValueError("Reliable GRBL work position is unavailable; recover/home the laser before teaching.") from exc
        if require_idle:
            self._require_teach_status_idle(second_status)
        second_x, second_y = self._work_xy_from_status(second_status)
        if abs(second_x - first_x) > tolerance_mm or abs(second_y - first_y) > tolerance_mm:
            raise ValueError(
                "GRBL work position is still changing; wait for motion to settle and record the point again."
            )
        return second_x, second_y, second_status

    def _teach_laser_action(self, label: str, callback, *, quiet_errors: bool = False):
        if self._laser_utility_busy:
            self.status_message_var.set("Laser utility is running.")
            if not quiet_errors:
                messagebox.showinfo("Laser Busy", "Wait for the current laser utility action to finish.", parent=self.root)
            return
        if not self._laser_connected_for_teaching():
            message = "Connect or simulate the GRBL laser first."
            self.status_message_var.set(message)
            self.laser_status_var.set(message)
            if not quiet_errors:
                messagebox.showinfo("Laser Required", message, parent=self.root)
            return
        if self._automation_busy():
            message = "Stop automation before using the laser utility."
            self.status_message_var.set(message)
            self.laser_status_var.set(message)
            if not quiet_errors:
                messagebox.showinfo("Automation Running", "Stop automation before teaching the bed.", parent=self.root)
            return
        self._laser_utility_stop_requested = False
        self._laser_utility_busy = True
        self.laser_status_var.set(f"{label}...")
        self._set_widget_state(self._automation_teach_widgets, "disabled")
        self._refresh_automation_controls()

        def _work():
            def _schedule_finish(result, status, exc: Exception | None):
                try:
                    self._post_to_ui(
                        lambda r=result, s=status, e=exc: self._finish_teach_action(label, r, s, e),
                    )
                except (RuntimeError, tk.TclError):
                    self._laser_utility_busy = False

            try:
                result = callback()
                status = result.get("status") if isinstance(result, dict) else None
                if status is None:
                    try:
                        status = self._poll_laser_status()
                    except Exception:
                        status = None
                _schedule_finish(result, status, None)
            except Exception as exc:
                _schedule_finish(None, None, exc)

        threading.Thread(target=_work, daemon=True, name="LaserTeachThread").start()

    def _finish_teach_action(self, label: str, result, status, exc: Exception | None):
        stopped = bool(getattr(self, "_laser_utility_stop_requested", False))
        self._laser_utility_busy = False
        if stopped and exc is None:
            self.laser_status_var.set(f"{label} stopped.")
            self.status_message_var.set(f"{label} stopped.")
        elif exc is not None:
            message = f"{label} failed: {exc}"
            self.laser_status_var.set(message)
            self.status_message_var.set(message)
            drop = getattr(self, "_drop_lost_laser_connection_if_dead", None)
            if callable(drop):
                drop()
        else:
            if isinstance(result, dict) and result.get("spectrum") is not None:
                wavelengths, intensities = result["spectrum"]
                self._display_laser_test_spectrum(wavelengths, intensities)
                summary = str(result.get("summary") or f"{label} complete.")
                self.laser_status_var.set(summary)
                self.status_message_var.set(summary)
            elif isinstance(result, dict) and result.get("diagnostic_log"):
                self.laser_status_var.set(str(result.get("summary") or f"{label} complete."))
                try:
                    self._show_laser_controller_check_log(str(result["diagnostic_log"]))
                except tk.TclError:
                    pass
            elif isinstance(result, dict) and "summary" in result:
                self.laser_status_var.set(str(result.get("summary") or f"{label} complete."))
            else:
                self.laser_status_var.set(str(result or f"{label} complete."))
            self._refresh_laser_position(status)
        self._laser_utility_stop_requested = False
        self._refresh_automation_controls()

    def _display_laser_test_spectrum(self, wavelengths, intensities) -> None:
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

    def on_stop_laser_utility(self):
        laser = getattr(self, "laser", None)
        if laser is None:
            return
        self._laser_utility_stop_requested = True
        hold = getattr(laser, "feed_hold", None)
        if callable(hold):
            try:
                hold()
            except Exception:
                logger.warning("Failed to send laser feed hold.", exc_info=True)
                if hasattr(self, "status_message_var"):
                    self.status_message_var.set(
                        "Feed hold failed to send; verify the laser stopped before continuing."
                    )

        def _confirm_laser_off():
            confirm = getattr(laser, "confirm_laser_off_and_status", None)
            off = getattr(laser, "emergency_laser_off", None)
            try:
                if callable(confirm):
                    confirm(timeout_s=2.0)
                elif callable(off):
                    off()
            except Exception as exc:
                failure_text = str(exc)
                logger.debug("Failed to confirm laser utility stop.", exc_info=True)
                try:
                    self._post_to_ui(
                        lambda: self.status_message_var.set(
                            f"Laser utility stop requested; reconnect may be required: {failure_text}"
                        ),
                    )
                except Exception:
                    pass

        threading.Thread(
            target=_confirm_laser_off,
            daemon=True,
            name="LaserUtilityConfirmOff",
        ).start()
        self._laser_utility_busy = False
        self.laser_status_var.set("Laser utility stop requested.")
        self.status_message_var.set("Laser utility stop requested.")
        self._refresh_automation_controls()

    def on_laser_unlock(self):
        self._teach_laser_action(
            "Recover/Home",
            lambda: (
                self._prepare_laser_controller_for_motion("manual recover/home", force_home=True),
                "Controller recovered and homed.",
            )[-1],
        )

    def on_laser_home(self):
        self._teach_laser_action(
            "Home",
            lambda: (self._prepare_laser_controller_for_motion("manual home", force_home=True), "Home complete.")[-1],
        )

    def on_laser_poll_status(self):
        def _poll():
            status = self._poll_laser_status()
            return {"summary": getattr(status, "raw", "") or "Status updated.", "status": status}

        self._teach_laser_action("Poll status", _poll)

    def on_laser_off(self):
        def _off():
            try:
                self.laser.laser_off()
            except Exception:
                self.laser.emergency_laser_off()
                raise
            return "Laser off command sent."
        self._teach_laser_action("Laser off", _off)

    def on_laser_controller_check(self):
        if not self._ensure_laser_utility_idle():
            return
        self._teach_laser_action("Controller check", self._build_laser_controller_check_report)

    def on_grbl_recovery_probe(self):
        return self.automation_controller.on_grbl_recovery_probe()

    def on_grbl_unlock_and_recheck(self):
        return self.automation_controller.on_grbl_unlock_and_recheck()

    def _finish_grbl_recovery_probe(self, title: str, result: GrblControllerProbeResult):
        self._laser_utility_busy = False
        self._last_grbl_recovery_probe = result
        summary = self._grbl_recovery_summary(result)
        self.laser_status_var.set(summary)
        self.status_message_var.set(summary)
        if result.raw_lines or result.identity_lines or result.error:
            try:
                self._show_laser_controller_check_log(self._build_grbl_recovery_log(title, result))
            except tk.TclError:
                pass
        self._refresh_automation_controls()

    def _grbl_recovery_summary(self, result: GrblControllerProbeResult) -> str:
        if not result.detected_grbl:
            return f"Controller probe failed: {result.error or 'no GRBL status or identity response'}"
        summary = f"Controller probe: state={result.state_text}, pins={result.active_pins_text}"
        if result.alarm_active:
            return f"{summary}; alarm detected. Use Unlock and Recheck only after confirming the machine is safe."
        if result.state_text.lower() == "idle":
            if result.work_coordinates_reliable:
                return f"{summary}; idle with reliable work coordinates."
            return f"{summary}; idle, but reliable work coordinates are unavailable. Home before normal motion."
        if result.error:
            return f"{summary}; {result.error}"
        return summary

    def _build_grbl_recovery_log(self, title: str, result: GrblControllerProbeResult) -> str:
        command_note = (
            "$X then ?"
            if title.lower().startswith("unlock")
            else "? and $I (plus one Ctrl-X soft reset if the first ? goes unanswered)"
        )
        lines = [
            f"{title} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Port: {result.port}",
            f"Baud: {result.baudrate}",
            f"Commands sent: {command_note}",
            "No homing, motion, or firing command is sent by this recovery path.",
            "",
            "[Status]",
        ]
        if result.status is None:
            lines.append("status read: unavailable")
        else:
            lines.append(grbl_status_line_for_report(result.status))
        if result.identity_lines:
            lines.extend(["", "[GRBL identity]"])
            lines.extend(result.identity_lines)
        if result.raw_lines:
            lines.extend(["", "[Raw responses]"])
            lines.extend(result.raw_lines)
        if result.error:
            lines.extend(["", "[Probe error]", result.error])

        pins = grbl_status_active_pins(result.status)
        limit_pins = sorted({"X", "Y", "Z"}.intersection(pins))
        lines.extend(["", "[Guidance]"])
        if result.status is None and result.identity_lines:
            lines.append("GRBL identity was detected, but no status line was returned.")
        elif result.status is None:
            lines.append("No GRBL status was detected on this port.")
        elif result.alarm_active:
            lines.append("$X is available only as an explicit operator action because it clears the alarm lock.")
        elif result.state_text.lower() == "idle":
            if result.work_coordinates_reliable:
                lines.append("Controller is Idle and reported reliable work coordinates.")
            else:
                lines.append("Controller is Idle, but work coordinates are not reliable; home before normal jog/run operations.")
        if limit_pins:
            lines.append(
                f"Limit input(s) active: {', '.join(limit_pins)}. The head may be sitting on a limit switch; "
                "move it off the switch before normal jog/run operations."
            )
        if "P" in pins:
            lines.append("Probe input P is active; treat it according to the selected machine profile before firing.")
        return "\n".join(lines)

    def _status_active_pins(self, status) -> frozenset[str]:
        return grbl_status_active_pins(status)

    def _status_line_for_report(self, status) -> str:
        return grbl_status_line_for_report(status)

    def _build_laser_controller_check_report(self) -> dict[str, str]:
        return self._automation_laser_report_builder.build()

    def _show_laser_controller_check_log(self, log: str) -> None:
        dialog = create_dialog(self.root, "Laser Controller Check", resizable=True)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)

        text = tk.Text(dialog, width=88, height=24, wrap="word", font=self._automation_font(9))
        scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.grid(row=0, column=0, padx=(12, 0), pady=12, sticky="nsew")
        scrollbar.grid(row=0, column=1, padx=(0, 12), pady=12, sticky="ns")
        text.insert("1.0", log)
        text.configure(state="disabled")
        ttk.Button(dialog, text="Close", command=dialog.destroy, style="Sidebar.TButton").grid(
            row=1, column=0, columnspan=2, padx=12, pady=(0, 12), sticky="e"
        )
        dialog.minsize(620, 360)
        try:
            text.focus_set()
        except tk.TclError:
            pass

    def on_jog_laser(self, x_sign: int, y_sign: int):
        try:
            step = self._float_var(self.jog_step_var, "jog step")
            feed = max(1.0, self._float_var(self.jog_feed_var, "jog feed"))
        except ValueError as exc:
            messagebox.showwarning("Jog Settings", str(exc), parent=self.root)
            return
        if step <= 0:
            messagebox.showwarning("Jog Settings", "Jog step must be greater than 0 mm.", parent=self.root)
            return
        dx = float(x_sign) * step
        dy = float(y_sign) * step
        try:
            current_x, current_y = self._current_laser_xy()
            self._validate_laser_motion_points([("Jog", current_x + dx, current_y + dy)])
        except Exception as exc:
            messagebox.showwarning("Motion Safety", str(exc), parent=self.root)
            self.status_message_var.set(str(exc))
            return

        def _jog():
            self._prepare_laser_controller_for_motion("jog")
            self.laser.jog(dx, dy, feed_mm_min=feed)
            _x, _y, status = self._stable_laser_work_position(require_idle=True, verify_stable=False)
            return {"summary": f"Jogged X{dx:g} Y{dy:g}.", "status": status}

        self._teach_laser_action("Jog", _jog)

    def _ensure_laser_utility_idle(self) -> bool:
        if self._laser_utility_busy:
            self.status_message_var.set("Laser utility is running.")
            messagebox.showinfo("Laser Busy", "Wait for the current laser utility action to finish.", parent=self.root)
            return False
        if self._automation_busy():
            messagebox.showinfo("Automation Running", "Stop automation before using laser utilities.", parent=self.root)
            return False
        if not self._laser_connected_for_teaching():
            messagebox.showinfo("Laser Required", "Connect or simulate the GRBL laser first.", parent=self.root)
            return False
        return True

    def _set_laser_status_from_worker_thread(self, message: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self.laser_status_var.set(message)
            return
        try:
            self._post_to_ui(lambda m=message: self.laser_status_var.set(m))
        except (RuntimeError, tk.TclError):
            pass

    def _prepare_laser_controller_for_motion(self, context: str, *, force_home: bool = False):
        return prepare_grbl_controller(
            self.laser,
            intent=GRBL_PREP_MOTION,
            context=context,
            force_home=force_home,
            send_status=self._set_laser_status_from_worker_thread,
        )

    def _prepare_laser_controller_for_firing(self, context: str):
        return prepare_grbl_controller(
            self.laser,
            intent=GRBL_PREP_FIRING,
            context=context,
            allow_p_input_for_firing=self._effective_grbl_p_input_nonblocking_enabled(),
            force_home=False,
            send_status=self._set_laser_status_from_worker_thread,
        )

    def _verify_laser_controller_for_firing(self, context: str):
        return verify_grbl_firing_interlock(
            self.laser,
            context=context,
            allow_p_input_for_firing=self._effective_grbl_p_input_nonblocking_enabled(),
            send_status=self._set_laser_status_from_worker_thread,
        )

    def _laser_motion_safety_grbl_settings(self) -> dict[str, float]:
        laser = getattr(self, "laser", None)
        read_settings = getattr(laser, "read_grbl_settings", None)
        if not callable(read_settings):
            return {}
        try:
            return read_settings({"130", "131"})
        except Exception:
            return {}

    def _validate_laser_motion_points(self, points: list[tuple[str, float, float]] | tuple[tuple[str, float, float], ...]) -> None:
        if self._automation_laser_simulated():
            return
        validate_motion_safety_points(
            points,
            limits=load_motion_safety_limits(),
            grbl_settings=self._laser_motion_safety_grbl_settings(),
        )

    def on_laser_go_to_xy(self):
        if not self._ensure_laser_utility_idle():
            return
        try:
            x = self._float_var(self.go_to_x_var, "target X")
            y = self._float_var(self.go_to_y_var, "target Y")
            feed = max(1.0, self._float_var(self.go_to_feed_var, "go-to feed"))
        except ValueError as exc:
            messagebox.showwarning("Go to XY", str(exc), parent=self.root)
            return
        try:
            self._validate_laser_motion_points([("Go to XY", x, y)])
        except ValueError as exc:
            messagebox.showwarning("Motion Safety", str(exc), parent=self.root)
            self.status_message_var.set(str(exc))
            return

        def _move():
            self._prepare_laser_controller_for_motion("go to XY")
            self.laser.move_to(x, y, feed_mm_min=feed)
            wait = getattr(self.laser, "wait_until_idle", None)
            if callable(wait):
                wait(timeout_s=60.0)
            self.laser.emergency_laser_off()
            return f"Moved to X{x:g} Y{y:g}."

        self._teach_laser_action("Go to XY", _move)

    def _center_target_for_plate_well(
        self,
        config: AutomationRunConfig,
        plate_index: int,
        well: str,
    ) -> AutomationTarget:
        well = str(well or "").strip().upper()
        matches = [
            target
            for target in automation_targets(config)
            if int(target.plate_index) == int(plate_index) and str(target.well).upper() == well
        ]
        if not matches:
            raise ValueError(f"No planned target found for plate P{int(plate_index):02d} well {well or '?'}.")
        for target in matches:
            if (
                int(target.shot_number) == 1
                and str(target.shot_offset_label or "center").lower() == "center"
                and abs(float(target.shot_offset_dx_mm)) <= 1e-9
                and abs(float(target.shot_offset_dy_mm)) <= 1e-9
            ):
                return target
        raise ValueError(f"Plate P{int(plate_index):02d} well {well} does not have a center target.")

    def on_laser_move_to_well(self):
        if not self._ensure_laser_utility_idle():
            return
        try:
            plate_index = self._int_var(self.move_to_well_plate_var, "plate")
            if plate_index <= 0:
                raise ValueError("Plate must be 1 or greater.")
            well = str(self.move_to_well_id_var.get() or "").strip().upper()
            if not well:
                raise ValueError("Enter a well ID, for example A5.")
            config = self._build_automation_config(ensure_run_directory=False)
            target = self._center_target_for_plate_well(config, plate_index, well)
            self._validate_laser_motion_points([
                (f"Plate {target.plate_index} {target.well}", target.x_mm, target.y_mm)
            ])
        except Exception as exc:
            message = str(exc)
            self.status_message_var.set(message)
            messagebox.showwarning("Move to Well", message, parent=self.root)
            return

        def _move():
            self._prepare_laser_controller_for_motion("move to well")
            try:
                self.laser.move_to(target.x_mm, target.y_mm, feed_mm_min=config.move_speed_mm_min)
                wait = getattr(self.laser, "wait_until_idle", None)
                if callable(wait):
                    wait(timeout_s=60.0)
            finally:
                self.laser.emergency_laser_off()
            return f"Moved to plate {target.plate_index} {target.well} at X{target.x_mm:g} Y{target.y_mm:g}."

        self._teach_laser_action("Move to Well", _move)

    def on_laser_test_movement(self):
        if not self._ensure_laser_utility_idle():
            return
        if self.automation_config is None and not self.on_apply_automation_plan():
            return
        config = self.automation_config
        if config is None:
            return
        try:
            slots = plan_plate_slots(config)
            if not slots:
                raise ValueError("No planned plates are available for movement testing.")
            first_slot = slots[0]
            last_slot = slots[-1]
            last_well = f"{chr(65 + config.plate_model.rows - 1)}{config.plate_model.columns}"
            targets = [
                ("first plate A1", first_slot, "A1"),
                (f"first plate {last_well}", first_slot, last_well),
                ("last plate A1", last_slot, "A1"),
            ]
            motion_points = []
            for label, slot, well in targets:
                x, y = target_xy_for_slot(config, slot, well)
                motion_points.append((f"Test movement {label}", x, y))
            self._validate_laser_motion_points(motion_points)
        except Exception as exc:
            messagebox.showwarning("Test Movement", str(exc), parent=self.root)
            return

        def _test_move():
            self._prepare_laser_controller_for_motion("test movement")
            for _label, slot, well in targets:
                x, y = target_xy_for_slot(config, slot, well)
                self.laser.move_to(x, y, feed_mm_min=config.move_speed_mm_min)
                wait = getattr(self.laser, "wait_until_idle", None)
                if callable(wait):
                    wait(timeout_s=60.0)
            self.laser.emergency_laser_off()
            return "Test movement complete. No laser pulse was fired."

        self._teach_laser_action("Test movement", _test_move)

    def on_laser_guarded_test_pulse(self):
        if self._laser_utility_busy:
            self.status_message_var.set("Laser utility is running.")
            self.laser_status_var.set("Laser utility is running.")
            return
        try:
            config = self._build_automation_config(ensure_run_directory=False)
        except Exception as exc:
            message = f"Test Pulse + Spectrum setup failed: {exc}"
            self.status_message_var.set(message)
            self.laser_status_var.set(message)
            return
        if not self._laser_connected_for_teaching():
            message = "Connect or simulate the GRBL laser first."
            self.status_message_var.set(message)
            self.laser_status_var.set(message)
            return
        spec = getattr(self, "spectrometer", None)
        if not (spec is not None and getattr(spec, "is_connected", False)):
            message = "Connect the spectrometer before running Test Pulse + Spectrum."
            self.status_message_var.set(message)
            self.laser_status_var.set(message)
            return
        caps = getattr(spec, "capabilities", None)
        external_mode = getattr(caps, "external_trigger_mode", None)
        if external_mode is None:
            message = "Connected spectrometer does not support external trigger capture."
            self.status_message_var.set(message)
            self.laser_status_var.set(message)
            return
        if config.pulse_ms <= 0:
            message = "Pulse duration must be greater than zero."
            self.status_message_var.set(message)
            self.laser_status_var.set(message)
            return
        if config.laser_profile == LASER_PROFILE_MONPORT_NDYAG_RELAY and config.relay_stroke_mm <= 0:
            message = "Relay stroke must be greater than zero for Test Pulse + Spectrum."
            self.status_message_var.set(message)
            self.laser_status_var.set(message)
            return
        correction_kwargs = self._laser_test_pulse_correction_kwargs(spec)
        normal_mode = getattr(caps, "normal_trigger_mode", None)

        def _pulse():
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="TestPulseTriggerRead")
            future = None
            cancel_requested = False
            deadline = time.monotonic() + max(0.001, float(config.capture_timeout_s))

            def _cancel_trigger_read() -> None:
                if normal_mode is not None and getattr(spec, "is_connected", False):
                    try:
                        spec.set_trigger_mode(normal_mode)
                    except Exception:
                        logger.exception("Failed to reset trigger mode during Test Pulse + Spectrum cancel.")
                if future is not None:
                    future.cancel()
                    try:
                        future.result(timeout=2.0)
                    except FutureTimeoutError:
                        logger.error("Test Pulse + Spectrum trigger read did not unwind after trigger reset.")
                    except Exception as exc:
                        logger.info("Cancelled Test Pulse + Spectrum read finished with error: %s", exc)

            try:
                if getattr(spec, "current_trigger_mode", None) != external_mode:
                    spec.set_trigger_mode(external_mode)
                future = executor.submit(spec.get_intensities, **correction_kwargs)
                if config.laser_profile == LASER_PROFILE_MONPORT_NDYAG_RELAY:
                    x, y = self._current_laser_xy_for_test_pulse()
                    end_x = x + float(config.relay_stroke_mm)
                    end_y = y
                    pulse_s = max(0.001, float(config.pulse_ms) / 1000.0)
                    feed = max(1.0, abs(float(config.relay_stroke_mm)) / pulse_s * 60.0)
                    fire_motion = getattr(self.laser, "fire_motion_pulse", None)
                    if not callable(fire_motion):
                        raise RuntimeError("Connected laser controller does not support motion-coupled relay firing.")
                    fire_motion(
                        end_x_mm=end_x,
                        end_y_mm=end_y,
                        s_value=config.laser_s_value,
                        pulse_ms=config.pulse_ms,
                        feed_mm_min=feed,
                        should_stop=lambda: bool(getattr(self, "_laser_utility_stop_requested", False)),
                    )
                else:
                    self.laser.fire_pulse(
                        s_value=config.laser_s_value,
                        pulse_ms=config.pulse_ms,
                        should_stop=lambda: bool(getattr(self, "_laser_utility_stop_requested", False)),
                    )
                while future is not None and not future.done():
                    if getattr(self, "_laser_utility_stop_requested", False):
                        cancel_requested = True
                        _cancel_trigger_read()
                        raise RuntimeError("Test Pulse + Spectrum stopped.")
                    if time.monotonic() >= deadline:
                        cancel_requested = True
                        _cancel_trigger_read()
                        raise TimeoutError(
                            f"Test Pulse + Spectrum capture timed out after {config.capture_timeout_s:g}s."
                        )
                    time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))
                intensities = future.result() if future is not None else None
                wavelengths = spec.get_wavelengths()
                return {
                    "summary": "Test Pulse + Spectrum captured.",
                    "spectrum": (wavelengths, intensities),
                }
            except Exception:
                if future is not None and not future.done():
                    cancel_requested = True
                    _cancel_trigger_read()
                raise
            finally:
                try:
                    if normal_mode is not None and getattr(spec, "is_connected", False):
                        spec.set_trigger_mode(normal_mode)
                finally:
                    self.laser.emergency_laser_off()
                    executor.shutdown(wait=not cancel_requested, cancel_futures=cancel_requested)

        self._teach_laser_action("Test Pulse + Spectrum", _pulse, quiet_errors=True)

    def _laser_test_pulse_correction_kwargs(self, spec) -> dict[str, bool]:
        dark_var = getattr(self, "correct_dark_var", None)
        nl_var = getattr(self, "correct_nl_var", None)
        dark_enabled = bool(dark_var.get()) if dark_var is not None else True
        nl_enabled = bool(nl_var.get()) if nl_var is not None else True
        return resolve_effective_corrections(
            getattr(spec, "capabilities", None),
            requested_dark_counts=dark_enabled,
            requested_nonlinearity=nl_enabled,
        ).to_kwargs()

    def _current_laser_xy_for_test_pulse(self) -> tuple[float, float]:
        try:
            status = self._poll_laser_status()
            x, y = getattr(status, "work_x", None), getattr(status, "work_y", None)
            if x is not None and y is not None:
                return float(x), float(y)
        except Exception:
            pass
        try:
            x, y, _z = getattr(self.laser, "position", (None, None, None))
        except Exception:
            x, y = None, None
        if x is None or y is None:
            raise ValueError("Current laser position is unavailable for Test Pulse + Spectrum.")
        return float(x), float(y)

    def _current_laser_xy(self, *, require_polled_work_position: bool = False) -> tuple[float, float]:
        if require_polled_work_position:
            x, y, _status = self._stable_laser_work_position(require_idle=True)
            return x, y
        if not self._laser_connected_for_teaching():
            raise ValueError("Laser is not connected.")
        try:
            status = self._poll_laser_status()
            x, y = getattr(status, "work_x", None), getattr(status, "work_y", None)
        except Exception:
            x, y, _z = getattr(self.laser, "position", (None, None, None))
        if x is None or y is None:
            raise ValueError("Reliable GRBL work position is unavailable; recover/home the laser before teaching.")
        return float(x), float(y)

    def on_record_corner(self, corner: str):
        try:
            x, y, status = self._stable_laser_work_position(require_idle=True)
        except Exception as exc:
            messagebox.showwarning("Record Corner", str(exc), parent=self.root)
            drop = getattr(self, "_drop_lost_laser_connection_if_dead", None)
            if callable(drop):
                drop()
            return
        if corner.upper() == "A":
            self._taught_corner_a = (x, y)
            self.corner_a_var.set(f"Corner A: X{x:.3f} Y{y:.3f}")
        else:
            self._taught_corner_b = (x, y)
            self.corner_b_var.set(f"Corner B: X{x:.3f} Y{y:.3f}")
        self._refresh_laser_position(status)

    def on_apply_taught_corners(self):
        if self._taught_corner_a is None or self._taught_corner_b is None:
            messagebox.showinfo("Teach Bed", "Record both corners before applying the taught bed.", parent=self.root)
            return
        try:
            bed = bed_area_from_corners(self._taught_corner_a, self._taught_corner_b)
        except ValueError as exc:
            messagebox.showwarning("Teach Bed", str(exc), parent=self.root)
            return
        self.bed_x_min_var.set(f"{bed.x_min_mm:g}")
        self.bed_x_max_var.set(f"{bed.x_max_mm:g}")
        self.bed_y_min_var.set(f"{bed.y_min_mm:g}")
        self.bed_y_max_var.set(f"{bed.y_max_mm:g}")
        self._active_fixture_calibration = None
        if hasattr(self, "fixture_profile_var"):
            self.fixture_profile_var.set(self._fixture_no_profile_label)
            self._clear_fixture_recording()
        if hasattr(self, "automation_preset_var"):
            self.automation_preset_var.set(self._custom_automation_preset_label())
        self.automation_plan_var.set("Taught bed applied. Re-apply the plate plan.")

    def _set_default_bed_area(self):
        self._active_fixture_calibration = None
        if hasattr(self, "fixture_profile_var"):
            self.fixture_profile_var.set(self._fixture_no_profile_label)
            self._clear_fixture_recording()
        self.bed_x_min_var.set("0")
        self.bed_x_max_var.set("300")
        self.bed_y_min_var.set("0")
        self.bed_y_max_var.set("200")

    def _set_motion_safety_limit_vars(self, limits: MotionSafetyLimits) -> None:
        normalized = limits.normalized()
        self.motion_safety_enabled_var.set(normalized.enabled)
        self.motion_safety_x_min_var.set(self._format_preset_number(normalized.x_min_mm))
        self.motion_safety_x_max_var.set(self._format_preset_number(normalized.x_max_mm))
        self.motion_safety_y_min_var.set(self._format_preset_number(normalized.y_min_mm))
        self.motion_safety_y_max_var.set(self._format_preset_number(normalized.y_max_mm))

    def _motion_safety_limits_from_vars(self) -> MotionSafetyLimits:
        x_min = self._float_var(self.motion_safety_x_min_var, "motion safety X minimum")
        x_max = self._float_var(self.motion_safety_x_max_var, "motion safety X maximum")
        y_min = self._float_var(self.motion_safety_y_min_var, "motion safety Y minimum")
        y_max = self._float_var(self.motion_safety_y_max_var, "motion safety Y maximum")
        if x_min >= x_max:
            raise ValueError("Motion safety X minimum must be less than X maximum.")
        if y_min >= y_max:
            raise ValueError("Motion safety Y minimum must be less than Y maximum.")
        return MotionSafetyLimits(
            enabled=bool(self.motion_safety_enabled_var.get()),
            x_min_mm=x_min,
            x_max_mm=x_max,
            y_min_mm=y_min,
            y_max_mm=y_max,
        ).normalized()

    def on_save_motion_safety_limits(self) -> bool:
        try:
            limits = self._motion_safety_limits_from_vars()
        except ValueError as exc:
            messagebox.showwarning("Motion Safety Limits", str(exc), parent=self.root)
            return False
        success, message = save_motion_safety_limits(limits)
        if not success:
            messagebox.showwarning("Motion Safety Limits", message, parent=self.root)
            return False
        self._set_motion_safety_limit_vars(limits)
        self.status_message_var.set("Motion safety limits saved.")
        return True

    def on_reset_motion_safety_limits(self) -> bool:
        limits = MotionSafetyLimits()
        success, message = save_motion_safety_limits(limits)
        if not success:
            messagebox.showwarning("Motion Safety Limits", message, parent=self.root)
            return False
        self._set_motion_safety_limit_vars(limits)
        self.status_message_var.set("Motion safety limits reset to defaults.")
        return True
