"""PUBLIC-EDITION STUB.

This feature is not included in the public edition. The interface below imports
cleanly and reports the feature as unavailable rather than failing silently.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any

logger = logging.getLogger(__name__)

MAPPING_ACQUISITION_UNAVAILABLE_REASON = (
    "Automated 2D mapping acquisition is available in the private ProLIBSpector "
    "edition; it rasters a Class-4 laser over the unpublished GRBL stage driver. "
    "Analysing existing mapping runs is fully supported in the public edition - "
    "load a mapping folder from Analysis mode."
)


@dataclass(frozen=True)
class MappingGridConfig:
    """Geometry of a rectangular mapping raster.

    A plain configuration record: no hardware is implied by holding one. It is
    kept public so mapping-vs-plate ``isinstance`` dispatch keeps working and so
    saved mapping runs can be described consistently.
    """

    x_length_mm: float = 0.0
    y_length_mm: float = 0.0
    step_mm: float = 1.0
    shots_per_point: int = 1
    experiment_name: str = "Map1"
    run_directory: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if float(self.step_mm) <= 0.0:
            raise ValueError("Mapping step size must be greater than zero.")
        if int(self.shots_per_point) < 1:
            raise ValueError("Mapping shots per point must be at least 1.")

    @property
    def columns(self) -> int:
        return max(1, int(float(self.x_length_mm) // float(self.step_mm)) + 1)

    @property
    def rows(self) -> int:
        return max(1, int(float(self.y_length_mm) // float(self.step_mm)) + 1)

    @property
    def total_points(self) -> int:
        return self.rows * self.columns

    @property
    def total_shots(self) -> int:
        return self.total_points * int(self.shots_per_point)

    @property
    def safe_experiment_name(self) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(self.experiment_name or "")).strip("._-")
        return cleaned or "Map1"

    def with_run_directory(self, run_directory: str | None) -> "MappingGridConfig":
        return replace(self, run_directory=run_directory)


@dataclass(frozen=True)
class MappingPlanDetails:
    """Summary of a planned mapping raster; always unavailable in this edition."""

    available: bool = False
    summary: str = MAPPING_ACQUISITION_UNAVAILABLE_REASON
    point_count: int = 0
    shot_count: int = 0
    targets: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=lambda: [MAPPING_ACQUISITION_UNAVAILABLE_REASON])

    def __bool__(self) -> bool:
        return self.available


def mapping_plan_details(config: Any = None, *, include_targets: bool = True) -> MappingPlanDetails:
    """Return mapping plan details; acquisition planning is private-edition only.

    The geometry counts are reported when a config is supplied - they are simple
    arithmetic and useful context - but ``available`` stays ``False`` because no
    raster can actually be executed here.
    """
    del include_targets
    if isinstance(config, MappingGridConfig):
        return MappingPlanDetails(
            point_count=config.total_points,
            shot_count=config.total_shots,
        )
    logger.info("Mapping acquisition planning requested, but it is a private-edition feature.")
    return MappingPlanDetails()


def load_mapping_resume_state_for_config(directory: Any, config: Any) -> None:
    """Return the saved mapping-run state for ``config``.

    Always ``None``: no mapping acquisition can run in the public edition, so
    there is no partial run of its own to resume.
    """
    del directory, config
    return None


__all__ = [
    "MAPPING_ACQUISITION_UNAVAILABLE_REASON",
    "MappingGridConfig",
    "MappingPlanDetails",
    "load_mapping_resume_state_for_config",
    "mapping_plan_details",
]

