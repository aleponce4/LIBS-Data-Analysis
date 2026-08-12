"""Automated laser-stage run planning - private edition feature.

PUBLIC-EDITION STUB
===================
The private ProLIBSpector edition plans automated runs across one or more plates
on a motorised laser stage: it converts plate-model geometry plus bed limits and
orientation into an ordered list of stage targets, persists per-target progress
so an interrupted run resumes on the exact next target, and enforces the safety
checklist before any target is fired. That planner is coupled to the GRBL laser
driver, which is not published (see ``hardware/grbl_laser.py``).

This module exists so ``acquisition/controllers.py`` imports cleanly. It
provides the orientation constants and honest no-op planners:
``automation_plan_details`` returns a details record that states the feature is
private, and ``load_resume_state_for_config`` returns ``None`` (no saved state
can exist, because no automated run can be performed here).

Nothing in this module pretends an automated run is possible. The public
edition's automated-acquisition view is not built, so these entry points are
reachable only through code paths the public UI never exposes; if one is ever
reached, it reports the private-edition requirement rather than failing oddly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

AUTOMATION_UNAVAILABLE_REASON = (
    "Automated laser-stage acquisition is available in the private ProLIBSpector "
    "edition. The public edition includes manual and simulated acquisition only, "
    "because automated runs drive a Class-4 laser over the unpublished GRBL driver."
)

ORIENTATION_NORMAL = "normal"
ORIENTATION_ROTATED = "rotated_90"

ORIENTATION_LABELS: dict[str, str] = {
    ORIENTATION_NORMAL: "Normal (A1 top-left)",
    ORIENTATION_ROTATED: "Rotated 90 degrees",
}


@dataclass(frozen=True)
class AutomationPlanDetails:
    """Summary of a planned automated run.

    In the public edition every plan is empty and ``available`` is ``False``;
    ``summary`` carries the reason so the UI can show it verbatim.
    """

    available: bool = False
    summary: str = AUTOMATION_UNAVAILABLE_REASON
    target_count: int = 0
    plate_count: int = 0
    targets: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=lambda: [AUTOMATION_UNAVAILABLE_REASON])

    def __bool__(self) -> bool:
        return self.available


def automation_plan_details(config: Any = None, *, include_targets: bool = True) -> AutomationPlanDetails:
    """Return plan details for ``config``; always unavailable in this edition."""
    del config, include_targets
    logger.info("Automated run planning requested, but it is a private-edition feature.")
    return AutomationPlanDetails()


def load_resume_state_for_config(directory: Any, config: Any) -> None:
    """Return the saved automated-run state for ``config``.

    Always ``None``: the public edition cannot perform an automated run, so no
    resumable state can exist. Callers already treat ``None`` as "nothing to
    resume" and surface that to the user.
    """
    del directory, config
    return None


__all__ = [
    "AUTOMATION_UNAVAILABLE_REASON",
    "ORIENTATION_LABELS",
    "ORIENTATION_NORMAL",
    "ORIENTATION_ROTATED",
    "AutomationPlanDetails",
    "automation_plan_details",
    "load_resume_state_for_config",
]
