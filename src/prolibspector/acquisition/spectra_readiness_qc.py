"""Spectra readiness QC for high-throughput plate runs.

PUBLIC-EDITION MODULE
=====================
This is a public-edition implementation written against the call sites in
``acquisition/app.py``, ``acquisition/worker.py`` and
``acquisition/controllers.py``.

What this does: scores every saved spectrum of a plate run with a transparent,
deterministic heuristic - detector saturation, signal-to-noise ratio, continuum
level, and dead/flat traces - and gates each well as ``pass``, ``warn`` or
``fail`` with human-readable reasons. Results are written to CSV and applied
back onto the plate map so failing wells can be queued for repeat acquisition.
This is real, working QC: no model file is needed and no result is fabricated.

What the private edition adds: a *trained* readiness calibration (a supervised
model fitted on labelled reference plates, with per-instrument calibration
files), matrix-specific acceptance thresholds, and a typeset PDF report. Where
the private edition writes ``pdf_path``, this edition leaves it empty and the
UI shows "not written" - it does not pretend a report exists.

Thresholds live in ``HeuristicSpectraQC`` and are intentionally conservative:
they are meant to catch obviously unusable spectra (saturated, dark, flat),
not to replace a calibrated acceptance model.
"""

from __future__ import annotations

import csv
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

logger = logging.getLogger(__name__)

GATE_PASS = "pass"
GATE_WARN = "warn"
GATE_FAIL = "fail"

QC_CSV_NAME = "spectra_readiness_qc.csv"

QC_CSV_COLUMNS = [
    "plate_name",
    "well",
    "shot_number",
    "gate_status",
    "readiness_score",
    "saturated_fraction",
    "signal_to_noise",
    "peak_intensity",
    "baseline_intensity",
    "top_failure_reasons",
    "filepath",
]


# ─── Calibration loading ──────────────────────────────────────────────────


@dataclass(frozen=True)
class SpectraQCLoadResult:
    """Outcome of resolving the QC calibration for this edition."""

    available: bool
    qc: "HeuristicSpectraQC | None"
    message: str


@dataclass(frozen=True)
class HeuristicSpectraQC:
    """Deterministic readiness scorer. No trained model, no hidden state."""

    #: Fraction of pixels at/above the saturation ceiling that fails a shot.
    saturated_fraction_fail: float = 0.02
    saturated_fraction_warn: float = 0.005
    #: Counts at/above which a pixel counts as saturated.
    saturation_ceiling: float = 64_000.0
    #: Peak-to-noise ratio gates.
    snr_fail: float = 5.0
    snr_warn: float = 15.0
    #: A trace whose peak barely exceeds its own baseline is effectively dead.
    min_peak_above_baseline: float = 50.0
    label: str = "public-edition heuristic"

    def score_spectrum(self, wavelengths: np.ndarray, intensities: np.ndarray) -> dict[str, Any]:
        """Score one spectrum and return its metrics, gate, and reasons."""
        values = np.asarray(intensities, dtype=float)
        finite = values[np.isfinite(values)]
        reasons: list[str] = []

        if finite.size < 16:
            return {
                "gate_status": GATE_FAIL,
                "readiness_score": 0.0,
                "saturated_fraction": None,
                "signal_to_noise": None,
                "peak_intensity": None,
                "baseline_intensity": None,
                "failure_reasons": ["spectrum is empty or unreadable"],
            }

        peak = float(np.max(finite))
        baseline = float(np.percentile(finite, 10))
        noise = float(np.std(np.diff(finite)) / math.sqrt(2.0))
        saturated_fraction = float(np.mean(finite >= self.saturation_ceiling))
        signal = peak - baseline
        if signal <= 0.0:
            # No signal at all: a perfectly flat trace has zero measurable noise
            # too, so treating it as "infinite SNR" would score a dead detector
            # as perfect. Report zero instead.
            snr = 0.0
        elif noise > 0:
            snr = float(signal / noise)
        else:
            snr = float("inf")

        gate = GATE_PASS
        if saturated_fraction >= self.saturated_fraction_fail:
            gate = GATE_FAIL
            reasons.append(f"detector saturated on {saturated_fraction * 100:.1f}% of pixels")
        elif saturated_fraction >= self.saturated_fraction_warn:
            gate = GATE_WARN
            reasons.append(f"detector near saturation on {saturated_fraction * 100:.2f}% of pixels")

        if signal < self.min_peak_above_baseline:
            gate = GATE_FAIL
            reasons.append(f"flat trace: peak only {signal:.1f} counts above baseline")
        elif math.isfinite(snr) and snr < self.snr_fail:
            gate = GATE_FAIL
            reasons.append(f"signal-to-noise {snr:.1f} below fail threshold {self.snr_fail:.0f}")
        elif math.isfinite(snr) and snr < self.snr_warn:
            if gate != GATE_FAIL:
                gate = GATE_WARN
            reasons.append(f"signal-to-noise {snr:.1f} below warn threshold {self.snr_warn:.0f}")

        # Readiness score: 0-100, driven by SNR headroom and saturation penalty.
        snr_component = 1.0 if not math.isfinite(snr) else min(snr / (self.snr_warn * 2.0), 1.0)
        saturation_penalty = min(saturated_fraction / max(self.saturated_fraction_fail, 1e-9), 1.0)
        score = max(0.0, min(100.0, 100.0 * snr_component * (1.0 - saturation_penalty)))
        if gate == GATE_FAIL:
            # A failing shot must never read as a high-confidence score in the
            # review table, whatever the individual components worked out to.
            score = 0.0

        return {
            "gate_status": gate,
            "readiness_score": round(score, 1),
            "saturated_fraction": round(saturated_fraction, 6),
            "signal_to_noise": None if not math.isfinite(snr) else round(snr, 2),
            "peak_intensity": round(peak, 3),
            "baseline_intensity": round(baseline, 3),
            "failure_reasons": reasons,
        }


def load_default_spectra_qc() -> SpectraQCLoadResult:
    """Return the QC scorer available in this edition.

    The public edition always has its heuristic scorer available, so QC can be
    enabled without any calibration file. The message states plainly which
    scorer is in use.
    """
    qc = HeuristicSpectraQC()
    return SpectraQCLoadResult(
        available=True,
        qc=qc,
        message=(
            "Spectra QC uses the public-edition heuristic scorer "
            "(saturation, signal-to-noise, flat-trace checks). "
            "The trained readiness calibration ships with the private edition."
        ),
    )


# ─── Results ──────────────────────────────────────────────────────────────


@dataclass
class SpectraQCResult:
    """Scored QC outcome for one plate run."""

    plate_name: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    csv_path: str = ""
    pdf_path: str = ""
    #: Which plate of a multi-plate run this is. A manual run has exactly one
    #: plate and leaves it at 1; an automated run scores several and needs the
    #: index to route a failed well back to the right plate.
    plate_index: int = 1
    scorer_label: str = HeuristicSpectraQC.label
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def _count(self, status: str) -> int:
        return sum(1 for row in self.rows if row.get("gate_status") == status)

    @property
    def pass_count(self) -> int:
        return self._count(GATE_PASS)

    @property
    def warn_count(self) -> int:
        return self._count(GATE_WARN)

    @property
    def fail_count(self) -> int:
        return self._count(GATE_FAIL)

    @property
    def failed_wells(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if row.get("gate_status") == GATE_FAIL]

    def to_mapping(self) -> dict[str, Any]:
        """Return the dict shape the UI consumes (see ``_populate_qc_review``)."""
        return {
            "plate_name": self.plate_name,
            "plate_index": self.plate_index,
            "rows": [dict(row) for row in self.rows],
            "pass_count": self.pass_count,
            "warn_count": self.warn_count,
            "fail_count": self.fail_count,
            "csv_path": self.csv_path,
            "pdf_path": self.pdf_path,
            "scorer": self.scorer_label,
            "generated_at": self.generated_at,
        }


def _read_spectrum(filepath: str) -> tuple[np.ndarray, np.ndarray]:
    """Read a tab-delimited two-column spectrum written by the worker."""
    data = np.genfromtxt(filepath, delimiter="\t", skip_header=1)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    if data.shape[1] < 2:
        raise ValueError(f"{filepath} does not contain two columns.")
    return data[:, 0], data[:, 1]


def run_high_throughput_spectra_qc(
    save_directory: str,
    plate_state: Any,
    *,
    qc: HeuristicSpectraQC | None = None,
    plate_index: int = 1,
    plate_dir: Path | None = None,
) -> SpectraQCResult:
    """Score every saved shot of ``plate_state`` and write the QC CSV.

    Unreadable files are reported as failing rows with the read error as the
    reason - never skipped silently.

    ``plate_dir`` overrides where the CSV lands. A manual run keeps the default
    (``save_directory/safe_plate_name``); an automated run names its plate
    folders itself and passes the folder it actually used.
    """
    scorer = qc or HeuristicSpectraQC()
    config = plate_state.config
    if plate_dir is None:
        plate_dir = Path(save_directory) / config.safe_plate_name
    result = SpectraQCResult(
        plate_name=config.plate_name,
        plate_index=int(plate_index),
        scorer_label=scorer.label,
    )

    for record in plate_state.records:
        row: dict[str, Any] = {
            "plate_name": config.plate_name,
            "plate_index": int(plate_index),
            "well": record.well,
            "shot_number": record.shot_number,
            "filepath": record.filepath,
        }
        try:
            wavelengths, intensities = _read_spectrum(record.filepath)
        except Exception as exc:
            row.update(
                {
                    "gate_status": GATE_FAIL,
                    "readiness_score": 0.0,
                    "saturated_fraction": None,
                    "signal_to_noise": None,
                    "peak_intensity": None,
                    "baseline_intensity": None,
                    "failure_reasons": [f"could not read spectrum: {exc}"],
                }
            )
        else:
            row.update(scorer.score_spectrum(wavelengths, intensities))

        row["top_failure_reasons"] = "; ".join(row.get("failure_reasons") or [])
        result.rows.append(row)

    result.csv_path = _write_qc_csv(plate_dir, result)
    return result


def _write_qc_csv(plate_dir: Path, result: SpectraQCResult) -> str:
    """Write the per-shot QC table; return the path, or "" if it could not be written."""
    plate_dir.mkdir(parents=True, exist_ok=True)
    csv_path = plate_dir / QC_CSV_NAME
    try:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=QC_CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in result.rows:
                writer.writerow(row)
    except OSError:
        logger.warning("Could not write spectra QC CSV: %s", csv_path, exc_info=True)
        return ""
    return str(csv_path)


def _worst_status(statuses: Sequence[str]) -> str:
    if GATE_FAIL in statuses:
        return GATE_FAIL
    if GATE_WARN in statuses:
        return GATE_WARN
    return GATE_PASS


def _qc_by_well_from_rows(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collapse per-shot rows into one entry per well, worst status winning."""
    by_well: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        well = str(row.get("well") or "").upper()
        if well:
            by_well.setdefault(well, []).append(row)

    qc_by_well: dict[str, dict[str, Any]] = {}
    for well, well_rows in by_well.items():
        statuses = [str(row.get("gate_status") or GATE_WARN) for row in well_rows]
        scores = [
            row.get("readiness_score")
            for row in well_rows
            if isinstance(row.get("readiness_score"), (int, float))
        ]
        reasons: list[str] = []
        for row in well_rows:
            for reason in row.get("failure_reasons") or []:
                if reason not in reasons:
                    reasons.append(reason)
        qc_by_well[well] = {
            "status": _worst_status(statuses),
            "gate_status": _worst_status(statuses),
            # The worst shot decides, so a well never shows as passing when one
            # of its shots failed.
            "readiness_score": min(scores) if scores else None,
            "shots_scored": len(well_rows),
            "reasons": reasons,
        }
    return qc_by_well


def _rows_for_plate(rows: Sequence[dict[str, Any]], plate_index: Any) -> list[dict[str, Any]]:
    """Return only the rows belonging to one plate of a multi-plate run."""
    try:
        wanted = int(plate_index)
    except (TypeError, ValueError):
        return list(rows)
    return [row for row in rows if int(row.get("plate_index", 1) or 1) == wanted]


def apply_spectra_qc_to_plate_states(result: Any, plate_states: Any) -> None:
    """Fold QC outcomes onto the plate map(s) as ``qc_by_well``.

    Accepts a single plate state (the manual workflow), a ``{plate_index: state}``
    mapping or a sequence of states (the automated workflow). Each plate is given
    only its own wells - folding another plate's rows onto a plate would mark
    wells that were never shot there.
    """
    rows = list(getattr(result, "rows", None) or [])

    if isinstance(plate_states, Mapping):
        for plate_index, state in plate_states.items():
            state.qc_by_well = _qc_by_well_from_rows(_rows_for_plate(rows, plate_index))
        return

    if isinstance(plate_states, (list, tuple)):
        for plate_index, state in enumerate(plate_states, start=1):
            state.qc_by_well = _qc_by_well_from_rows(_rows_for_plate(rows, plate_index))
        return

    plate_states.qc_by_well = _qc_by_well_from_rows(rows)


# ─── Automated multi-plate QC ─────────────────────────────────────────────
#
# The manual workflow scores one plate and reports per shot. An automated run
# scores every plate of a run and reports per *well*, because the thing the
# operator acts on is "re-shoot this well" - and re-shooting is per well, not
# per shot. So this layer aggregates the per-shot scoring above rather than
# replacing it, and carries the plate index and stage coordinates a repeat
# needs in order to drive back to the right place.

QC_AUTOMATED_CSV_NAME = "spectra_readiness_qc.csv"

QC_AUTOMATED_CSV_COLUMNS = [
    "plate_index",
    "plate_name",
    "well",
    "gate_status",
    "readiness_score",
    "shot_count",
    "target_x_mm",
    "target_y_mm",
    "top_failure_reasons",
]


@dataclass(frozen=True)
class AutomatedQCWellRow:
    """One well's aggregated QC verdict across all of its shots."""

    plate_index: int
    plate_name: str
    well: str
    gate_status: str
    readiness_score: float | None = None
    target_x_mm: float | None = None
    target_y_mm: float | None = None
    shot_count: int = 0
    failure_reasons: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "plate_index": self.plate_index,
            "plate_name": self.plate_name,
            "well": self.well,
            "gate_status": self.gate_status,
            "readiness_score": self.readiness_score,
            "shot_count": self.shot_count,
            "target_x_mm": self.target_x_mm,
            "target_y_mm": self.target_y_mm,
            "failure_reasons": list(self.failure_reasons),
            "top_failure_reasons": "; ".join(self.failure_reasons),
        }


@dataclass
class AutomatedSpectraQCResult:
    """QC outcome for a whole automated run, aggregated per well."""

    well_rows: list[AutomatedQCWellRow] = field(default_factory=list)
    #: Per-shot rows, kept so the plate map can be folded exactly as the manual
    #: workflow folds it (see :func:`apply_spectra_qc_to_plate_states`).
    shot_rows: list[dict[str, Any]] = field(default_factory=list)
    csv_path: str = ""
    #: The public edition writes no PDF report; the UI renders "not written".
    pdf_path: str = ""
    scorer_label: str = HeuristicSpectraQC.label
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def rows(self) -> list[dict[str, Any]]:
        """Well rows as dicts - what the QC review tree renders."""
        return [row.to_mapping() for row in self.well_rows]

    def _count(self, status: str) -> int:
        return sum(1 for row in self.well_rows if row.gate_status == status)

    @property
    def pass_count(self) -> int:
        return self._count(GATE_PASS)

    @property
    def warn_count(self) -> int:
        return self._count(GATE_WARN)

    @property
    def fail_count(self) -> int:
        return self._count(GATE_FAIL)

    @property
    def failed_well_targets(self) -> tuple[dict[str, Any], ...]:
        """Failed wells, each carrying what a repeat acquisition needs."""
        return tuple(row.to_mapping() for row in self.well_rows if row.gate_status == GATE_FAIL)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "pass_count": self.pass_count,
            "warn_count": self.warn_count,
            "fail_count": self.fail_count,
            "csv_path": self.csv_path,
            "pdf_path": self.pdf_path,
            "scorer": self.scorer_label,
            "generated_at": self.generated_at,
        }


def _target_coordinates(plan_details: Any, config: Any) -> dict[tuple[int, str], tuple[float, float]]:
    """Map ``(plate_index, well)`` to stage coordinates.

    Prefers the already-computed plan, falling back to re-planning the targets,
    so a caller that passes no plan still gets coordinates rather than blanks.
    """
    coordinates: dict[tuple[int, str], tuple[float, float]] = {}

    targets = None
    if isinstance(plan_details, Mapping):
        targets = plan_details.get("targets")
    for target in targets or []:
        try:
            key = (int(target["plate_index"]), str(target["well"]).upper())
            coordinates.setdefault(key, (float(target["x_mm"]), float(target["y_mm"])))
        except (KeyError, TypeError, ValueError):
            continue
    if coordinates:
        return coordinates

    try:
        from prolibspector.acquisition.automation import automation_targets

        for target in automation_targets(config):
            key = (int(target.plate_index), str(target.well).upper())
            coordinates.setdefault(key, (float(target.x_mm), float(target.y_mm)))
    except Exception:
        logger.debug("Could not resolve target coordinates for QC rows.", exc_info=True)
    return coordinates


def _plate_state_for_qc(plate_dir: Path, plate_name: str) -> Any:
    """Return the saved plate state, rebuilding from files when it is missing."""
    from prolibspector.acquisition.plate_autosave import (
        PlateAutosaveConfig,
        PlateRunState,
        load_plate_run_state,
        records_from_plate_folder,
    )

    state = load_plate_run_state(plate_dir)
    if state is not None:
        return state
    # The spectra are the durable record: a plate whose state file never landed
    # is still fully scoreable from the CSVs it wrote.
    records = records_from_plate_folder(plate_dir)
    if not records:
        return None
    return PlateRunState(PlateAutosaveConfig(plate_name=plate_name), records)


def run_automated_spectra_qc(
    save_directory: str,
    automation_config: Any,
    plan_details: Any = None,
    *,
    qc: HeuristicSpectraQC | None = None,
) -> AutomatedSpectraQCResult:
    """Score every plate of an automated run and write one run-level QC CSV."""
    from prolibspector.acquisition.automation import plan_plate_slots

    scorer = qc or HeuristicSpectraQC()
    root = Path(save_directory)
    coordinates = _target_coordinates(plan_details, automation_config)
    result = AutomatedSpectraQCResult(scorer_label=scorer.label)

    for slot in plan_plate_slots(automation_config):
        plate_index = int(getattr(slot, "index", slot))
        plate_dir = root / automation_config.plate_folder_name(plate_index)
        plate_name = automation_config.plate_display_name(plate_index)

        plate_state = _plate_state_for_qc(plate_dir, plate_name)
        if plate_state is None:
            continue

        plate_result = run_high_throughput_spectra_qc(
            str(root),
            plate_state,
            qc=scorer,
            plate_index=plate_index,
            plate_dir=plate_dir,
        )
        result.shot_rows.extend(plate_result.rows)

        for well, verdict in _qc_by_well_from_rows(plate_result.rows).items():
            x_mm, y_mm = coordinates.get((plate_index, well), (None, None))
            result.well_rows.append(
                AutomatedQCWellRow(
                    plate_index=plate_index,
                    plate_name=plate_name,
                    well=well,
                    gate_status=str(verdict["gate_status"]),
                    readiness_score=verdict["readiness_score"],
                    target_x_mm=x_mm,
                    target_y_mm=y_mm,
                    shot_count=int(verdict["shots_scored"]),
                    failure_reasons=tuple(verdict["reasons"]),
                )
            )

    result.well_rows.sort(key=lambda row: (row.plate_index, row.well))
    result.csv_path = _write_automated_qc_csv(root, result)
    return result


def _write_automated_qc_csv(root: Path, result: AutomatedSpectraQCResult) -> str:
    """Write the run-level well table; return the path, or "" if unwritable."""
    csv_path = root / QC_AUTOMATED_CSV_NAME
    try:
        root.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=QC_AUTOMATED_CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in result.well_rows:
                writer.writerow(row.to_mapping())
    except OSError:
        logger.warning("Could not write automated spectra QC CSV: %s", csv_path, exc_info=True)
        return ""
    return str(csv_path)


__all__ = [
    "GATE_FAIL",
    "GATE_PASS",
    "GATE_WARN",
    "AutomatedQCWellRow",
    "AutomatedSpectraQCResult",
    "HeuristicSpectraQC",
    "SpectraQCLoadResult",
    "SpectraQCResult",
    "apply_spectra_qc_to_plate_states",
    "load_default_spectra_qc",
    "run_automated_spectra_qc",
    "run_high_throughput_spectra_qc",
]
