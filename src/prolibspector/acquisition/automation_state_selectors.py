"""Pure render/gating selectors for the automated-acquisition sidebar.

Every function in this module is a pure projection: it reads an
``AutomationUiState`` snapshot plus explicit scalar inputs and returns render
values. Nothing here may touch Tk, the app object, hardware, or global state —
that is what makes the guided-flow gating logic unit-testable without a widget
tree. App-side wrappers in ``automated_app.py`` gather the inputs (Tk variable
reads, controller lookups, bound-method commands) and delegate here.
"""

from __future__ import annotations

from dataclasses import dataclass

from prolibspector.acquisition.automation_ui_state import (
    AutomationDetailsPanelState,
    AutomationPlanPanelState,
    AutomationRenderState,
    AutomationUiState,
    DevicePanelState,
    PlateSamplesPanelState,
    PlateSamplesWorkspaceState,
    RunConsolePanelState,
    SafetyPreflightPanelState,
    SpectraQCPanelState,
)

# next_action() returns one of these keys; the app maps keys to bound commands.
ACTION_START = "start"
ACTION_RESUME = "resume"
ACTION_CONNECT_SPECTROMETER = "connect_spectrometer"
ACTION_CONNECT_LASER = "connect_laser"
ACTION_APPLY_PLAN = "apply_plan"
ACTION_NAME_PLATES = "name_plates"
ACTION_APPLY_PLATE_NAMES = "apply_plate_names"
ACTION_DRY_RUN = "dry_run"


def raw_step_for_state(state: AutomationUiState) -> str:
    if state.motion_running:
        return "run"
    if not state.spectrometer_worker_ready and not state.laser_pattern_test:
        return "devices_spectrometer"
    if not state.laser_ready:
        return "devices_laser"
    if not state.has_plan:
        return "plan"
    if not state.plate_names_ready:
        return "samples"
    if not state.dry_run_ready:
        return "run"
    if not state.safety_ready:
        return "safety"
    return "run"


def sidebar_step_group(step: str) -> str:
    if step in {"devices_spectrometer", "devices_laser"}:
        return "devices"
    if step == "preflight":
        return "safety"
    if step in {"devices", "plan", "samples", "safety", "run"}:
        return step
    return "run"


def sidebar_step_available(step: str, state: AutomationUiState) -> bool:
    step = sidebar_step_group(step)
    if step == "devices":
        return True
    if step == "plan":
        return bool(state.devices_ready)
    if step == "samples":
        return bool(state.devices_ready and state.has_plan)
    if step == "safety":
        return bool(
            state.devices_ready
            and state.has_plan
            and state.plate_names_ready
            and state.dry_run_ready
        )
    if step == "run":
        return state.sidebar_step == "run"
    return False


def ready_reason(
    *,
    has_plan: bool,
    plate_name_edits_pending: bool,
    plate_names_ready: bool,
    laser_pattern_test: bool,
    spectrometer_connected: bool,
    worker_alive: bool,
    laser_ready: bool,
    device_mode: str,
    dry_run_ready: bool,
    safety_ready: bool,
    mapping_mode: bool,
    draft_plate_name_error: str | None,
    plate_name_error: str | None,
    laser_utility_busy: bool,
    has_worker: bool,
    worker_engaged: bool,
) -> tuple[bool, str]:
    if not has_plan:
        return False, "Apply a mapping plan." if mapping_mode else "Apply an automated plate plan."
    if plate_name_edits_pending:
        if draft_plate_name_error:
            return False, draft_plate_name_error
        return False, "Apply plate names."
    if not plate_names_ready:
        return False, plate_name_error or "Name every plate."
    if not laser_pattern_test and not (spectrometer_connected and has_worker):
        return False, "Connect the spectrometer."
    if not laser_pattern_test and not worker_alive:
        return False, "Reconnect the spectrometer."
    if not laser_ready:
        return False, "Connect the GRBL laser."
    if laser_utility_busy:
        return False, "Laser utility is running."
    if worker_engaged:
        return False, "Automation is busy."
    if not laser_pattern_test and device_mode == "mixed":
        return False, "Use either all simulated devices or all real devices."
    if not dry_run_ready:
        return False, "Run a dry run after the latest settings change."
    if not safety_ready:
        return False, "Complete the safety checks."
    if laser_pattern_test:
        return True, "Ready to test the full laser pattern without spectrometer trigger capture."
    return True, "Ready to run guarded firing."


def dry_run_reason(
    *,
    has_plan: bool,
    plate_name_edits_pending: bool,
    plate_names_ready: bool,
    laser_pattern_test: bool,
    spectrometer_connected: bool,
    worker_alive: bool,
    worker_idle: bool,
    laser_ready: bool,
    device_mode: str,
    mapping_mode: bool,
    draft_plate_name_error: str | None,
    plate_name_error: str | None,
    laser_utility_busy: bool,
    has_worker: bool,
) -> tuple[bool, str]:
    if not has_plan:
        return False, "Apply a mapping plan." if mapping_mode else "Apply an automated plate plan."
    if plate_name_edits_pending:
        if draft_plate_name_error:
            return False, draft_plate_name_error
        return False, "Apply plate names."
    if not plate_names_ready:
        return False, plate_name_error or "Name every plate."
    if not laser_pattern_test and not (spectrometer_connected and has_worker):
        return False, "Connect the spectrometer."
    if not laser_pattern_test and not worker_alive:
        return False, "Reconnect the spectrometer."
    if not laser_ready:
        return False, "Connect the GRBL laser."
    if laser_utility_busy:
        return False, "Laser utility is running."
    if not worker_idle:
        return False, "Automation is busy."
    if not laser_pattern_test and device_mode == "mixed":
        return False, "Use either all simulated devices or all real devices."
    if laser_pattern_test:
        return True, "Ready to dry-run the full laser pattern without spectrometer trigger capture."
    return True, "Ready to dry-run the selected motion plan."


def start_button_render_values(
    state: AutomationUiState, *, pause_pending: bool
) -> tuple[str, str, bool, str]:
    if state.motion_running:
        if pause_pending:
            return (
                "Pausing...",
                "disabled",
                False,
                "Pause requested. Waiting for the worker to reach a safe boundary.",
            )
        return (
            "Pause Run",
            "normal",
            False,
            "Automation is running. Pause will stop at the next safe boundary.",
        )
    if state.ready_to_run and state.start_label == "Run Simulation":
        message = "Ready to run a full simulated acquisition."
    elif state.ready_to_run and state.start_label == "Resume Simulation":
        message = "Ready to resume the paused simulated acquisition."
    elif state.ready_to_run and state.start_label == "Resume Run":
        message = "Ready to resume the paused run from saved progress."
    elif state.ready_to_run and state.start_label == "Run Laser Pattern Test":
        message = "Ready to test the full laser pattern without spectrometer trigger capture."
    else:
        message = state.ready_reason
    return (
        state.start_label,
        "normal" if state.ready_to_run else "disabled",
        state.start_label in {"Resume Run", "Resume Simulation"},
        message,
    )


def next_action(
    state: AutomationUiState,
    *,
    pause_pending: bool,
    laser_connecting: bool,
    draft_plate_name_error: str | None,
    plate_name_error: str | None,
) -> tuple[str, str, bool, str]:
    """Return (label, action key, enabled, message) for the guided next step."""
    if state.motion_running:
        if pause_pending:
            return (
                "Pausing...",
                ACTION_START,
                False,
                "Pause requested. Waiting for the worker to reach a safe boundary.",
            )
        return (
            "Pause Run",
            ACTION_START,
            True,
            "Automation is running. Pause will stop at the next safe boundary.",
        )

    if not state.spectrometer_connected and not state.laser_pattern_test:
        return (
            "Connect Spectrometer",
            ACTION_CONNECT_SPECTROMETER,
            not state.busy,
            "Connect the spectrometer first.",
        )
    if not state.worker_alive and not state.laser_pattern_test:
        return ("Reconnect Spectrometer", ACTION_CONNECT_SPECTROMETER, False, "Reconnect the spectrometer.")
    if not state.laser_ready:
        if laser_connecting:
            return ("Connect Laser", ACTION_CONNECT_LASER, False, "Connecting laser...")
        return ("Connect Laser", ACTION_CONNECT_LASER, not state.busy, "Connect the GRBL laser controller.")
    if not state.has_plan:
        message = "Choose grid settings and apply a mapping plan." if state.mapping_mode else "Choose run settings and apply a plate plan."
        return ("Apply Plan", ACTION_APPLY_PLAN, not state.busy, message)
    if state.plate_name_edits_pending:
        if draft_plate_name_error:
            return ("Name Plates", ACTION_NAME_PLATES, not state.busy, draft_plate_name_error)
        return (
            "Apply Plate Names",
            ACTION_APPLY_PLATE_NAMES,
            not state.busy,
            "Apply plate names to update folders.",
        )
    if not state.plate_names_ready:
        return (
            "Name Plates",
            ACTION_NAME_PLATES,
            not state.busy,
            plate_name_error or "Name every plate before dry run.",
        )
    if not state.laser_pattern_test and state.device_mode == "mixed":
        return (
            "Check Devices",
            ACTION_START,
            False,
            "Use either all simulated devices or all real devices.",
        )

    if not state.dry_run_ready:
        enabled = bool(state.can_dry_run)
        reason = state.dry_run_reason if not enabled else "Run a dry run after the latest settings change."
        return ("Dry Run", ACTION_DRY_RUN, enabled, reason)

    ready = state.ready_to_run
    reason = state.ready_reason
    label = state.start_label
    if ready and label == "Run Simulation":
        reason = "Ready to run a full simulated acquisition."
    elif ready and label == "Resume Simulation":
        reason = "Ready to resume the paused simulated acquisition."
    elif ready and label == "Resume Run":
        reason = "Ready to resume the paused run from saved progress."
    elif ready and label == "Run Laser Pattern Test":
        reason = "Ready to test the full laser pattern without spectrometer trigger capture."
    action = ACTION_RESUME if label in {"Resume Run", "Resume Simulation"} else ACTION_START
    return (label, action, ready, reason)


def run_console_steps(state: AutomationUiState) -> tuple[dict[str, bool], str]:
    statuses = {
        "devices": state.devices_ready and state.laser_ready,
        "plan": state.has_plan,
        "samples": state.plate_names_ready,
        "preflight": state.safety_ready,
        "run": state.motion_running or state.ready_to_run,
    }
    if state.motion_running:
        return statuses, "run"
    if not statuses["devices"]:
        return statuses, "devices"
    if not statuses["plan"]:
        return statuses, "plan"
    if not statuses["samples"]:
        return statuses, "samples"
    if not state.dry_run_ready:
        return statuses, "run"
    if not state.safety_ready:
        return statuses, "preflight"
    return statuses, "run"


@dataclass(frozen=True)
class SidebarInputs:
    """Tk-side scalars needed by sidebar_render_values, gathered by the app.

    ``unverified_model`` arrives pre-masked for mapping mode; the plate-name
    errors are the first error string or None, computed only when the
    corresponding pending/not-ready gate is set.
    """

    active_step: str
    plan_label: str
    has_active_plan: bool
    draft_plate_name_error: str | None
    plate_name_error: str | None
    details_expanded: bool
    unverified_model: bool
    resume_available: bool
    spectrometer_phase: str
    laser_phase: str


@dataclass(frozen=True)
class ComponentInputs:
    """App-state scalars needed by component_render_state.

    Fixture flags arrive pre-masked for mapping mode where the original code
    masked them (``fixture_active``, ``has_effective_fixture``).
    """

    sidebar: SidebarInputs
    pause_pending: bool
    laser_connecting: bool
    laser_switch_transitioning: bool
    hardware_read_active: bool
    laser_utility_busy: bool
    fixture_active: bool
    has_effective_fixture: bool
    has_selected_fixture: bool
    selected_fixture_is_repo: bool
    selected_fixture_is_legacy: bool
    fixture_recording_started: bool
    fixture_points_count: int
    fixture_points_total: int
    fixture_slots_summary: str
    grbl_probe_state: str
    grbl_unlock_state: str
    show_qc: bool
    pending_plate_edits: bool
    plate_names_locked: bool
    spectra_qc_available: bool
    can_run_plate_qc_hook: bool
    qc_csv_available: bool
    qc_pdf_available: bool
    back_available: bool
    has_resume_state: bool
    has_latest_plan_details: bool
    activity_running: bool
    precheck_passed: tuple[bool, ...]


def sidebar_render_values(state: AutomationUiState, inputs: SidebarInputs) -> dict:
    readiness_step = state.raw_step
    active_step = inputs.active_step
    if state.laser_pattern_test and state.laser_ready:
        devices_summary = "Laser connected. Spectrometer trigger disabled for pattern test."
    elif state.laser_pattern_test:
        devices_summary = "Connect the laser. Spectrometer trigger is disabled for pattern test."
    elif not state.spectrometer_worker_ready:
        devices_summary = "Spectrometer disconnected."
    elif not state.laser_ready:
        devices_summary = "Spectrometer connected. Connect the laser."
    else:
        devices_summary = "Spectrometer and laser connected."

    new_run_summary = (
        inputs.plan_label
        if inputs.has_active_plan
        else (
            "Choose grid settings and apply a mapping plan."
            if state.mapping_mode
            else "Choose run settings and apply a plate plan."
        )
    )
    if not state.has_plan:
        plate_samples_summary = "Apply a mapping plan." if state.mapping_mode else "Apply a plan before naming plates."
    elif state.mapping_mode:
        plate_samples_summary = "Mapping grid is ready."
    elif state.plate_name_edits_pending:
        plate_samples_summary = inputs.draft_plate_name_error or "Apply plate names to update folders."
    elif state.plate_names_ready:
        plate_samples_summary = "Plate names are ready."
    else:
        plate_samples_summary = inputs.plate_name_error or "Name every plate."

    if not state.dry_run_ready:
        safety_summary = "Dry run is required before safety checks."
    elif state.safety_ready:
        safety_summary = "Safety checks complete."
    else:
        safety_summary = "Complete the safety checks."
    details_label = f"Details & Tools {'v' if inputs.details_expanded else '>'}"
    unverified_model = inputs.unverified_model
    restart_available = bool(inputs.resume_available and not state.motion_running)
    content_signature = (
        devices_summary,
        new_run_summary,
        plate_samples_summary,
        safety_summary,
        details_label,
        state.has_plan,
        state.plate_names_ready,
        state.plate_name_edits_pending,
        state.safety_ready,
        state.mapping_mode,
        inputs.spectrometer_phase,
        inputs.laser_phase,
    )
    structure_signature = (
        readiness_step,
        active_step,
        active_step == "plan",
        active_step == "safety",
        state.motion_running,
        inputs.details_expanded,
        unverified_model,
        restart_available,
    )
    return {
        "active_step": active_step,
        "readiness_step": readiness_step,
        "devices_summary": devices_summary,
        "new_run_summary": new_run_summary,
        "plate_samples_summary": plate_samples_summary,
        "safety_summary": safety_summary,
        "details_label": details_label,
        "details_expanded": inputs.details_expanded,
        "unverified_model": unverified_model,
        "restart_available": restart_available,
        "content_signature": content_signature,
        "structure_signature": structure_signature,
    }


def component_render_state(state: AutomationUiState, inputs: ComponentInputs) -> AutomationRenderState:
    sidebar = sidebar_render_values(state, inputs.sidebar)
    plan_state = "disabled" if state.busy else "normal"
    combo_state = "disabled" if state.busy else "readonly"
    laser_input_state = "disabled" if state.laser_ready or inputs.laser_connecting or state.busy else "normal"
    teach_state = "normal" if state.laser_ready and not state.busy else "disabled"
    safety_widget_state = "disabled" if state.busy else "normal"
    laser_switch_state = (
        "disabled" if inputs.laser_switch_transitioning or state.busy else "normal"
    )
    simulate_laser_state = "disabled" if state.laser_ready or inputs.laser_connecting or state.busy else "normal"
    spectrometer_preview_running = bool(
        state.worker_alive
        and state.spectrometer_connected
        and not state.motion_running
        and (
            state.worker_state in {"LIVE", "ARMED", "TEST"}
            or inputs.hardware_read_active
        )
    )
    spectrometer_live_state = (
        "normal"
        if state.spectrometer_connected
        and state.worker_alive
        and state.worker_state == "IDLE"
        and not state.busy
        else "disabled"
    )
    spectrometer_stop_state = "normal" if spectrometer_preview_running else "disabled"
    apply_fixture_text = "Use Selected" if inputs.has_selected_fixture else "Clear Fixture"
    can_record_fixture_point = (
        state.laser_ready
        and not state.busy
        and inputs.fixture_recording_started
        and inputs.fixture_points_count < inputs.fixture_points_total
    )
    can_back_fixture_point = bool(
        not state.busy and (inputs.fixture_recording_started or inputs.fixture_points_count)
    )
    calibration_stop_state = "normal" if inputs.laser_utility_busy and state.laser_ready else "disabled"
    can_save_fixture = bool(
        not state.busy
        and (
            inputs.fixture_points_count == inputs.fixture_points_total
            or (
                inputs.selected_fixture_is_legacy
                and not inputs.fixture_recording_started
                and not inputs.fixture_points_count
            )
        )
    )
    start_text, start_state, start_uses_resume, ready_message = start_button_render_values(
        state, pause_pending=inputs.pause_pending
    )
    label, _action, enabled, message = next_action(
        state,
        pause_pending=inputs.pause_pending,
        laser_connecting=inputs.laser_connecting,
        draft_plate_name_error=inputs.sidebar.draft_plate_name_error,
        plate_name_error=inputs.sidebar.plate_name_error,
    )
    statuses, current_step = run_console_steps(state)
    active_step = sidebar["active_step"]
    plate_apply_due = bool(
        inputs.pending_plate_edits
        and not inputs.sidebar.draft_plate_name_error
        and not state.busy
        and not inputs.plate_names_locked
    )
    can_rerun_qc = inputs.can_run_plate_qc_hook and not state.busy and inputs.spectra_qc_available
    return AutomationRenderState(
        ui=state,
        active_step=active_step,
        content_signature=sidebar["content_signature"],
        structure_signature=sidebar["structure_signature"],
        anchor_step=active_step,
        device=DevicePanelState(
            summary=sidebar["devices_summary"],
            show_summary=True,
            spectrometer_phase=inputs.sidebar.spectrometer_phase,
            laser_phase=inputs.sidebar.laser_phase,
            spectrometer_live_state=spectrometer_live_state,
            spectrometer_stop_state=spectrometer_stop_state,
            laser_input_state=laser_input_state,
            teach_state=teach_state,
            laser_switch_state=laser_switch_state,
            simulate_laser_state=simulate_laser_state,
            grbl_probe_state=inputs.grbl_probe_state,
            grbl_unlock_state=inputs.grbl_unlock_state,
        ),
        plan=AutomationPlanPanelState(
            summary=sidebar["new_run_summary"],
            show_summary=active_step != "plan",
            detail_visible=active_step == "plan",
            status_visible=active_step == "plan",
            entry_state=plan_state,
            combo_state=combo_state,
            spectra_qc_state=(
                "normal"
                if inputs.spectra_qc_available and not state.busy and not state.mapping_mode
                else "disabled"
            ),
            fixture_slots_visible=inputs.fixture_active,
            fixture_slots_state="disabled" if state.busy else "normal",
            fixture_slots_summary=inputs.fixture_slots_summary,
            apply_plan_state=plan_state,
            browse_state=plan_state,
            mapping_mode=state.mapping_mode,
            mapping_fields_visible=state.mapping_mode,
            plate_fields_visible=not state.mapping_mode,
            run_type_state=combo_state,
        ),
        details=AutomationDetailsPanelState(
            label=sidebar["details_label"],
            body_visible=sidebar["details_expanded"],
            mapping_mode=state.mapping_mode,
            entry_state=plan_state,
            combo_state=combo_state,
            button_state=plan_state,
            geometry_locked=inputs.has_effective_fixture,
            teach_state=teach_state,
            prepare_simulation_state="normal" if not state.busy else "disabled",
            resume_state=(
                "normal"
                if (
                    inputs.has_resume_state
                    and state.has_plan
                    and not state.busy
                    and state.worker_alive
                    and state.plate_names_ready
                )
                else "disabled"
            ),
            export_state=(
                "normal"
                if inputs.has_latest_plan_details and state.plate_names_ready and not state.busy
                else "disabled"
            ),
            apply_fixture_text=apply_fixture_text,
            apply_fixture_state=(
                "normal"
                if not state.mapping_mode
                and not state.busy
                and (inputs.has_selected_fixture or inputs.has_effective_fixture)
                else "disabled"
            ),
            delete_fixture_state=(
                "normal"
                if not state.mapping_mode
                and not state.busy
                and inputs.selected_fixture_is_repo
                else "disabled"
            ),
            record_fixture_point_state="normal" if not state.mapping_mode and can_record_fixture_point else "disabled",
            back_fixture_point_state="normal" if not state.mapping_mode and can_back_fixture_point else "disabled",
            calibration_stop_state=calibration_stop_state if not state.mapping_mode else "disabled",
            save_fixture_state="normal" if not state.mapping_mode and can_save_fixture else "disabled",
        ),
        safety=SafetyPreflightPanelState(
            summary=sidebar["safety_summary"],
            show_summary=active_step != "safety",
            detail_visible=active_step == "safety",
            safety_widget_state=safety_widget_state,
            dry_run_state="normal" if state.can_dry_run else "disabled",
            start_text=start_text,
            start_state=start_state,
            start_uses_resume=start_uses_resume,
            restart_visible=sidebar["restart_available"],
            restart_state="normal" if sidebar["restart_available"] and state.ready_to_run else "disabled",
            ready_message=ready_message,
            precheck_passed=inputs.precheck_passed,
            unverified_model_ack_visible=bool(active_step == "safety" and sidebar["unverified_model"]),
        ),
        run_console=RunConsolePanelState(
            primary_text=label,
            primary_state="normal" if enabled else "disabled",
            status_message=message,
            activity_running=inputs.activity_running,
            restart_visible=sidebar["restart_available"],
            restart_state="normal" if sidebar["restart_available"] and enabled else "disabled",
            resume_visible=sidebar["restart_available"],
            resume_state="normal" if sidebar["restart_available"] and not state.busy else "disabled",
            simulation_visible=False,
            step_statuses=statuses,
            current_step=current_step,
            back_state="normal" if inputs.back_available else "disabled",
        ),
        plate_samples=PlateSamplesPanelState(
            summary=sidebar["plate_samples_summary"],
            show_name_button=active_step == "samples",
        ),
        plate_workspace=PlateSamplesWorkspaceState(
            has_plan=state.has_plan,
            show_qc=inputs.show_qc,
            plate_name_entry_state="disabled" if state.busy or inputs.plate_names_locked else "normal",
            plate_name_apply_state="normal" if plate_apply_due else "disabled",
            plate_name_apply_style="Accent.TButton" if plate_apply_due else "Sidebar.TButton",
        ),
        spectra_qc=SpectraQCPanelState(
            run_state="normal" if can_rerun_qc else "disabled",
            open_csv_state="normal" if inputs.qc_csv_available else "disabled",
            open_report_state="normal" if inputs.qc_pdf_available else "disabled",
        ),
    )


def grbl_recovery_states(
    state: AutomationUiState,
    *,
    laser_connecting: bool,
    last_probe,
    selected_port_baud: tuple[str, int] | None,
) -> tuple[str, str]:
    can_probe = bool(not state.laser_ready and not laser_connecting and not state.busy)
    unlock_ready = False
    if (
        can_probe
        and last_probe is not None
        and last_probe.alarm_active
        and selected_port_baud is not None
    ):
        port, baudrate = selected_port_baud
        try:
            unlock_ready = str(port) == str(last_probe.port) and int(baudrate) == int(last_probe.baudrate)
        except ValueError:
            unlock_ready = False
    return (
        "normal" if can_probe else "disabled",
        "normal" if unlock_ready else "disabled",
    )
