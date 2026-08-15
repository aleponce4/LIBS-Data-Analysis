"""High-throughput plate autosave: geometry, run state, and resume discovery.

PUBLIC-EDITION MODULE
=====================
This is a public-edition implementation, written from scratch against the call
sites in ``acquisition/app.py`` and ``acquisition/worker.py``. The private
ProLIBSpector edition ships a richer version of this module that additionally
covers plate-handling robot integration, multi-plate batch scheduling tied to
the automated laser stage, and a signed reproducibility manifest format used
for regulated workflows.

Nothing here is a placeholder: plate geometry, well ordering, shot assignment,
discard/repair bookkeeping, atomic state persistence and resume discovery are
all fully functional. A public-edition plate run saves, resumes, discards and
repairs exactly as the UI describes.

State is written as JSON (``plate_run_state.json``) inside the plate folder,
plus an append-only ``reproducibility_events.jsonl`` event log and a
``reproducibility.json`` snapshot at run boundaries.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)


# ─── Plate geometry ───────────────────────────────────────────────────────

#: Supported plate formats mapped to ``(rows, columns)``.
PLATE_FORMATS: dict[int, tuple[int, int]] = {
    6: (2, 3),
    12: (3, 4),
    24: (4, 6),
    48: (6, 8),
    96: (8, 12),
    384: (16, 24),
}

ORDER_ROW = "row"
ORDER_COLUMN = "column"

ORDER_LABELS: dict[str, str] = {
    ORDER_ROW: "Row by row (A1, A2, A3, ...)",
    ORDER_COLUMN: "Column by column (A1, B1, C1, ...)",
}

DEFAULT_PLATE_TYPE = 96
DEFAULT_PLATE_NAME = "Plate1"

STATE_FILE_NAME = "plate_run_state.json"
REPRODUCIBILITY_FILE_NAME = "reproducibility.json"
REPRODUCIBILITY_EVENTS_FILE_NAME = "reproducibility_events.jsonl"
DISCARDED_DIR_NAME = "Discarded"

_WELL_RE = re.compile(r"^([A-Z])([0-9]{1,2})$")
#: The trailing ``(?:_[0-9]+)?`` matches the collision suffix ``unique_output_path``
#: adds when two shots would land on the same name. Without it this module
#: generates filenames its own resume scanner rejects, and a resumed run
#: silently re-shoots wells whose spectra are already on disk.
_SHOT_FILE_RE = re.compile(
    r"^(?P<plate>.+)_(?P<well>[A-Z][0-9]{1,2})_shot(?P<shot>[0-9]{2,})_"
    r"(?P<timestamp>[0-9]{8}_[0-9]{6}_[0-9]+)_(?P<index>[0-9]+)(?:_[0-9]+)?\.csv$"
)


def row_letter(row_index: int) -> str:
    """Return the plate row letter for a zero-based row index."""
    return chr(ord("A") + int(row_index))


def well_names(rows: int, columns: int, order_mode: str = ORDER_ROW) -> list[str]:
    """Return every well label in acquisition order.

    ``ORDER_ROW`` walks A1..A12 then B1..B12; ``ORDER_COLUMN`` walks A1..H1
    then A2..H2. Well labels always match the UI's ``f"{chr(65 + row)}{col}"``.
    """
    rows = int(rows)
    columns = int(columns)
    if order_mode == ORDER_COLUMN:
        return [f"{row_letter(row)}{column}" for column in range(1, columns + 1) for row in range(rows)]
    return [f"{row_letter(row)}{column}" for row in range(rows) for column in range(1, columns + 1)]


#: Spelling the automation stack uses for :func:`well_names`. Both names are
#: load-bearing — the manual plate workflow grew up calling it ``well_names``
#: and the automated one ``ordered_wells`` — and they must stay the same
#: function, because a plate resumed by one path is read by the other.
ordered_wells = well_names


def sanitize_filename_part(value: Any, fallback: str = "Sample") -> str:
    """Return ``value`` reduced to a safe, non-empty filename fragment."""
    text = str(value or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return cleaned or fallback


def unique_output_path(directory: str | os.PathLike[str], filename: str) -> str:
    """Return a path inside ``directory`` that does not yet exist.

    Collisions get a ``_2``, ``_3``, ... suffix before the extension so a
    duplicate timestamp can never silently overwrite a saved spectrum.
    """
    base_dir = Path(directory)
    stem, extension = os.path.splitext(filename)
    candidate = base_dir / filename
    counter = 2
    while candidate.exists():
        candidate = base_dir / f"{stem}_{counter}{extension}"
        counter += 1
    return str(candidate)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_optional_float(value: Any) -> float | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _coerce_optional_int(value: Any) -> int | None:
    """Like :func:`_coerce_int` but distinguishes "absent" from "zero"."""
    parsed = _coerce_optional_float(value)
    return None if parsed is None else int(parsed)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


# ─── Run configuration ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlateAutosaveConfig:
    """Immutable description of one high-throughput plate run."""

    plate_type: int = DEFAULT_PLATE_TYPE
    plate_name: str = DEFAULT_PLATE_NAME
    shots_per_well: int = 1
    order_mode: str = ORDER_ROW
    laser_wavelength_nm: float | None = None
    laser_energy: str = ""
    laser_energy_mj: float | None = None
    laser_hz: str = ""
    delay_enabled: bool = False
    delay_ms: str = ""
    #: Spectrometer/run settings captured alongside the plate for reproducibility.
    runtime: dict[str, Any] = field(default_factory=dict)
    #: Set when an automated run writes into a dedicated run folder.
    run_directory: str | None = None

    _KNOWN_KEYS = (
        "plate_type",
        "plate_name",
        "shots_per_well",
        "order_mode",
        "laser_wavelength_nm",
        "laser_energy",
        "laser_energy_mj",
        "laser_hz",
        "delay_enabled",
        "delay_ms",
        "runtime",
        "run_directory",
    )

    def __post_init__(self) -> None:
        if int(self.plate_type) not in PLATE_FORMATS:
            raise ValueError(
                f"Unsupported plate format: {self.plate_type}. "
                f"Supported: {', '.join(str(key) for key in sorted(PLATE_FORMATS))}."
            )
        if int(self.shots_per_well) < 1:
            raise ValueError("Shots per well must be at least 1.")
        if self.order_mode not in ORDER_LABELS:
            raise ValueError(f"Unknown well order mode: {self.order_mode!r}.")

    # ── Construction ─────────────────────────────────────────────────────

    @classmethod
    def from_mapping(cls, values: Any) -> "PlateAutosaveConfig":
        """Build a config from a mapping of raw (possibly string) UI values.

        Unknown keys are preserved under ``runtime`` so the reproducibility log
        keeps whatever the caller captured.
        """
        if isinstance(values, cls):
            return values
        if values is None:
            return cls()
        if not isinstance(values, dict):
            raise TypeError(f"Cannot build PlateAutosaveConfig from {type(values).__name__}.")

        runtime = dict(values.get("runtime") or {})
        for key, value in values.items():
            if key not in cls._KNOWN_KEYS:
                runtime[key] = value

        return cls(
            plate_type=_coerce_int(values.get("plate_type"), DEFAULT_PLATE_TYPE),
            plate_name=str(values.get("plate_name") or DEFAULT_PLATE_NAME).strip() or DEFAULT_PLATE_NAME,
            shots_per_well=_coerce_int(values.get("shots_per_well"), 1),
            order_mode=str(values.get("order_mode") or ORDER_ROW),
            laser_wavelength_nm=_coerce_optional_float(values.get("laser_wavelength_nm")),
            laser_energy=str(values.get("laser_energy") or ""),
            laser_energy_mj=_coerce_optional_float(values.get("laser_energy_mj")),
            laser_hz=str(values.get("laser_hz") or ""),
            delay_enabled=bool(values.get("delay_enabled", False)),
            delay_ms=str(values.get("delay_ms") or ""),
            runtime=runtime,
            run_directory=(str(values["run_directory"]) if values.get("run_directory") else None),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "plate_type": self.plate_type,
            "plate_name": self.plate_name,
            "shots_per_well": self.shots_per_well,
            "order_mode": self.order_mode,
            "laser_wavelength_nm": self.laser_wavelength_nm,
            "laser_energy": self.laser_energy,
            "laser_energy_mj": self.laser_energy_mj,
            "laser_hz": self.laser_hz,
            "delay_enabled": self.delay_enabled,
            "delay_ms": self.delay_ms,
            "runtime": dict(self.runtime),
            "run_directory": self.run_directory,
        }

    def with_run_directory(self, run_directory: str | None) -> "PlateAutosaveConfig":
        return replace(self, run_directory=run_directory)

    # ── Geometry ─────────────────────────────────────────────────────────

    @property
    def rows(self) -> int:
        return PLATE_FORMATS[int(self.plate_type)][0]

    @property
    def columns(self) -> int:
        return PLATE_FORMATS[int(self.plate_type)][1]

    @property
    def total_wells(self) -> int:
        return self.rows * self.columns

    @property
    def total_shots(self) -> int:
        return self.total_wells * int(self.shots_per_well)

    @property
    def ordered_wells(self) -> list[str]:
        return well_names(self.rows, self.columns, self.order_mode)

    @property
    def order_label(self) -> str:
        return ORDER_LABELS.get(self.order_mode, self.order_mode)

    @property
    def safe_plate_name(self) -> str:
        """Filesystem-safe plate folder / filename prefix."""
        return sanitize_filename_part(self.plate_name, fallback=DEFAULT_PLATE_NAME)

    @property
    def experiment_name(self) -> str:
        return self.plate_name


# ─── Saved shots ──────────────────────────────────────────────────────────


@dataclass
class PlateShotRecord:
    """One saved spectrum, tied to a well and a per-well shot number."""

    well: str
    shot_number: int
    filepath: str
    shot_index: int = 0
    saved_at: str = field(default_factory=_utc_now)
    #: Exposure actually used for this shot. Recorded per shot rather than per
    #: plate because an automated run may vary integration time by row band and
    #: trigger delay by column, so the plate-level config does not describe any
    #: individual spectrum. ``None`` means "not recorded" - which is what a
    #: record rebuilt by scanning filenames can honestly say.
    integration_time_ms: float | None = None
    integration_time_us: int | None = None
    trigger_delay_us: float | None = None

    @classmethod
    def from_mapping(cls, values: Any) -> "PlateShotRecord":
        if isinstance(values, cls):
            return values
        if not isinstance(values, dict):
            raise TypeError(f"Cannot build PlateShotRecord from {type(values).__name__}.")
        well = str(values.get("well") or "").strip().upper()
        if not _WELL_RE.match(well):
            raise ValueError(f"Invalid well label in saved record: {well!r}.")
        return cls(
            well=well,
            shot_number=_coerce_int(values.get("shot_number"), 1),
            filepath=str(values.get("filepath") or ""),
            shot_index=_coerce_int(values.get("shot_index"), 0),
            saved_at=str(values.get("saved_at") or _utc_now()),
            integration_time_ms=_coerce_optional_float(values.get("integration_time_ms")),
            integration_time_us=_coerce_optional_int(values.get("integration_time_us")),
            trigger_delay_us=_coerce_optional_float(values.get("trigger_delay_us")),
        )

    def to_mapping(self) -> dict[str, Any]:
        payload = {
            "well": self.well,
            "shot_number": self.shot_number,
            "filepath": self.filepath,
            "shot_index": self.shot_index,
            "saved_at": self.saved_at,
        }
        # Omitted when absent, so a manual plate run keeps writing exactly the
        # state file shape it wrote before, and older files still load.
        for key in ("integration_time_ms", "integration_time_us", "trigger_delay_us"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


# ─── Run state ────────────────────────────────────────────────────────────


class PlateRunState:
    """Mutable progress of one plate run: saved shots, repairs, and QC."""

    def __init__(self, config: PlateAutosaveConfig, records: Sequence[PlateShotRecord] | None = None) -> None:
        self.config = PlateAutosaveConfig.from_mapping(config)
        self.records: list[PlateShotRecord] = list(records or [])
        self.repair_queue: list[str] = []
        self.repair_resume_well: str | None = None
        self.repaired_wells: list[str] = []
        self.qc_by_well: dict[str, Any] = {}
        self.closed_early: bool = False
        self.plate_label: str | None = None
        self.dry_run: bool = False
        self.dry_run_visited_wells: list[str] = []
        self.started_at: str = _utc_now()

    # ── Construction ─────────────────────────────────────────────────────

    @classmethod
    def from_records(
        cls,
        config: PlateAutosaveConfig,
        records: Iterable[Any],
    ) -> "PlateRunState":
        """Rebuild a run state from saved shot records (used by resume).

        Records are renumbered per well in their given order so a plate scanned
        off disk always has contiguous ``shot_number`` values.
        """
        parsed = [PlateShotRecord.from_mapping(record) for record in records]
        resolved_config = PlateAutosaveConfig.from_mapping(config)
        valid_wells = set(resolved_config.ordered_wells)

        unknown = sorted({record.well for record in parsed} - valid_wells)
        if unknown:
            raise ValueError(
                f"Saved wells {', '.join(unknown)} do not exist on a "
                f"{resolved_config.plate_type}-well plate."
            )

        counts: dict[str, int] = {}
        renumbered: list[PlateShotRecord] = []
        for record in parsed:
            counts[record.well] = counts.get(record.well, 0) + 1
            if counts[record.well] > resolved_config.shots_per_well:
                raise ValueError(
                    f"Well {record.well} has more saved shots than the configured "
                    f"{resolved_config.shots_per_well} shot(s) per well."
                )
            renumbered.append(replace(record, shot_number=counts[record.well]))

        return cls(resolved_config, renumbered)

    @classmethod
    def from_mapping(cls, values: Any) -> "PlateRunState":
        """Rebuild a run state from its ``to_mapping()`` form."""
        if isinstance(values, cls):
            return values
        if not isinstance(values, dict):
            raise TypeError(f"Cannot build PlateRunState from {type(values).__name__}.")

        state = cls(
            PlateAutosaveConfig.from_mapping(values.get("config") or {}),
            [PlateShotRecord.from_mapping(record) for record in values.get("records") or []],
        )
        valid_wells = set(state.config.ordered_wells)
        state.repair_queue = [
            str(well).upper() for well in values.get("repair_queue") or [] if str(well).upper() in valid_wells
        ]
        resume_well = values.get("repair_resume_well")
        state.repair_resume_well = str(resume_well).upper() if resume_well else None
        state.repaired_wells = [
            str(well).upper() for well in values.get("repaired_wells") or [] if str(well).upper() in valid_wells
        ]
        state.qc_by_well = dict(values.get("qc_by_well") or {})
        state.closed_early = bool(values.get("closed_early", False))
        state.plate_label = values.get("plate_label") or None
        state.dry_run = bool(values.get("dry_run", False))
        state.dry_run_visited_wells = [str(well).upper() for well in values.get("dry_run_visited_wells") or []]
        state.started_at = str(values.get("started_at") or state.started_at)
        return state

    def to_mapping(self) -> dict[str, Any]:
        return {
            "config": self.config.to_mapping(),
            "records": [record.to_mapping() for record in self.records],
            "repair_queue": list(self.repair_queue),
            "repair_resume_well": self.repair_resume_well,
            "repaired_wells": list(self.repaired_wells),
            "qc_by_well": dict(self.qc_by_well),
            "closed_early": self.closed_early,
            "plate_label": self.plate_label,
            "dry_run": self.dry_run,
            "dry_run_visited_wells": list(self.dry_run_visited_wells),
            "started_at": self.started_at,
            "schema": "prolibspector.plate_run_state/1",
        }

    # ── Derived progress ─────────────────────────────────────────────────

    @property
    def shots_by_well(self) -> dict[str, int]:
        counts = {well: 0 for well in self.config.ordered_wells}
        for record in self.records:
            if record.well in counts:
                counts[record.well] += 1
        return counts

    @property
    def saved_shots(self) -> int:
        return len(self.records)

    @property
    def complete_wells(self) -> int:
        shots_per_well = self.config.shots_per_well
        return sum(1 for count in self.shots_by_well.values() if count >= shots_per_well)

    @property
    def repair_active(self) -> bool:
        return bool(self.repair_queue)

    @property
    def current_well(self) -> str | None:
        assignment = self.next_assignment(consume=False)
        return assignment[0] if assignment else None

    @property
    def is_complete(self) -> bool:
        return self.next_assignment(consume=False) is None and not self.closed_early

    @property
    def can_discard(self) -> bool:
        return bool(self.records) and not self.closed_early

    # ── Shot assignment ──────────────────────────────────────────────────

    def next_assignment(self, *, consume: bool = False) -> tuple[str, int] | None:
        """Return ``(well, shot_number)`` for the next shot, or None if done.

        Repair wells jump the queue; the normal plate order resumes once every
        queued repair well is full again. ``consume`` exists only so callers can
        read the value without implying mutation - assignment itself is derived
        from the saved records, so nothing is consumed either way.
        """
        del consume  # assignment is always derived, never popped
        counts = self.shots_by_well
        shots_per_well = self.config.shots_per_well

        for well in self.repair_queue:
            if counts.get(well, 0) < shots_per_well:
                return well, counts.get(well, 0) + 1

        for well in self.config.ordered_wells:
            if counts.get(well, 0) < shots_per_well:
                return well, counts.get(well, 0) + 1
        return None

    def record_saved(
        self,
        filepath: str,
        shot_index: int = 0,
        *,
        integration_time_ms: float | None = None,
        integration_time_us: int | None = None,
        trigger_delay_us: float | None = None,
    ) -> dict[str, Any]:
        """Record a saved spectrum against the next assignment.

        Returns the fresh progress payload. Raises RuntimeError when the plate
        has no remaining assignment, so a caller cannot silently lose a shot.

        The exposure arguments are keyword-only and optional: a manual plate run
        holds one exposure for the whole plate and passes none of them, while an
        automated run varies exposure per target and records what it used.
        """
        assignment = self.next_assignment()
        if assignment is None:
            raise RuntimeError("Plate run is already complete; no well is expecting a shot.")

        well, shot_number = assignment
        self.records.append(
            PlateShotRecord(
                well=well,
                shot_number=shot_number,
                filepath=str(filepath),
                shot_index=int(shot_index),
                integration_time_ms=integration_time_ms,
                integration_time_us=integration_time_us,
                trigger_delay_us=trigger_delay_us,
            )
        )

        # Drop repair wells that are full again, then clear the resume marker.
        counts = self.shots_by_well
        shots_per_well = self.config.shots_per_well
        still_queued = [queued for queued in self.repair_queue if counts.get(queued, 0) < shots_per_well]
        for finished in self.repair_queue:
            if finished not in still_queued and finished not in self.repaired_wells:
                self.repaired_wells.append(finished)
        self.repair_queue = still_queued
        if not self.repair_queue:
            self.repair_resume_well = None

        return self.progress_payload()

    # ── Discard and repair ───────────────────────────────────────────────

    def discard_last(self, discarded_dir: str | os.PathLike[str]) -> tuple[PlateShotRecord | None, dict[str, Any]]:
        """Move the most recent saved shot to ``discarded_dir`` and roll back."""
        if not self.records:
            return None, self.progress_payload()

        record = self.records.pop()
        _move_to_discarded(record.filepath, discarded_dir)
        if record.well in self.repaired_wells:
            self.repaired_wells.remove(record.well)
        return record, self.progress_payload()

    def start_repair(
        self,
        wells: Sequence[str],
        discarded_dir: str | os.PathLike[str],
    ) -> tuple[list[PlateShotRecord], dict[str, Any]]:
        """Queue ``wells`` for re-acquisition, moving their files aside.

        Returns ``(removed_records, payload)``. Raises ValueError for wells that
        are not on the plate and RuntimeError when a repair is already running,
        matching the error handling the worker expects.
        """
        requested = [str(well).strip().upper() for well in wells if str(well).strip()]
        if not requested:
            raise ValueError("Select at least one well to repair.")
        if self.repair_active:
            raise RuntimeError("A repair pass is already queued for this plate.")

        valid_wells = set(self.config.ordered_wells)
        unknown = [well for well in requested if well not in valid_wells]
        if unknown:
            raise ValueError(
                f"{', '.join(unknown)} is not on a {self.config.plate_type}-well plate."
                if len(unknown) == 1
                else f"{', '.join(unknown)} are not on a {self.config.plate_type}-well plate."
            )

        targets = {well for well in requested}
        removed = [record for record in self.records if record.well in targets]
        if not removed:
            raise ValueError("The selected well(s) have no saved shots to repair.")

        # Remember where the normal order would have resumed before the detour.
        self.repair_resume_well = self.current_well

        for record in removed:
            _move_to_discarded(record.filepath, discarded_dir)
        self.records = [record for record in self.records if record.well not in targets]
        self.repair_queue = [well for well in self.config.ordered_wells if well in targets]
        self.repaired_wells = [well for well in self.repaired_wells if well not in targets]
        for well in targets:
            self.qc_by_well.pop(well, None)

        return removed, self.progress_payload()

    # ── Payload ──────────────────────────────────────────────────────────

    def progress_payload(self) -> dict[str, Any]:
        """Return the UI-facing progress snapshot.

        Every key here is read by ``acquisition/app.py`` (plate map drawing,
        history cards, and button enablement), so the shape is a contract.
        """
        config = self.config
        return {
            "plate_name": config.plate_name,
            "plate_label": self.plate_label,
            "plate_type": config.plate_type,
            "rows": config.rows,
            "columns": config.columns,
            "order_mode": config.order_mode,
            "order_label": config.order_label,
            "shots_per_well": config.shots_per_well,
            "shots_by_well": self.shots_by_well,
            "current_well": self.current_well,
            "total_wells": config.total_wells,
            "complete_wells": self.complete_wells,
            "total_shots": config.total_shots,
            "saved_shots": self.saved_shots,
            "complete": self.is_complete,
            "closed_early": self.closed_early,
            "can_discard": self.can_discard,
            "repair_active": self.repair_active,
            "repair_queue": list(self.repair_queue),
            "repair_resume_well": self.repair_resume_well,
            "repaired_wells": list(self.repaired_wells),
            "qc_by_well": dict(self.qc_by_well),
            "dry_run": self.dry_run,
            "dry_run_visited_wells": list(self.dry_run_visited_wells),
        }


def _move_to_discarded(filepath: str, discarded_dir: str | os.PathLike[str]) -> str | None:
    """Move ``filepath`` into ``discarded_dir``; return the new path or None."""
    if not filepath:
        return None
    source = Path(filepath)
    if not source.exists():
        logger.warning("Discarded shot file is already gone: %s", filepath)
        return None
    target_dir = Path(discarded_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = unique_output_path(target_dir, source.name)
    shutil.move(str(source), destination)
    return destination


# ─── Persistence ──────────────────────────────────────────────────────────


def save_plate_run_state(
    plate_dir: str | os.PathLike[str],
    plate_state: PlateRunState,
    *,
    closed_early: bool = False,
    timing: dict | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Write the plate run state atomically into ``plate_dir``.

    ``extra`` is merged into the persisted payload. An automated run uses it to
    stamp the schedules that vary within one plate - per-column trigger delays
    and per-row-band integration times - which the plate config cannot express
    because it describes the plate, not the individual target.
    """
    if closed_early:
        plate_state.closed_early = True

    payload = plate_state.to_mapping()
    payload["closed_early"] = bool(plate_state.closed_early)
    payload["written_at"] = _utc_now()
    payload["progress"] = plate_state.progress_payload()
    if extra:
        payload.update(extra)

    state_path = Path(plate_dir) / STATE_FILE_NAME
    if timing is not None:
        timing.setdefault("plate_state_write_start", time.perf_counter())
    _atomic_write_text(state_path, json.dumps(payload, indent=2, default=str))
    if timing is not None:
        timing["plate_state_write_end"] = time.perf_counter()
        timing["plate_state_path"] = str(state_path)
    return str(state_path)


def load_plate_run_state(plate_dir: str | os.PathLike[str]) -> PlateRunState | None:
    """Load a saved plate run state, or None when absent/unreadable."""
    state_path = Path(plate_dir) / STATE_FILE_NAME
    if not state_path.exists():
        return None
    try:
        return PlateRunState.from_mapping(json.loads(state_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        logger.warning("Could not read plate state: %s", state_path, exc_info=True)
        return None


def _plate_dir_for(save_directory: str | os.PathLike[str], plate_state: PlateRunState) -> Path:
    return Path(save_directory) / plate_state.config.safe_plate_name


def save_plate_reproducibility_log(
    save_directory: str | os.PathLike[str],
    plate_state: PlateRunState,
    *,
    spectrometer_info: dict | None = None,
    timing_sample: dict | None = None,
    event: str | None = None,
    closed_early: bool = False,
) -> str:
    """Write a run-boundary reproducibility snapshot for the plate."""
    plate_dir = _plate_dir_for(save_directory, plate_state)
    snapshot = {
        "schema": "prolibspector.plate_reproducibility/1",
        "edition": "public",
        "written_at": _utc_now(),
        "event": event,
        "closed_early": bool(closed_early or plate_state.closed_early),
        "config": plate_state.config.to_mapping(),
        "progress": plate_state.progress_payload(),
        "spectrometer": dict(spectrometer_info or {}),
        "timing_sample": dict(timing_sample or {}),
        "records": [record.to_mapping() for record in plate_state.records],
    }
    log_path = plate_dir / REPRODUCIBILITY_FILE_NAME
    _atomic_write_text(log_path, json.dumps(snapshot, indent=2, default=str))
    append_plate_reproducibility_event(
        save_directory,
        plate_state,
        event=event or "snapshot",
        timing_sample=timing_sample,
    )
    return str(log_path)


def append_plate_reproducibility_event(
    save_directory: str | os.PathLike[str],
    plate_state: PlateRunState,
    *,
    event: str,
    timing_sample: dict | None = None,
) -> str:
    """Append one O(1) event line to the plate's reproducibility event log."""
    plate_dir = _plate_dir_for(save_directory, plate_state)
    entry = {
        "at": _utc_now(),
        "event": event,
        "saved_shots": plate_state.saved_shots,
        "complete_wells": plate_state.complete_wells,
        "current_well": plate_state.current_well,
        "repair_queue": list(plate_state.repair_queue),
    }
    if timing_sample:
        entry["timing_sample"] = dict(timing_sample)

    events_path = plate_dir / REPRODUCIBILITY_EVENTS_FILE_NAME
    try:
        plate_dir.mkdir(parents=True, exist_ok=True)
        with events_path.open("a", encoding="utf-8") as events_file:
            events_file.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        # An automated run appends one of these per shot, so a transient I/O
        # failure here - the log open in Excel on Windows, a full disk, a
        # network share hiccup - would otherwise abort a multi-hour unattended
        # run. The event log is diagnostic; the spectra are the product.
        logger.warning("Could not append plate reproducibility event: %s", events_path, exc_info=True)
    return str(events_path)


# ─── Per-shot timing log ──────────────────────────────────────────────────
#
# The worker stamps ``perf_counter()`` marks around each stage of a shot and
# hands the raw dict here. Storing durations rather than the stamps themselves
# is what makes the file readable across runs: the stamps share no epoch
# between processes, so only the differences mean anything. An unpaired stage
# writes an empty cell instead of a zero, because a stage that never ran and a
# stage that took no measurable time are different facts.

PLATE_TIMING_FILENAME = "_plate_timing_samples.csv"
PLATE_TIMING_COLUMNS = [
    "recorded_at",
    "shot_index",
    "plate_index",
    "well",
    "shot_number",
    "mode",
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
]

#: Output column ← the pair of stamps whose difference fills it.
_PLATE_TIMING_STAGES: tuple[tuple[str, str, str], ...] = (
    ("motion_command_ms", "motion_start", "motion_command_end"),
    ("idle_wait_ms", "idle_wait_start", "idle_wait_end"),
    ("settle_ms", "settle_start", "settle_end"),
    ("interlock_ms", "interlock_start", "interlock_end"),
    ("trigger_read_ms", "trigger_wait_start", "trigger_wait_end"),
    ("wavelengths_fetch_ms", "wavelengths_fetch_start", "wavelengths_fetch_end"),
    ("save_ms", "save_start", "save_end"),
    ("state_write_ms", "state_write_start", "state_write_end"),
    ("cycle_ms", "cycle_start", "cycle_end"),
    # Dead time between shots: the gap from the previous cycle closing to this
    # one opening. It is the number that exposes a slow UI thread.
    ("loop_gap_ms", "previous_cycle_end", "cycle_start"),
)


def plate_timing_samples_path(save_directory: str | os.PathLike[str]) -> str:
    return str(Path(save_directory) / PLATE_TIMING_FILENAME)


def _timing_duration_ms(timing: dict[str, Any], start_key: str, end_key: str) -> Any:
    start = timing.get(start_key)
    end = timing.get(end_key)
    if start is None or end is None:
        return ""
    try:
        return (float(end) - float(start)) * 1000.0
    except (TypeError, ValueError):
        return ""


def plate_timing_row(timing: dict[str, Any]) -> dict[str, Any]:
    """Convert one shot's perf-counter stamps into a row of stage durations."""
    row: dict[str, Any] = {column: "" for column in PLATE_TIMING_COLUMNS}
    row["recorded_at"] = datetime.now().isoformat(timespec="milliseconds")
    for key in ("shot_index", "plate_index", "well", "shot_number", "mode"):
        value = timing.get(key)
        if value is not None:
            row[key] = value
    for column, start_key, end_key in _PLATE_TIMING_STAGES:
        row[column] = _timing_duration_ms(timing, start_key, end_key)
    return row


def append_plate_timing_row(save_directory: str | os.PathLike[str], row: dict[str, Any]) -> str:
    """Append one timing row, writing the header if the file is new or empty."""
    filepath = Path(save_directory) / PLATE_TIMING_FILENAME
    filepath.parent.mkdir(parents=True, exist_ok=True)
    write_header = not filepath.is_file() or filepath.stat().st_size == 0
    with filepath.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PLATE_TIMING_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)
        # Flushed per row: a run that dies mid-plate must still leave every
        # timing sample it managed to take.
        handle.flush()
    return str(filepath)


# ─── Resume discovery ─────────────────────────────────────────────────────


def _records_from_plate_folder(plate_dir: Path) -> list[PlateShotRecord]:
    """Infer saved shots by scanning the plate folder's CSV filenames."""
    found: list[tuple[str, int, str, Path]] = []
    for csv_path in sorted(plate_dir.glob("*.csv")):
        match = _SHOT_FILE_RE.match(csv_path.name)
        if not match:
            continue
        found.append(
            (
                match.group("timestamp"),
                int(match.group("shot")),
                match.group("well").upper(),
                csv_path,
            )
        )
    found.sort(key=lambda item: (item[0], item[1]))
    return [
        PlateShotRecord(well=well, shot_number=shot, filepath=str(path))
        for _timestamp, shot, well, path in found
    ]


def records_from_plate_folder(plate_dir: str | os.PathLike[str]) -> list[PlateShotRecord]:
    """Rebuild the saved-shot list for a plate by scanning the files on disk.

    The state file is the fast path; this is the authority when it is missing or
    unreadable, because the spectra themselves are the durable record. An
    automated run that lost its state JSON can resume from this rather than
    re-shooting wells it has already burned.
    """
    return _records_from_plate_folder(Path(plate_dir))


def _infer_config_from_records(plate_name: str, records: Sequence[PlateShotRecord]) -> PlateAutosaveConfig:
    """Guess plate format and shots-per-well from scanned filenames."""
    max_row = 0
    max_column = 1
    per_well: dict[str, int] = {}
    for record in records:
        match = _WELL_RE.match(record.well)
        if not match:
            continue
        max_row = max(max_row, ord(match.group(1)) - ord("A") + 1)
        max_column = max(max_column, int(match.group(2)))
        per_well[record.well] = per_well.get(record.well, 0) + 1

    plate_type = DEFAULT_PLATE_TYPE
    for well_count in sorted(PLATE_FORMATS):
        rows, columns = PLATE_FORMATS[well_count]
        if max_row <= rows and max_column <= columns:
            plate_type = well_count
            break

    return PlateAutosaveConfig(
        plate_type=plate_type,
        plate_name=plate_name,
        shots_per_well=max(1, max(per_well.values(), default=1)),
    )


def discover_resumable_plate_runs(save_directory: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Find incomplete plate folders under ``save_directory``.

    Each candidate is a dict with the keys the resume dialog reads::

        {"plate_name", "payload", "source_label", "needs_confirmation",
         "state", "records", "plate_dir"}

    A folder with a readable ``plate_run_state.json`` resumes directly. A folder
    with only CSV files is offered with ``needs_confirmation=True`` so the user
    confirms the inferred plate format before it is trusted.
    """
    base = Path(save_directory or "")
    if not base.is_dir():
        return []

    candidates: list[dict[str, Any]] = []
    for plate_dir in sorted(path for path in base.iterdir() if path.is_dir()):
        if plate_dir.name == DISCARDED_DIR_NAME:
            continue

        state = load_plate_run_state(plate_dir)
        if state is not None:
            if state.is_complete or state.closed_early:
                continue
            candidates.append(
                {
                    "plate_name": state.config.plate_name,
                    "plate_dir": str(plate_dir),
                    "payload": state.progress_payload(),
                    "source_label": "state file",
                    "needs_confirmation": False,
                    "state": state,
                    "records": list(state.records),
                }
            )
            continue

        records = _records_from_plate_folder(plate_dir)
        if not records:
            continue
        config = _infer_config_from_records(plate_dir.name, records)
        try:
            scanned_state = PlateRunState.from_records(config, records)
        except ValueError:
            logger.warning("Skipping unreadable plate folder: %s", plate_dir, exc_info=True)
            continue
        if scanned_state.is_complete:
            continue
        candidates.append(
            {
                "plate_name": config.plate_name,
                "plate_dir": str(plate_dir),
                "payload": scanned_state.progress_payload(),
                "source_label": "file scan",
                "needs_confirmation": True,
                "state": scanned_state,
                "records": records,
            }
        )

    return candidates


__all__ = [
    "DISCARDED_DIR_NAME",
    "ORDER_COLUMN",
    "ORDER_LABELS",
    "ORDER_ROW",
    "PLATE_FORMATS",
    "PLATE_TIMING_COLUMNS",
    "PLATE_TIMING_FILENAME",
    "PlateAutosaveConfig",
    "PlateRunState",
    "PlateShotRecord",
    "append_plate_reproducibility_event",
    "append_plate_timing_row",
    "discover_resumable_plate_runs",
    "load_plate_run_state",
    "ordered_wells",
    "records_from_plate_folder",
    "plate_timing_row",
    "plate_timing_samples_path",
    "sanitize_filename_part",
    "save_plate_reproducibility_log",
    "save_plate_run_state",
    "unique_output_path",
    "well_names",
]
