"""Fixture-calibration workflow for the automated-acquisition view.

Verbatim extraction from automated_app.py (module split): profile library,
slot selection, guided corner recording, validation, and apply/delete of
fixture calibration profiles. Methods run on the AutomatedAcquisitionApp
instance via mixin inheritance, so ``self`` is the app.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import logging
import tkinter as tk
from tkinter import messagebox

from prolibspector.acquisition.automation import (
    FIXTURE_CALIBRATION_SOURCE_LEGACY_SETTINGS,
    FIXTURE_CALIBRATION_SOURCE_REPO,
    FIXTURE_POINT_SEQUENCE,
    FIXTURE_PROFILE_PLATE_COUNT,
    AutomationRunConfig,
    BedArea,
    FixtureCalibration,
    ORIENTATION_NORMAL,
    clear_fixture_point_draft,
    create_fixture_calibration_from_points,
    delete_fixture_calibration,
    effective_bed_area,
    load_fixture_calibrations,
    load_fixture_point_draft,
    save_fixture_calibration_profile,
    save_fixture_point_draft,
    validate_fixture_calibration,
)
from prolibspector.acquisition.plate_autosave import sanitize_filename_part

_FIXTURE_GEOMETRY_PRESET_ID = "generic_96_2x2"

logger = logging.getLogger(__name__)


class AutomationFixtureMixin:
    def _fixture_profile_label(self, profile: FixtureCalibration) -> str:
        name = (profile.display_name or profile.id).strip() or profile.id
        if profile.source_kind == FIXTURE_CALIBRATION_SOURCE_LEGACY_SETTINGS:
            return f"Legacy - {name}"
        timestamp_text = profile.updated_at or profile.created_at
        try:
            timestamp = datetime.fromisoformat(timestamp_text)
            return f"{timestamp.strftime('%Y-%m-%d %H:%M')} - {name}"
        except (TypeError, ValueError):
            pass
        if profile.source_filename:
            return f"{profile.source_filename[:-5]} - {name}" if profile.source_filename.endswith(".json") else f"{profile.source_filename} - {name}"
        return name

    def _refresh_fixture_profile_library(
        self,
        *,
        selected_id: str | None = None,
        select_latest_if_none: bool = False,
    ) -> None:
        self.fixture_calibrations = load_fixture_calibrations()
        labels = [self._fixture_no_profile_label]
        self._fixture_calibrations_by_label = {}
        selected_label = self._fixture_no_profile_label
        used_labels = set(labels)
        if selected_id is None and self._active_fixture_calibration is not None:
            selected_id = self._active_fixture_calibration.id
        if selected_id is None and select_latest_if_none and self.fixture_calibrations:
            selected_id = self.fixture_calibrations[0].id
        for profile in self.fixture_calibrations:
            label = self._fixture_profile_label(profile)
            if label in used_labels:
                label = f"{label} ({profile.id})"
            base_label = label
            suffix = 2
            while label in used_labels:
                label = f"{base_label} {suffix}"
                suffix += 1
            labels.append(label)
            used_labels.add(label)
            self._fixture_calibrations_by_label[label] = profile
            if profile.id == selected_id:
                selected_label = label
        if hasattr(self, "fixture_profile_combo"):
            self.fixture_profile_combo.configure(values=labels)
        if hasattr(self, "fixture_profile_var"):
            self.fixture_profile_var.set(selected_label)

    def _latest_fixture_calibration(self) -> FixtureCalibration | None:
        if not self.fixture_calibrations:
            self._refresh_fixture_profile_library()
        repo_profiles = [
            profile
            for profile in self.fixture_calibrations
            if profile.source_kind == FIXTURE_CALIBRATION_SOURCE_REPO
        ]
        if repo_profiles:
            return repo_profiles[0]
        if self.fixture_calibrations:
            return self.fixture_calibrations[0]
        return None

    def _effective_fixture_calibration(self) -> FixtureCalibration | None:
        """Return the fixture geometry currently activated for the plan."""
        profile = self._active_fixture_calibration
        if profile is None:
            return None
        if self._fixture_calibrations_by_label and any(
            fixture.id == profile.id for fixture in self._fixture_calibrations_by_label.values()
        ):
            return profile
        if any(fixture.id == profile.id for fixture in self.fixture_calibrations):
            return profile
        return profile

    def _clear_stale_active_fixture_if_missing(self) -> bool:
        profile = self._active_fixture_calibration
        if profile is None:
            return False
        if self._fixture_calibrations_by_label and any(
            fixture.id == profile.id for fixture in self._fixture_calibrations_by_label.values()
        ):
            return False
        if any(fixture.id == profile.id for fixture in self.fixture_calibrations):
            return False
        self._clear_active_fixture_for_manual_plan()
        return True

    def _fixture_geometry_preset_label(self) -> str:
        return next(
            (
                preset.display_name
                for preset in self.automation_presets
                if preset.id == _FIXTURE_GEOMETRY_PRESET_ID
            ),
            self._custom_automation_preset_label(),
        )

    def _apply_latest_fixture_calibration_default(self, *, force: bool = False) -> bool:
        if self._fixture_recording_started or self._fixture_calibration_points:
            return False
        profile = self._latest_fixture_calibration()
        if profile is None:
            return False
        if not force and self._active_fixture_calibration is not None and self._active_fixture_calibration.id == profile.id:
            return False
        self._apply_fixture_profile_to_plan_vars(profile)
        self._refresh_fixture_profile_library(selected_id=profile.id)
        return True

    def _clear_active_fixture_for_manual_plan(self) -> None:
        self._active_fixture_calibration = None
        self._fixture_slot_selection_profile_id = None
        if hasattr(self, "fixture_profile_var"):
            self.fixture_profile_var.set(self._fixture_no_profile_label)
        self._refresh_fixture_slot_selector()

    def _selected_fixture_calibration(self) -> FixtureCalibration | None:
        if not hasattr(self, "fixture_profile_var"):
            return None
        return self._fixture_calibrations_by_label.get(self.fixture_profile_var.get())

    def _fixture_slot_indices_from_vars(self) -> tuple[int, ...]:
        selected = [
            int(index)
            for index, var in sorted(getattr(self, "_fixture_slot_vars", {}).items())
            if bool(var.get())
        ]
        return tuple(selected)

    def _set_fixture_slot_selection(self, plate_indices, *, sync_count: bool = True) -> None:
        indices = []
        for raw_index in plate_indices or ():
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if 1 <= index <= FIXTURE_PROFILE_PLATE_COUNT and index not in indices:
                indices.append(index)
        if not indices:
            indices = list(range(1, FIXTURE_PROFILE_PLATE_COUNT + 1))
        suspended = self._automation_plan_update_suspended
        self._automation_plan_update_suspended = True
        try:
            for index, var in getattr(self, "_fixture_slot_vars", {}).items():
                desired = index in indices
                if bool(var.get()) != desired:
                    var.set(desired)
            if sync_count and hasattr(self, "max_plates_var"):
                value = str(len(indices))
                if self.max_plates_var.get() != value:
                    self.max_plates_var.set(value)
        finally:
            self._automation_plan_update_suspended = suspended
        self._refresh_fixture_slot_selector()

    def _fixture_slot_summary_text(self, plate_indices: tuple[int, ...] | None = None) -> str:
        indices = tuple(plate_indices if plate_indices is not None else self._fixture_slot_indices_from_vars())
        if not indices:
            return "Select at least one calibrated slot."
        return "Selected calibrated slots: " + ", ".join(f"P{index}" for index in indices) + "."

    def _refresh_fixture_slot_selector(self) -> None:
        panel = getattr(self, "_automation_plan_panel", None)
        if panel is not None and hasattr(self, "fixture_slots_label"):
            panel.render(self._automation_component_render_state().plan)
            return
        fixture_active = self._active_fixture_calibration is not None
        busy = self._automation_busy() if hasattr(self, "worker") else False
        if hasattr(self, "fixture_slots_label"):
            self._set_grid_visible(self.fixture_slots_label, fixture_active)
        if hasattr(self, "fixture_slots_frame"):
            self._set_grid_visible(self.fixture_slots_frame, fixture_active)
        if hasattr(self, "fixture_slots_status_label"):
            self._set_grid_visible(self.fixture_slots_status_label, fixture_active)
        if hasattr(self, "fixture_slots_status_var"):
            text = self._fixture_slot_summary_text() if fixture_active else ""
            if self.fixture_slots_status_var.get() != text:
                self.fixture_slots_status_var.set(text)
        if hasattr(self, "plates_to_run_entry"):
            try:
                self.plates_to_run_entry.configure(state="disabled" if fixture_active or busy else "normal")
            except tk.TclError:
                pass
        if hasattr(self, "max_plates_entry"):
            try:
                self.max_plates_entry.configure(state="disabled" if fixture_active or busy else "normal")
            except tk.TclError:
                pass
        for check in getattr(self, "fixture_slot_checkbuttons", []):
            try:
                check.configure(state="disabled" if busy else "normal")
            except tk.TclError:
                pass

    def _on_fixture_slot_selection_changed(self) -> None:
        indices = self._fixture_slot_indices_from_vars()
        if not indices:
            messagebox.showwarning("Fixture Slots", "Select at least one calibrated fixture slot.", parent=self.root)
            self._set_fixture_slot_selection(range(1, FIXTURE_PROFILE_PLATE_COUNT + 1))
            return
        self._set_fixture_slot_selection(indices)
        self._mark_automation_dirty(custom_preset=True)

    def on_fixture_profile_selected(self, _event=None):
        profile = self._selected_fixture_calibration()
        if (
            profile is not None
            and hasattr(self, "fixture_profile_name_var")
            and not self._fixture_recording_started
            and not self._fixture_calibration_points
        ):
            self.fixture_profile_name_var.set(profile.display_name)
        self._refresh_fixture_calibration_status()
        self._refresh_automation_controls()

    @staticmethod
    def _fixture_step_label(step_key: str) -> str:
        plate, well = step_key.split("_", 1)
        plate_number = plate[1:] if plate.startswith("P") else plate
        return f"Plate {plate_number} well {well}"

    def _current_fixture_step_key(self) -> str:
        index = max(0, min(self._fixture_calibration_step_index, len(FIXTURE_POINT_SEQUENCE) - 1))
        return FIXTURE_POINT_SEQUENCE[index]

    def _fixture_reference_profile_for_assisted_move(self) -> FixtureCalibration | None:
        return self._selected_fixture_calibration() or self._active_fixture_calibration or self._latest_fixture_calibration()

    def _fixture_reference_point_for_step(self, step_key: str) -> tuple[float, float] | None:
        profile = self._fixture_reference_profile_for_assisted_move()
        if profile is None:
            return None
        point = profile.measured_points.get(step_key)
        if point is not None:
            return point
        plate, well = step_key.split("_", 1)
        if well == "A1" and plate.startswith("P"):
            try:
                return profile.plate_a1_positions[int(plate[1:])]
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def _mark_fixture_calibration_map_dirty(self) -> None:
        self._mark_automation_map_dirty()
        self._schedule_automation_map_redraw()

    def _move_near_current_fixture_point_from_previous(self) -> bool:
        if not self._fixture_recording_started or len(self._fixture_calibration_points) >= len(FIXTURE_POINT_SEQUENCE):
            return False
        if self._laser_utility_busy or not self._laser_connected_for_teaching():
            return False
        step_key = self._current_fixture_step_key()
        point = self._fixture_reference_point_for_step(step_key)
        if point is None:
            return False
        x, y = float(point[0]), float(point[1])
        label = self._fixture_step_label(step_key)
        try:
            self._validate_laser_motion_points([(f"Calibration {label}", x, y)])
        except ValueError as exc:
            self.status_message_var.set(str(exc))
            try:
                messagebox.showwarning("Motion Safety", str(exc), parent=self.root)
            except tk.TclError:
                pass
            return False

        def _move():
            self._prepare_laser_controller_for_motion(f"calibration {label}")
            self.laser.move_to(x, y, feed_mm_min=max(1.0, self._float_var(self.go_to_feed_var, "go-to feed")))
            wait = getattr(self.laser, "wait_until_idle", None)
            if callable(wait):
                wait(timeout_s=60.0)
            self.laser.emergency_laser_off()
            return f"Moved near previous {label} at X{x:g} Y{y:g}. Fine-adjust, then Record Point."

        self._teach_laser_action(f"Move near {label}", _move)
        return True

    def _fixture_profile_id_from_name(self, name: str) -> str:
        safe = sanitize_filename_part(name.lower().replace(" ", "_"), fallback="fixture")
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in safe)
        if not safe:
            safe = "fixture"
        existing = {profile.id for profile in self.fixture_calibrations}
        if safe not in existing:
            return safe
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = f"{safe}_{suffix}"
        counter = 2
        while candidate in existing:
            candidate = f"{safe}_{suffix}_{counter}"
            counter += 1
        return candidate

    def _fixture_status_text(self) -> str:
        recorded = len(self._fixture_calibration_points)
        total = len(FIXTURE_POINT_SEQUENCE)
        if recorded >= total:
            return f"All {total} points recorded. Enter a name and press Save Fixture."
        if self._fixture_recording_started or recorded:
            step = self._fixture_step_label(self._current_fixture_step_key())
            manual = "" if self._fixture_reference_profile_for_assisted_move() else " (no previous profile; jog manually)"
            return f"Step {recorded + 1} of {total}: jog the laser to {step}, then press Record Point{manual}."
        active = self._active_fixture_calibration
        if active is not None:
            return f"Using latest: {active.display_name}."
        selected = self._selected_fixture_calibration()
        if selected is not None:
            if selected.source_kind == FIXTURE_CALIBRATION_SOURCE_LEGACY_SETTINGS:
                return f"Legacy selected: {selected.display_name}. Save Fixture to track it."
            return f"Selected: {selected.display_name}."
        step = self._fixture_step_label(self._current_fixture_step_key())
        return f"Select a saved fixture, or start calibration. First point: {step}."

    def _refresh_fixture_calibration_status(self) -> None:
        if hasattr(self, "fixture_calibration_status_var"):
            self.fixture_calibration_status_var.set(self._fixture_status_text())

    def _clear_fixture_recording(self) -> None:
        # Deliberately leaves the on-disk draft alone: this also runs on
        # profile apply / bed teach, and the draft must survive until the
        # profile is saved or the user declines to resume.
        self._fixture_calibration_points = {}
        self._fixture_calibration_step_index = 0
        self._fixture_recording_started = False
        self._refresh_fixture_calibration_status()
        self._mark_fixture_calibration_map_dirty()

    def _autosave_fixture_draft(self) -> None:
        """Best-effort persistence of in-progress points; never blocks recording."""
        try:
            if self._fixture_calibration_points:
                name = self.fixture_profile_name_var.get().strip() if hasattr(self, "fixture_profile_name_var") else ""
                save_fixture_point_draft(self._fixture_calibration_points, name)
            else:
                clear_fixture_point_draft()
        except Exception:
            logger.debug("Fixture draft autosave failed.", exc_info=True)

    def _fixture_action_blocked_by_busy(self) -> bool:
        if not self._automation_busy():
            return False
        messagebox.showinfo(
            "Automation Busy",
            "Wait for the current automation or laser utility action before changing fixture calibration.",
            parent=self.root,
        )
        return True

    def on_start_fixture_calibration(self):
        if self._fixture_action_blocked_by_busy():
            return
        self._clear_fixture_recording()
        draft = load_fixture_point_draft()
        if draft is not None:
            points, draft_name = draft
            if messagebox.askyesno(
                "Fixture Calibration",
                f"Resume the previous calibration in progress ({len(points)} of {len(FIXTURE_POINT_SEQUENCE)} points recorded)?",
                parent=self.root,
            ):
                self._fixture_calibration_points = dict(points)
                self._fixture_calibration_step_index = min(len(points), len(FIXTURE_POINT_SEQUENCE) - 1)
                if draft_name and hasattr(self, "fixture_profile_name_var"):
                    self.fixture_profile_name_var.set(draft_name)
            else:
                clear_fixture_point_draft()
        self._fixture_recording_started = True
        if hasattr(self, "fixture_profile_name_var") and not self.fixture_profile_name_var.get().strip():
            self.fixture_profile_name_var.set("2x2 96-well fixture")
        self.status_message_var.set("Fixture calibration started.")
        self._refresh_fixture_calibration_status()
        self._refresh_automation_controls()
        self._mark_fixture_calibration_map_dirty()
        self._move_near_current_fixture_point_from_previous()

    def on_back_fixture_calibration(self):
        if self._fixture_action_blocked_by_busy():
            return
        if not self._fixture_calibration_points:
            self._fixture_calibration_step_index = 0
        else:
            previous = FIXTURE_POINT_SEQUENCE[max(0, len(self._fixture_calibration_points) - 1)]
            self._fixture_calibration_points.pop(previous, None)
            self._fixture_calibration_step_index = max(0, len(self._fixture_calibration_points))
            self._autosave_fixture_draft()
        self._refresh_fixture_calibration_status()
        self._refresh_automation_controls()
        self._mark_fixture_calibration_map_dirty()
        self._move_near_current_fixture_point_from_previous()

    def on_record_fixture_point(self):
        if self._fixture_action_blocked_by_busy():
            return
        try:
            x, y, status = self._stable_laser_work_position(require_idle=True)
        except Exception as exc:
            next_step = self._fixture_step_label(self._current_fixture_step_key())
            messagebox.showwarning(
                "Fixture Calibration",
                f"{exc}\n\nRecorded points are kept. Use Recover/Home, then continue with {next_step}.",
                parent=self.root,
            )
            drop = getattr(self, "_drop_lost_laser_connection_if_dead", None)
            if callable(drop):
                drop()
            return
        self._fixture_recording_started = True
        if len(self._fixture_calibration_points) >= len(FIXTURE_POINT_SEQUENCE):
            self.status_message_var.set("Fixture calibration already has all required points.")
            self._refresh_fixture_calibration_status()
            return
        step_key = FIXTURE_POINT_SEQUENCE[len(self._fixture_calibration_points)]
        self._fixture_calibration_points[step_key] = (x, y)
        self._autosave_fixture_draft()
        self._refresh_laser_position(status)
        self._fixture_calibration_step_index = min(
            len(self._fixture_calibration_points),
            len(FIXTURE_POINT_SEQUENCE) - 1,
        )
        self.status_message_var.set(f"Recorded {self._fixture_step_label(step_key)} at X{x:.3f} Y{y:.3f}.")
        self._refresh_fixture_calibration_status()
        self._refresh_automation_controls()
        self._mark_fixture_calibration_map_dirty()
        self._move_near_current_fixture_point_from_previous()

    def _fixture_calibration_from_recorded_points(self) -> FixtureCalibration:
        name = self.fixture_profile_name_var.get().strip() if hasattr(self, "fixture_profile_name_var") else ""
        if not name:
            raise ValueError("Enter a fixture profile name before saving.")
        selected = self._selected_fixture_calibration()
        if (
            len(self._fixture_calibration_points) < len(FIXTURE_POINT_SEQUENCE)
            and selected is not None
            and selected.source_kind == FIXTURE_CALIBRATION_SOURCE_LEGACY_SETTINGS
            and not self._fixture_recording_started
            and not self._fixture_calibration_points
        ):
            return replace(
                selected,
                id=self._fixture_profile_id_from_name(name),
                display_name=name,
                source_filename="",
                source_path="",
                source_kind="",
            )
        profile_id = self._fixture_profile_id_from_name(name)
        return create_fixture_calibration_from_points(
            profile_id=profile_id,
            display_name=name,
            plate_model=self._selected_plate_model(),
            measured_points=dict(self._fixture_calibration_points),
        )

    def _show_fixture_validation_messages(self, profile: FixtureCalibration) -> bool:
        messages = validate_fixture_calibration(profile, self._plate_model_for_fixture_profile(profile))
        errors = [item["message"] for item in messages if item["severity"] == "error"]
        if errors:
            messagebox.showwarning("Fixture Calibration", errors[0], parent=self.root)
            return False
        warnings = [item["message"] for item in messages if item["severity"] == "warning"]
        if warnings and not messagebox.askyesno(
            "Fixture Calibration Warning",
            "\n\n".join(warnings) + "\n\nSave this fixture profile anyway?",
            parent=self.root,
        ):
            return False
        return True

    def on_save_fixture_profile(self):
        if self._fixture_action_blocked_by_busy():
            return
        try:
            profile = self._fixture_calibration_from_recorded_points()
        except Exception as exc:
            messagebox.showwarning("Fixture Calibration", str(exc), parent=self.root)
            return
        if not self._show_fixture_validation_messages(profile):
            return
        ok, message, saved_profile = save_fixture_calibration_profile(profile)
        if not ok:
            messagebox.showwarning("Fixture Calibration", message, parent=self.root)
            return
        profile = saved_profile or profile
        clear_fixture_point_draft()
        self._refresh_fixture_profile_library(selected_id=profile.id)
        self._active_fixture_calibration = profile
        self._fixture_calibration_points = {}
        self._fixture_calibration_step_index = 0
        self._fixture_recording_started = False
        self._apply_fixture_profile_to_plan_vars(profile)
        self.status_message_var.set(f"Fixture profile saved: {profile.display_name}.")
        self._refresh_fixture_calibration_status()
        self._mark_fixture_calibration_map_dirty()
        self._mark_automation_dirty(custom_preset=True)

    def _plate_model_for_fixture_profile(self, profile: FixtureCalibration):
        model_label = self._plate_label_for_model_id(profile.plate_model_id)
        if model_label:
            return self._plate_models_by_label[model_label]
        return self._selected_plate_model()

    def _apply_fixture_profile_to_plan_vars(self, profile: FixtureCalibration | None) -> bool:
        if profile is None:
            self._active_fixture_calibration = None
            self._fixture_slot_selection_profile_id = None
            self._fixture_calibration_points = {}
            self._fixture_calibration_step_index = 0
            self._fixture_recording_started = False
            if hasattr(self, "fixture_profile_var"):
                self.fixture_profile_var.set(self._fixture_no_profile_label)
            self.status_message_var.set("No fixture profile is available. Manual bed/gap planning is active.")
            self._mark_automation_dirty(custom_preset=True)
            self._refresh_fixture_calibration_status()
            self._refresh_fixture_slot_selector()
            self._refresh_automation_controls()
            return True
        model_label = self._plate_label_for_model_id(profile.plate_model_id)
        messages = validate_fixture_calibration(profile, self._plate_model_for_fixture_profile(profile))
        errors = [item["message"] for item in messages if item.get("severity") == "error"]
        if errors:
            self._active_fixture_calibration = None
            self.status_message_var.set(f"Fixture profile error: {errors[0]}")
            messagebox.showwarning("Fixture Calibration", errors[0], parent=self.root)
            self._refresh_fixture_calibration_status()
            self._refresh_automation_controls()
            return False
        self._automation_plan_update_suspended = True
        bounds_error = ""
        same_profile = self._fixture_slot_selection_profile_id == profile.id
        preserved_slots = self._fixture_slot_indices_from_vars() if same_profile else ()
        selected_slots = preserved_slots or tuple(range(1, FIXTURE_PROFILE_PLATE_COUNT + 1))
        try:
            if model_label:
                self.plate_model_var.set(model_label)
            self._active_fixture_calibration = profile
            self._fixture_slot_selection_profile_id = profile.id
            self.orientation_var.set(ORIENTATION_NORMAL)
            self._set_fixture_slot_selection(selected_slots, sync_count=True)
            self.plate_gap_x_var.set("0")
            self.plate_gap_y_var.set("0")
            temp_config = AutomationRunConfig(
                plate_model=self._plate_model_for_fixture_profile(profile),
                bed_area=BedArea(),
                fixture_calibration=profile,
                max_plates=FIXTURE_PROFILE_PLATE_COUNT,
            )
            bed = effective_bed_area(temp_config)
            self.bed_x_min_var.set(f"{bed.x_min_mm:g}")
            self.bed_x_max_var.set(f"{bed.x_max_mm:g}")
            self.bed_y_min_var.set(f"{bed.y_min_mm:g}")
            self.bed_y_max_var.set(f"{bed.y_max_mm:g}")
        except Exception as exc:
            bounds_error = str(exc)
        finally:
            self._automation_plan_update_suspended = False
        if hasattr(self, "automation_preset_var"):
            self.automation_preset_var.set(self._fixture_geometry_preset_label())
        self._refresh_fixture_profile_library(selected_id=profile.id)
        if bounds_error:
            self.status_message_var.set(f"Fixture profile active, but bounds are unavailable: {bounds_error}")
        else:
            self.status_message_var.set(f"Latest fixture active: {profile.display_name}.")
        self._mark_automation_dirty(custom_preset=False)
        self._refresh_fixture_calibration_status()
        self._refresh_fixture_slot_selector()
        self._refresh_automation_controls()
        return True

    def on_apply_fixture_profile(self):
        if self._fixture_action_blocked_by_busy():
            return
        profile = self._selected_fixture_calibration()
        if profile is not None and hasattr(self, "fixture_profile_name_var"):
            self.fixture_profile_name_var.set(profile.display_name)
        self._apply_fixture_profile_to_plan_vars(profile)

    def on_delete_fixture_profile(self):
        if self._fixture_action_blocked_by_busy():
            return
        profile = self._selected_fixture_calibration()
        if profile is None:
            messagebox.showinfo("Fixture Calibration", "Select a saved fixture profile to delete.", parent=self.root)
            return
        if not messagebox.askyesno("Delete Fixture Profile", f"Delete {profile.display_name}?", parent=self.root):
            return
        ok, message = delete_fixture_calibration(profile.id)
        if not ok:
            messagebox.showwarning("Fixture Calibration", message, parent=self.root)
            return
        if self._active_fixture_calibration and self._active_fixture_calibration.id == profile.id:
            self._active_fixture_calibration = None
        self._refresh_fixture_profile_library(select_latest_if_none=True)
        self._apply_fixture_profile_to_plan_vars(None)
        self._refresh_fixture_calibration_status()
        self._refresh_automation_controls()
