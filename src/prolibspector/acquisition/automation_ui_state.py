"""Guided-run readiness state for automated acquisition UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AutomationUiState:
    run_type: str
    mapping_mode: bool
    worker_state: str
    worker_alive: bool
    worker_idle: bool
    motion_running: bool
    busy: bool
    laser_ready: bool
    spectrometer_connected: bool
    spectrometer_worker_ready: bool
    laser_pattern_test: bool
    has_plan: bool
    plate_name_edits_pending: bool
    plate_names_ready: bool
    devices_ready: bool
    worker_available: bool
    dry_run_ready: bool
    safety_ready: bool
    ready_to_run: bool
    ready_reason: str
    raw_step: str
    sidebar_step: str
    start_label: str
    can_dry_run: bool
    dry_run_reason: str
    device_mode: str


@dataclass(frozen=True)
class DevicePanelState:
    summary: str
    show_summary: bool
    spectrometer_phase: str
    laser_phase: str
    spectrometer_live_state: str
    spectrometer_stop_state: str
    laser_input_state: str
    teach_state: str
    laser_switch_state: str
    simulate_laser_state: str
    grbl_probe_state: str
    grbl_unlock_state: str


@dataclass(frozen=True)
class AutomationPlanPanelState:
    summary: str
    show_summary: bool
    detail_visible: bool
    status_visible: bool
    entry_state: str
    combo_state: str
    spectra_qc_state: str
    fixture_slots_visible: bool
    fixture_slots_state: str
    fixture_slots_summary: str
    apply_plan_state: str
    browse_state: str
    mapping_mode: bool
    mapping_fields_visible: bool
    plate_fields_visible: bool
    run_type_state: str


@dataclass(frozen=True)
class AutomationDetailsPanelState:
    label: str
    body_visible: bool
    mapping_mode: bool
    entry_state: str
    combo_state: str
    button_state: str
    geometry_locked: bool
    teach_state: str
    prepare_simulation_state: str
    resume_state: str
    export_state: str
    apply_fixture_text: str
    apply_fixture_state: str
    delete_fixture_state: str
    record_fixture_point_state: str
    back_fixture_point_state: str
    calibration_stop_state: str
    save_fixture_state: str


@dataclass(frozen=True)
class SafetyPreflightPanelState:
    summary: str
    show_summary: bool
    detail_visible: bool
    safety_widget_state: str
    dry_run_state: str
    start_text: str
    start_state: str
    start_uses_resume: bool
    restart_visible: bool
    restart_state: str
    ready_message: str
    precheck_passed: tuple[bool, ...]
    unverified_model_ack_visible: bool


@dataclass(frozen=True)
class RunConsolePanelState:
    primary_text: str
    primary_state: str
    status_message: str
    activity_running: bool
    restart_visible: bool
    restart_state: str
    resume_visible: bool
    resume_state: str
    simulation_visible: bool
    step_statuses: dict[str, bool]
    current_step: str
    back_state: str


@dataclass(frozen=True)
class PlateSamplesPanelState:
    summary: str
    show_name_button: bool


@dataclass(frozen=True)
class PlateSamplesWorkspaceState:
    has_plan: bool
    show_qc: bool
    plate_name_entry_state: str
    plate_name_apply_state: str
    plate_name_apply_style: str


@dataclass(frozen=True)
class SpectraQCPanelState:
    run_state: str
    open_csv_state: str
    open_report_state: str


@dataclass(frozen=True)
class AutomationRenderState:
    ui: AutomationUiState
    active_step: str
    content_signature: tuple[Any, ...]
    structure_signature: tuple[Any, ...]
    anchor_step: str
    device: DevicePanelState
    plan: AutomationPlanPanelState
    details: AutomationDetailsPanelState
    safety: SafetyPreflightPanelState
    run_console: RunConsolePanelState
    plate_samples: PlateSamplesPanelState
    plate_workspace: PlateSamplesWorkspaceState
    spectra_qc: SpectraQCPanelState
