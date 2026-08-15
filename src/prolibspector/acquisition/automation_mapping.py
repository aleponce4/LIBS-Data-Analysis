"""2D mapping acquisition plan and persistence helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import csv
import hashlib
import json
import math
import os
from typing import Any

from prolibspector.acquisition.automation import (
    DEFAULT_RELAY_STROKE_MM,
    DELAY_SWEEP_COLUMN_DELAYS_US,
    LASER_PROFILE_GENERIC_GRBL,
    LASER_PROFILES,
    LASER_PROFILE_MONPORT_NDYAG_RELAY,
    LASER_TUBE_LIFE_POWER_WARNING_PERCENT,
    MAX_LASER_PULSE_MS,
    MotionSafetyLimits,
    load_motion_safety_limits,
    normalized_column_delay_schedule,
    normalized_integration_schedule,
    validate_motion_safety_points,
)
from prolibspector.acquisition.plate_autosave import sanitize_filename_part
from prolibspector.acquisition.mapping_spectrum_store import mapping_spectrum_store_summary
from prolibspector.hardware.grbl_laser import StreamLine


MAPPING_RUN_TYPE = "mapping"
MAPPING_STATE_FILENAME = "_mapping_grid_state.json"
MAPPING_MANIFEST_FILENAME = "_mapping_grid_manifest.json"
MAPPING_INDEX_FILENAME = "_mapping_grid_index.csv"
MAPPING_TIMING_SAMPLES_FILENAME = "_mapping_timing_samples.csv"
MAPPING_BACKGROUND_DIRNAME = "_mapping_background"
MAPPING_BACKGROUND_REFERENCE_FILENAME = "_mapping_background_reference.csv"
MAPPING_BACKGROUND_METADATA_FILENAME = "metadata.json"

MAPPING_TIMING_COLUMNS = [
    "recorded_at",
    "shot_index",
    "target_key",
    "point_key",
    "row_index",
    "column_index",
    "shot_number",
    "mode",
    "effective_move_speed_mm_min",
    "trigger_delay_us",
    "motion_command_ms",
    "idle_wait_ms",
    "settle_ms",
    "interlock_ms",
    "trigger_read_ms",
    "wavelengths_fetch_ms",
    "save_ms",
    "state_write_ms",
    "cycle_ms",
    "loop_gap_ms",
    "gui_queue_ms",
    "burst_shot",
    "stream_segment_index",
    "frame_interval_ms",
    "scheduled_fire_offset_ms",
    "hold_events",
]
DEFAULT_MAPPING_STEP_MM = 1.0
MIN_MAPPING_STEP_MM = 0.254
NOMINAL_MAPPING_LINE_WIDTH_MM = 0.508
DEFAULT_MAPPING_MOVE_SPEED_MM_MIN = 12000.0
# Production-validated LIBS capture window (docs/trigger_delay_sweep.md). The
# generic capture default (50 ms) silently costs ~120 ms per mapping shot.
DEFAULT_MAPPING_INTEGRATION_TIME_MS = 0.08
MAX_MAPPING_MOVE_SPEED_MM_MIN = 21000.0
MAX_PROGRESS_STATIC_TARGETS = 2500

# Plate-free trigger-delay sweep: each mapping grid column is captured with
# one delay from this schedule, on a plain metal blank. Shares the plate
# preset's draft schedule so both modes stay comparable.
MAPPING_DELAY_SWEEP_COLUMN_DELAYS_US = DELAY_SWEEP_COLUMN_DELAYS_US

# Joint delay x integration screen on a metal blank: delay by grid column
# BLOCK, integration time by contiguous row band, replicates = the block's
# columns x the band's rows. Tiling the replicates in both directions keeps
# the footprint near-square (24 x 20 points with the defaults) so a small
# rectangular blank works. The delay axis is trimmed to where the 1D sweep
# shows structure; the integration axis spans the published 9-us minimum to
# well past plasma death. Draft values until the replacement SDK fixes the
# real ranges.
MAPPING_JOINT_SWEEP_COLUMN_DELAYS_US = (0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0)
MAPPING_JOINT_SWEEP_ROW_INTEGRATION_TIMES_MS = (0.009, 0.03, 0.1, 0.3, 1.0)
MAPPING_JOINT_SWEEP_DEFAULT_COLUMNS_PER_DELAY = 3
MAPPING_JOINT_SWEEP_DEFAULT_ROWS_PER_BAND = 4

# Interleaved paired-settings sweeps. Each (delay_us, integration_ms) pair is
# assigned per grid point along Latin diagonals — pair = (column + row) mod N —
# so every setting's replicates are scattered across the whole plate instead of
# occupying one contiguous block (position/focus gradients then inflate the
# per-setting spread instead of biasing one setting). Presets follow the laser
# characterization (docs/laser_pulse_temporal_structure.md): pump energy sets
# the PULSE COUNT, so single- and multi-pulse operation need different windows.
#
# Single-pulse confirmation (laser at 200 mJ = 1 deterministic pulse): the
# four settings shortlisted by the merged timing sweeps, ~30 replicates each.
MAPPING_SINGLE_PULSE_CONFIRMATION_PAIRS = (
    (0.0, 0.03),
    (0.0, 0.1),
    (0.0, 0.3),
    (0.5, 0.009),
)
MAPPING_SINGLE_PULSE_ENERGY_MJ = 200.0
# Pulse-train ladder (laser at 700 mJ = 3 pulses, first separation
# 24.5 +/- 1.7 us): 700 mJ keeps the laser at its 10 Hz repetition rate,
# which 1000 mJ cannot sustain, while still delivering a fully reproducible
# multi-pulse train. 15 us windows isolate each pulse's plasma without
# saturation; 80 us windows starting at the same delays capture the train
# minus the first k pulses (per-pulse contributions by subtraction, robust
# to jitter). Because timing CV is 7% at this energy, the pulse-2/3 windows
# OPEN ~4 us before the estimated arrival so an early pulse cannot slip out;
# refine the arrivals from the saved oscilloscope waveforms if they disagree.
MAPPING_PULSE_TRAIN_LADDER_PAIRS = (
    (0.0, 0.015),
    (20.0, 0.015),
    (45.0, 0.015),
    (0.0, 0.08),
    (20.0, 0.08),
    (45.0, 0.08),
)
MAPPING_PULSE_TRAIN_ENERGY_MJ = 700.0
# Pump energies characterized on the oscilloscope (5 shots each; see
# docs/laser_pulse_temporal_structure.md). Runs record which one was used —
# the laser is dialed by hand, so the operator must declare it.
LASER_CHARACTERIZED_ENERGIES_MJ = (200, 300, 400, 500, 600, 700, 800, 900, 1000)
# Production best (measured 2026-07-26, cross-energy comparison): 700 mJ with
# 0 us delay + 0.08 ms integration — 77 persistent lines, no saturation, and
# the laser holds 10 Hz. 200 mJ + 0.1 ms is the clean single-pulse reference.
LASER_RECOMMENDED_ENERGY_MJ = 700
# Both presets tile a 12 x 10 grid (120 points): 30 replicates per pair for
# the 4-pair confirmation, 12 per pair for the 10-pair ladder.
MAPPING_INTERLEAVED_SWEEP_COLUMNS = 12
MAPPING_INTERLEAVED_SWEEP_ROWS = 10


def normalized_paired_settings(values) -> tuple[tuple[float, float], ...]:
    """Sanitize a (delay_us, integration_ms) pair list."""
    pairs: list[tuple[float, float]] = []
    if isinstance(values, (list, tuple)):
        for item in values:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                delay_us = float(item[0])
                integration_ms = float(item[1])
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(delay_us) and math.isfinite(integration_ms)):
                continue
            if delay_us < 0.0 or integration_ms <= 0.0:
                continue
            pairs.append((delay_us, integration_ms))
    return tuple(pairs)


def mapping_paired_setting(
    config: "MappingGridConfig", row_index: int, column_index: int
) -> tuple[float, float] | None:
    """(delay_us, integration_ms) for a grid point in an interleaved sweep.

    Latin-diagonal assignment: consecutive points in a row cycle through every
    pair, and each row starts one pair later than the previous, so no setting
    forms a contiguous row/column stripe and equal grid areas get near-equal
    replicate counts (exactly equal when points divide evenly by the pairs).
    """
    if not config.paired_settings:
        return None
    count = len(config.paired_settings)
    index = (max(1, int(column_index)) - 1 + max(1, int(row_index)) - 1) % count
    return config.paired_settings[index]


def decorrelated_schedule(values: tuple[float, ...] | list[float]) -> tuple[float, ...]:
    """Reorder a sweep schedule so setting value and grid position are
    uncorrelated.

    An ascending schedule assigns the largest values to one side of the
    grid, so any smooth spatial gradient (beam-alignment drift through the
    XY mirror path, surface tilt, focus change) masquerades as a
    delay/integration effect. Weaving the sorted values low, high,
    second-lowest, second-highest, ... makes the value-vs-position
    correlation approximately zero while keeping the order deterministic
    and reproducible. The analysis groups shots by the recorded per-shot
    value, so schedule order never affects the metrics themselves.
    """
    ordered = sorted(float(value) for value in values)
    woven: list[float] = []
    low, high = 0, len(ordered) - 1
    while low <= high:
        woven.append(ordered[low])
        low += 1
        if low <= high:
            woven.append(ordered[high])
            high -= 1
    return tuple(woven)


@dataclass(frozen=True)
class MappingGridTarget:
    row_index: int
    column_index: int
    shot_number: int
    x_mm: float
    y_mm: float

    @property
    def point_key(self) -> str:
        return f"R{self.row_index:03d}:C{self.column_index:03d}"

    @property
    def target_key(self) -> str:
        return f"{self.point_key}:S{self.shot_number:02d}"

    @property
    def key(self) -> str:
        return self.target_key

    def to_mapping(self) -> dict[str, Any]:
        return {
            "row_index": self.row_index,
            "column_index": self.column_index,
            "shot_number": self.shot_number,
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "point_key": self.point_key,
            "target_key": self.target_key,
            "key": self.key,
        }


@dataclass(frozen=True)
class MappingGridConfig:
    experiment_name: str = "MappingRun"
    run_directory: str = ""
    origin_x_mm: float = 0.0
    origin_y_mm: float = 0.0
    x_length_mm: float = 10.0
    y_length_mm: float = 10.0
    step_mm: float = DEFAULT_MAPPING_STEP_MM
    spot_diameter_mm: float = NOMINAL_MAPPING_LINE_WIDTH_MM
    shots_per_point: int = 1
    laser_port: str = ""
    laser_baudrate: int = 115200
    laser_profile: str = LASER_PROFILE_GENERIC_GRBL
    laser_s_max: int = 1000
    laser_power_percent: float = 10.0
    pulse_ms: float = 50.0
    relay_stroke_mm: float = DEFAULT_RELAY_STROKE_MM
    move_speed_mm_min: float = DEFAULT_MAPPING_MOVE_SPEED_MM_MIN
    settle_ms: float = 100.0
    capture_timeout_s: float = 10.0
    write_csv_files: bool = False
    column_delays_us: tuple[float, ...] = ()
    columns_per_delay: int = 1
    row_integration_times_ms: tuple[float, ...] = ()
    paired_settings: tuple[tuple[float, float], ...] = ()
    laser_energy_mj: float = 0.0
    # Streamed-row acquisition (opt-in, experimental): rows execute as one
    # continuous GRBL job instead of per-point stop-and-confirm cycles.
    streaming_enabled: bool = False
    stream_cadence_ms: float = 125.0

    def __post_init__(self) -> None:
        experiment_name = str(self.experiment_name or "MappingRun").strip() or "MappingRun"
        profile = str(self.laser_profile or LASER_PROFILE_GENERIC_GRBL).strip()
        if profile not in LASER_PROFILES:
            profile = LASER_PROFILE_GENERIC_GRBL
        object.__setattr__(self, "experiment_name", experiment_name)
        object.__setattr__(self, "run_directory", str(self.run_directory or "").strip())
        object.__setattr__(self, "origin_x_mm", float(self.origin_x_mm))
        object.__setattr__(self, "origin_y_mm", float(self.origin_y_mm))
        object.__setattr__(self, "x_length_mm", float(self.x_length_mm))
        object.__setattr__(self, "y_length_mm", float(self.y_length_mm))
        object.__setattr__(self, "step_mm", float(self.step_mm))
        object.__setattr__(self, "spot_diameter_mm", max(0.0, float(self.spot_diameter_mm)))
        object.__setattr__(self, "shots_per_point", int(self.shots_per_point))
        object.__setattr__(self, "laser_baudrate", max(1, int(self.laser_baudrate)))
        object.__setattr__(self, "laser_profile", profile)
        object.__setattr__(self, "laser_s_max", max(1, int(self.laser_s_max)))
        object.__setattr__(self, "laser_power_percent", max(0.0, min(100.0, float(self.laser_power_percent))))
        object.__setattr__(self, "pulse_ms", max(0.0, float(self.pulse_ms)))
        object.__setattr__(self, "relay_stroke_mm", max(0.0, float(self.relay_stroke_mm)))
        object.__setattr__(self, "move_speed_mm_min", max(1.0, float(self.move_speed_mm_min)))
        object.__setattr__(self, "settle_ms", max(0.0, float(self.settle_ms)))
        object.__setattr__(self, "capture_timeout_s", max(0.1, float(self.capture_timeout_s)))
        object.__setattr__(self, "write_csv_files", bool(self.write_csv_files))
        object.__setattr__(self, "column_delays_us", normalized_column_delay_schedule(self.column_delays_us))
        try:
            columns_per_delay = max(1, int(self.columns_per_delay))
        except (TypeError, ValueError):
            columns_per_delay = 1
        object.__setattr__(self, "columns_per_delay", columns_per_delay)
        object.__setattr__(self, "row_integration_times_ms", normalized_integration_schedule(self.row_integration_times_ms))
        object.__setattr__(self, "paired_settings", normalized_paired_settings(self.paired_settings))
        try:
            laser_energy_mj = max(0.0, float(self.laser_energy_mj))
        except (TypeError, ValueError):
            laser_energy_mj = 0.0
        object.__setattr__(self, "laser_energy_mj", laser_energy_mj)
        object.__setattr__(self, "streaming_enabled", bool(self.streaming_enabled))
        try:
            stream_cadence_ms = float(self.stream_cadence_ms)
        except (TypeError, ValueError):
            stream_cadence_ms = 125.0
        if not math.isfinite(stream_cadence_ms) or stream_cadence_ms <= 0:
            stream_cadence_ms = 125.0
        object.__setattr__(self, "stream_cadence_ms", stream_cadence_ms)

    @property
    def safe_experiment_name(self) -> str:
        return sanitize_filename_part(self.experiment_name, fallback="MappingRun")

    @property
    def laser_s_value(self) -> int:
        fraction = max(0.0, min(100.0, float(self.laser_power_percent))) / 100.0
        return int(round(max(0, int(self.laser_s_max)) * fraction))

    @property
    def covered_x_max_mm(self) -> float:
        _rows, columns = mapping_grid_shape(self)
        return float(self.origin_x_mm) + (max(0, columns - 1) * float(self.step_mm))

    @property
    def covered_y_max_mm(self) -> float:
        rows, _columns = mapping_grid_shape(self)
        return float(self.origin_y_mm) + (max(0, rows - 1) * float(self.step_mm))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "run_type": MAPPING_RUN_TYPE,
            "experiment_name": self.experiment_name,
            "safe_experiment_name": self.safe_experiment_name,
            "run_directory": self.run_directory,
            "origin_x_mm": self.origin_x_mm,
            "origin_y_mm": self.origin_y_mm,
            "x_length_mm": self.x_length_mm,
            "y_length_mm": self.y_length_mm,
            "step_mm": self.step_mm,
            "spot_diameter_mm": self.spot_diameter_mm,
            "shots_per_point": self.shots_per_point,
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
            "write_csv_files": self.write_csv_files,
            "column_delays_us": list(self.column_delays_us),
            "columns_per_delay": self.columns_per_delay,
            "row_integration_times_ms": list(self.row_integration_times_ms),
            "paired_settings": [list(pair) for pair in self.paired_settings],
            "laser_energy_mj": self.laser_energy_mj,
            "streaming_enabled": self.streaming_enabled,
            "stream_cadence_ms": self.stream_cadence_ms,
        }

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "MappingGridConfig":
        return cls(
            experiment_name=str(values.get("experiment_name", "MappingRun") or "MappingRun"),
            run_directory=str(values.get("run_directory", "") or ""),
            origin_x_mm=float(values.get("origin_x_mm", 0.0)),
            origin_y_mm=float(values.get("origin_y_mm", 0.0)),
            x_length_mm=float(values.get("x_length_mm", 10.0)),
            y_length_mm=float(values.get("y_length_mm", 10.0)),
            step_mm=float(values.get("step_mm", DEFAULT_MAPPING_STEP_MM)),
            spot_diameter_mm=float(values.get("spot_diameter_mm", NOMINAL_MAPPING_LINE_WIDTH_MM)),
            shots_per_point=int(values.get("shots_per_point", 1)),
            laser_port=str(values.get("laser_port", "")),
            laser_baudrate=int(values.get("laser_baudrate", 115200)),
            laser_profile=str(values.get("laser_profile", LASER_PROFILE_GENERIC_GRBL) or LASER_PROFILE_GENERIC_GRBL),
            laser_s_max=int(values.get("laser_s_max", 1000)),
            laser_power_percent=float(values.get("laser_power_percent", 10.0)),
            pulse_ms=float(values.get("pulse_ms", 50.0)),
            relay_stroke_mm=float(values.get("relay_stroke_mm", DEFAULT_RELAY_STROKE_MM)),
            move_speed_mm_min=float(values.get("move_speed_mm_min", DEFAULT_MAPPING_MOVE_SPEED_MM_MIN)),
            settle_ms=float(values.get("settle_ms", 100.0)),
            capture_timeout_s=float(values.get("capture_timeout_s", 10.0)),
            write_csv_files=bool(values.get("write_csv_files", False)),
            column_delays_us=tuple(
                values.get("column_delays_us", ())
                if isinstance(values.get("column_delays_us", ()), (list, tuple))
                else ()
            ),
            columns_per_delay=int(values.get("columns_per_delay", 1) or 1),
            row_integration_times_ms=tuple(
                values.get("row_integration_times_ms", ())
                if isinstance(values.get("row_integration_times_ms", ()), (list, tuple))
                else ()
            ),
            paired_settings=normalized_paired_settings(values.get("paired_settings", ())),
            laser_energy_mj=float(values.get("laser_energy_mj", 0.0) or 0.0),
            streaming_enabled=bool(values.get("streaming_enabled", False)),
            stream_cadence_ms=float(values.get("stream_cadence_ms", 125.0) or 125.0),
        )

    def _signature_mapping(self) -> dict[str, Any]:
        relevant = MappingGridConfig.from_mapping(self.to_mapping()).to_mapping()
        relevant.pop("laser_port", None)
        relevant.pop("run_directory", None)
        relevant.pop("write_csv_files", None)
        # Streaming is an execution/IO concern: it visits the same points with
        # the same strokes, so toggling it must not invalidate saved resume
        # states or dry-run vouchers.
        relevant.pop("streaming_enabled", None)
        relevant.pop("stream_cadence_ms", None)
        return relevant

    def dry_run_signature(self) -> str:
        relevant = self._signature_mapping()
        relevant.pop("experiment_name", None)
        relevant.pop("safe_experiment_name", None)
        relevant.pop("shots_per_point", None)
        # Spot diameter is a validation/annotation input, not motion.
        relevant.pop("spot_diameter_mm", None)
        relevant.pop("laser_baudrate", None)
        relevant.pop("laser_s_max", None)
        relevant.pop("laser_power_percent", None)
        relevant.pop("pulse_ms", None)
        relevant.pop("settle_ms", None)
        relevant.pop("capture_timeout_s", None)
        relevant.pop("column_delays_us", None)
        relevant.pop("columns_per_delay", None)
        relevant.pop("row_integration_times_ms", None)
        # Paired settings and the intended laser energy annotate the shots;
        # they do not change the motion path the dry run vouches for.
        relevant.pop("paired_settings", None)
        relevant.pop("laser_energy_mj", None)
        payload = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def run_identity_signature(self) -> str:
        payload = json.dumps(self._signature_mapping(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


MAPPING_ABORT_EVENT_HISTORY_LIMIT = 50


@dataclass
class MappingRunState:
    config: MappingGridConfig
    completed_targets: set[str] = field(default_factory=set)
    active_target: str | None = None
    dry_run_signature: str | None = None
    abort_reason: str | None = None
    abort_events: list[dict[str, Any]] = field(default_factory=list)
    # Targets a streamed segment ablated but whose frames were discarded on a
    # frame/stroke mismatch: permanently skipped (never re-shot) across
    # resumes; the index records them with an exclude_reason.
    missing_targets: set[str] = field(default_factory=set)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": 1,
            "config": self.config.to_mapping(),
            "completed_targets": sorted(self.completed_targets),
            "active_target": self.active_target,
            "dry_run_signature": self.dry_run_signature,
            "abort_reason": self.abort_reason,
            "abort_events": list(self.abort_events),
            "missing_targets": sorted(self.missing_targets),
        }

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "MappingRunState":
        raw_events = values.get("abort_events") or []
        return cls(
            config=MappingGridConfig.from_mapping(values["config"]),
            completed_targets=set(str(item) for item in values.get("completed_targets", [])),
            active_target=values.get("active_target"),
            dry_run_signature=values.get("dry_run_signature"),
            abort_reason=values.get("abort_reason"),
            abort_events=[dict(event) for event in raw_events if isinstance(event, dict)],
            missing_targets=set(str(item) for item in values.get("missing_targets", [])),
        )

    def record_started(self, target: MappingGridTarget) -> None:
        self.active_target = target.key

    def record_completed(self, target: MappingGridTarget) -> None:
        self.completed_targets.add(target.key)
        self.active_target = None
        self.abort_reason = None

    def record_missing(self, target_key: str) -> None:
        self.missing_targets.add(str(target_key))

    def is_target_done(self, target_key: str) -> bool:
        return target_key in self.completed_targets or target_key in self.missing_targets

    def record_abort(self, reason: str) -> None:
        self.abort_reason = str(reason)
        # Aborts accumulate across resume sessions (like pause_events), so a
        # run that stops repeatedly keeps evidence of every failure, not just
        # the last one.
        self.abort_events.append(
            {
                "reason": str(reason),
                "recorded_at": datetime.now().isoformat(timespec="milliseconds"),
                "active_target": self.active_target,
                "completed_count": len(self.completed_targets),
            }
        )
        del self.abort_events[:-MAPPING_ABORT_EVENT_HISTORY_LIMIT]
        self.active_target = None


def _axis_values(length_mm: float, step_mm: float) -> list[float]:
    length = float(length_mm)
    step = float(step_mm)
    if length < 0 or step <= 0:
        return []
    values: list[float] = []
    index = 0
    eps = 1e-9
    while True:
        value = index * step
        if value > length + eps:
            break
        values.append(min(value, length))
        index += 1
    return values or [0.0]


def mapping_grid_targets(config: MappingGridConfig) -> list[MappingGridTarget]:
    x_offsets = _axis_values(config.x_length_mm, config.step_mm)
    y_offsets = _axis_values(config.y_length_mm, config.step_mm)
    targets: list[MappingGridTarget] = []
    for row_index, y_offset in enumerate(y_offsets, start=1):
        for column_index, x_offset in enumerate(x_offsets, start=1):
            x = float(config.origin_x_mm) + float(x_offset)
            y = float(config.origin_y_mm) + float(y_offset)
            for shot_number in range(1, int(config.shots_per_point) + 1):
                targets.append(
                    MappingGridTarget(
                        row_index=row_index,
                        column_index=column_index,
                        shot_number=shot_number,
                        x_mm=x,
                        y_mm=y,
                    )
                )
    return targets


def mapping_grid_shape(config: MappingGridConfig) -> tuple[int, int]:
    columns = len(_axis_values(config.x_length_mm, config.step_mm))
    rows = len(_axis_values(config.y_length_mm, config.step_mm))
    return rows, columns


def mapping_target_count(config: MappingGridConfig) -> int:
    rows, columns = mapping_grid_shape(config)
    return rows * columns * max(0, int(config.shots_per_point))


def mapping_column_delay_us(config: MappingGridConfig, column_index: int) -> float | None:
    """Trigger delay (µs) for a grid column, or None when no delay sweep is set.

    Columns are grouped into blocks of ``columns_per_delay``; every column in
    a block is a replicate of the same delay, which lets the sweep footprint
    stay near-square instead of one tall strip per delay.
    """
    if not config.column_delays_us:
        return None
    block = (int(column_index) - 1) // max(1, int(config.columns_per_delay))
    if block < 0 or block >= len(config.column_delays_us):
        raise ValueError(
            f"Trigger-delay sweep covers {len(config.column_delays_us) * config.columns_per_delay} "
            f"column(s) ({len(config.column_delays_us)} delay(s) x {config.columns_per_delay} column(s) each), "
            f"but column {column_index} is outside that layout."
        )
    return float(config.column_delays_us[block])


def mapping_rows_per_integration_band(config: MappingGridConfig) -> int:
    """Rows per integration band, or 0 when the layout is absent/invalid.

    A joint sweep splits the grid rows into ``len(row_integration_times_ms)``
    contiguous bands (row 1 at the origin belongs to the first band). The row
    count must divide evenly so every integration gets the same replicate
    count.
    """
    if not config.row_integration_times_ms:
        return 0
    rows, _columns = mapping_grid_shape(config)
    bands = len(config.row_integration_times_ms)
    if rows <= 0 or rows % bands != 0:
        return 0
    return rows // bands


def mapping_row_band_integration_time_ms(config: MappingGridConfig, row_index: int) -> float | None:
    """Integration time (ms) for a grid row, or None when no row sweep is set."""
    if not config.row_integration_times_ms:
        return None
    rows_per_band = mapping_rows_per_integration_band(config)
    if rows_per_band <= 0:
        rows, _columns = mapping_grid_shape(config)
        raise ValueError(
            f"Row integration sweep has {len(config.row_integration_times_ms)} band(s), "
            f"but the grid has {rows} row(s); rows must divide evenly into bands."
        )
    band = (int(row_index) - 1) // rows_per_band
    if band < 0 or band >= len(config.row_integration_times_ms):
        raise ValueError(f"Row {row_index} is outside the integration band layout.")
    return float(config.row_integration_times_ms[band])


def _point_key_from_target_key(target_key: str) -> str:
    parts = str(target_key or "").split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return ""


def completed_mapping_point_keys(completed_targets: set[str], shots_per_point: int) -> set[str]:
    required = max(1, int(shots_per_point))
    if required <= 1:
        return {
            point_key
            for point_key in (_point_key_from_target_key(target_key) for target_key in completed_targets)
            if point_key
        }
    counts: dict[str, set[str]] = {}
    for target_key in completed_targets:
        point_key = _point_key_from_target_key(target_key)
        if not point_key:
            continue
        counts.setdefault(point_key, set()).add(str(target_key))
    return {
        point_key
        for point_key, target_keys in counts.items()
        if len(target_keys) >= required
    }


def unique_mapping_position_targets(config: MappingGridConfig) -> list[MappingGridTarget]:
    seen: set[tuple[int, int, float, float]] = set()
    unique: list[MappingGridTarget] = []
    for target in mapping_grid_targets(config):
        key = (target.row_index, target.column_index, round(target.x_mm, 6), round(target.y_mm, 6))
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return unique


def mapping_bounds(config: MappingGridConfig) -> tuple[float, float, float, float]:
    rows, columns = mapping_grid_shape(config)
    if rows <= 0 or columns <= 0:
        return (config.origin_x_mm, config.origin_y_mm, config.origin_x_mm, config.origin_y_mm)
    return (
        float(config.origin_x_mm),
        float(config.origin_y_mm),
        float(config.origin_x_mm) + ((columns - 1) * float(config.step_mm)),
        float(config.origin_y_mm) + ((rows - 1) * float(config.step_mm)),
    )


def mapping_relay_stroke_endpoint(config: MappingGridConfig, target: MappingGridTarget) -> tuple[float, float]:
    stroke = float(config.relay_stroke_mm)
    if stroke <= 0:
        raise ValueError("Relay stroke must be greater than zero.")
    x_min, _y_min, x_max, _y_max = mapping_bounds(config)
    x = float(target.x_mm)
    y = float(target.y_mm)
    eps = 1e-6
    if x < x_min - eps or x > x_max + eps:
        raise ValueError(f"Relay trigger start point X{x:g} is outside mapping X range {x_min:g} to {x_max:g}.")
    if x + stroke <= x_max + eps:
        return x + stroke, y
    if x - stroke >= x_min - eps:
        return x - stroke, y
    raise ValueError(
        f"Relay trigger stroke of {stroke:g} mm does not fit inside mapping X range "
        f"{x_min:g} to {x_max:g} mm at X{x:g}."
    )


def mapping_relay_stroke_feed_mm_min(config: MappingGridConfig, target: MappingGridTarget) -> float:
    end_x, end_y = mapping_relay_stroke_endpoint(config, target)
    dx = float(end_x) - float(target.x_mm)
    dy = float(end_y) - float(target.y_mm)
    distance_mm = (dx * dx + dy * dy) ** 0.5
    pulse_s = max(0.001, float(config.pulse_ms) / 1000.0)
    return max(1.0, distance_mm / pulse_s * 60.0)


MAPPING_STREAM_MAX_STROKES_PER_SEGMENT = 50
MIN_STREAM_CADENCE_MS = 100.0


@dataclass(frozen=True)
class MappingStreamStroke:
    """One planned fire stroke inside a streamed segment."""

    target: MappingGridTarget
    start_x_mm: float
    start_y_mm: float
    end_x_mm: float
    end_y_mm: float
    stroke_feed_mm_min: float
    travel_feed_mm_min: float
    # Cumulative feasible fire time from segment start; diagnostic telemetry
    # for frame/stroke mismatch analysis, not a control input.
    scheduled_fire_offset_s: float


@dataclass(frozen=True)
class MappingStreamSegment:
    strokes: tuple[MappingStreamStroke, ...]
    lines: tuple[StreamLine, ...]
    cadence_s: float
    estimated_duration_s: float


def compile_mapping_stream_segments(
    config: MappingGridConfig,
    targets: list[MappingGridTarget],
    *,
    cadence_ms: float,
    start_x_mm: float,
    start_y_mm: float,
    s_value: int,
    max_travel_feed_mm_min: float,
    max_strokes: int = MAPPING_STREAM_MAX_STROKES_PER_SEGMENT,
) -> list[MappingStreamSegment]:
    """Compile targets into per-row streamed segments of travel + fire strokes.

    One grid row per segment (capped at ``max_strokes``): rows are
    mono-directional so travel/stroke junctions blend in the planner, and a
    segment bounds the frame-mismatch blast radius. Travel feeds are sized so
    stroke-to-stroke period approaches ``cadence_ms``; when the feed clamp
    binds (long hops, row starts) the schedule extends honestly rather than
    pretending the nominal period.

    G-code mirrors ``fire_motion_pulse`` exactly: ``G90``/``M3 S`` once per
    segment, explicit ``S0`` on travels so the beam stays off, ``M5`` last.
    The stroke endpoint reuses ``mapping_relay_stroke_endpoint`` (including
    the +X edge direction flip).
    """
    period_s = max(0.0, float(cadence_ms)) / 1000.0
    pulse_s = max(0.001, float(config.pulse_ms) / 1000.0)
    travel_budget_s = max(0.010, period_s - pulse_s)
    max_travel_feed = max(1.0, float(max_travel_feed_mm_min))

    segments: list[MappingStreamSegment] = []
    row_groups: list[list[MappingGridTarget]] = []
    for target in targets:
        if row_groups and row_groups[-1][0].row_index == target.row_index and len(row_groups[-1]) < int(max_strokes):
            row_groups[-1].append(target)
        else:
            row_groups.append([target])

    head_x, head_y = float(start_x_mm), float(start_y_mm)
    for group in row_groups:
        strokes: list[MappingStreamStroke] = []
        lines: list[StreamLine] = [StreamLine("G90"), StreamLine(f"M3 S{int(s_value)}")]
        offset_s = 0.0
        for fire_index, target in enumerate(group):
            end_x, end_y = mapping_relay_stroke_endpoint(config, target)
            stroke_feed = mapping_relay_stroke_feed_mm_min(config, target)
            travel_distance = math.hypot(float(target.x_mm) - head_x, float(target.y_mm) - head_y)
            if travel_distance > 1e-9:
                travel_feed = min(max_travel_feed, max(1.0, travel_distance / travel_budget_s * 60.0))
                lines.append(
                    StreamLine(f"G1 X{float(target.x_mm):.3f} Y{float(target.y_mm):.3f} S0 F{travel_feed:.1f}")
                )
                offset_s += travel_distance / (travel_feed / 60.0)
            else:
                travel_feed = max_travel_feed
            lines.append(
                StreamLine(
                    f"G1 X{float(end_x):.3f} Y{float(end_y):.3f} S{int(s_value)} F{stroke_feed:.1f}",
                    is_fire=True,
                    fire_index=fire_index,
                )
            )
            strokes.append(
                MappingStreamStroke(
                    target=target,
                    start_x_mm=float(target.x_mm),
                    start_y_mm=float(target.y_mm),
                    end_x_mm=float(end_x),
                    end_y_mm=float(end_y),
                    stroke_feed_mm_min=float(stroke_feed),
                    travel_feed_mm_min=float(travel_feed),
                    scheduled_fire_offset_s=offset_s,
                )
            )
            offset_s += pulse_s
            head_x, head_y = float(end_x), float(end_y)
        lines.append(StreamLine("M5"))
        segments.append(
            MappingStreamSegment(
                strokes=tuple(strokes),
                lines=tuple(lines),
                cadence_s=period_s,
                estimated_duration_s=offset_s,
            )
        )
    return segments


def representative_mapping_dry_run_targets(config: MappingGridConfig) -> list[MappingGridTarget]:
    rows, columns = mapping_grid_shape(config)
    if rows <= 0 or columns <= 0 or int(config.shots_per_point) <= 0:
        return []
    corners = ((1, 1), (1, columns), (rows, columns), (rows, 1))
    selected: list[MappingGridTarget] = []
    seen: set[str] = set()
    for row, column in corners:
        target = MappingGridTarget(
            row_index=row,
            column_index=column,
            shot_number=1,
            x_mm=float(config.origin_x_mm) + ((column - 1) * float(config.step_mm)),
            y_mm=float(config.origin_y_mm) + ((row - 1) * float(config.step_mm)),
        )
        if target.key in seen:
            continue
        seen.add(target.key)
        selected.append(target)
    return selected


def estimate_mapping_duration_s(config: MappingGridConfig) -> float:
    rows, columns = mapping_grid_shape(config)
    unique_positions = rows * columns
    target_count = unique_positions * max(0, int(config.shots_per_point))
    motion_allowance_s = unique_positions * 0.25
    settle_s = unique_positions * (max(0.0, float(config.settle_ms)) / 1000.0)
    pulse_s = target_count * (max(0.0, float(config.pulse_ms)) / 1000.0)
    capture_s = target_count * max(0.0, min(float(config.capture_timeout_s), 1.0))
    return motion_allowance_s + settle_s + pulse_s + capture_s


def adaptive_mapping_eta_s(
    *,
    completed_since_start: int,
    targets_remaining: int,
    elapsed_s: float,
    targets_total: int,
    min_fraction: float = 0.10,
    min_shots: int = 20,
) -> float | None:
    """Elapsed-rate ETA, available once enough shots completed to trust the rate.

    Returns ``None`` until ``completed_since_start`` reaches
    ``max(min_shots, ceil(min_fraction * targets_total))``; before that the
    plan-based estimate should keep being shown.
    """
    try:
        completed = int(completed_since_start)
        remaining = int(targets_remaining)
        elapsed = float(elapsed_s)
        total = int(targets_total)
    except (TypeError, ValueError):
        return None
    if completed <= 0 or elapsed <= 0 or total <= 0:
        return None
    threshold = max(int(min_shots), math.ceil(min_fraction * total))
    if completed < threshold:
        return None
    return max(0, remaining) * (elapsed / completed)


def effective_mapping_move_speed_mm_min(
    requested_mm_min: float,
    grbl_settings: dict[str, Any] | None = None,
) -> float:
    """Return a mapping feed rate capped by manual and GRBL max-rate limits."""
    try:
        requested = float(requested_mm_min)
    except (TypeError, ValueError):
        requested = DEFAULT_MAPPING_MOVE_SPEED_MM_MIN
    if not math.isfinite(requested) or requested <= 0:
        requested = DEFAULT_MAPPING_MOVE_SPEED_MM_MIN
    candidates = [requested, MAX_MAPPING_MOVE_SPEED_MM_MIN]
    for key in ("110", "111"):
        try:
            value = None if grbl_settings is None else grbl_settings.get(key)
            value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            candidates.append(value)
    return max(1.0, min(candidates))


def _target_points_for_validation(config: MappingGridConfig) -> list[tuple[str, float, float]]:
    return [
        (f"Mapping {target.point_key}", float(target.x_mm), float(target.y_mm))
        for target in representative_mapping_dry_run_targets(config)
    ]


def validate_mapping_grid_config(
    config: MappingGridConfig,
    *,
    motion_limits: MotionSafetyLimits | None = None,
    grbl_settings: dict[str, Any] | None = None,
    include_motion_safety: bool = False,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for label, value in (
        ("X length", config.x_length_mm),
        ("Y length", config.y_length_mm),
        ("step", config.step_mm),
    ):
        if not math.isfinite(float(value)):
            messages.append({
                "severity": "error",
                "code": f"mapping_{label.lower().replace(' ', '_')}",
                "message": f"Mapping {label} must be a finite number.",
            })
    if config.x_length_mm <= 0:
        messages.append({"severity": "error", "code": "mapping_x_length", "message": "Mapping X length must be greater than 0 mm."})
    if config.y_length_mm <= 0:
        messages.append({"severity": "error", "code": "mapping_y_length", "message": "Mapping Y length must be greater than 0 mm."})
    if config.step_mm < MIN_MAPPING_STEP_MM:
        messages.append({
            "severity": "error",
            "code": "mapping_step",
            "message": f"Mapping step must be at least {MIN_MAPPING_STEP_MM:g} mm.",
        })
    elif config.spot_diameter_mm > 0 and config.step_mm < config.spot_diameter_mm:
        stroke_note = (
            f" (the relay stroke adds {config.relay_stroke_mm:g} mm along X)"
            if config.relay_stroke_mm > 0
            else ""
        )
        messages.append({
            "severity": "warning",
            "code": "mapping_spot_overlap",
            "message": (
                f"Grid step {config.step_mm:g} mm is smaller than the ablation spot "
                f"(~{config.spot_diameter_mm:g} mm){stroke_note}: adjacent shots overlap, so "
                "replicates land on pre-ablated surface. Increase the step to at least the "
                "spot diameter, or reduce the spot (focus)."
            ),
        })
    elif config.step_mm < NOMINAL_MAPPING_LINE_WIDTH_MM:
        messages.append({
            "severity": "warning",
            "code": "mapping_step_below_line_width",
            "message": (
                f"Mapping step is below the nominal laser line width "
                f"({NOMINAL_MAPPING_LINE_WIDTH_MM:g} mm); adjacent spots may overlap."
            ),
        })
    if config.shots_per_point < 1:
        messages.append({"severity": "error", "code": "mapping_shots", "message": "Shots per spot must be at least 1."})
    if config.laser_s_max <= 0:
        messages.append({"severity": "error", "code": "laser_s_max", "message": "Laser S max must be greater than zero."})
    if not 0 <= config.laser_power_percent <= 100:
        messages.append({"severity": "error", "code": "laser_power", "message": "Laser power percent must be between 0 and 100."})
    elif (
        config.laser_profile != LASER_PROFILE_MONPORT_NDYAG_RELAY
        and config.laser_power_percent > LASER_TUBE_LIFE_POWER_WARNING_PERCENT
    ):
        messages.append({"severity": "warning", "code": "laser_power_high", "message": "Laser power above 70% can reduce tube life; use only when the hardware procedure requires it."})
    if config.pulse_ms <= 0:
        messages.append({"severity": "error", "code": "pulse_ms", "message": "Pulse duration must be greater than zero for guarded firing."})
    elif config.pulse_ms > MAX_LASER_PULSE_MS:
        messages.append({"severity": "error", "code": "pulse_ms_max", "message": "Pulse duration must be 250 ms or less for guarded firing."})
    if config.laser_profile == LASER_PROFILE_MONPORT_NDYAG_RELAY and config.relay_stroke_mm <= 0:
        messages.append({"severity": "error", "code": "relay_stroke", "message": "Relay stroke must be greater than zero for the Monport Nd:YAG relay profile."})
    if config.move_speed_mm_min <= 0:
        messages.append({"severity": "error", "code": "move_speed", "message": "Move speed must be greater than zero."})
    if config.capture_timeout_s <= 0:
        messages.append({"severity": "error", "code": "capture_timeout", "message": "Capture timeout must be greater than zero."})
    if config.column_delays_us:
        _rows, columns = mapping_grid_shape(config)
        capacity = len(config.column_delays_us) * config.columns_per_delay
        if columns > capacity:
            messages.append({
                "severity": "error",
                "code": "mapping_delay_sweep_columns",
                "message": (
                    f"Trigger-delay sweep covers {capacity} column(s) "
                    f"({len(config.column_delays_us)} delay(s) x {config.columns_per_delay} column(s) each), "
                    f"but the grid has {columns} column(s). Shorten X length or extend the delay schedule."
                ),
            })
        elif columns < capacity:
            messages.append({
                "severity": "warning",
                "code": "mapping_delay_sweep_unused",
                "message": (
                    f"Trigger-delay sweep covers {capacity} column(s) but the grid only has "
                    f"{columns}; late delay(s) will be unmeasured or under-replicated."
                ),
            })
        if (
            columns <= capacity
            and config.columns_per_delay > 1
            and columns % config.columns_per_delay != 0
        ):
            # Applies to partial grids too (columns < capacity), where the
            # last delay block is truncated and replicates are unequal.
            messages.append({
                "severity": "warning",
                "code": "mapping_delay_sweep_uneven",
                "message": (
                    f"Grid columns ({columns}) do not divide evenly into blocks of "
                    f"{config.columns_per_delay}; delays will have unequal replicate counts."
                ),
            })
    if config.paired_settings:
        if config.column_delays_us or config.row_integration_times_ms:
            messages.append({
                "severity": "error",
                "code": "mapping_paired_sweep_conflict",
                "message": (
                    "Interleaved paired settings cannot be combined with a column delay "
                    "or row integration schedule — the two assignment schemes would "
                    "contradict each other."
                ),
            })
        else:
            rows, columns = mapping_grid_shape(config)
            points = rows * columns
            pair_count = len(config.paired_settings)
            if points % pair_count != 0:
                messages.append({
                    "severity": "warning",
                    "code": "mapping_paired_sweep_uneven",
                    "message": (
                        f"Grid has {points} point(s), not a multiple of the {pair_count} "
                        "interleaved setting pair(s); replicate counts will be unequal."
                    ),
                })
    if config.row_integration_times_ms:
        rows, _columns = mapping_grid_shape(config)
        bands = len(config.row_integration_times_ms)
        if mapping_rows_per_integration_band(config) <= 0:
            messages.append({
                "severity": "error",
                "code": "mapping_integration_sweep_rows",
                "message": (
                    f"Row integration sweep has {bands} integration value(s), but the grid has "
                    f"{rows} row(s). Set Y length so the row count is a multiple of {bands} "
                    f"(each band's rows are that integration's replicates)."
                ),
            })

    if not any(item["severity"] == "error" for item in messages):
        representative_targets = representative_mapping_dry_run_targets(config)
        if mapping_target_count(config) <= 0:
            messages.append({"severity": "error", "code": "mapping_empty", "message": "Mapping grid has no targets."})
        if include_motion_safety:
            try:
                validate_motion_safety_points(
                    _target_points_for_validation(config),
                    limits=motion_limits or load_motion_safety_limits(),
                    grbl_settings=grbl_settings,
                )
            except ValueError as exc:
                messages.append({"severity": "error", "code": "mapping_motion_safety", "message": str(exc)})
        if config.laser_profile == LASER_PROFILE_MONPORT_NDYAG_RELAY and config.relay_stroke_mm > 0:
            for target in representative_targets:
                try:
                    mapping_relay_stroke_endpoint(config, target)
                except ValueError as exc:
                    messages.append({"severity": "error", "code": "relay_stroke_bounds", "message": str(exc)})
                    break

    if config.streaming_enabled:
        if config.laser_profile != LASER_PROFILE_MONPORT_NDYAG_RELAY:
            messages.append({
                "severity": "error",
                "code": "stream_requires_relay_profile",
                "message": "Streamed row acquisition requires the Monport Nd:YAG relay profile (firing is a powered stroke).",
            })
        if config.column_delays_us or config.row_integration_times_ms or config.paired_settings:
            messages.append({
                "severity": "error",
                "code": "stream_no_sweeps",
                "message": (
                    "Streamed row acquisition cannot run timing sweeps: spectrometer configuration "
                    "changes cannot interleave with armed reads mid-segment."
                ),
            })
        min_cadence = max(MIN_STREAM_CADENCE_MS, float(config.pulse_ms) + 30.0)
        if config.stream_cadence_ms < min_cadence:
            messages.append({
                "severity": "error",
                "code": "stream_cadence",
                "message": (
                    f"Stream cadence must be at least {min_cadence:g} ms "
                    f"(laser floor {MIN_STREAM_CADENCE_MS:g} ms, pulse {config.pulse_ms:g} ms + margin)."
                ),
            })
        elif config.stream_cadence_ms < 110.0:
            messages.append({
                "severity": "warning",
                "code": "stream_cadence_margin",
                "message": (
                    "Stream cadence below 110 ms leaves little laser-recharge margin; "
                    "clear it with the V2 recharge bench before relying on it."
                ),
            })
    return messages


def mapping_plan_details(config: MappingGridConfig, *, include_targets: bool = True) -> dict[str, Any]:
    rows, columns = mapping_grid_shape(config)
    target_count = mapping_target_count(config)
    covered_x = config.origin_x_mm + (max(0, columns - 1) * config.step_mm)
    covered_y = config.origin_y_mm + (max(0, rows - 1) * config.step_mm)
    include_target_list = bool(include_targets and target_count <= MAX_PROGRESS_STATIC_TARGETS)
    targets = mapping_grid_targets(config) if include_target_list else []
    return {
        "version": 1,
        "run_type": MAPPING_RUN_TYPE,
        "dry_run_signature": config.dry_run_signature(),
        "run_identity_signature": config.run_identity_signature(),
        "config": config.to_mapping(),
        "experiment_name": config.experiment_name,
        "safe_experiment_name": config.safe_experiment_name,
        "run_directory": config.run_directory,
        "origin": {"x_mm": config.origin_x_mm, "y_mm": config.origin_y_mm},
        "requested_size": {"x_length_mm": config.x_length_mm, "y_length_mm": config.y_length_mm},
        "covered_bounds": {
            "x_min_mm": config.origin_x_mm,
            "y_min_mm": config.origin_y_mm,
            "x_max_mm": covered_x,
            "y_max_mm": covered_y,
        },
        "step_mm": config.step_mm,
        "targets": [target.to_mapping() for target in targets],
        "targets_truncated": not include_target_list,
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
            "max_mapping_move_speed_mm_min": MAX_MAPPING_MOVE_SPEED_MM_MIN,
            "settle_ms": config.settle_ms,
            "capture_timeout_s": config.capture_timeout_s,
            "column_delays_us": list(config.column_delays_us),
            "columns_per_delay": config.columns_per_delay,
            "row_integration_times_ms": list(config.row_integration_times_ms),
            "rows_per_integration_band": mapping_rows_per_integration_band(config),
            "paired_settings": [list(pair) for pair in config.paired_settings],
            "laser_energy_mj": config.laser_energy_mj,
        },
        "output_naming": {
            "run_folder": "<safe_experiment_name>_<YYYYMMDD_HHMMSS>",
            "spectrum_file": (
                "<safe_experiment>_R{row:03d}_C{column:03d}_X<xxx.xxx>_Y<yyy.yyy>_"
                "shot{shot_number:02d}_<YYYYMMDD_HHMMSS_ffffff>_<global_shot:03d>.csv"
            ),
            "index_file": MAPPING_INDEX_FILENAME,
        },
        "summary": {
            "rows": rows,
            "columns": columns,
            "points": rows * columns,
            "shots_per_point": config.shots_per_point,
            "target_count": target_count,
            "estimated_moves": rows * columns,
            "estimated_duration_s": estimate_mapping_duration_s(config),
            "covered_x_max_mm": covered_x,
            "covered_y_max_mm": covered_y,
        },
        "validation": validate_mapping_grid_config(config),
    }


def mapping_state_path(save_directory: str) -> str:
    return os.path.join(save_directory, MAPPING_STATE_FILENAME)


def mapping_manifest_path(save_directory: str) -> str:
    return os.path.join(save_directory, MAPPING_MANIFEST_FILENAME)


def mapping_index_path(save_directory: str) -> str:
    return os.path.join(save_directory, MAPPING_INDEX_FILENAME)


def mapping_timing_samples_path(save_directory: str) -> str:
    return os.path.join(save_directory, MAPPING_TIMING_SAMPLES_FILENAME)


def mapping_background_dir(save_directory: str) -> str:
    return os.path.join(save_directory, MAPPING_BACKGROUND_DIRNAME)


def mapping_background_reference_path(save_directory: str) -> str:
    return os.path.join(save_directory, MAPPING_BACKGROUND_REFERENCE_FILENAME)


def mapping_background_metadata_path(save_directory: str) -> str:
    return os.path.join(mapping_background_dir(save_directory), MAPPING_BACKGROUND_METADATA_FILENAME)


def aggregate_background_frames(frames: list, method: str = "median") -> list[float]:
    """Aggregate equal-length background frames pixel-by-pixel."""
    frames = [[float(value) for value in frame] for frame in frames]
    if not frames:
        raise ValueError("No background frames to aggregate.")
    length = len(frames[0])
    if any(len(frame) != length for frame in frames):
        raise ValueError("Background frames have mismatched pixel counts.")
    if method == "mean":
        return [sum(values) / len(frames) for values in zip(*frames)]
    if method != "median":
        raise ValueError(f"Unknown background aggregation method: {method!r}")
    result = []
    for values in zip(*frames):
        ordered = sorted(values)
        mid = len(ordered) // 2
        result.append(ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0)
    return result


def _timing_duration_ms(timing: dict[str, Any], start_key: str, end_key: str) -> float | str:
    start = timing.get(start_key)
    end = timing.get(end_key)
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return ""
    return round((float(end) - float(start)) * 1000.0, 3)


def mapping_timing_row(timing: dict[str, Any]) -> dict[str, Any]:
    """Convert a per-shot timing sample (perf_counter stamps) into CSV durations.

    Missing stamps produce empty cells so a partial sample never raises.
    ``gui_queue_ms`` is only measurable on the GUI side and stays blank in the
    worker-side export.
    """
    row: dict[str, Any] = {column: "" for column in MAPPING_TIMING_COLUMNS}
    row["recorded_at"] = datetime.now().isoformat(timespec="milliseconds")
    for key in (
        "shot_index",
        "target_key",
        "point_key",
        "row_index",
        "column_index",
        "shot_number",
        "mode",
        "effective_move_speed_mm_min",
        "trigger_delay_us",
        "burst_shot",
        "stream_segment_index",
        "frame_interval_ms",
        "scheduled_fire_offset_ms",
        "hold_events",
    ):
        value = timing.get(key)
        if value is not None:
            row[key] = value
    row["motion_command_ms"] = _timing_duration_ms(timing, "motion_start", "motion_command_end")
    row["idle_wait_ms"] = _timing_duration_ms(timing, "idle_wait_start", "idle_wait_end")
    row["settle_ms"] = _timing_duration_ms(timing, "settle_start", "settle_end")
    row["interlock_ms"] = _timing_duration_ms(timing, "interlock_start", "interlock_end")
    row["trigger_read_ms"] = _timing_duration_ms(timing, "trigger_wait_start", "trigger_wait_end")
    row["wavelengths_fetch_ms"] = _timing_duration_ms(timing, "wavelengths_fetch_start", "wavelengths_fetch_end")
    row["save_ms"] = _timing_duration_ms(timing, "save_start", "save_end")
    row["state_write_ms"] = _timing_duration_ms(timing, "state_write_start", "state_write_end")
    row["cycle_ms"] = _timing_duration_ms(timing, "cycle_start", "cycle_end")
    # Wall time between the previous target's cycle end and this cycle start:
    # catches stalls outside every per-stage timer (GUI backpressure, GIL
    # holds, hidden hardware waits).
    row["loop_gap_ms"] = _timing_duration_ms(timing, "previous_cycle_end", "cycle_start")
    return row


def append_mapping_timing_row(save_directory: str, row: dict[str, Any]) -> str:
    """Append one timing row, creating the CSV with a header when needed.

    Rows are flushed immediately so the export survives an interrupted run.
    """
    filepath = mapping_timing_samples_path(save_directory)
    os.makedirs(save_directory, exist_ok=True)
    write_header = not os.path.isfile(filepath) or os.path.getsize(filepath) == 0
    with open(filepath, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPING_TIMING_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
    return filepath


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


def save_mapping_run_state(save_directory: str, state: MappingRunState) -> str:
    return _write_json_atomic(mapping_state_path(save_directory), state.to_mapping())


def load_mapping_run_state(save_directory: str) -> MappingRunState | None:
    filepath = mapping_state_path(save_directory)
    if not os.path.isfile(filepath):
        return None
    with open(filepath, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Mapping state file must contain a JSON object.")
    return MappingRunState.from_mapping(payload)


def load_mapping_resume_state_for_config(save_directory: str, config: MappingGridConfig) -> MappingRunState | None:
    try:
        state = load_mapping_run_state(save_directory)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if state is None:
        return None
    if state.config.run_identity_signature() != config.run_identity_signature():
        return None
    index_completed = completed_mapping_targets_from_index(save_directory, config)
    state.completed_targets.update(index_completed)
    # Discarded stream segments are durable only in the index when the state
    # write failed; union them back so ablated craters are never re-shot.
    state.missing_targets.update(missing_mapping_targets_from_index(save_directory, config))
    return state


def mapping_manifest_payload(
    *,
    config: MappingGridConfig,
    state: MappingRunState | None,
    completed_dry_run_signature: str | None,
    safety_checklist: dict[str, Any] | None,
    command_transcript: list[str] | None,
    laser_safety: dict[str, Any] | None,
    spectrometer_info: dict[str, Any] | None,
    event: str | None,
    run_mode: str | None,
    effective_move_speed_mm_min: float | None = None,
    pause_events: list[dict[str, Any]] | None = None,
    background_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    total_targets = mapping_target_count(config)
    completed = set(state.completed_targets) if state else set()
    completed_targets = sorted(completed) if len(completed) <= MAX_PROGRESS_STATIC_TARGETS else []
    storage = mapping_spectrum_store_summary(
        config.run_directory or "",
        targets_total=total_targets,
        csv_compatibility_enabled=config.write_csv_files,
    )
    payload = {
        "version": 1,
        "event": event,
        "run_type": MAPPING_RUN_TYPE,
        "run_mode": run_mode,
        "plan": mapping_plan_details(config, include_targets=False),
        "dry_run_completed": completed_dry_run_signature == config.dry_run_signature(),
        "completed_targets": completed_targets,
        "completed_targets_count": len(completed),
        "completed_targets_truncated": len(completed_targets) != len(completed),
        "active_target": state.active_target if state else None,
        "abort_reason": state.abort_reason if state else None,
        "abort_events": list(state.abort_events) if state else [],
        "targets_total": total_targets,
        "shots_saved": min(len(completed), total_targets),
        "gcode_command_transcript": list(command_transcript or []),
        "safety_checklist": dict(safety_checklist or {}),
        "laser_safety": dict(laser_safety or {}),
        "spectrometer": dict(spectrometer_info or {}),
        "storage": storage,
    }
    if effective_move_speed_mm_min is not None:
        payload["effective_move_speed_mm_min"] = effective_move_speed_mm_min
        payload["requested_move_speed_mm_min"] = config.move_speed_mm_min
    if pause_events:
        payload["pause_events"] = [dict(item) for item in pause_events]
    if background_reference:
        payload["background_reference"] = dict(background_reference)
    return payload


def save_mapping_manifest(
    save_directory: str,
    *,
    config: MappingGridConfig,
    state: MappingRunState | None,
    completed_dry_run_signature: str | None,
    safety_checklist: dict[str, Any] | None,
    command_transcript: list[str] | None,
    laser_safety: dict[str, Any] | None,
    spectrometer_info: dict[str, Any] | None,
    event: str | None,
    run_mode: str | None,
    effective_move_speed_mm_min: float | None = None,
    pause_events: list[dict[str, Any]] | None = None,
    background_reference: dict[str, Any] | None = None,
) -> str:
    return _write_json_atomic(
        mapping_manifest_path(save_directory),
        mapping_manifest_payload(
            config=config,
            state=state,
            completed_dry_run_signature=completed_dry_run_signature,
            safety_checklist=safety_checklist,
            command_transcript=command_transcript,
            laser_safety=laser_safety,
            spectrometer_info=spectrometer_info,
            event=event,
            run_mode=run_mode,
            effective_move_speed_mm_min=effective_move_speed_mm_min,
            pause_events=pause_events,
            background_reference=background_reference,
        ),
    )


def mapping_summary_payload(
    *,
    config: MappingGridConfig,
    state: MappingRunState,
    save_directory: str,
    timing_summary: dict[str, Any] | None = None,
    run_mode: str | None = None,
    pause_events: list[dict[str, Any]] | None = None,
    background_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows, columns = mapping_grid_shape(config)
    total_targets = mapping_target_count(config)
    completed_count = min(len(state.completed_targets), total_targets)
    storage = mapping_spectrum_store_summary(
        save_directory,
        targets_total=total_targets,
        csv_compatibility_enabled=config.write_csv_files,
    )
    skipped = []
    skipped_truncated = False
    if completed_count < total_targets and total_targets <= MAX_PROGRESS_STATIC_TARGETS:
        targets = mapping_grid_targets(config)
        target_keys = {target.key for target in targets}
        completed = target_keys.intersection(state.completed_targets)
        skipped = sorted(target_keys.difference(completed))
    elif completed_count < total_targets:
        skipped_truncated = True
    return {
        "version": 1,
        "run_type": MAPPING_RUN_TYPE,
        "run_mode": run_mode,
        "dry_run_signature": config.dry_run_signature(),
        "run_identity_signature": config.run_identity_signature(),
        "experiment_name": config.experiment_name,
        "points_total": rows * columns,
        "shots_saved": completed_count,
        "targets_total": total_targets,
        "skipped_or_incomplete_targets": skipped,
        "skipped_or_incomplete_targets_truncated": skipped_truncated,
        "abort_reason": state.abort_reason,
        "abort_events": list(state.abort_events),
        "timing_summary": dict(timing_summary or {}),
        "pause_events": [dict(item) for item in (pause_events or [])],
        "background_reference": dict(background_reference or {}),
        "save_directory": save_directory,
        "index_path": mapping_index_path(save_directory),
        "storage": storage,
    }


def save_mapping_summary(
    save_directory: str,
    *,
    config: MappingGridConfig,
    state: MappingRunState,
    timing_summary: dict[str, Any] | None = None,
    run_mode: str | None = None,
    pause_events: list[dict[str, Any]] | None = None,
    background_reference: dict[str, Any] | None = None,
) -> str:
    return _write_json_atomic(
        os.path.join(save_directory, "_mapping_grid_summary.json"),
        mapping_summary_payload(
            config=config,
            state=state,
            save_directory=save_directory,
            timing_summary=timing_summary,
            run_mode=run_mode,
            pause_events=pause_events,
            background_reference=background_reference,
        ),
    )


def format_mapping_coordinate(value: float) -> str:
    sign = "m" if float(value) < 0 else ""
    return f"{sign}{abs(float(value)):07.3f}"


def mapping_spectrum_filename(
    config: MappingGridConfig,
    target: MappingGridTarget,
    *,
    timestamp: str,
    shot_index: int,
) -> str:
    return (
        f"{config.safe_experiment_name}_"
        f"R{target.row_index:03d}_C{target.column_index:03d}_"
        f"X{format_mapping_coordinate(target.x_mm)}_Y{format_mapping_coordinate(target.y_mm)}_"
        f"shot{target.shot_number:02d}_{timestamp}_{int(shot_index):03d}.csv"
    )


MAPPING_INDEX_COLUMNS = [
    "target_key",
    "point_key",
    "row_index",
    "column_index",
    "x_mm",
    "y_mm",
    "shot_number",
    "shot_index",
    "filename",
    "filepath",
    "captured_at",
    "save_ms",
    "integration_time_us",
    "trigger_delay_us",
    "storage_kind",
    "store_dir",
    "binary_row_index",
    "storage_uri",
    "binary_written",
    "shot_quality_state",
    "exclude_reason",
]


def append_mapping_index_row(save_directory: str, row: dict[str, Any]) -> str:
    filepath = mapping_index_path(save_directory)
    os.makedirs(save_directory, exist_ok=True)
    exists = os.path.isfile(filepath)
    columns = MAPPING_INDEX_COLUMNS
    if exists:
        # A resumed pre-upgrade index keeps its original header; appending
        # rows in a newer schema would silently misalign every column after
        # the first new one.
        try:
            with open(filepath, "r", encoding="utf-8", newline="") as handle:
                existing_header = next(csv.reader(handle), None)
        except OSError:
            existing_header = None
        if existing_header:
            columns = existing_header
    with open(filepath, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow({column: row.get(column, "") for column in columns})
    return filepath


def _mapping_target_key_within_config(target_key: str, config: MappingGridConfig) -> bool:
    try:
        row_part, column_part, shot_part = str(target_key or "").split(":", 2)
        if not row_part.startswith("R") or not column_part.startswith("C") or not shot_part.startswith("S"):
            return False
        row = int(row_part[1:])
        column = int(column_part[1:])
        shot = int(shot_part[1:])
    except (TypeError, ValueError):
        return False
    rows, columns = mapping_grid_shape(config)
    return (
        1 <= row <= rows
        and 1 <= column <= columns
        and 1 <= shot <= max(0, int(config.shots_per_point))
    )


def completed_mapping_targets_from_index(save_directory: str, config: MappingGridConfig) -> set[str]:
    filepath = mapping_index_path(save_directory)
    if not os.path.isfile(filepath):
        return set()
    completed: set[str] = set()
    try:
        binary_mask = None
        binary_mask_loaded = False
        with open(filepath, "r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                key = str(row.get("target_key") or "").strip()
                if not _mapping_target_key_within_config(key, config):
                    continue
                storage_kind = str(row.get("storage_kind") or "").strip().lower()
                if storage_kind == "binary_npy":
                    try:
                        from prolibspector.acquisition.mapping_spectrum_store import binary_row_is_written, load_binary_written_mask

                        if not binary_mask_loaded:
                            binary_mask = load_binary_written_mask(save_directory)
                            binary_mask_loaded = True
                        if not binary_row_is_written(save_directory, row.get("binary_row_index"), binary_mask):
                            continue
                    except Exception:
                        continue
                if storage_kind and storage_kind != "csv" and storage_kind != "binary_npy":
                    continue
                if storage_kind == "csv" and not row.get("filepath") and not row.get("filename"):
                    continue
                if storage_kind == "":
                    # Legacy mapping rows are CSV-backed and did not record storage_kind.
                    pass
                if _mapping_target_key_within_config(key, config):
                    completed.add(key)
    except OSError:
        return set()
    return completed


def missing_mapping_targets_from_index(save_directory: str, config: MappingGridConfig) -> set[str]:
    """Targets durably recorded as fired-but-unmatched (never re-shoot).

    The index rows are the only durable record of a discarded stream segment
    when the state-JSON write failed; resume unions them back into
    ``missing_targets`` so ablated craters are never re-shot after a crash.
    """
    filepath = mapping_index_path(save_directory)
    if not os.path.isfile(filepath):
        return set()
    missing: set[str] = set()
    try:
        with open(filepath, "r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if str(row.get("exclude_reason") or "").strip() != "stream_frame_mismatch":
                    continue
                key = str(row.get("target_key") or "").strip()
                if _mapping_target_key_within_config(key, config):
                    missing.add(key)
    except OSError:
        return set()
    return missing


def mapping_progress_payload(
    config: MappingGridConfig,
    state: MappingRunState | None,
    *,
    current_target: MappingGridTarget | None = None,
    dry_run: bool = False,
    dry_run_visited_positions: int = 0,
    dry_run_total_positions: int = 0,
    completed_target: MappingGridTarget | None = None,
    include_static_plan: bool = False,
) -> dict[str, Any]:
    rows, columns = mapping_grid_shape(config)
    target_count = mapping_target_count(config)
    completed_targets = set(state.completed_targets) if state else set()
    completed_points = completed_mapping_point_keys(completed_targets, config.shots_per_point)
    saved_shots = min(len(completed_targets), target_count)
    completed_target_key = completed_target.key if completed_target is not None else ""
    completed_point_key = (
        completed_target.point_key
        if completed_target is not None and completed_target.point_key in completed_points
        else ""
    )
    payload = {
        "run_type": MAPPING_RUN_TYPE,
        "experiment_name": config.experiment_name,
        "safe_experiment_name": config.safe_experiment_name,
        "origin_x_mm": config.origin_x_mm,
        "origin_y_mm": config.origin_y_mm,
        "x_length_mm": config.x_length_mm,
        "y_length_mm": config.y_length_mm,
        "step_mm": config.step_mm,
        "rows": rows,
        "columns": columns,
        "points_total": rows * columns,
        "targets_total": target_count,
        "shots_per_point": config.shots_per_point,
        "saved_shots": saved_shots,
        "completed_target_key": completed_target_key,
        "completed_point_key": completed_point_key,
        "current_target": current_target.to_mapping() if current_target else None,
        "complete": bool(target_count and saved_shots >= target_count),
        "dry_run": bool(dry_run),
        "dry_run_visited_positions": int(dry_run_visited_positions),
        "dry_run_total_positions": int(dry_run_total_positions),
    }
    if include_static_plan:
        payload["completed_targets"] = sorted(completed_targets)
        payload["completed_points"] = sorted(completed_points)
        if target_count <= MAX_PROGRESS_STATIC_TARGETS:
            targets = mapping_grid_targets(config)
            payload["targets"] = [target.to_mapping() for target in targets]
            payload["static_plan_truncated"] = False
        else:
            payload["targets"] = []
            payload["static_plan_truncated"] = True
    return payload
