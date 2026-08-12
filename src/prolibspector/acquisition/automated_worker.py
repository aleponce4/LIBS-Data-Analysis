"""Automated acquisition worker - private edition feature.

PUBLIC-EDITION STUB
===================
In the private ProLIBSpector edition this worker owns the automated run loop:
for every planned stage target it verifies the safety checklist, moves the GRBL
stage, arms the spectrometer's hardware trigger, fires the laser, persists the
shot, and records per-target progress so an interrupted run resumes exactly
where it stopped. It also implements the dry run (full motion, laser inhibited)
and the laser pattern test.

That loop cannot be published: it fires a Class-4 laser through the unpublished
GRBL driver (see ``hardware/grbl_laser.py``).

This module exists so ``acquisition/controllers.py`` imports cleanly. The worker
class below refuses construction with a clear message instead of starting a
thread that could never do its job, and ``prepare_grbl_controller`` refuses to
prepare a controller for motion. The public edition's acquisition view uses
``acquisition.worker.AcquisitionWorker``, which is complete and fully
functional for manual and simulated acquisition.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from prolibspector.acquisition.automation import AUTOMATION_UNAVAILABLE_REASON
from prolibspector.acquisition.automation_launch import (
    RUN_MODE_DRY_RUN,
    RUN_MODE_REAL,
    RUN_MODE_SIMULATION,
)
from prolibspector.hardware.grbl_laser import GrblLaserError

logger = logging.getLogger(__name__)

#: Preparation intents the private worker distinguishes before a stage command.
GRBL_PREP_MOTION = "motion"
GRBL_PREP_STATUS = "status"
GRBL_PREP_IDLE = "idle"


class AutomatedAcquisitionUnavailableError(RuntimeError):
    """Raised when automated acquisition is requested in the public edition."""


class AutomatedAcquisitionWorker:
    """Refuses to run: automated acquisition is a private-edition feature.

    Construction raises rather than returning an inert object, so no caller can
    mistake a silent no-op for a started run. The message names the edition and
    points at the supported alternative.
    """

    def __init__(self, spectrometer: Any = None, laser: Any = None, *_args, **_kwargs) -> None:
        del spectrometer, laser
        raise AutomatedAcquisitionUnavailableError(
            f"{AUTOMATION_UNAVAILABLE_REASON} "
            "Use Simulated Acquisition for a hardware-free live acquisition run."
        )


def prepare_grbl_controller(
    controller: Any,
    *,
    intent: str = GRBL_PREP_MOTION,
    context: str = "",
    force_home: bool = False,
    send_status: Callable[[str], None] | None = None,
) -> None:
    """Refuse to prepare a GRBL controller for automated motion.

    A simulated controller is still prepared for real, since that is pure
    software and carries no beam: it is homed when asked. Anything else raises,
    because this edition must never put a physical stage into a motion-ready
    state.
    """
    del intent
    if getattr(controller, "is_simulated", False):
        if force_home and hasattr(controller, "home"):
            status = controller.home()
            if send_status is not None:
                send_status(str(status))
        return None

    where = f" ({context})" if context else ""
    raise GrblLaserError(f"Cannot prepare the laser controller{where}: {AUTOMATION_UNAVAILABLE_REASON}")


__all__ = [
    "GRBL_PREP_IDLE",
    "GRBL_PREP_MOTION",
    "GRBL_PREP_STATUS",
    "RUN_MODE_DRY_RUN",
    "RUN_MODE_REAL",
    "RUN_MODE_SIMULATION",
    "AutomatedAcquisitionUnavailableError",
    "AutomatedAcquisitionWorker",
    "prepare_grbl_controller",
]
