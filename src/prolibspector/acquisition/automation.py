"""PUBLIC-EDITION STUB.

This feature is not included in the public edition. The interface below imports
cleanly and reports the feature as unavailable rather than failing silently.
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

