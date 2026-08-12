"""PUBLIC-EDITION STUB.

This feature is not included in the public edition. The interface below imports
cleanly and reports the feature as unavailable rather than failing silently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from prolibspector.acquisition.automation import AUTOMATION_UNAVAILABLE_REASON

logger = logging.getLogger(__name__)

RUN_MODE_REAL = "real"
RUN_MODE_DRY_RUN = "dry_run"
RUN_MODE_SIMULATION = "simulation"


@dataclass(frozen=True)
class AutomationLaunchPlan:
    """Decision record for one launch attempt.

    Field names are the contract with ``AutomatedAcquisitionController``; a
    blocked plan simply has every capability flag false.
    """

    ready: bool = False
    can_start: bool = False
    block_reason: str = AUTOMATION_UNAVAILABLE_REASON
    run_mode: str = RUN_MODE_DRY_RUN
    run_config: Any = None
    simulation_run: bool = False
    laser_pattern_test: bool = False
    needs_simulation_directory: bool = False
    simulation_directory_to_remember: str | None = None
    spectra_qc_enabled: bool = False


def plan_automation_dry_run_launch(state: Any = None) -> AutomationLaunchPlan:
    """Return the dry-run launch decision; always blocked in this edition."""
    del state
    logger.info("Automated dry run requested, but it is a private-edition feature.")
    return AutomationLaunchPlan(run_mode=RUN_MODE_DRY_RUN)


def plan_automation_start_launch(
    config: Any = None,
    state: Any = None,
    *,
    devices_simulated: bool = False,
    resume_available: bool = False,
    worker_config: Any = None,
    existing_simulation_run_directory: str | None = None,
    new_simulation_run_directory: str | None = None,
    spectra_qc_selected: bool = False,
    spectra_qc_available: bool = False,
) -> AutomationLaunchPlan:
    """Return the firing-run launch decision; always blocked in this edition."""
    del (
        config,
        state,
        devices_simulated,
        resume_available,
        worker_config,
        existing_simulation_run_directory,
        new_simulation_run_directory,
        spectra_qc_selected,
        spectra_qc_available,
    )
    logger.info("Automated firing run requested, but it is a private-edition feature.")
    return AutomationLaunchPlan(run_mode=RUN_MODE_REAL)


def automation_config_with_run_directory(config: Any, run_directory: str | None) -> Any:
    """Return ``config`` bound to ``run_directory``.

    This is pure data plumbing, so it does the real thing when it can: it uses a
    ``with_run_directory`` method or dataclass ``replace`` when the config
    supports it, and otherwise returns the config unchanged.
    """
    if config is None:
        return None
    binder = getattr(config, "with_run_directory", None)
    if callable(binder):
        return binder(run_directory)
    try:
        return replace(config, run_directory=run_directory)
    except TypeError:
        logger.debug("Config %r cannot carry a run directory; returning it unchanged.", type(config).__name__)
        return config


__all__ = [
    "RUN_MODE_DRY_RUN",
    "RUN_MODE_REAL",
    "RUN_MODE_SIMULATION",
    "AutomationLaunchPlan",
    "automation_config_with_run_directory",
    "plan_automation_dry_run_launch",
    "plan_automation_start_launch",
]

