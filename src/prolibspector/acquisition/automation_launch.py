"""GUI-independent automated acquisition launch decisions."""

from __future__ import annotations

from dataclasses import dataclass

from prolibspector.acquisition.automation_ui_state import AutomationUiState


RUN_MODE_REAL = "real"
RUN_MODE_SIMULATION = "simulation"


@dataclass(frozen=True)
class AutomationDryRunLaunchPlan:
    can_start: bool
    block_reason: str
    laser_pattern_test: bool


@dataclass(frozen=True)
class AutomationStartLaunchPlan:
    ready: bool
    block_reason: str
    laser_pattern_test: bool
    simulation_run: bool
    run_mode: str
    run_config: object | None
    needs_simulation_directory: bool = False
    simulation_directory_to_remember: str = ""
    spectra_qc_enabled: bool = False


def automation_config_with_run_directory(
    config,
    run_directory: str,
) -> object:
    mapping = config.to_mapping()
    mapping["run_directory"] = run_directory
    return type(config).from_mapping(mapping)


def plan_automation_dry_run_launch(state: AutomationUiState) -> AutomationDryRunLaunchPlan:
    return AutomationDryRunLaunchPlan(
        can_start=bool(state.can_dry_run),
        block_reason="" if state.can_dry_run else state.dry_run_reason,
        laser_pattern_test=bool(state.laser_pattern_test),
    )


def plan_automation_start_launch(
    config,
    state: AutomationUiState,
    *,
    devices_simulated: bool,
    resume_available: bool = False,
    worker_config=None,
    existing_simulation_run_directory: str | None = None,
    new_simulation_run_directory: str | None = None,
    spectra_qc_selected: bool = False,
    spectra_qc_available: bool = False,
) -> AutomationStartLaunchPlan:
    laser_pattern_test = bool(state.laser_pattern_test)
    simulation_run = bool(devices_simulated and not laser_pattern_test)
    run_mode = RUN_MODE_SIMULATION if simulation_run else RUN_MODE_REAL
    qc_enabled = bool(not laser_pattern_test and spectra_qc_selected and spectra_qc_available)

    if config is None:
        return AutomationStartLaunchPlan(
            ready=False,
            block_reason="Configure an automated run plan first.",
            laser_pattern_test=laser_pattern_test,
            simulation_run=simulation_run,
            run_mode=run_mode,
            run_config=None,
            spectra_qc_enabled=False,
        )
    if not state.ready_to_run:
        return AutomationStartLaunchPlan(
            ready=False,
            block_reason=state.ready_reason,
            laser_pattern_test=laser_pattern_test,
            simulation_run=simulation_run,
            run_mode=run_mode,
            run_config=None,
            spectra_qc_enabled=False,
        )

    run_config = config
    simulation_directory_to_remember = ""
    if simulation_run:
        if resume_available and worker_config is not None and worker_config.run_directory:
            run_config = worker_config
            simulation_directory_to_remember = worker_config.run_directory
        elif resume_available and existing_simulation_run_directory:
            run_config = automation_config_with_run_directory(config, existing_simulation_run_directory)
        elif not new_simulation_run_directory:
            return AutomationStartLaunchPlan(
                ready=True,
                block_reason="",
                laser_pattern_test=laser_pattern_test,
                simulation_run=True,
                run_mode=run_mode,
                run_config=None,
                needs_simulation_directory=True,
                spectra_qc_enabled=qc_enabled,
            )
        else:
            run_config = automation_config_with_run_directory(config, new_simulation_run_directory)
            simulation_directory_to_remember = new_simulation_run_directory

    return AutomationStartLaunchPlan(
        ready=True,
        block_reason="",
        laser_pattern_test=laser_pattern_test,
        simulation_run=simulation_run,
        run_mode=run_mode,
        run_config=run_config,
        simulation_directory_to_remember=simulation_directory_to_remember,
        spectra_qc_enabled=qc_enabled,
    )
