"""Automated acquisition planning and persisted run state."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from prolibspector.acquisition.plate_autosave import (
    ORDER_COLUMN,
    ORDER_ROW,
    ordered_wells,
    sanitize_filename_part,
)
from prolibspector.acquisition.plate_models import DEFAULT_96_PLATE_MODEL_ID, PlateModel, plate_model_by_id
from prolibspector.core.paths import settings_dir, working_path
from prolibspector.core.settings import get_default_settings, load_settings, save_settings, ui_prefs_enabled


AUTOMATION_STATE_FILENAME = "_automated_acquisition_state.json"
AUTOMATION_MANIFEST_FILENAME = "_automated_acquisition_manifest.json"
AUTOMATION_SUMMARY_FILENAME = "_automated_acquisition_summary.json"
AUTOMATION_RUN_METADATA_FILENAME = "run_metadata.txt"
AUTOMATION_SETTINGS_SECTION = "automated_acquisition"
AUTOMATION_PRESETS_KEY = "automation_presets"
FIXTURE_CALIBRATIONS_KEY = "fixture_calibrations"
FIXTURE_CALIBRATION_SCHEMA_VERSION = 1
FIXTURE_CALIBRATION_LIBRARY_PARTS = ("qc_calibrations", "fixtures", "2x2_96well")
FIXTURE_CALIBRATION_SOURCE_REPO = "repo"
FIXTURE_CALIBRATION_SOURCE_LEGACY_SETTINGS = "legacy_settings"
DEFAULT_AUTOMATION_PRESET_ID = "monport_ndyag_relay_96_single"
CUSTOM_AUTOMATION_PRESET_ID = "custom"
INTEGRATION_SWEEP_PRESET_ID = "monport_ndyag_relay_96_integration_sweep"
INTRA_WELL_DENSE_PRESET_ID = "monport_ndyag_relay_96_intrawell_dense"
INTEGRATION_SWEEP_COLUMN_TIMES_MS = (
    0.05,
    0.10,
    0.18,
    0.33,
    0.62,
    1.20,
    2.20,
    4.00,
    7.50,
    14.00,
    27.00,
    50.00,
)
DELAY_SWEEP_PRESET_ID = "ysm8111_relay_96_delay_sweep"
# Draft trigger-delay schedule (µs) for delay-capable spectrometers such as
# the YSM-8111-06-01. Log-spaced from gate-open through late plasma decay;
# revisit once the replacement SDK documents the real delay range/steps.
DELAY_SWEEP_COLUMN_DELAYS_US = (
    0.0,
    0.25,
    0.5,
    1.0,
    2.0,
    4.0,
    8.0,
    16.0,
    32.0,
    64.0,
    128.0,
    256.0,
)
PLATE_JOINT_SWEEP_PRESET_ID = "ysm8111_relay_96x4_delay_integration_sweep"
# Pared-down joint sweep for 96-well plates run four at a time: delay varies
# by well column (12 values above), integration time varies by plate. The
# 9-µs point is the published YSM-8111 minimum integration; the rest are
# log-spaced through "well past plasma death" so the dark-noise penalty of
# over-integrating is measurable.
PLATE_JOINT_SWEEP_INTEGRATION_TIMES_MS = (0.009, 0.04, 0.2, 1.0)
PLAN_DETAILS_VERSION = 1
MAX_LASER_PULSE_MS = 250.0
LASER_TUBE_LIFE_POWER_WARNING_PERCENT = 70.0
LASER_PROFILE_GENERIC_GRBL = "generic_grbl"
LASER_PROFILE_MONPORT_NDYAG_RELAY = "monport_ndyag_relay"
LASER_PROFILES = {
    LASER_PROFILE_GENERIC_GRBL,
    LASER_PROFILE_MONPORT_NDYAG_RELAY,
}
DEFAULT_RELAY_STROKE_MM = 0.20
ORIENTATION_NORMAL = "normal"
ORIENTATION_ROTATED_90 = "rotated_90"
ORIENTATION_ROTATED_180 = "rotated_180"
ORIENTATION_ROTATED_270 = "rotated_270"
ORIENTATIONS = {
    ORIENTATION_NORMAL,
    ORIENTATION_ROTATED_90,
    ORIENTATION_ROTATED_180,
    ORIENTATION_ROTATED_270,
}
FIXTURE_POINT_SEQUENCE = ("P1_A1", "P1_A12", "P1_H1", "P2_A1", "P3_A1", "P4_A1")
FIXTURE_PROFILE_PLATE_COUNT = 4
FIXTURE_PITCH_WARNING_MM = 0.25
FIXTURE_PITCH_ERROR_MM = 1.0
FIXTURE_SQUARE_WARNING_DEG = 1.5
FIXTURE_SQUARE_MIN_DEG = 75.0
FIXTURE_SQUARE_MAX_DEG = 105.0
FIXTURE_P4_WARNING_MM = 2.0
MOTION_SAFETY_LIMITS_KEY = "motion_safety_limits"
DEFAULT_MOTION_SAFETY_X_MIN_MM = 0.0
DEFAULT_MOTION_SAFETY_X_MAX_MM = 220.0
DEFAULT_MOTION_SAFETY_Y_MIN_MM = 0.0
DEFAULT_MOTION_SAFETY_Y_MAX_MM = 290.0


def normalized_column_delay_schedule(values) -> tuple[float, ...]:
    """Normalize a per-column trigger-delay schedule (µs).

    Unlike integration times, a delay of exactly zero is a valid and
    important sweep point (gate opens with the trigger edge).
    """
    delays: list[float] = []
    for raw_value in values or ():
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if value >= 0 and math.isfinite(value):
            delays.append(value)
    return tuple(delays)


def normalized_integration_schedule(values) -> tuple[float, ...]:
    """Normalize an integration-time sweep schedule (ms, strictly positive)."""
    times: list[float] = []
    for raw_value in values or ():
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if value > 0 and math.isfinite(value):
            times.append(value)
    return tuple(times)


@dataclass(frozen=True)
class MotionSafetyLimits:
    enabled: bool = True
    x_min_mm: float = DEFAULT_MOTION_SAFETY_X_MIN_MM
    x_max_mm: float = DEFAULT_MOTION_SAFETY_X_MAX_MM
    y_min_mm: float = DEFAULT_MOTION_SAFETY_Y_MIN_MM
    y_max_mm: float = DEFAULT_MOTION_SAFETY_Y_MAX_MM

    @classmethod
    def from_mapping(cls, values: Any) -> "MotionSafetyLimits":
        if not isinstance(values, dict):
            return cls()

        def _bool_value(key: str, default: bool) -> bool:
            value = values.get(key, default)
            if isinstance(value, str):
                return value.strip().lower() not in {"0", "false", "no", "off"}
            return bool(value)

        def _float_value(key: str, default: float) -> float:
            try:
                return float(values.get(key, default))
            except (TypeError, ValueError):
                return float(default)

        return cls(
            enabled=_bool_value("enabled", True),
            x_min_mm=_float_value("x_min_mm", DEFAULT_MOTION_SAFETY_X_MIN_MM),
            x_max_mm=_float_value("x_max_mm", DEFAULT_MOTION_SAFETY_X_MAX_MM),
            y_min_mm=_float_value("y_min_mm", DEFAULT_MOTION_SAFETY_Y_MIN_MM),
            y_max_mm=_float_value("y_max_mm", DEFAULT_MOTION_SAFETY_Y_MAX_MM),
        ).normalized()

    def normalized(self) -> "MotionSafetyLimits":
        x_min = min(float(self.x_min_mm), float(self.x_max_mm))
        x_max = max(float(self.x_min_mm), float(self.x_max_mm))
        y_min = min(float(self.y_min_mm), float(self.y_max_mm))
        y_max = max(float(self.y_min_mm), float(self.y_max_mm))
        return MotionSafetyLimits(
            enabled=bool(self.enabled),
            x_min_mm=x_min,
            x_max_mm=x_max,
            y_min_mm=y_min,
            y_max_mm=y_max,
        )


def load_motion_safety_limits(settings: dict[str, Any] | None = None) -> MotionSafetyLimits:
    settings = settings if settings is not None else (load_settings() or {})
    section = settings.get(AUTOMATION_SETTINGS_SECTION, {}) if isinstance(settings, dict) else {}
    return MotionSafetyLimits.from_mapping(section.get(MOTION_SAFETY_LIMITS_KEY, {}))


def save_motion_safety_limits(limits: MotionSafetyLimits) -> tuple[bool, str]:
    settings = load_settings() or get_default_settings()
    section = settings.setdefault(AUTOMATION_SETTINGS_SECTION, {})
    if not isinstance(section, dict):
        section = {}
        settings[AUTOMATION_SETTINGS_SECTION] = section
    normalized = limits.normalized()
    section[MOTION_SAFETY_LIMITS_KEY] = {
        "enabled": normalized.enabled,
        "x_min_mm": normalized.x_min_mm,
        "x_max_mm": normalized.x_max_mm,
        "y_min_mm": normalized.y_min_mm,
        "y_max_mm": normalized.y_max_mm,
    }
    return save_settings(settings)


def effective_motion_safety_limits(
    limits: MotionSafetyLimits | None = None,
    *,
    grbl_settings: dict[str, Any] | None = None,
) -> MotionSafetyLimits:
    limits = (limits or load_motion_safety_limits()).normalized()
    if not limits.enabled:
        return limits
    x_max = limits.x_max_mm
    y_max = limits.y_max_mm
    if isinstance(grbl_settings, dict):
        try:
            if grbl_settings.get("130") is not None:
                x_max = min(x_max, float(grbl_settings["130"]))
        except (TypeError, ValueError):
            pass
        try:
            if grbl_settings.get("131") is not None:
                y_max = min(y_max, float(grbl_settings["131"]))
        except (TypeError, ValueError):
            pass
    return MotionSafetyLimits(
        enabled=limits.enabled,
        x_min_mm=limits.x_min_mm,
        x_max_mm=x_max,
        y_min_mm=limits.y_min_mm,
        y_max_mm=y_max,
    ).normalized()


def motion_safety_error(
    x_mm: float,
    y_mm: float,
    *,
    label: str = "Move",
    limits: MotionSafetyLimits | None = None,
    grbl_settings: dict[str, Any] | None = None,
) -> str | None:
    active = effective_motion_safety_limits(limits, grbl_settings=grbl_settings)
    if not active.enabled:
        return None
    x = float(x_mm)
    y = float(y_mm)
    prefix = str(label or "Move")
    if x < active.x_min_mm:
        return f"{prefix} blocked: X{x:g} is below safety limit X{active.x_min_mm:g} mm."
    if x > active.x_max_mm:
        return f"{prefix} blocked: X{x:g} exceeds safety limit X{active.x_max_mm:g} mm."
    if y < active.y_min_mm:
        return f"{prefix} blocked: Y{y:g} is below safety limit Y{active.y_min_mm:g} mm."
    if y > active.y_max_mm:
        return f"{prefix} blocked: Y{y:g} exceeds safety limit Y{active.y_max_mm:g} mm."
    return None


def validate_motion_safety_points(
    points: list[tuple[str, float, float]] | tuple[tuple[str, float, float], ...],
    *,
    limits: MotionSafetyLimits | None = None,
    grbl_settings: dict[str, Any] | None = None,
) -> None:
    for label, x_mm, y_mm in points:
        error = motion_safety_error(
            float(x_mm),
            float(y_mm),
            label=label,
            limits=limits,
            grbl_settings=grbl_settings,
        )
        if error:
            raise ValueError(error)


def _point_mapping(point: tuple[float, float]) -> dict[str, float]:
    return {"x_mm": float(point[0]), "y_mm": float(point[1])}


def _point_from_mapping(values: Any) -> tuple[float, float]:
    if isinstance(values, dict):
        return float(values["x_mm"]), float(values["y_mm"])
    if isinstance(values, (list, tuple)) and len(values) >= 2:
        return float(values[0]), float(values[1])
    raise ValueError("Point must contain X and Y coordinates.")


def _is_finite_point(point: tuple[float, float]) -> bool:
    return math.isfinite(float(point[0])) and math.isfinite(float(point[1]))


def _vector_between(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    divisor: float = 1.0,
) -> tuple[float, float]:
    scale = float(divisor)
    if abs(scale) < 1e-12:
        raise ValueError("Calibration divisor must be non-zero.")
    return (float(end[0] - start[0]) / scale, float(end[1] - start[1]) / scale)


def _vector_length(vector: tuple[float, float]) -> float:
    return math.hypot(float(vector[0]), float(vector[1]))


def _vector_angle_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    len_a = _vector_length(a)
    len_b = _vector_length(b)
    if len_a <= 0 or len_b <= 0:
        raise ValueError("Calibration vectors must be non-zero.")
    ratio = ((a[0] * b[0]) + (a[1] * b[1])) / (len_a * len_b)
    ratio = max(-1.0, min(1.0, ratio))
    return math.degrees(math.acos(ratio))


@dataclass(frozen=True)
class FixtureCalibration:
    id: str
    display_name: str
    plate_model_id: str
    plate_a1_positions: dict[int, tuple[float, float]]
    column_vector_mm: tuple[float, float]
    row_vector_mm: tuple[float, float]
    measured_points: dict[str, tuple[float, float]] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    schema_version: int = FIXTURE_CALIBRATION_SCHEMA_VERSION
    source_filename: str = ""
    source_path: str = ""
    source_kind: str = ""

    def __post_init__(self):
        object.__setattr__(self, "id", str(self.id).strip())
        object.__setattr__(self, "display_name", str(self.display_name or self.id).strip() or self.id)
        object.__setattr__(self, "plate_model_id", str(self.plate_model_id).strip())
        object.__setattr__(
            self,
            "plate_a1_positions",
            {
                int(index): (float(point[0]), float(point[1]))
                for index, point in dict(self.plate_a1_positions).items()
            },
        )
        object.__setattr__(
            self,
            "column_vector_mm",
            (float(self.column_vector_mm[0]), float(self.column_vector_mm[1])),
        )
        object.__setattr__(
            self,
            "row_vector_mm",
            (float(self.row_vector_mm[0]), float(self.row_vector_mm[1])),
        )
        object.__setattr__(
            self,
            "measured_points",
            {
                str(key): (float(point[0]), float(point[1]))
                for key, point in dict(self.measured_points or {}).items()
            },
        )
        try:
            schema_version = int(self.schema_version)
        except (TypeError, ValueError):
            schema_version = FIXTURE_CALIBRATION_SCHEMA_VERSION
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "source_filename", str(self.source_filename or "").strip())
        object.__setattr__(self, "source_path", str(self.source_path or "").strip())
        object.__setattr__(self, "source_kind", str(self.source_kind or "").strip())

    def to_mapping(self) -> dict[str, Any]:
        values = {
            "schema_version": self.schema_version,
            "id": self.id,
            "display_name": self.display_name,
            "plate_model_id": self.plate_model_id,
            "plate_a1_positions": {
                str(index): _point_mapping(point)
                for index, point in sorted(self.plate_a1_positions.items())
            },
            "column_vector_mm": _point_mapping(self.column_vector_mm),
            "row_vector_mm": _point_mapping(self.row_vector_mm),
            "measured_points": {
                key: _point_mapping(point)
                for key, point in sorted(self.measured_points.items())
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.source_filename:
            values["source_filename"] = self.source_filename
        if self.source_path:
            values["source_path"] = self.source_path
        if self.source_kind:
            values["source_kind"] = self.source_kind
        return values

    def signature_mapping(self) -> dict[str, Any]:
        return {
            "plate_model_id": self.plate_model_id,
            "plate_a1_positions": {
                str(index): _point_mapping(point)
                for index, point in sorted(self.plate_a1_positions.items())
            },
            "column_vector_mm": _point_mapping(self.column_vector_mm),
            "row_vector_mm": _point_mapping(self.row_vector_mm),
        }

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "FixtureCalibration":
        raw_a1 = values.get("plate_a1_positions", {})
        if not isinstance(raw_a1, dict):
            raw_a1 = {}
        raw_points = values.get("measured_points", {})
        if not isinstance(raw_points, dict):
            raw_points = {}
        return cls(
            id=str(values["id"]).strip(),
            display_name=str(values.get("display_name") or values["id"]).strip(),
            plate_model_id=str(values.get("plate_model_id", DEFAULT_96_PLATE_MODEL_ID)).strip(),
            plate_a1_positions={
                int(index): _point_from_mapping(point)
                for index, point in raw_a1.items()
            },
            column_vector_mm=_point_from_mapping(values["column_vector_mm"]),
            row_vector_mm=_point_from_mapping(values["row_vector_mm"]),
            measured_points={
                str(key): _point_from_mapping(point)
                for key, point in raw_points.items()
            },
            created_at=str(values.get("created_at", "") or ""),
            updated_at=str(values.get("updated_at", "") or ""),
            schema_version=int(values.get("schema_version", FIXTURE_CALIBRATION_SCHEMA_VERSION) or FIXTURE_CALIBRATION_SCHEMA_VERSION),
            source_filename=str(values.get("source_filename", "") or ""),
            source_path=str(values.get("source_path", "") or ""),
            source_kind=str(values.get("source_kind", "") or ""),
        )

    def well_xy(self, plate_index: int, well: str) -> tuple[float, float]:
        a1 = self.plate_a1_positions[int(plate_index)]
        row = ord(str(well)[0].upper()) - 65
        column = int(str(well)[1:]) - 1
        return (
            a1[0] + (column * self.column_vector_mm[0]) + (row * self.row_vector_mm[0]),
            a1[1] + (column * self.column_vector_mm[1]) + (row * self.row_vector_mm[1]),
        )

    def plate_axis_vectors(self, model: PlateModel) -> tuple[tuple[float, float], tuple[float, float]]:
        if (
            not math.isfinite(model.pitch_x_mm)
            or not math.isfinite(model.pitch_y_mm)
            or model.pitch_x_mm <= 0
            or model.pitch_y_mm <= 0
        ):
            raise ValueError("Plate pitch must be positive for fixture calibration.")
        return (
            (self.column_vector_mm[0] / model.pitch_x_mm, self.column_vector_mm[1] / model.pitch_x_mm),
            (self.row_vector_mm[0] / model.pitch_y_mm, self.row_vector_mm[1] / model.pitch_y_mm),
        )


def create_fixture_calibration_from_points(
    *,
    profile_id: str,
    display_name: str,
    plate_model: PlateModel,
    measured_points: dict[str, tuple[float, float]],
    existing_created_at: str = "",
) -> FixtureCalibration:
    missing = [key for key in FIXTURE_POINT_SEQUENCE if key not in measured_points]
    if missing:
        raise ValueError(f"Missing calibration point(s): {', '.join(missing)}.")
    points = {
        key: (float(measured_points[key][0]), float(measured_points[key][1]))
        for key in FIXTURE_POINT_SEQUENCE
    }
    p1_a1 = points["P1_A1"]
    column_vector = _vector_between(p1_a1, points["P1_A12"], divisor=max(1, plate_model.columns - 1))
    row_vector = _vector_between(p1_a1, points["P1_H1"], divisor=max(1, plate_model.rows - 1))
    timestamp = datetime.now().isoformat(timespec="seconds")
    calibration = FixtureCalibration(
        id=profile_id,
        display_name=display_name,
        plate_model_id=plate_model.id,
        plate_a1_positions={
            1: p1_a1,
            2: points["P2_A1"],
            3: points["P3_A1"],
            4: points["P4_A1"],
        },
        column_vector_mm=column_vector,
        row_vector_mm=row_vector,
        measured_points=points,
        created_at=existing_created_at or timestamp,
        updated_at=timestamp,
    )
    errors = [item["message"] for item in validate_fixture_calibration(calibration, plate_model) if item["severity"] == "error"]
    if errors:
        raise ValueError(errors[0])
    return calibration


def validate_fixture_calibration(
    calibration: FixtureCalibration,
    plate_model: PlateModel,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if not calibration.id:
        messages.append({"severity": "error", "code": "fixture_id", "message": "Fixture profile id is required."})
    if calibration.plate_model_id != plate_model.id:
        messages.append({
            "severity": "error",
            "code": "fixture_plate_model",
            "message": f"Fixture profile uses plate model '{calibration.plate_model_id}', not '{plate_model.id}'.",
        })
    missing = [
        index
        for index in range(1, FIXTURE_PROFILE_PLATE_COUNT + 1)
        if index not in calibration.plate_a1_positions
    ]
    if missing:
        messages.append({
            "severity": "error",
            "code": "fixture_missing_plates",
            "message": f"Fixture profile is missing A1 position(s) for plate(s): {', '.join(f'P{item}' for item in missing)}.",
        })
    required = set(range(1, FIXTURE_PROFILE_PLATE_COUNT + 1))
    extra = sorted(index for index in calibration.plate_a1_positions if index not in required)
    if extra:
        messages.append({
            "severity": "error",
            "code": "fixture_extra_plates",
            "message": f"Fixture profile has unsupported plate position(s): {', '.join(f'P{item}' for item in extra)}.",
        })
    missing_source = [
        key
        for key in FIXTURE_POINT_SEQUENCE
        if key not in calibration.measured_points
    ]
    if missing_source:
        messages.append({
            "severity": "error",
            "code": "fixture_missing_source_points",
            "message": f"Fixture profile is missing measured source point(s): {', '.join(missing_source)}.",
        })
    source_points = [
        calibration.measured_points[key]
        for key in FIXTURE_POINT_SEQUENCE
        if key in calibration.measured_points
    ]
    finite_points = list(calibration.plate_a1_positions.values()) + [
        calibration.column_vector_mm,
        calibration.row_vector_mm,
    ] + source_points
    if any(not _is_finite_point(point) for point in finite_points):
        messages.append({"severity": "error", "code": "fixture_nonfinite", "message": "Fixture coordinates must be finite numbers."})
        return messages
    if len({(round(point[0], 6), round(point[1], 6)) for point in calibration.plate_a1_positions.values()}) < len(calibration.plate_a1_positions):
        messages.append({"severity": "error", "code": "fixture_duplicate", "message": "Fixture plate A1 positions must be unique."})
    if len({(round(point[0], 6), round(point[1], 6)) for point in source_points}) < len(source_points):
        messages.append({"severity": "error", "code": "fixture_duplicate_source_points", "message": "Fixture measured points must be unique."})

    column_len = _vector_length(calibration.column_vector_mm)
    row_len = _vector_length(calibration.row_vector_mm)
    if column_len <= 1e-6 or row_len <= 1e-6:
        messages.append({"severity": "error", "code": "fixture_degenerate", "message": "Fixture row and column vectors must be non-zero."})
        return messages

    for code, measured, expected, axis in (
        ("fixture_pitch_x", column_len, float(plate_model.pitch_x_mm), "column"),
        ("fixture_pitch_y", row_len, float(plate_model.pitch_y_mm), "row"),
    ):
        diff = abs(measured - expected)
        if diff > FIXTURE_PITCH_ERROR_MM:
            messages.append({
                "severity": "error",
                "code": code,
                "message": f"Measured {axis} pitch is {measured:.3f} mm; expected about {expected:.3f} mm.",
            })
        elif diff > FIXTURE_PITCH_WARNING_MM:
            messages.append({
                "severity": "warning",
                "code": code,
                "message": f"Measured {axis} pitch differs from the plate model by {diff:.3f} mm.",
            })

    try:
        angle = _vector_angle_deg(calibration.column_vector_mm, calibration.row_vector_mm)
    except ValueError:
        angle = 0.0
    if angle < FIXTURE_SQUARE_MIN_DEG or angle > FIXTURE_SQUARE_MAX_DEG:
        messages.append({
            "severity": "error",
            "code": "fixture_square",
            "message": f"Measured row/column angle is {angle:.2f} degrees; expected near 90 degrees.",
        })
    elif abs(angle - 90.0) > FIXTURE_SQUARE_WARNING_DEG:
        messages.append({
            "severity": "warning",
            "code": "fixture_square",
            "message": f"Measured row/column angle is {angle:.2f} degrees; fixture may be slightly skewed.",
        })

    try:
        p1 = calibration.plate_a1_positions[1]
        p2 = calibration.plate_a1_positions[2]
        p3 = calibration.plate_a1_positions[3]
        p4 = calibration.plate_a1_positions[4]
    except KeyError:
        return messages
    implied_p4 = (p2[0] + p3[0] - p1[0], p2[1] + p3[1] - p1[1])
    p4_delta = _vector_length((p4[0] - implied_p4[0], p4[1] - implied_p4[1]))
    if p4_delta > FIXTURE_P4_WARNING_MM:
        messages.append({
            "severity": "warning",
            "code": "fixture_p4",
            "message": f"Measured P4 A1 is {p4_delta:.2f} mm from the rectangle implied by P1/P2/P3; using measured P4.",
        })
    return messages


def _calibrated_slot_for_plate(
    calibration: FixtureCalibration,
    model: PlateModel,
    plate_index: int,
) -> PlateSlot:
    a1 = calibration.plate_a1_positions[int(plate_index)]
    x_axis, y_axis = calibration.plate_axis_vectors(model)
    origin = (
        a1[0] - (x_axis[0] * model.a1_center_x_mm) - (y_axis[0] * model.a1_center_y_mm),
        a1[1] - (x_axis[1] * model.a1_center_x_mm) - (y_axis[1] * model.a1_center_y_mm),
    )
    corners = [
        origin,
        (origin[0] + (x_axis[0] * model.width_mm), origin[1] + (x_axis[1] * model.width_mm)),
        (origin[0] + (y_axis[0] * model.height_mm), origin[1] + (y_axis[1] * model.height_mm)),
        (
            origin[0] + (x_axis[0] * model.width_mm) + (y_axis[0] * model.height_mm),
            origin[1] + (x_axis[1] * model.width_mm) + (y_axis[1] * model.height_mm),
        ),
    ]
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    row = 1 if plate_index in {1, 2} else 2
    column = 1 if plate_index in {1, 3} else 2
    return PlateSlot(
        index=int(plate_index),
        row=row,
        column=column,
        origin_x_mm=min_x,
        origin_y_mm=min_y,
        width_mm=max(0.0, max_x - min_x),
        height_mm=max(0.0, max_y - min_y),
    )


def effective_bed_area(config: "AutomationRunConfig") -> BedArea:
    calibration = config.fixture_calibration
    if calibration is None:
        return config.bed_area.normalized()
    try:
        slots = [
            _calibrated_slot_for_plate(calibration, config.plate_model, plate_index)
            for plate_index in range(1, FIXTURE_PROFILE_PLATE_COUNT + 1)
        ]
    except (KeyError, OverflowError, TypeError, ValueError, ZeroDivisionError):
        return config.bed_area.normalized()
    if not slots:
        return config.bed_area.normalized()
    if any(
        not all(
            math.isfinite(value)
            for value in (
                slot.origin_x_mm,
                slot.origin_y_mm,
                slot.width_mm,
                slot.height_mm,
            )
        )
        for slot in slots
    ):
        return config.bed_area.normalized()
    return BedArea(
        min(slot.origin_x_mm for slot in slots),
        min(slot.origin_y_mm for slot in slots),
        max(slot.origin_x_mm + slot.width_mm for slot in slots),
        max(slot.origin_y_mm + slot.height_mm for slot in slots),
    ).normalized()


@dataclass(frozen=True)
class BedArea:
    x_min_mm: float = 0.0
    y_min_mm: float = 0.0
    x_max_mm: float = 300.0
    y_max_mm: float = 200.0

    def normalized(self) -> "BedArea":
        return BedArea(
            x_min_mm=min(self.x_min_mm, self.x_max_mm),
            y_min_mm=min(self.y_min_mm, self.y_max_mm),
            x_max_mm=max(self.x_min_mm, self.x_max_mm),
            y_max_mm=max(self.y_min_mm, self.y_max_mm),
        )

    @property
    def width_mm(self) -> float:
        bed = self.normalized()
        return bed.x_max_mm - bed.x_min_mm

    @property
    def height_mm(self) -> float:
        bed = self.normalized()
        return bed.y_max_mm - bed.y_min_mm

    def to_mapping(self) -> dict[str, float]:
        bed = self.normalized()
        return {
            "x_min_mm": bed.x_min_mm,
            "y_min_mm": bed.y_min_mm,
            "x_max_mm": bed.x_max_mm,
            "y_max_mm": bed.y_max_mm,
        }

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "BedArea":
        return cls(
            x_min_mm=float(values.get("x_min_mm", 0.0)),
            y_min_mm=float(values.get("y_min_mm", 0.0)),
            x_max_mm=float(values.get("x_max_mm", 300.0)),
            y_max_mm=float(values.get("y_max_mm", 200.0)),
        ).normalized()


def bed_area_from_corners(
    corner_a: tuple[float | None, float | None] | tuple[float | None, float | None, float | None],
    corner_b: tuple[float | None, float | None] | tuple[float | None, float | None, float | None],
) -> BedArea:
    """Create a normalized bed rectangle from two taught GRBL positions."""
    if len(corner_a) < 2 or len(corner_b) < 2:
        raise ValueError("Both taught corners must include X and Y coordinates.")
    ax, ay = corner_a[0], corner_a[1]
    bx, by = corner_b[0], corner_b[1]
    if ax is None or ay is None or bx is None or by is None:
        raise ValueError("Both taught corners must include known X and Y coordinates.")
    return BedArea(float(ax), float(ay), float(bx), float(by)).normalized()


@dataclass(frozen=True)
class PlateSlot:
    index: int
    row: int
    column: int
    origin_x_mm: float
    origin_y_mm: float
    width_mm: float
    height_mm: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "row": self.row,
            "column": self.column,
            "origin_x_mm": self.origin_x_mm,
            "origin_y_mm": self.origin_y_mm,
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
        }


@dataclass(frozen=True)
class AutomationShotOffset:
    label: str
    dx_mm: float = 0.0
    dy_mm: float = 0.0

    @classmethod
    def from_mapping(cls, values: Any) -> "AutomationShotOffset":
        if isinstance(values, AutomationShotOffset):
            return values
        if not isinstance(values, dict):
            return cls(label="center")
        label = str(values.get("label", "") or "offset").strip() or "offset"
        try:
            dx = float(values.get("dx_mm", values.get("dx", 0.0)))
        except (TypeError, ValueError):
            dx = 0.0
        try:
            dy = float(values.get("dy_mm", values.get("dy", 0.0)))
        except (TypeError, ValueError):
            dy = 0.0
        return cls(label=label, dx_mm=dx, dy_mm=dy)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "dx_mm": self.dx_mm,
            "dy_mm": self.dy_mm,
        }


@dataclass(frozen=True)
class AutomationTarget:
    plate_index: int
    well: str
    shot_number: int
    x_mm: float
    y_mm: float
    shot_offset_label: str = ""
    shot_offset_dx_mm: float = 0.0
    shot_offset_dy_mm: float = 0.0

    @property
    def key(self) -> str:
        return f"P{self.plate_index:02d}:{self.well}:S{self.shot_number:02d}"

    def to_mapping(self) -> dict[str, Any]:
        payload = {
            "plate_index": self.plate_index,
            "well": self.well,
            "shot_number": self.shot_number,
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "key": self.key,
        }
        if self.shot_offset_label or self.shot_offset_dx_mm or self.shot_offset_dy_mm:
            payload.update({
                "shot_offset_label": self.shot_offset_label,
                "shot_offset_dx_mm": self.shot_offset_dx_mm,
                "shot_offset_dy_mm": self.shot_offset_dy_mm,
            })
        return payload


@dataclass(frozen=True)
class AutomationRunConfig:
    plate_model: PlateModel
    bed_area: BedArea = field(default_factory=BedArea)
    fixture_calibration: FixtureCalibration | None = None
    orientation: str = ORIENTATION_NORMAL
    plate_gap_x_mm: float = 8.0
    plate_gap_y_mm: float = 8.0
    max_plates: int = 1
    shots_per_well: int = 1
    order_mode: str = ORDER_ROW
    plate_name_prefix: str = "AutomatedPlate"
    experiment_name: str = ""
    plate_names_by_index: dict[int, str] = field(default_factory=dict)
    run_directory: str = ""
    laser_port: str = ""
    laser_baudrate: int = 115200
    laser_profile: str = LASER_PROFILE_GENERIC_GRBL
    laser_s_max: int = 1000
    laser_power_percent: float = 10.0
    pulse_ms: float = 50.0
    relay_stroke_mm: float = DEFAULT_RELAY_STROKE_MM
    move_speed_mm_min: float = 3000.0
    settle_ms: float = 100.0
    capture_timeout_s: float = 10.0
    fixture_plate_indices: tuple[int, ...] = ()
    column_integration_times_ms: tuple[float, ...] = ()
    column_delays_us: tuple[float, ...] = ()
    plate_integration_times_ms: tuple[float, ...] = ()
    shot_offsets: tuple[AutomationShotOffset, ...] = ()
    # Pump energy as dialed by hand on the laser (0 = not declared). It sets
    # the pulse count per trigger (docs/laser_pulse_temporal_structure.md),
    # so the GUI requires the operator to declare it for every run.
    laser_energy_mj: float = 0.0

    def __post_init__(self):
        if self.orientation not in ORIENTATIONS:
            raise ValueError(f"Unsupported plate orientation: {self.orientation}")
        if self.order_mode not in {ORDER_ROW, ORDER_COLUMN}:
            raise ValueError(f"Unsupported acquisition order: {self.order_mode}")
        if self.max_plates < 1:
            raise ValueError("max_plates must be at least 1.")
        if self.shots_per_well < 1:
            raise ValueError("shots_per_well must be at least 1.")
        fixture_plate_indices: list[int] = []
        for raw_index in self.fixture_plate_indices or ():
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if 1 <= index <= FIXTURE_PROFILE_PLATE_COUNT and index not in fixture_plate_indices:
                fixture_plate_indices.append(index)
        experiment_name = str(self.experiment_name or self.plate_name_prefix or "AutomatedPlate").strip() or "AutomatedPlate"
        plate_name_prefix = str(self.plate_name_prefix or experiment_name).strip() or experiment_name
        normalized_names: dict[int, str] = {}
        for raw_index, raw_name in dict(self.plate_names_by_index or {}).items():
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if index < 1:
                continue
            normalized_names[index] = str(raw_name or "").strip()
        object.__setattr__(self, "experiment_name", experiment_name)
        object.__setattr__(self, "plate_name_prefix", plate_name_prefix)
        object.__setattr__(self, "plate_names_by_index", normalized_names)
        object.__setattr__(self, "fixture_plate_indices", tuple(sorted(fixture_plate_indices)))
        object.__setattr__(self, "run_directory", str(self.run_directory or "").strip())
        column_times: list[float] = []
        for raw_value in self.column_integration_times_ms or ():
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                column_times.append(value)
        object.__setattr__(self, "column_integration_times_ms", tuple(column_times))
        object.__setattr__(self, "column_delays_us", normalized_column_delay_schedule(self.column_delays_us))
        object.__setattr__(self, "plate_integration_times_ms", normalized_integration_schedule(self.plate_integration_times_ms))
        offsets: list[AutomationShotOffset] = []
        for raw_offset in self.shot_offsets or ():
            offset = AutomationShotOffset.from_mapping(raw_offset)
            if offset.label or offset.dx_mm or offset.dy_mm:
                offsets.append(offset)
        object.__setattr__(self, "shot_offsets", tuple(offsets))
        profile = str(self.laser_profile or LASER_PROFILE_GENERIC_GRBL).strip()
        if profile not in LASER_PROFILES:
            profile = LASER_PROFILE_GENERIC_GRBL
        object.__setattr__(self, "laser_profile", profile)
        object.__setattr__(self, "relay_stroke_mm", max(0.0, float(self.relay_stroke_mm)))
        try:
            laser_energy_mj = max(0.0, float(self.laser_energy_mj))
        except (TypeError, ValueError):
            laser_energy_mj = 0.0
        object.__setattr__(self, "laser_energy_mj", laser_energy_mj)

    @property
    def laser_s_value(self) -> int:
        fraction = max(0.0, min(100.0, float(self.laser_power_percent))) / 100.0
        return int(round(max(0, int(self.laser_s_max)) * fraction))

    @property
    def safe_experiment_name(self) -> str:
        return sanitize_filename_part(self.experiment_name, fallback="AutomatedRun")

    def raw_plate_name(self, plate_index: int) -> str:
        index = int(plate_index)
        return self.plate_names_by_index.get(index, "").strip()

    def plate_display_name(self, plate_index: int) -> str:
        explicit = self.raw_plate_name(plate_index)
        if explicit:
            return explicit
        return f"{self.plate_name_prefix}_P{int(plate_index):02d}"

    def plate_display_label(self, plate_index: int) -> str:
        return f"P{int(plate_index):02d} - {self.plate_display_name(plate_index)}"

    def safe_plate_name(self, plate_index: int) -> str:
        explicit = self.raw_plate_name(plate_index)
        if explicit:
            return sanitize_filename_part(explicit, fallback=f"Plate{int(plate_index):02d}")
        return sanitize_filename_part(f"{self.plate_name_prefix}_P{int(plate_index):02d}", fallback=f"Plate{int(plate_index):02d}")

    def plate_folder_name(self, plate_index: int) -> str:
        explicit = self.raw_plate_name(plate_index)
        if explicit:
            return f"P{int(plate_index):02d}_{self.safe_plate_name(plate_index)}"
        return self.safe_plate_name(plate_index)

    def plate_file_prefix(self, plate_index: int) -> str:
        explicit = self.raw_plate_name(plate_index)
        if explicit:
            return f"{self.safe_experiment_name}_{self.safe_plate_name(plate_index)}_P{int(plate_index):02d}"
        return self.safe_plate_name(plate_index)

    def to_mapping(self) -> dict[str, Any]:
        payload = {
            "plate_model": self.plate_model.to_mapping(),
            "bed_area": self.bed_area.to_mapping(),
            "fixture_calibration": (
                self.fixture_calibration.to_mapping()
                if self.fixture_calibration is not None
                else None
            ),
            "orientation": self.orientation,
            "plate_gap_x_mm": self.plate_gap_x_mm,
            "plate_gap_y_mm": self.plate_gap_y_mm,
            "max_plates": self.max_plates,
            "fixture_plate_indices": list(self.fixture_plate_indices),
            "shots_per_well": self.shots_per_well,
            "order_mode": self.order_mode,
            "plate_name_prefix": self.plate_name_prefix,
            "experiment_name": self.experiment_name,
            "plate_names_by_index": {
                str(index): name
                for index, name in sorted(self.plate_names_by_index.items())
            },
            "run_directory": self.run_directory,
            "laser_port": self.laser_port,
            "laser_baudrate": self.laser_baudrate,
            "laser_profile": self.laser_profile,
            "laser_s_max": self.laser_s_max,
            "laser_power_percent": self.laser_power_percent,
            "pulse_ms": self.pulse_ms,
            "relay_stroke_mm": self.relay_stroke_mm,
            "move_speed_mm_min": self.move_speed_mm_min,
            "settle_ms": self.settle_ms,
            "capture_timeout_s": self.capture_timeout_s,
            "column_integration_times_ms": list(self.column_integration_times_ms),
            "column_delays_us": list(self.column_delays_us),
            "plate_integration_times_ms": list(self.plate_integration_times_ms),
            "laser_energy_mj": self.laser_energy_mj,
        }
        if self.shot_offsets:
            payload["shot_offsets"] = [offset.to_mapping() for offset in self.shot_offsets]
        return payload

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "AutomationRunConfig":
        legacy_prefix = str(values.get("plate_name_prefix", "AutomatedPlate") or "AutomatedPlate")
        raw_plate_names = values.get("plate_names_by_index", {})
        if not isinstance(raw_plate_names, dict):
            raw_plate_names = {}
        plate_names: dict[int, str] = {}
        for index, name in raw_plate_names.items():
            try:
                plate_names[int(index)] = str(name)
            except (TypeError, ValueError):
                continue
        raw_fixture_plate_indices = values.get("fixture_plate_indices", ())
        if not isinstance(raw_fixture_plate_indices, (list, tuple, set)):
            raw_fixture_plate_indices = ()
        raw_column_times = values.get("column_integration_times_ms", ())
        if not isinstance(raw_column_times, (list, tuple)):
            raw_column_times = ()
        raw_column_delays = values.get("column_delays_us", ())
        if not isinstance(raw_column_delays, (list, tuple)):
            raw_column_delays = ()
        raw_plate_integrations = values.get("plate_integration_times_ms", ())
        if not isinstance(raw_plate_integrations, (list, tuple)):
            raw_plate_integrations = ()
        raw_shot_offsets = values.get("shot_offsets", ())
        if not isinstance(raw_shot_offsets, (list, tuple)):
            raw_shot_offsets = ()
        return cls(
            plate_model=PlateModel.from_mapping(values["plate_model"]),
            bed_area=BedArea.from_mapping(values.get("bed_area", {})),
            fixture_calibration=(
                FixtureCalibration.from_mapping(values["fixture_calibration"])
                if isinstance(values.get("fixture_calibration"), dict)
                else None
            ),
            orientation=str(values.get("orientation", ORIENTATION_NORMAL)),
            plate_gap_x_mm=float(values.get("plate_gap_x_mm", 8.0)),
            plate_gap_y_mm=float(values.get("plate_gap_y_mm", 8.0)),
            max_plates=max(1, int(values.get("max_plates", 1))),
            fixture_plate_indices=tuple(raw_fixture_plate_indices),
            shots_per_well=max(1, int(values.get("shots_per_well", 1))),
            order_mode=str(values.get("order_mode", ORDER_ROW)),
            plate_name_prefix=legacy_prefix,
            experiment_name=str(values.get("experiment_name", legacy_prefix) or legacy_prefix),
            plate_names_by_index=plate_names,
            run_directory=str(values.get("run_directory", "") or ""),
            laser_port=str(values.get("laser_port", "")),
            laser_baudrate=int(values.get("laser_baudrate", 115200)),
            laser_profile=str(values.get("laser_profile", LASER_PROFILE_GENERIC_GRBL) or LASER_PROFILE_GENERIC_GRBL),
            laser_s_max=int(values.get("laser_s_max", 1000)),
            laser_power_percent=float(values.get("laser_power_percent", 10.0)),
            pulse_ms=float(values.get("pulse_ms", 50.0)),
            relay_stroke_mm=float(values.get("relay_stroke_mm", DEFAULT_RELAY_STROKE_MM)),
            move_speed_mm_min=float(values.get("move_speed_mm_min", 3000.0)),
            settle_ms=float(values.get("settle_ms", 100.0)),
            capture_timeout_s=float(values.get("capture_timeout_s", 10.0)),
            column_integration_times_ms=tuple(raw_column_times),
            column_delays_us=tuple(raw_column_delays),
            plate_integration_times_ms=tuple(raw_plate_integrations),
            shot_offsets=tuple(AutomationShotOffset.from_mapping(item) for item in raw_shot_offsets),
            laser_energy_mj=float(values.get("laser_energy_mj", 0.0) or 0.0),
        )

    def _signature_mapping(self) -> dict[str, Any]:
        relevant = AutomationRunConfig.from_mapping(self.to_mapping()).to_mapping()
        relevant.pop("laser_port", None)
        relevant.pop("run_directory", None)
        if isinstance(relevant.get("fixture_calibration"), dict):
            relevant["fixture_calibration"] = FixtureCalibration.from_mapping(
                relevant["fixture_calibration"]
            ).signature_mapping()
            relevant.pop("bed_area", None)
            relevant.pop("plate_gap_x_mm", None)
            relevant.pop("plate_gap_y_mm", None)
            relevant.pop("orientation", None)
        else:
            relevant.pop("fixture_plate_indices", None)
        return relevant

    def dry_run_signature(self) -> str:
        relevant = self._signature_mapping()
        # Dry run is a representative motion check only. Firing, capture, and
        # naming settings must not invalidate a completed motion check.
        relevant.pop("experiment_name", None)
        relevant.pop("plate_name_prefix", None)
        relevant.pop("plate_names_by_index", None)
        relevant.pop("shots_per_well", None)
        relevant.pop("laser_baudrate", None)
        relevant.pop("laser_s_max", None)
        relevant.pop("laser_power_percent", None)
        relevant.pop("pulse_ms", None)
        relevant.pop("settle_ms", None)
        relevant.pop("capture_timeout_s", None)
        relevant.pop("column_integration_times_ms", None)
        relevant.pop("column_delays_us", None)
        relevant.pop("plate_integration_times_ms", None)
        # Declared pump energy annotates the shots; it changes no motion.
        relevant.pop("laser_energy_mj", None)
        payload = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def run_identity_signature(self) -> str:
        relevant = self._signature_mapping()
        payload = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AutomationPreset:
    id: str
    display_name: str
    plate_model_id: str = DEFAULT_96_PLATE_MODEL_ID
    bed_area: BedArea = field(default_factory=BedArea)
    orientation: str = ORIENTATION_NORMAL
    plate_gap_x_mm: float = 8.0
    plate_gap_y_mm: float = 8.0
    max_plates: int = 1
    shots_per_well: int = 1
    order_mode: str = ORDER_ROW
    laser_profile: str = LASER_PROFILE_GENERIC_GRBL
    laser_s_max: int = 1000
    laser_power_percent: float = 10.0
    pulse_ms: float = 50.0
    relay_stroke_mm: float = DEFAULT_RELAY_STROKE_MM
    move_speed_mm_min: float = 3000.0
    settle_ms: float = 100.0
    capture_timeout_s: float = 10.0
    column_integration_times_ms: tuple[float, ...] = ()
    column_delays_us: tuple[float, ...] = ()
    plate_integration_times_ms: tuple[float, ...] = ()
    shot_offsets: tuple[AutomationShotOffset, ...] = ()
    notes: str = ""

    def __post_init__(self):
        profile = str(self.laser_profile or LASER_PROFILE_GENERIC_GRBL).strip()
        if profile not in LASER_PROFILES:
            profile = LASER_PROFILE_GENERIC_GRBL
        object.__setattr__(self, "laser_profile", profile)
        object.__setattr__(self, "relay_stroke_mm", max(0.0, float(self.relay_stroke_mm)))
        column_times: list[float] = []
        for raw_value in self.column_integration_times_ms or ():
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                column_times.append(value)
        object.__setattr__(self, "column_integration_times_ms", tuple(column_times))
        object.__setattr__(self, "column_delays_us", normalized_column_delay_schedule(self.column_delays_us))
        object.__setattr__(self, "plate_integration_times_ms", normalized_integration_schedule(self.plate_integration_times_ms))
        offsets: list[AutomationShotOffset] = []
        for raw_offset in self.shot_offsets or ():
            offset = AutomationShotOffset.from_mapping(raw_offset)
            if offset.label or offset.dx_mm or offset.dy_mm:
                offsets.append(offset)
        object.__setattr__(self, "shot_offsets", tuple(offsets))

    def to_mapping(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "display_name": self.display_name,
            "plate_model_id": self.plate_model_id,
            "bed_area": self.bed_area.to_mapping(),
            "orientation": self.orientation,
            "plate_gap_x_mm": self.plate_gap_x_mm,
            "plate_gap_y_mm": self.plate_gap_y_mm,
            "max_plates": self.max_plates,
            "shots_per_well": self.shots_per_well,
            "order_mode": self.order_mode,
            "laser_profile": self.laser_profile,
            "laser_s_max": self.laser_s_max,
            "laser_power_percent": self.laser_power_percent,
            "pulse_ms": self.pulse_ms,
            "relay_stroke_mm": self.relay_stroke_mm,
            "move_speed_mm_min": self.move_speed_mm_min,
            "settle_ms": self.settle_ms,
            "capture_timeout_s": self.capture_timeout_s,
            "column_integration_times_ms": list(self.column_integration_times_ms),
            "column_delays_us": list(self.column_delays_us),
            "plate_integration_times_ms": list(self.plate_integration_times_ms),
            "notes": self.notes,
        }
        if self.shot_offsets:
            payload["shot_offsets"] = [offset.to_mapping() for offset in self.shot_offsets]
        return payload

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "AutomationPreset":
        raw_bed = values.get("bed_area", {})
        if not isinstance(raw_bed, dict):
            raw_bed = {}
        raw_shot_offsets = values.get("shot_offsets", ())
        if not isinstance(raw_shot_offsets, (list, tuple)):
            raw_shot_offsets = ()
        return cls(
            id=str(values["id"]).strip(),
            display_name=str(values.get("display_name") or values["id"]).strip(),
            plate_model_id=str(values.get("plate_model_id", DEFAULT_96_PLATE_MODEL_ID) or DEFAULT_96_PLATE_MODEL_ID).strip(),
            bed_area=BedArea.from_mapping(raw_bed),
            orientation=str(values.get("orientation", ORIENTATION_NORMAL)),
            plate_gap_x_mm=float(values.get("plate_gap_x_mm", 8.0)),
            plate_gap_y_mm=float(values.get("plate_gap_y_mm", 8.0)),
            max_plates=max(1, int(values.get("max_plates", 1))),
            shots_per_well=max(1, int(values.get("shots_per_well", 1))),
            order_mode=str(values.get("order_mode", ORDER_ROW)),
            laser_profile=str(values.get("laser_profile", LASER_PROFILE_GENERIC_GRBL) or LASER_PROFILE_GENERIC_GRBL),
            laser_s_max=max(1, int(values.get("laser_s_max", 1000))),
            laser_power_percent=float(values.get("laser_power_percent", 10.0)),
            pulse_ms=float(values.get("pulse_ms", 50.0)),
            relay_stroke_mm=float(values.get("relay_stroke_mm", DEFAULT_RELAY_STROKE_MM)),
            move_speed_mm_min=float(values.get("move_speed_mm_min", 3000.0)),
            settle_ms=float(values.get("settle_ms", 100.0)),
            capture_timeout_s=float(values.get("capture_timeout_s", 10.0)),
            column_integration_times_ms=tuple(
                values.get("column_integration_times_ms", ())
                if isinstance(values.get("column_integration_times_ms", ()), (list, tuple))
                else ()
            ),
            column_delays_us=tuple(
                values.get("column_delays_us", ())
                if isinstance(values.get("column_delays_us", ()), (list, tuple))
                else ()
            ),
            plate_integration_times_ms=tuple(
                values.get("plate_integration_times_ms", ())
                if isinstance(values.get("plate_integration_times_ms", ()), (list, tuple))
                else ()
            ),
            shot_offsets=tuple(AutomationShotOffset.from_mapping(item) for item in raw_shot_offsets),
            notes=str(values.get("notes", "") or ""),
        )

    def to_config(
        self,
        *,
        plate_model: PlateModel | None = None,
        plate_name_prefix: str = "AutomatedPlate",
        experiment_name: str | None = None,
        plate_names_by_index: dict[int, str] | None = None,
        run_directory: str = "",
        laser_port: str = "",
        laser_baudrate: int = 115200,
    ) -> AutomationRunConfig:
        if plate_model is None:
            from prolibspector.acquisition.plate_models import plate_model_by_id

            plate_model = plate_model_by_id(self.plate_model_id)
        return AutomationRunConfig(
            plate_model=plate_model,
            bed_area=self.bed_area,
            orientation=self.orientation,
            plate_gap_x_mm=self.plate_gap_x_mm,
            plate_gap_y_mm=self.plate_gap_y_mm,
            max_plates=self.max_plates,
            shots_per_well=self.shots_per_well,
            order_mode=self.order_mode,
            plate_name_prefix=plate_name_prefix,
            experiment_name=experiment_name or plate_name_prefix,
            plate_names_by_index=dict(plate_names_by_index or {}),
            run_directory=run_directory,
            laser_port=laser_port,
            laser_baudrate=laser_baudrate,
            laser_profile=self.laser_profile,
            laser_s_max=self.laser_s_max,
            laser_power_percent=self.laser_power_percent,
            pulse_ms=self.pulse_ms,
            relay_stroke_mm=self.relay_stroke_mm,
            move_speed_mm_min=self.move_speed_mm_min,
            settle_ms=self.settle_ms,
            capture_timeout_s=self.capture_timeout_s,
            column_integration_times_ms=self.column_integration_times_ms,
            column_delays_us=self.column_delays_us,
            plate_integration_times_ms=self.plate_integration_times_ms,
            shot_offsets=self.shot_offsets,
        )


def bundled_automation_presets() -> list[AutomationPreset]:
    return [
        AutomationPreset(
            id=DEFAULT_AUTOMATION_PRESET_ID,
            display_name="96-well single plate",
            plate_model_id=DEFAULT_96_PLATE_MODEL_ID,
            bed_area=BedArea(0.0, 0.0, 300.0, 200.0),
            max_plates=1,
            laser_profile=LASER_PROFILE_MONPORT_NDYAG_RELAY,
            laser_s_max=1000,
            laser_power_percent=100.0,
            pulse_ms=50.0,
            relay_stroke_mm=DEFAULT_RELAY_STROKE_MM,
            notes="Default single Corning/Costar 96-well plate in the taught/default bed.",
        ),
        AutomationPreset(
            id=INTEGRATION_SWEEP_PRESET_ID,
            display_name="96-well integration sweep",
            plate_model_id=DEFAULT_96_PLATE_MODEL_ID,
            bed_area=BedArea(0.0, 0.0, 300.0, 200.0),
            max_plates=1,
            shots_per_well=5,
            order_mode=ORDER_COLUMN,
            laser_profile=LASER_PROFILE_MONPORT_NDYAG_RELAY,
            laser_s_max=1000,
            laser_power_percent=100.0,
            pulse_ms=50.0,
            relay_stroke_mm=DEFAULT_RELAY_STROKE_MM,
            column_integration_times_ms=INTEGRATION_SWEEP_COLUMN_TIMES_MS,
            notes="Single-plate 5-shot-per-well Ocean Optics integration sweep by column.",
        ),
        AutomationPreset(
            id=DELAY_SWEEP_PRESET_ID,
            display_name="96-well trigger-delay sweep",
            plate_model_id=DEFAULT_96_PLATE_MODEL_ID,
            bed_area=BedArea(0.0, 0.0, 300.0, 200.0),
            max_plates=1,
            shots_per_well=5,
            order_mode=ORDER_COLUMN,
            laser_profile=LASER_PROFILE_MONPORT_NDYAG_RELAY,
            laser_s_max=1000,
            laser_power_percent=100.0,
            pulse_ms=50.0,
            relay_stroke_mm=DEFAULT_RELAY_STROKE_MM,
            column_delays_us=DELAY_SWEEP_COLUMN_DELAYS_US,
            notes=(
                "Single-plate 5-shot-per-well trigger-delay sweep by column for "
                "delay-capable spectrometers (YSM-8111-06-01). Draft schedule; "
                "requires a spectrometer with a programmable trigger delay."
            ),
        ),
        AutomationPreset(
            id=PLATE_JOINT_SWEEP_PRESET_ID,
            display_name="96-well 4-plate delay+integration sweep",
            plate_model_id=DEFAULT_96_PLATE_MODEL_ID,
            bed_area=BedArea(0.0, 0.0, 300.0, 200.0),
            max_plates=4,
            shots_per_well=1,
            order_mode=ORDER_COLUMN,
            laser_profile=LASER_PROFILE_MONPORT_NDYAG_RELAY,
            laser_s_max=1000,
            laser_power_percent=100.0,
            pulse_ms=50.0,
            relay_stroke_mm=DEFAULT_RELAY_STROKE_MM,
            column_delays_us=DELAY_SWEEP_COLUMN_DELAYS_US,
            plate_integration_times_ms=PLATE_JOINT_SWEEP_INTEGRATION_TIMES_MS,
            notes=(
                "Joint timing sweep across four plates: trigger delay by well "
                "column (12 values), integration time by plate (4 values), 8 "
                "wells per column as replicates. One shot per well keeps every "
                "replicate on a fresh surface; raise shots/well for more "
                "replicates per cell. Requires a delay-capable spectrometer "
                "(YSM-8111-06-01); draft schedules."
            ),
        ),
        AutomationPreset(
            id=INTRA_WELL_DENSE_PRESET_ID,
            display_name="96-well intra-well 5-shot",
            plate_model_id=DEFAULT_96_PLATE_MODEL_ID,
            bed_area=BedArea(0.0, 0.0, 300.0, 200.0),
            max_plates=1,
            shots_per_well=5,
            laser_profile=LASER_PROFILE_MONPORT_NDYAG_RELAY,
            laser_s_max=1000,
            laser_power_percent=100.0,
            pulse_ms=50.0,
            relay_stroke_mm=DEFAULT_RELAY_STROKE_MM,
            shot_offsets=(
                AutomationShotOffset("center", 0.0, 0.0),
                AutomationShotOffset("x_plus", 1.0, 0.0),
                AutomationShotOffset("x_minus", -1.0, 0.0),
                AutomationShotOffset("y_plus", 0.0, 1.0),
                AutomationShotOffset("y_minus", 0.0, -1.0),
            ),
            notes="Single-plate 5-shot-per-well dense sampling pattern: center plus 1 mm X/Y offsets.",
        ),
        AutomationPreset(
            id="generic_96_2x2",
            display_name="96-well 2x2 plates",
            plate_model_id=DEFAULT_96_PLATE_MODEL_ID,
            bed_area=BedArea(0.0, 0.0, 300.0, 200.0),
            max_plates=4,
            notes="Four Corning/Costar 96-well plates arranged row-major when the taught bed is large enough.",
        ),
        AutomationPreset(
            id=CUSTOM_AUTOMATION_PRESET_ID,
            display_name="Custom",
            plate_model_id=DEFAULT_96_PLATE_MODEL_ID,
            bed_area=BedArea(0.0, 0.0, 300.0, 200.0),
            max_plates=1,
            notes="Manual geometry and laser settings.",
        ),
    ]


def user_automation_presets_from_settings() -> list[AutomationPreset]:
    settings = load_settings() or {}
    raw_presets = settings.get(AUTOMATION_SETTINGS_SECTION, {}).get(AUTOMATION_PRESETS_KEY, [])
    if not isinstance(raw_presets, list):
        return []
    presets: list[AutomationPreset] = []
    for item in raw_presets:
        if not isinstance(item, dict):
            continue
        try:
            preset = AutomationPreset.from_mapping(item)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if preset.id == CUSTOM_AUTOMATION_PRESET_ID:
            continue
        presets.append(preset)
    return presets


def load_automation_presets() -> list[AutomationPreset]:
    by_id = {preset.id: preset for preset in bundled_automation_presets()}
    for preset in user_automation_presets_from_settings():
        by_id[preset.id] = preset
    presets = [preset for preset in by_id.values() if preset.id != CUSTOM_AUTOMATION_PRESET_ID]
    presets.sort(key=lambda preset: (preset.id != DEFAULT_AUTOMATION_PRESET_ID, preset.display_name.lower()))
    presets.append(by_id[CUSTOM_AUTOMATION_PRESET_ID])
    return presets


def automation_preset_by_id(preset_id: str) -> AutomationPreset:
    for preset in load_automation_presets():
        if preset.id == preset_id:
            return preset
    raise KeyError(f"Unknown automation preset: {preset_id}")


def save_user_automation_presets(presets: list[AutomationPreset]) -> tuple[bool, str]:
    settings = load_settings() or get_default_settings()
    section = settings.setdefault(AUTOMATION_SETTINGS_SECTION, {})
    if not isinstance(section, dict):
        section = {}
        settings[AUTOMATION_SETTINGS_SECTION] = section
    unique = {
        preset.id: preset
        for preset in presets
        if preset.id and preset.id != CUSTOM_AUTOMATION_PRESET_ID
    }
    section[AUTOMATION_PRESETS_KEY] = [
        preset.to_mapping()
        for preset in sorted(unique.values(), key=lambda item: item.display_name.lower())
    ]
    return save_settings(settings)


def _fixture_calibration_error_messages(profile: FixtureCalibration) -> list[str]:
    try:
        model = plate_model_by_id(profile.plate_model_id)
    except Exception:
        return [f"Fixture profile uses unknown plate model '{profile.plate_model_id}'."]
    return [
        item["message"]
        for item in validate_fixture_calibration(profile, model)
        if item.get("severity") == "error"
    ]


def user_fixture_calibrations_from_settings() -> list[FixtureCalibration]:
    settings = load_settings() or {}
    raw_profiles = settings.get(AUTOMATION_SETTINGS_SECTION, {}).get(FIXTURE_CALIBRATIONS_KEY, [])
    if not isinstance(raw_profiles, list):
        return []
    profiles: list[FixtureCalibration] = []
    for item in raw_profiles:
        if not isinstance(item, dict):
            continue
        try:
            profile = replace(
                FixtureCalibration.from_mapping(item),
                source_kind=FIXTURE_CALIBRATION_SOURCE_LEGACY_SETTINGS,
                source_filename="",
                source_path="",
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if _fixture_calibration_error_messages(profile):
            continue
        profiles.append(profile)
    return profiles


def fixture_calibration_library_dir() -> str:
    """Where saved fixture calibrations live.

    ``working_path`` rather than ``source_root``: these are written at runtime,
    and in a frozen build the source root is a temporary extraction directory
    that is deleted on exit, so a calibration saved there would vanish.
    """
    return working_path(*FIXTURE_CALIBRATION_LIBRARY_PARTS)


def _fixture_calibration_relative_path(filename: str) -> str:
    return "/".join((*FIXTURE_CALIBRATION_LIBRARY_PARTS, filename))


def _fixture_calibration_sort_key(profile: FixtureCalibration) -> tuple[str, str]:
    timestamp = profile.updated_at or profile.created_at or profile.source_filename
    return str(timestamp), profile.display_name.lower()


def fixture_calibrations_from_repo() -> list[FixtureCalibration]:
    directory = Path(fixture_calibration_library_dir())
    if not directory.exists():
        return []
    profiles: list[FixtureCalibration] = []
    for path in sorted(directory.glob("*.json")):
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                values = json.load(handle)
            if not isinstance(values, dict):
                continue
            profile = FixtureCalibration.from_mapping(values)
        except (OSError, json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError):
            continue
        profile = replace(
            profile,
            id=path.stem,
            source_filename=path.name,
            source_path=_fixture_calibration_relative_path(path.name),
            source_kind=FIXTURE_CALIBRATION_SOURCE_REPO,
        )
        if _fixture_calibration_error_messages(profile):
            continue
        profiles.append(profile)
    return sorted(profiles, key=_fixture_calibration_sort_key, reverse=True)


def load_fixture_calibrations() -> list[FixtureCalibration]:
    legacy_profiles = sorted(
        user_fixture_calibrations_from_settings(),
        key=lambda item: item.display_name.lower(),
    )
    return fixture_calibrations_from_repo() + legacy_profiles


def _fixture_calibration_safe_name(name: str) -> str:
    safe = sanitize_filename_part(str(name or "").lower().replace(" ", "_"), fallback="fixture")
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in safe).strip("._-")
    return safe or "fixture"


def _next_fixture_calibration_file(profile: FixtureCalibration, timestamp: datetime) -> tuple[str, str]:
    directory = Path(fixture_calibration_library_dir())
    safe_name = _fixture_calibration_safe_name(profile.display_name)
    stem = f"{timestamp.strftime('%Y%m%d_%H%M%S')}_{safe_name}"
    filename = f"{stem}.json"
    counter = 2
    while (directory / filename).exists():
        filename = f"{stem}_{counter:02d}.json"
        counter += 1
    return filename[:-5], filename


def save_fixture_calibration_profile(profile: FixtureCalibration) -> tuple[bool, str, FixtureCalibration | None]:
    timestamp = datetime.now()
    timestamp_text = timestamp.isoformat(timespec="seconds")
    profile_id, filename = _next_fixture_calibration_file(profile, timestamp)
    saved_profile = replace(
        profile,
        id=profile_id,
        created_at=profile.created_at or timestamp_text,
        updated_at=timestamp_text,
        schema_version=FIXTURE_CALIBRATION_SCHEMA_VERSION,
        source_filename=filename,
        source_path=_fixture_calibration_relative_path(filename),
        source_kind=FIXTURE_CALIBRATION_SOURCE_REPO,
    )
    errors = _fixture_calibration_error_messages(saved_profile)
    if errors:
        return False, f"Fixture profile is invalid: {errors[0]}", None
    filepath = str(Path(fixture_calibration_library_dir()) / filename)
    try:
        _write_json_atomic(filepath, saved_profile.to_mapping())
    except Exception as exc:
        return False, f"Error saving fixture profile: {exc}", None
    return True, f"Fixture profile saved to {saved_profile.source_path}", saved_profile


def upsert_fixture_calibration(profile: FixtureCalibration) -> tuple[bool, str]:
    ok, message, _saved_profile = save_fixture_calibration_profile(profile)
    return ok, message


def delete_fixture_calibration(profile_id: str) -> tuple[bool, str]:
    profile_id = str(profile_id).strip()
    repo_profiles = fixture_calibrations_from_repo()
    match = next((profile for profile in repo_profiles if profile.id == profile_id), None)
    if match is None:
        if any(profile.id == profile_id for profile in user_fixture_calibrations_from_settings()):
            return False, "Legacy fixture profiles are read-only. Save it as a new fixture profile before deleting."
        return False, "Fixture profile not found."
    filename = os.path.basename(match.source_filename or "")
    if not filename:
        return False, "Fixture profile has no tracked filename."
    directory = Path(fixture_calibration_library_dir()).resolve()
    filepath = (directory / filename).resolve()
    if filepath.parent != directory:
        return False, "Fixture profile path is outside the fixture library."
    try:
        filepath.unlink()
    except OSError as exc:
        return False, f"Error deleting fixture profile: {exc}"
    return True, "Fixture profile deleted."


# In-progress fixture recordings are autosaved so an alarm or crash before Save
# never loses taught points. The draft is transient session state, so it lives
# in settings_dir() (never the git-tracked fixture library) and honors the same
# persistence kill switch as the other session preferences.
FIXTURE_POINT_DRAFT_FILENAME = "fixture_recording_draft.json"


def _fixture_point_draft_path() -> Path:
    return settings_dir() / FIXTURE_POINT_DRAFT_FILENAME


def save_fixture_point_draft(points: dict[str, tuple[float, float]], display_name: str) -> None:
    if not ui_prefs_enabled():
        return
    payload = {
        "display_name": str(display_name or ""),
        "points": {str(key): [float(x), float(y)] for key, (x, y) in points.items()},
    }
    _write_json_atomic(str(_fixture_point_draft_path()), payload)


def load_fixture_point_draft() -> tuple[dict[str, tuple[float, float]], str] | None:
    """Return (points, display_name) if a valid in-progress draft exists.

    Valid means the recorded keys are exactly the first N entries of
    FIXTURE_POINT_SEQUENCE with 1 <= N <= len(sequence); anything else
    (corrupt file, reordered or unknown keys) is treated as no draft.
    """
    if not ui_prefs_enabled():
        return None
    path = _fixture_point_draft_path()
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return None
        raw_points = payload.get("points")
        if not isinstance(raw_points, dict) or not raw_points:
            return None
        expected = FIXTURE_POINT_SEQUENCE[: len(raw_points)]
        if set(raw_points) != set(expected):
            return None
        points = {key: (float(raw_points[key][0]), float(raw_points[key][1])) for key in expected}
    except (OSError, json.JSONDecodeError, LookupError, TypeError, ValueError):
        return None
    return points, str(payload.get("display_name") or "")


def clear_fixture_point_draft() -> None:
    try:
        _fixture_point_draft_path().unlink(missing_ok=True)
    except OSError:
        # Best-effort cleanup; a stale draft only re-prompts on next start.
        pass


@dataclass
class AutomatedRunState:
    config: AutomationRunConfig
    completed_targets: set[str] = field(default_factory=set)
    active_target: str | None = None
    abort_reason: str | None = None
    dry_run_signature: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": 1,
            "config": self.config.to_mapping(),
            "completed_targets": sorted(self.completed_targets),
            "active_target": self.active_target,
            "abort_reason": self.abort_reason,
            "dry_run_signature": self.dry_run_signature,
        }

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "AutomatedRunState":
        return cls(
            config=AutomationRunConfig.from_mapping(values["config"]),
            completed_targets=set(str(item) for item in values.get("completed_targets", [])),
            active_target=values.get("active_target"),
            abort_reason=values.get("abort_reason"),
            dry_run_signature=values.get("dry_run_signature"),
        )

    def record_started(self, target: AutomationTarget) -> None:
        self.active_target = target.key
        self.abort_reason = None

    def record_completed(self, target: AutomationTarget) -> None:
        self.completed_targets.add(target.key)
        self.active_target = None

    def record_abort(self, reason: str) -> None:
        self.abort_reason = reason
        self.active_target = None


def automation_state_path(save_directory: str) -> str:
    return os.path.join(save_directory, AUTOMATION_STATE_FILENAME)


def automation_manifest_path(save_directory: str) -> str:
    return os.path.join(save_directory, AUTOMATION_MANIFEST_FILENAME)


def automation_summary_path(save_directory: str) -> str:
    return os.path.join(save_directory, AUTOMATION_SUMMARY_FILENAME)


def _write_json_atomic(filepath: str, payload: dict[str, Any]) -> str:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp_filepath = f"{filepath}.tmp"
    try:
        with open(tmp_filepath, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp_filepath, filepath)
    except Exception:
        try:
            if os.path.exists(tmp_filepath):
                os.remove(tmp_filepath)
        except OSError:
            pass
        raise
    return filepath


def save_automation_run_state(save_directory: str, state: AutomatedRunState) -> str:
    return _write_json_atomic(automation_state_path(save_directory), state.to_mapping())


def load_automation_run_state(save_directory: str) -> AutomatedRunState | None:
    filepath = automation_state_path(save_directory)
    if not os.path.isfile(filepath):
        return None
    with open(filepath, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Automation state must contain a JSON object.")
    return AutomatedRunState.from_mapping(payload)


def automation_state_matches_config(state: AutomatedRunState, config: AutomationRunConfig) -> bool:
    return state.config.run_identity_signature() == config.run_identity_signature()


def load_resume_state_for_config(save_directory: str, config: AutomationRunConfig) -> AutomatedRunState | None:
    """Return saved run state only when it matches the current motion plan."""
    try:
        state = load_automation_run_state(save_directory)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if state is None or not automation_state_matches_config(state, config):
        return None
    return state


def plate_footprint(model: PlateModel, orientation: str) -> tuple[float, float]:
    if orientation in {ORIENTATION_ROTATED_90, ORIENTATION_ROTATED_270}:
        return model.height_mm, model.width_mm
    return model.width_mm, model.height_mm


def plan_plate_slots(config: AutomationRunConfig) -> list[PlateSlot]:
    if config.fixture_calibration is not None:
        plate_indices = tuple(config.fixture_plate_indices)
        if not plate_indices:
            try:
                plate_limit = max(1, min(FIXTURE_PROFILE_PLATE_COUNT, int(config.max_plates)))
            except (TypeError, ValueError):
                plate_limit = FIXTURE_PROFILE_PLATE_COUNT
            plate_indices = tuple(range(1, plate_limit + 1))
        slots: list[PlateSlot] = []
        for plate_index in plate_indices:
            try:
                slot = _calibrated_slot_for_plate(config.fixture_calibration, config.plate_model, plate_index)
            except (KeyError, OverflowError, TypeError, ValueError, ZeroDivisionError):
                return []
            if not all(
                math.isfinite(value)
                for value in (
                    slot.origin_x_mm,
                    slot.origin_y_mm,
                    slot.width_mm,
                    slot.height_mm,
                )
            ):
                return []
            slots.append(slot)
        return slots

    bed = config.bed_area.normalized()
    plate_width, plate_height = plate_footprint(config.plate_model, config.orientation)
    if plate_width <= 0 or plate_height <= 0:
        raise ValueError("Plate dimensions must be positive.")
    if bed.width_mm < plate_width or bed.height_mm < plate_height:
        return []

    step_x = plate_width + max(0.0, config.plate_gap_x_mm)
    step_y = plate_height + max(0.0, config.plate_gap_y_mm)
    columns = int((bed.width_mm + max(0.0, config.plate_gap_x_mm)) // step_x)
    rows = int((bed.height_mm + max(0.0, config.plate_gap_y_mm)) // step_y)
    slots: list[PlateSlot] = []
    for row in range(rows):
        for column in range(columns):
            if len(slots) >= config.max_plates:
                return slots
            origin_x = bed.x_min_mm + (column * step_x)
            origin_y = bed.y_min_mm + (row * step_y)
            if origin_x + plate_width <= bed.x_max_mm + 1e-9 and origin_y + plate_height <= bed.y_max_mm + 1e-9:
                slots.append(
                    PlateSlot(
                        index=len(slots) + 1,
                        row=row + 1,
                        column=column + 1,
                        origin_x_mm=origin_x,
                        origin_y_mm=origin_y,
                        width_mm=plate_width,
                        height_mm=plate_height,
                    )
                )
    return slots


def _well_offset(model: PlateModel, well: str, orientation: str) -> tuple[float, float]:
    row = ord(well[0].upper()) - 65
    column = int(well[1:]) - 1
    x = model.a1_center_x_mm + (column * model.pitch_x_mm)
    y = model.a1_center_y_mm + (row * model.pitch_y_mm)

    if orientation == ORIENTATION_NORMAL:
        return x, y
    if orientation == ORIENTATION_ROTATED_180:
        return model.width_mm - x, model.height_mm - y
    if orientation == ORIENTATION_ROTATED_90:
        return y, model.width_mm - x
    if orientation == ORIENTATION_ROTATED_270:
        return model.height_mm - y, x
    raise ValueError(f"Unsupported plate orientation: {orientation}")


def target_xy_for_slot(config: AutomationRunConfig, slot: PlateSlot, well: str) -> tuple[float, float]:
    if config.fixture_calibration is not None:
        return config.fixture_calibration.well_xy(slot.index, well)
    dx, dy = _well_offset(config.plate_model, well, config.orientation)
    return slot.origin_x_mm + dx, slot.origin_y_mm + dy


def _shot_offset_for_number(config: AutomationRunConfig, shot_number: int) -> AutomationShotOffset:
    index = max(0, int(shot_number) - 1)
    if 0 <= index < len(config.shot_offsets):
        return config.shot_offsets[index]
    return AutomationShotOffset("center", 0.0, 0.0)


def automation_targets(config: AutomationRunConfig) -> list[AutomationTarget]:
    slots = plan_plate_slots(config)
    wells = ordered_wells(config.plate_model.rows, config.plate_model.columns, config.order_mode)
    targets: list[AutomationTarget] = []
    for slot in slots:
        for well in wells:
            center_x, center_y = target_xy_for_slot(config, slot, well)
            for shot_number in range(1, config.shots_per_well + 1):
                offset = _shot_offset_for_number(config, shot_number) if config.shot_offsets else None
                offset_dx = offset.dx_mm if offset is not None else 0.0
                offset_dy = offset.dy_mm if offset is not None else 0.0
                targets.append(
                    AutomationTarget(
                        plate_index=slot.index,
                        well=well,
                        shot_number=shot_number,
                        x_mm=center_x + offset_dx,
                        y_mm=center_y + offset_dy,
                        shot_offset_label=offset.label if offset is not None else "",
                        shot_offset_dx_mm=offset_dx,
                        shot_offset_dy_mm=offset_dy,
                    )
                )
    return targets


def relay_stroke_endpoint(config: AutomationRunConfig, target: AutomationTarget) -> tuple[float, float]:
    """Return the safe endpoint for the Monport relay's powered trigger stroke."""
    bed = effective_bed_area(config)
    stroke = float(config.relay_stroke_mm)
    if stroke <= 0:
        raise ValueError("Relay stroke must be greater than zero.")
    x = float(target.x_mm)
    y = float(target.y_mm)
    eps = 1e-6
    if x < bed.x_min_mm - eps or x > bed.x_max_mm + eps or y < bed.y_min_mm - eps or y > bed.y_max_mm + eps:
        raise ValueError(
            f"Relay trigger start point X{x:g} Y{y:g} is outside bed area "
            f"X{bed.x_min_mm:g} to {bed.x_max_mm:g}, Y{bed.y_min_mm:g} to {bed.y_max_mm:g}."
        )
    if x + stroke <= bed.x_max_mm + eps:
        return x + stroke, y
    if x - stroke >= bed.x_min_mm - eps:
        return x - stroke, y
    raise ValueError(
        f"Relay trigger stroke of {stroke:g} mm does not fit inside bed X range "
        f"{bed.x_min_mm:g} to {bed.x_max_mm:g} mm at X{x:g}."
    )


def relay_stroke_feed_mm_min(config: AutomationRunConfig, target: AutomationTarget) -> float:
    """Feed rate that makes the relay trigger stroke last roughly ``pulse_ms``."""
    end_x, end_y = relay_stroke_endpoint(config, target)
    dx = float(end_x) - float(target.x_mm)
    dy = float(end_y) - float(target.y_mm)
    distance_mm = (dx * dx + dy * dy) ** 0.5
    pulse_s = max(0.001, float(config.pulse_ms) / 1000.0)
    return max(1.0, distance_mm / pulse_s * 60.0)


def unique_position_targets(config: AutomationRunConfig) -> list[AutomationTarget]:
    seen: set[tuple[int, str, float, float]] = set()
    unique: list[AutomationTarget] = []
    for target in automation_targets(config):
        key = (target.plate_index, target.well, round(float(target.x_mm), 6), round(float(target.y_mm), 6))
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return unique


def representative_dry_run_targets(config: AutomationRunConfig) -> list[AutomationTarget]:
    """Return a fast no-fire route that validates the planned run envelope."""
    targets = automation_targets(config)
    if not targets:
        return []

    min_x = min(float(target.x_mm) for target in targets)
    max_x = max(float(target.x_mm) for target in targets)
    min_y = min(float(target.y_mm) for target in targets)
    max_y = max(float(target.y_mm) for target in targets)
    corner_specs = (
        (min_x, min_y, "x"),
        (max_x, min_y, "y"),
        (max_x, max_y, "x"),
        (min_x, max_y, "y"),
    )
    order = {target.key: index for index, target in enumerate(targets)}
    selected: list[AutomationTarget] = []
    seen: set[str] = set()
    for corner_x, corner_y, primary_axis in corner_specs:
        def _corner_sort_key(item: AutomationTarget) -> tuple[float, float, float, int]:
            dx = abs(float(item.x_mm) - corner_x)
            dy = abs(float(item.y_mm) - corner_y)
            primary = dx if primary_axis == "x" else dy
            secondary = dy if primary_axis == "x" else dx
            return (dx * dx + dy * dy, primary, secondary, order[item.key])

        target = min(
            targets,
            key=_corner_sort_key,
        )
        if target.key in seen:
            continue
        seen.add(target.key)
        selected.append(target)
    return selected


def estimate_run_duration_s(config: AutomationRunConfig) -> float:
    """Return a conservative planning estimate for dry-run/automated duration."""
    target_count = len(automation_targets(config))
    unique_positions = len(unique_position_targets(config))
    motion_allowance_s = unique_positions * 0.25
    settle_s = unique_positions * (max(0.0, float(config.settle_ms)) / 1000.0)
    pulse_s = target_count * (max(0.0, float(config.pulse_ms)) / 1000.0)
    capture_s = target_count * max(0.0, min(float(config.capture_timeout_s), 1.0))
    return motion_allowance_s + settle_s + pulse_s + capture_s


def validate_automation_config(config: AutomationRunConfig) -> list[dict[str, str]]:
    """Return user-facing validation messages for a proposed automated plan."""
    messages: list[dict[str, str]] = []
    fixture_messages: list[dict[str, str]] = []
    if config.fixture_calibration is not None:
        fixture_messages = validate_fixture_calibration(config.fixture_calibration, config.plate_model)
    fixture_has_errors = any(item["severity"] == "error" for item in fixture_messages)
    bed = effective_bed_area(config)
    if bed.width_mm <= 0 or bed.height_mm <= 0:
        messages.append({
            "severity": "error",
            "code": "bed_empty",
            "message": "Plate motion area must have positive X and Y size.",
        })
    elif bed.width_mm < 5 or bed.height_mm < 5:
        messages.append({
            "severity": "error",
            "code": "bed_tiny",
            "message": "Plate motion area is too small to use safely.",
        })

    plate_dimensions_ok = (
        math.isfinite(config.plate_model.width_mm)
        and math.isfinite(config.plate_model.height_mm)
        and config.plate_model.width_mm > 0
        and config.plate_model.height_mm > 0
    )
    plate_pitch_ok = (
        math.isfinite(config.plate_model.pitch_x_mm)
        and math.isfinite(config.plate_model.pitch_y_mm)
        and config.plate_model.pitch_x_mm > 0
        and config.plate_model.pitch_y_mm > 0
    )
    if not plate_dimensions_ok:
        messages.append({
            "severity": "error",
            "code": "plate_dimensions",
            "message": "Plate dimensions must be positive.",
        })
    if not plate_pitch_ok:
        messages.append({
            "severity": "error",
            "code": "plate_pitch",
            "message": "Plate well pitch must be positive.",
        })
    messages.extend(fixture_messages)
    if (
        plate_dimensions_ok
        and plate_pitch_ok
        and not fixture_has_errors
        and not plan_plate_slots(config)
    ):
        messages.append({
            "severity": "error",
            "code": "no_fit",
            "message": "No plates fit inside the taught bed area with the selected model and gaps.",
        })

    if not config.plate_model.verified:
        messages.append({
            "severity": "warning",
            "code": "unverified_model",
            "message": "Selected plate model is unverified; confirm measured geometry before guarded firing.",
        })

    if config.laser_s_max <= 0:
        messages.append({
            "severity": "error",
            "code": "laser_s_max",
            "message": "Laser S max must be greater than zero.",
        })
    if not 0 <= config.laser_power_percent <= 100:
        messages.append({
            "severity": "error",
            "code": "laser_power",
            "message": "Laser power percent must be between 0 and 100.",
        })
    elif (
        config.laser_profile != LASER_PROFILE_MONPORT_NDYAG_RELAY
        and config.laser_power_percent > LASER_TUBE_LIFE_POWER_WARNING_PERCENT
    ):
        messages.append({
            "severity": "warning",
            "code": "laser_power_high",
            "message": "Laser power above 70% can reduce tube life; use only when the hardware procedure requires it.",
        })
    if config.pulse_ms <= 0:
        messages.append({
            "severity": "error",
            "code": "pulse_ms",
            "message": "Pulse duration must be greater than zero for guarded firing.",
        })
    elif config.pulse_ms > MAX_LASER_PULSE_MS:
        messages.append({
            "severity": "error",
            "code": "pulse_ms_max",
            "message": "Pulse duration must be 250 ms or less for guarded firing.",
        })
    if config.laser_profile == LASER_PROFILE_MONPORT_NDYAG_RELAY and config.relay_stroke_mm <= 0:
        messages.append({
            "severity": "error",
            "code": "relay_stroke",
            "message": "Relay stroke must be greater than zero for the Monport Nd:YAG relay profile.",
        })
    elif config.laser_profile == LASER_PROFILE_MONPORT_NDYAG_RELAY:
        try:
            for target in automation_targets(config):
                relay_stroke_endpoint(config, target)
        except ValueError as exc:
            messages.append({
                "severity": "error",
                "code": "relay_stroke_bounds",
                "message": str(exc),
            })
    if config.move_speed_mm_min <= 0:
        messages.append({
            "severity": "error",
            "code": "move_speed",
            "message": "Move speed must be greater than zero.",
        })
    if config.capture_timeout_s <= 0:
        messages.append({
            "severity": "error",
            "code": "capture_timeout",
            "message": "Capture timeout must be greater than zero.",
        })
    if config.plate_integration_times_ms and config.column_integration_times_ms:
        messages.append({
            "severity": "error",
            "code": "integration_sweep_conflict",
            "message": (
                "Integration time cannot vary both by column and by plate in the "
                "same run; pick one sweep axis for integration."
            ),
        })
    if config.plate_integration_times_ms:
        planned_plates = len(plan_plate_slots(config))
        schedule_length = len(config.plate_integration_times_ms)
        if planned_plates > schedule_length:
            messages.append({
                "severity": "error",
                "code": "plate_integration_sweep_plates",
                "message": (
                    f"Per-plate integration sweep has {schedule_length} value(s), but "
                    f"{planned_plates} plate(s) are planned. Reduce plates or extend the schedule."
                ),
            })
        elif planned_plates < schedule_length:
            messages.append({
                "severity": "warning",
                "code": "plate_integration_sweep_unused",
                "message": (
                    f"Per-plate integration sweep defines {schedule_length} value(s) but only "
                    f"{planned_plates} plate(s) are planned; the last "
                    f"{schedule_length - planned_plates} value(s) will not be measured."
                ),
            })
    if config.shot_offsets and len(config.shot_offsets) != config.shots_per_well:
        messages.append({
            "severity": "error",
            "code": "shot_offsets",
            "message": "Shot offset count must match shots per well.",
        })
    return messages


def automation_plan_details(config: AutomationRunConfig) -> dict[str, Any]:
    slots = plan_plate_slots(config)
    targets = automation_targets(config)
    unique_positions = unique_position_targets(config)
    plate_names = {
        str(slot.index): config.plate_display_name(slot.index)
        for slot in slots
    }
    plate_folders = {
        str(slot.index): config.plate_folder_name(slot.index)
        for slot in slots
    }
    return {
        "version": PLAN_DETAILS_VERSION,
        "dry_run_signature": config.dry_run_signature(),
        "run_identity_signature": config.run_identity_signature(),
        "config": config.to_mapping(),
        "experiment_name": config.experiment_name,
        "safe_experiment_name": config.safe_experiment_name,
        "run_directory": config.run_directory,
        "plate_names": plate_names,
        "plate_folders": plate_folders,
        "bed": effective_bed_area(config).to_mapping(),
        "manual_bed": config.bed_area.to_mapping(),
        "fixture_calibration": (
            config.fixture_calibration.to_mapping()
            if config.fixture_calibration is not None
            else None
        ),
        "plate_model": config.plate_model.to_mapping(),
        "orientation": config.orientation,
        "plate_gaps": {
            "x_mm": config.plate_gap_x_mm,
            "y_mm": config.plate_gap_y_mm,
        },
        "slots": [slot.to_mapping() for slot in slots],
        "targets": [target.to_mapping() for target in targets],
        "laser_settings": {
            "port": config.laser_port,
            "baudrate": config.laser_baudrate,
            "profile": config.laser_profile,
            "power_percent": config.laser_power_percent,
            "s_max": config.laser_s_max,
            "s_value": config.laser_s_value,
            "pulse_ms": config.pulse_ms,
            "relay_stroke_mm": config.relay_stroke_mm,
        },
        "timing_settings": {
            "move_speed_mm_min": config.move_speed_mm_min,
            "settle_ms": config.settle_ms,
            "capture_timeout_s": config.capture_timeout_s,
            "column_integration_times_ms": list(config.column_integration_times_ms),
            "column_delays_us": list(config.column_delays_us),
            "plate_integration_times_ms": list(config.plate_integration_times_ms),
        },
        "output_naming": {
            "run_folder": "<safe_experiment_name>_<YYYYMMDD_HHMMSS>",
            "plate_folder": "P{plate_index:02d}_<safe_plate_name>",
            "spectrum_file": (
                "<safe_experiment>_<safe_plate>_P{plate_index:02d}_{well}_"
                "shot{shot_number:02d}_<YYYYMMDD_HHMMSS_ffffff>_<global_shot:03d>.csv"
            ),
        },
        "summary": {
            "plates": len(slots),
            "wells_per_plate": config.plate_model.well_count,
            "shots_per_well": config.shots_per_well,
            "target_count": len(targets),
            "estimated_moves": len(unique_positions),
            "estimated_duration_s": estimate_run_duration_s(config),
        },
        "validation": validate_automation_config(config),
    }


def automation_run_metadata_path(save_directory: str) -> str:
    return os.path.join(save_directory, AUTOMATION_RUN_METADATA_FILENAME)


def save_automation_run_metadata(
    save_directory: str,
    *,
    config: AutomationRunConfig,
    created_at: datetime | None = None,
    spectrometer_info: dict[str, Any] | None = None,
    safety_checklist: dict[str, bool] | None = None,
    laser_safety: dict[str, Any] | None = None,
    run_mode: str | None = None,
) -> str:
    created = created_at or datetime.now()
    slots = plan_plate_slots(config)
    lines = [
        "Automated Acquisition Run Metadata",
        f"Created local: {created.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Experiment name: {config.experiment_name}",
        f"Safe experiment name: {config.safe_experiment_name}",
        f"Run folder: {os.path.abspath(save_directory)}",
    ]
    if run_mode:
        lines.append(f"Run mode: {run_mode}")
    lines.extend([
        "",
        "Plate plan",
        f"Plate model: {config.plate_model.display_name}",
        f"Plate model id: {config.plate_model.id}",
        f"Plate count: {len(slots)}",
        f"Wells per plate: {config.plate_model.well_count}",
        f"Shots per well: {config.shots_per_well}",
        f"Order mode: {config.order_mode}",
        f"Orientation: {config.orientation}",
        f"Bed X mm: {config.bed_area.normalized().x_min_mm:g} to {config.bed_area.normalized().x_max_mm:g}",
        f"Bed Y mm: {config.bed_area.normalized().y_min_mm:g} to {config.bed_area.normalized().y_max_mm:g}",
        f"Plate gap X mm: {config.plate_gap_x_mm:g}",
        f"Plate gap Y mm: {config.plate_gap_y_mm:g}",
    ])
    if config.shot_offsets:
        lines.extend(["", "Shot offsets"])
        for index, offset in enumerate(config.shot_offsets, start=1):
            lines.append(
                f"shot{index:02d}: {offset.label} | dx_mm={offset.dx_mm:g} | dy_mm={offset.dy_mm:g}"
            )
    lines.extend(["", "Plates"])
    for slot in slots:
        lines.append(
            f"P{slot.index:02d}: {config.plate_display_name(slot.index)} | folder={config.plate_folder_name(slot.index)}"
        )

    lines.extend([
        "",
        "Laser settings",
        f"Port: {config.laser_port}",
        f"Baudrate: {config.laser_baudrate}",
        f"Profile: {config.laser_profile}",
        f"Power percent: {config.laser_power_percent:g}",
        f"S max: {config.laser_s_max}",
        f"S value: {config.laser_s_value}",
        f"Pulse ms: {config.pulse_ms:g}",
        f"Relay stroke mm: {config.relay_stroke_mm:g}",
        f"Move speed mm/min: {config.move_speed_mm_min:g}",
        f"Settle ms: {config.settle_ms:g}",
        f"Capture timeout s: {config.capture_timeout_s:g}",
    ])
    if config.column_integration_times_ms:
        lines.extend([
            "",
            "Integration sweep by column (ms)",
            ", ".join(f"{value:g}" for value in config.column_integration_times_ms),
        ])
    if config.column_delays_us:
        lines.extend([
            "",
            "Trigger-delay sweep by column (us)",
            ", ".join(f"{value:g}" for value in config.column_delays_us),
        ])
    if config.plate_integration_times_ms:
        lines.extend([
            "",
            "Integration sweep by plate (ms)",
            ", ".join(f"{value:g}" for value in config.plate_integration_times_ms),
        ])
    lines.extend([
        "",
        "Safety checklist",
    ])
    for key, value in sorted(dict(safety_checklist or {}).items()):
        lines.append(f"{key}: {bool(value)}")
    if not safety_checklist:
        lines.append("No safety checklist captured yet.")

    lines.extend(["", "Laser controller safety"])
    for key, value in sorted(dict(laser_safety or {}).items()):
        if isinstance(value, (dict, list, tuple, set)):
            value = json.dumps(list(value) if isinstance(value, set) else value, sort_keys=True)
        lines.append(f"{key}: {value}")
    if not laser_safety:
        lines.append("No laser controller safety snapshot captured yet.")

    lines.extend(["", "Spectrometer"])
    for key, value in sorted(dict(spectrometer_info or {}).items()):
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, sort_keys=True)
        lines.append(f"{key}: {value}")
    if not spectrometer_info:
        lines.append("No spectrometer metadata captured yet.")

    filepath = automation_run_metadata_path(save_directory)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp_filepath = f"{filepath}.tmp"
    try:
        with open(tmp_filepath, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
            handle.write("\n")
        os.replace(tmp_filepath, filepath)
    except Exception:
        try:
            if os.path.exists(tmp_filepath):
                os.remove(tmp_filepath)
        except OSError:
            pass
        raise
    return filepath


def safety_checklist_mapping(
    *,
    cover: bool = False,
    cooling: bool = False,
    exhaust: bool = False,
    grounding: bool = False,
    estop: bool = False,
    material: bool = False,
    unverified_model_ack: bool = False,
) -> dict[str, bool]:
    return {
        "cover_closed": bool(cover),
        "cooling_verified": bool(cooling),
        "exhaust_running": bool(exhaust),
        "grounding_power_checked": bool(grounding),
        "estop_tested_reachable": bool(estop),
        "material_safe": bool(material),
        "unverified_model_acknowledged": bool(unverified_model_ack),
    }


def automation_manifest_payload(
    *,
    config: AutomationRunConfig,
    state: AutomatedRunState | None = None,
    dry_run_completed: bool = False,
    safety_checklist: dict[str, bool] | None = None,
    command_transcript: list[str] | None = None,
    qc: dict[str, Any] | None = None,
    laser_safety: dict[str, Any] | None = None,
    event: str | None = None,
    run_mode: str | None = None,
) -> dict[str, Any]:
    payload = {
        "version": 1,
        "event": event,
        "plan": automation_plan_details(config),
        "dry_run_completed": bool(dry_run_completed),
        "completed_targets": sorted(state.completed_targets) if state else [],
        "active_target": state.active_target if state else None,
        "abort_reason": state.abort_reason if state else None,
        "dry_run_signature": state.dry_run_signature if state else config.dry_run_signature(),
        "gcode_command_transcript": list(command_transcript or []),
        "safety_checklist": dict(safety_checklist or {}),
        "laser_safety": dict(laser_safety or {}),
        "qc": dict(qc or {}),
    }
    if run_mode:
        payload["run_mode"] = run_mode
    return payload


def save_automation_manifest(
    save_directory: str,
    *,
    config: AutomationRunConfig,
    state: AutomatedRunState | None = None,
    dry_run_completed: bool = False,
    safety_checklist: dict[str, bool] | None = None,
    command_transcript: list[str] | None = None,
    qc: dict[str, Any] | None = None,
    laser_safety: dict[str, Any] | None = None,
    event: str | None = None,
    run_mode: str | None = None,
) -> str:
    payload = automation_manifest_payload(
        config=config,
        state=state,
        dry_run_completed=dry_run_completed,
        safety_checklist=safety_checklist,
        command_transcript=command_transcript,
        qc=qc,
        laser_safety=laser_safety,
        event=event,
        run_mode=run_mode,
    )
    return _write_json_atomic(automation_manifest_path(save_directory), payload)


def automation_run_summary_payload(
    *,
    config: AutomationRunConfig,
    state: AutomatedRunState,
    save_directory: str,
    timing_summary: dict[str, Any] | None = None,
    run_mode: str | None = None,
) -> dict[str, Any]:
    targets = automation_targets(config)
    target_keys = {target.key for target in targets}
    completed = target_keys.intersection(state.completed_targets)
    incomplete = sorted(target_keys.difference(completed))
    wells_completed = {
        (target.plate_index, target.well)
        for target in targets
        if all(
            f"P{target.plate_index:02d}:{target.well}:S{shot:02d}" in completed
            for shot in range(1, config.shots_per_well + 1)
        )
    }
    complete_plate_indexes = {
        plate_index
        for plate_index in {target.plate_index for target in targets}
        if sum(1 for item in wells_completed if item[0] == plate_index) >= config.plate_model.well_count
    }
    plate_paths = [
        os.path.join(save_directory, config.plate_folder_name(plate_index))
        for plate_index in sorted({target.plate_index for target in targets})
    ]
    payload = {
        "version": 1,
        "dry_run_signature": config.dry_run_signature(),
        "run_identity_signature": config.run_identity_signature(),
        "experiment_name": config.experiment_name,
        "plates_total": len(plan_plate_slots(config)),
        "plates_completed": len(complete_plate_indexes),
        "wells_completed": len(wells_completed),
        "shots_saved": len(completed),
        "targets_total": len(targets),
        "skipped_or_incomplete_targets": incomplete,
        "abort_reason": state.abort_reason,
        "timing_summary": dict(timing_summary or {}),
        "save_directory": save_directory,
        "plate_paths": plate_paths,
    }
    if run_mode:
        payload["run_mode"] = run_mode
    return payload


def save_automation_run_summary(
    save_directory: str,
    *,
    config: AutomationRunConfig,
    state: AutomatedRunState,
    timing_summary: dict[str, Any] | None = None,
    run_mode: str | None = None,
) -> str:
    payload = automation_run_summary_payload(
        config=config,
        state=state,
        save_directory=save_directory,
        timing_summary=timing_summary,
        run_mode=run_mode,
    )
    return _write_json_atomic(automation_summary_path(save_directory), payload)
