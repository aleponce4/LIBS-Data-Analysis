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
) -> SpectraQCResult:
    """Score every saved shot of ``plate_state`` and write the QC CSV.

    Unreadable files are reported as failing rows with the read error as the
    reason - never skipped silently.
    """
    scorer = qc or HeuristicSpectraQC()
    config = plate_state.config
    plate_dir = Path(save_directory) / config.safe_plate_name
    result = SpectraQCResult(plate_name=config.plate_name, scorer_label=scorer.label)

    for record in plate_state.records:
        row: dict[str, Any] = {
            "plate_name": config.plate_name,
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


def apply_spectra_qc_to_plate_states(result: SpectraQCResult, plate_state: Any) -> None:
    """Fold QC outcomes onto the plate map as ``qc_by_well``.

    A well takes its worst shot status so the plate map never shows a well as
    passing when one of its shots failed.
    """
    by_well: dict[str, list[dict[str, Any]]] = {}
    for row in result.rows:
        well = str(row.get("well") or "").upper()
        if well:
            by_well.setdefault(well, []).append(row)

    qc_by_well: dict[str, dict[str, Any]] = {}
    for well, rows in by_well.items():
        statuses = [str(row.get("gate_status") or GATE_WARN) for row in rows]
        scores = [row.get("readiness_score") for row in rows if isinstance(row.get("readiness_score"), (int, float))]
        reasons: list[str] = []
        for row in rows:
            for reason in row.get("failure_reasons") or []:
                if reason not in reasons:
                    reasons.append(reason)
        qc_by_well[well] = {
            "status": _worst_status(statuses),
            "gate_status": _worst_status(statuses),
            "readiness_score": min(scores) if scores else None,
            "shots_scored": len(rows),
            "reasons": reasons,
        }

    plate_state.qc_by_well = qc_by_well


__all__ = [
    "GATE_FAIL",
    "GATE_PASS",
    "GATE_WARN",
    "HeuristicSpectraQC",
    "SpectraQCLoadResult",
    "SpectraQCResult",
    "apply_spectra_qc_to_plate_states",
    "load_default_spectra_qc",
    "run_high_throughput_spectra_qc",
]
