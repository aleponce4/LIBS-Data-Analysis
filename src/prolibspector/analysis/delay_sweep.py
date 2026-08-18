"""Timing-sweep analysis: compare delay/integration settings, recommend one.

Works on the sweep acquisition modes, which all store the same per-shot
facts (``trigger_delay_us`` and ``integration_time_us``):

- 96-well plate sweeps (``column_delays_us`` and/or per-plate
  ``plate_integration_times_ms``): recorded in each plate's
  ``_plate_state.json`` history.
- 2D mapping sweeps on a blank metal plate (``column_delays_us`` by grid
  column, ``row_integration_times_ms`` by row band): recorded in
  ``_mapping_grid_index.csv`` with spectra in the binary store (or CSVs).

A run that varies only the delay is a 1D sweep; a run that also varies
integration is analyzed per (delay, integration) cell. The metrics avoid
element identification: on a sweep blank the questions are only "how much
line signal is left", "how much continuum remains", "what is the noise
floor", and "is anything saturated".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import base64
import csv
import html
import json
import math
import os
from typing import Any

import numpy as np
import pandas as pd

from prolibspector.acquisition.automation_mapping import (
    MAPPING_INDEX_FILENAME,
    MAPPING_MANIFEST_FILENAME,
)
from prolibspector.core.spectrum_display import (
    DEFAULT_NDYAG_EXCLUDED_WAVELENGTH_BANDS_NM,
    NDYAG_FUNDAMENTAL_NM as _NDYAG_FUNDAMENTAL_NM,
    excluded_wavelength_mask,
    spectrum_autoscale_peak,
)

SATURATION_PIXEL_THRESHOLD = 0.98
DEFAULT_SNR_TOLERANCE = 0.85
DEFAULT_SATURATION_MAX_FRACTION = 0.005

# The Nd:YAG band is shared with live display scaling. Keep this established
# delay-sweep name as the public/default analysis configuration.
NDYAG_FUNDAMENTAL_NM = _NDYAG_FUNDAMENTAL_NM
DEFAULT_EXCLUDED_WAVELENGTH_BANDS_NM = DEFAULT_NDYAG_EXCLUDED_WAVELENGTH_BANDS_NM


@dataclass
class DelaySweepShot:
    delay_us: float
    intensities: np.ndarray
    integration_us: float | None = None
    source: str = ""
    run_id: str = ""
    x_mm: float | None = None
    y_mm: float | None = None
    row_index: int | None = None
    column_index: int | None = None


@dataclass
class DelaySweepDataset:
    wavelengths: np.ndarray
    shots: list[DelaySweepShot] = field(default_factory=list)
    run_type: str = ""
    run_directory: str = ""
    column_delays_us: tuple[float, ...] = ()
    integration_times_ms: tuple[float, ...] = ()
    max_intensity: float = 65535.0

    def delays(self) -> list[float]:
        return sorted({float(shot.delay_us) for shot in self.shots})

    def shots_for_delay(self, delay_us: float) -> list[DelaySweepShot]:
        return [shot for shot in self.shots if float(shot.delay_us) == float(delay_us)]

    def cells(self) -> list[tuple[float, float | None]]:
        """Sorted unique (delay_us, integration_us) sweep cells."""
        keys = {(float(shot.delay_us), None if shot.integration_us is None else float(shot.integration_us)) for shot in self.shots}
        return sorted(keys, key=lambda key: (key[0], -1.0 if key[1] is None else key[1]))

    def shots_for_cell(self, delay_us: float, integration_us: float | None) -> list[DelaySweepShot]:
        return [
            shot
            for shot in self.shots
            if float(shot.delay_us) == float(delay_us)
            and (
                (shot.integration_us is None and integration_us is None)
                or (
                    shot.integration_us is not None
                    and integration_us is not None
                    and float(shot.integration_us) == float(integration_us)
                )
            )
        ]

    def integration_values_us(self) -> list[float | None]:
        values = {None if shot.integration_us is None else float(shot.integration_us) for shot in self.shots}
        return sorted(values, key=lambda value: -1.0 if value is None else value)


def _load_spectrum_csv(filepath: str) -> tuple[np.ndarray, np.ndarray]:
    data = np.loadtxt(filepath, delimiter="\t", skiprows=1)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    return np.asarray(data[:, 0], dtype=float), np.asarray(data[:, 1], dtype=float)


def _delay_from_column(schedule: tuple[float, ...], column: int) -> float | None:
    if not schedule or column < 1 or column > len(schedule):
        return None
    return float(schedule[column - 1])


def _optional_float(value) -> float | None:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def load_mapping_delay_sweep(run_directory: str) -> DelaySweepDataset:
    """Load a mapping timing-sweep run from its index and stores."""
    from prolibspector.acquisition.mapping_spectrum_store import read_binary_mapping_spectrum

    manifest_path = os.path.join(run_directory, MAPPING_MANIFEST_FILENAME)
    schedule: tuple[float, ...] = ()
    integration_schedule: tuple[float, ...] = ()
    max_intensity = 65535.0
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        config = (manifest.get("plan") or {}).get("config") or {}
        raw_schedule = config.get("column_delays_us") or ()
        if isinstance(raw_schedule, (list, tuple)):
            schedule = tuple(float(value) for value in raw_schedule)
        raw_integrations = config.get("row_integration_times_ms") or ()
        if isinstance(raw_integrations, (list, tuple)):
            integration_schedule = tuple(float(value) for value in raw_integrations)
        spectrometer = manifest.get("spectrometer") or {}
        try:
            max_intensity = float(spectrometer.get("max_intensity") or 65535.0)
        except (TypeError, ValueError):
            max_intensity = 65535.0

    index_path = os.path.join(run_directory, MAPPING_INDEX_FILENAME)
    if not os.path.isfile(index_path):
        raise FileNotFoundError(f"Mapping index not found: {index_path}")

    wavelengths: np.ndarray | None = None
    shots: list[DelaySweepShot] = []
    with open(index_path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            delay_us = _optional_float(row.get("trigger_delay_us"))
            if delay_us is None:
                try:
                    delay_us = _delay_from_column(schedule, int(row.get("column_index") or 0))
                except (TypeError, ValueError):
                    delay_us = None
            if delay_us is None:
                continue
            integration_us = _optional_float(row.get("integration_time_us"))

            storage_kind = str(row.get("storage_kind") or "").strip().lower()
            spectrum: tuple[np.ndarray, np.ndarray] | None = None
            if storage_kind == "binary_npy":
                try:
                    spectrum = read_binary_mapping_spectrum(run_directory, row.get("binary_row_index"))
                except Exception:
                    spectrum = None
            if spectrum is None:
                filepath = str(row.get("filepath") or "").strip()
                if filepath and not os.path.isabs(filepath):
                    filepath = os.path.join(run_directory, filepath)
                if filepath and os.path.isfile(filepath):
                    spectrum = _load_spectrum_csv(filepath)
            if spectrum is None:
                continue

            shot_wavelengths, intensities = spectrum
            if wavelengths is None:
                wavelengths = np.asarray(shot_wavelengths, dtype=float)
            shots.append(
                DelaySweepShot(
                    delay_us=delay_us,
                    intensities=np.asarray(intensities, dtype=float),
                    integration_us=integration_us,
                    source=str(row.get("target_key") or ""),
                    run_id=os.path.basename(os.path.normpath(run_directory)),
                    x_mm=_optional_float(row.get("x_mm")),
                    y_mm=_optional_float(row.get("y_mm")),
                    row_index=(
                        int(row["row_index"])
                        if str(row.get("row_index") or "").strip().isdigit()
                        else None
                    ),
                    column_index=(
                        int(row["column_index"])
                        if str(row.get("column_index") or "").strip().isdigit()
                        else None
                    ),
                )
            )

    if wavelengths is None or not shots:
        raise ValueError(f"No timing-sweep shots found in mapping run: {run_directory}")
    return DelaySweepDataset(
        wavelengths=wavelengths,
        shots=shots,
        run_type="mapping",
        run_directory=run_directory,
        column_delays_us=schedule,
        integration_times_ms=integration_schedule,
        max_intensity=max_intensity,
    )


def load_plate_delay_sweep(run_directory: str) -> DelaySweepDataset:
    """Load a 96-well timing-sweep run from plate states and CSVs."""
    wavelengths: np.ndarray | None = None
    shots: list[DelaySweepShot] = []
    schedule: tuple[float, ...] = ()
    integration_schedule: tuple[float, ...] = ()

    for entry in sorted(os.listdir(run_directory)):
        plate_dir = os.path.join(run_directory, entry)
        state_path = os.path.join(plate_dir, "_plate_state.json")
        if not os.path.isfile(state_path):
            continue
        with open(state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        raw_schedule = state.get("column_delays_us") or ()
        if isinstance(raw_schedule, (list, tuple)) and raw_schedule:
            schedule = tuple(float(value) for value in raw_schedule)
        raw_integrations = state.get("plate_integration_times_ms") or ()
        if isinstance(raw_integrations, (list, tuple)) and raw_integrations:
            integration_schedule = tuple(float(value) for value in raw_integrations)
        for record in state.get("history", []):
            delay_us = record.get("trigger_delay_us")
            if delay_us is None and schedule:
                well = str(record.get("well") or "").strip().upper()
                digits = "".join(ch for ch in well[1:] if ch.isdigit())
                if digits:
                    delay_us = _delay_from_column(schedule, int(digits))
            if delay_us is None:
                continue
            integration_us = record.get("integration_time_us")
            if integration_us is None and record.get("integration_time_ms") is not None:
                integration_us = float(record["integration_time_ms"]) * 1000.0
            filepath = str(record.get("filepath") or "")
            if filepath and not os.path.isabs(filepath):
                filepath = os.path.join(plate_dir, filepath)
            if not filepath or not os.path.isfile(filepath):
                continue
            shot_wavelengths, intensities = _load_spectrum_csv(filepath)
            if wavelengths is None:
                wavelengths = np.asarray(shot_wavelengths, dtype=float)
            shots.append(
                DelaySweepShot(
                    delay_us=float(delay_us),
                    intensities=np.asarray(intensities, dtype=float),
                    integration_us=None if integration_us is None else float(integration_us),
                    source=str(record.get("well") or ""),
                    run_id=os.path.basename(os.path.normpath(run_directory)),
                )
            )

    if wavelengths is None or not shots:
        raise ValueError(f"No timing-sweep shots found in plate run: {run_directory}")
    return DelaySweepDataset(
        wavelengths=wavelengths,
        shots=shots,
        run_type="plate",
        run_directory=run_directory,
        column_delays_us=schedule,
        integration_times_ms=integration_schedule,
    )


def load_delay_sweep(run_directory: str) -> DelaySweepDataset:
    """Load either sweep mode from a run directory (auto-detected)."""
    if os.path.isfile(os.path.join(run_directory, MAPPING_INDEX_FILENAME)):
        return load_mapping_delay_sweep(run_directory)
    return load_plate_delay_sweep(run_directory)


def load_delay_sweep_runs(run_directories: list[str] | tuple[str, ...]) -> DelaySweepDataset:
    """Merge several sweep runs of the same plan into one pooled dataset.

    Repeat runs are replicates: pooling them tightens every per-cell standard
    error. Wavelength grids must match; schedules are taken from the first
    run (cells group by the recorded per-shot values, so runs whose schedule
    orders differ still merge correctly).
    """
    directories = [str(path) for path in run_directories if str(path).strip()]
    if not directories:
        raise ValueError("No run directories to merge.")
    first = load_delay_sweep(directories[0])
    if len(directories) == 1:
        return first
    merged_shots = list(first.shots)
    for path in directories[1:]:
        dataset = load_delay_sweep(path)
        if dataset.wavelengths.shape != first.wavelengths.shape or not np.allclose(
            dataset.wavelengths, first.wavelengths, rtol=0.0, atol=1e-6
        ):
            raise ValueError(
                f"Wavelength calibration in {path} does not match {directories[0]}; "
                "merge only runs from the same instrument configuration."
            )
        merged_shots.extend(dataset.shots)
    return DelaySweepDataset(
        wavelengths=first.wavelengths,
        shots=merged_shots,
        run_type=first.run_type,
        run_directory=" + ".join(directories),
        column_delays_us=first.column_delays_us,
        integration_times_ms=first.integration_times_ms,
        max_intensity=first.max_intensity,
    )


PEAK_MIN_SNR = 5.0
PEAK_MIN_SEPARATION_PX = 4
# A peak found on the cell's MEAN spectrum must also clear this SNR in at
# least this fraction of the individual shots to count as a line: averaging
# dilutes a single-shot spike (cosmic ray, one lucky plasma) below the
# per-shot threshold, but never to zero, so mean-only detection would keep it.
PEAK_PRESENCE_MIN_SNR = 3.0
PEAK_MIN_PRESENCE_FRACTION = 0.5
# Fraction of exactly-zero samples above which a cell is treated as
# zero-clamped (the YSM-8111 dark-subtracts and clamps at 0 in firmware, so
# fully clamped pixels carry no noise information and one ADC count is the
# smallest resolvable step).
ZERO_CLAMP_FRACTION = 0.05


def _cell_noise_floor(stack: np.ndarray, net: np.ndarray) -> float:
    """Per-shot high-frequency detection noise floor for one sweep cell.

    Peak detection asks whether a narrow line stands out of the LOCAL
    continuum, so the floor must be the uncorrelated pixel-to-pixel noise —
    not the shot-to-shot std of a pixel, which is dominated by the whole
    spectrum breathing up and down together (plasma brightness fluctuation;
    that variation belongs to the SE/RSD repeatability metrics, and a naive
    per-pixel std would also collapse to ~0 on zero-clamped quiet pixels).
    First differences of each shot cancel the smooth continuum and its
    correlated scaling; a robust MAD over them estimates the pixel noise
    (sigma of a difference of two iid pixels is sigma*sqrt(2)). Diffs are
    taken only between pixels active in most shots (clamped pixels carry no
    noise information), narrow lines contribute a handful of large diffs
    that the MAD ignores, and heavily clamped cells floor at one ADC count.
    """
    stack = np.asarray(stack, dtype=float)
    active = (stack > 0).mean(axis=0) >= 0.5
    noise = float("nan")
    pair = active[:-1] & active[1:]
    if int(pair.sum()) >= 32:
        diffs = np.diff(stack, axis=1)[:, pair]
        mad = np.nanmedian(np.abs(diffs - np.nanmedian(diffs, axis=1, keepdims=True)), axis=1)
        noise = float(np.nanmedian(1.4826 * mad / math.sqrt(2.0)))
    if not (math.isfinite(noise) and noise > 0):
        base = net[active] if active.any() else net
        quiet_values = base[base <= np.nanpercentile(base, 60.0)] if base.size else base
        if quiet_values.size:
            noise = float(1.4826 * np.nanmedian(np.abs(quiet_values - np.nanmedian(quiet_values))))
    if not (math.isfinite(noise) and noise > 0):
        noise = 1e-9
    if float(np.mean(stack == 0.0)) > ZERO_CLAMP_FRACTION:
        noise = max(noise, 1.0)
    return noise


def _analyze_cell_peaks(
    stack: np.ndarray, baseline: np.ndarray, net: np.ndarray, noise: float
) -> list[dict[str, Any]]:
    """Peaks of the cell mean, each with its per-shot presence fraction."""
    analyzed = []
    for index, height in detect_peaks(net, noise):
        shot_net = stack[:, index] - baseline[index]
        presence = float(np.mean(shot_net >= PEAK_PRESENCE_MIN_SNR * noise))
        analyzed.append(
            {
                "index": int(index),
                "height": float(height),
                "presence": presence,
                "persistent": presence >= PEAK_MIN_PRESENCE_FRACTION,
            }
        )
    return analyzed


def _iter_cell_statistics(dataset: "DelaySweepDataset", keep: np.ndarray):
    """Per-cell (delay, integration, full_stack, stack, mean, baseline, net, noise).

    The one place the per-cell spectra statistics are computed, so the
    metrics, the line inventory, and the target-line table can never drift
    apart on noise or baseline definitions.
    """
    for delay_us, integration_us in dataset.cells():
        full_stack = np.vstack([shot.intensities for shot in dataset.shots_for_cell(delay_us, integration_us)])
        stack = full_stack[:, keep]
        mean_spectrum = np.nanmean(stack, axis=0)
        baseline = rolling_percentile_baseline(mean_spectrum)
        net = mean_spectrum - baseline
        noise = _cell_noise_floor(stack, net)
        yield delay_us, integration_us, full_stack, stack, mean_spectrum, baseline, net, noise


PEAK_PROMINENCE_HALF_WINDOW_PX = 10


def detect_peaks(net: np.ndarray, noise: float, *,
                 min_snr: float = PEAK_MIN_SNR,
                 min_separation_px: int = PEAK_MIN_SEPARATION_PX) -> list[tuple[int, float]]:
    """Local maxima of the net spectrum above ``min_snr`` × noise.

    A candidate must also rise ``min_snr`` × noise above the 25th percentile
    of its ±``PEAK_PROMINENCE_HALF_WINDOW_PX`` neighborhood: an emission
    line is narrow, so its local surroundings sit near zero, while a broad
    positive residual (the rolling-percentile baseline undershoots where the
    continuum is steep) elevates its whole neighborhood and is rejected.
    Greedy strongest-first selection with a minimum pixel separation, so one
    broad line does not count as several peaks. Returns (index, height).
    """
    net = np.asarray(net, dtype=float)
    if net.size < 3 or not math.isfinite(noise) or noise <= 0:
        return []
    threshold = float(min_snr) * float(noise)
    interior = (net[1:-1] >= net[:-2]) & (net[1:-1] >= net[2:]) & (net[1:-1] > threshold)
    candidates = np.flatnonzero(interior) + 1
    if candidates.size == 0:
        return []
    half = PEAK_PROMINENCE_HALF_WINDOW_PX
    prominent = [
        index
        for index in candidates
        if net[index] - float(np.percentile(net[max(0, index - half):index + half + 1], 25.0)) > threshold
    ]
    candidates = np.asarray(prominent, dtype=int)
    if candidates.size == 0:
        return []
    order = candidates[np.argsort(net[candidates])[::-1]]
    picked: list[int] = []
    for index in order:
        if all(abs(index - other) >= min_separation_px for other in picked):
            picked.append(int(index))
    picked.sort()
    return [(index, float(net[index])) for index in picked]


LINE_MATCH_TOLERANCE_NM = 0.3
_ION_ROMAN = {1: "I", 2: "II", 3: "III"}


def _line_reference() -> "pd.DataFrame | None":
    """Curated persistent-lines database (the same one the labeling GUI uses)."""
    try:
        from prolibspector.analysis.element_database import get_database_path, load_element_database

        return load_element_database(get_database_path("Persistent Lines database"))
    except Exception:
        logger = __import__("logging").getLogger(__name__)
        logger.warning("Persistent-lines database unavailable; line labels skipped.", exc_info=True)
        return None


def _atmospheric_lines() -> tuple[tuple[str, float], ...]:
    try:
        from prolibspector.analysis.mapping_analysis import ATMOSPHERIC_LINES_NM

        flattened = []
        if isinstance(ATMOSPHERIC_LINES_NM, dict):
            for species, lines in ATMOSPHERIC_LINES_NM.items():
                for line in lines:
                    flattened.append((str(species), float(line)))
        else:
            for entry in ATMOSPHERIC_LINES_NM:
                if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                    flattened.append((str(entry[0]), float(entry[1])))
                else:
                    flattened.append(("air", float(entry)))
        return tuple(flattened)
    except Exception:
        return ()


def _match_candidates(
    wavelength_nm: float,
    reference: "pd.DataFrame | None",
    tolerance_nm: float = LINE_MATCH_TOLERANCE_NM,
) -> list[dict[str, Any]]:
    if reference is None or reference.empty:
        return []
    diffs = (reference["Wavelength"].astype(float) - float(wavelength_nm)).abs()
    hits = reference.loc[diffs[diffs <= tolerance_nm].index]
    candidates = []
    for index, row in hits.iterrows():
        try:
            roman = _ION_ROMAN.get(int(row["Ionization Level"]), "")
        except (TypeError, ValueError):
            roman = ""
        candidates.append(
            {
                "symbol": str(row["Symbol"]).strip().capitalize(),
                "roman": roman,
                "reference_nm": float(row["Wavelength"]),
                "diff": float(diffs.loc[index]),
            }
        )
    candidates.sort(key=lambda item: item["diff"])
    return candidates


def _candidate_label(candidate: dict[str, Any]) -> str:
    return f"{candidate['symbol']} {candidate['roman']} {candidate['reference_nm']:.2f}".replace("  ", " ").strip()


def identify_line(wavelength_nm: float, reference: "pd.DataFrame | None") -> str:
    """Best persistent-line match within the GUI's ±0.3 nm tolerance."""
    candidates = _match_candidates(wavelength_nm, reference)
    return _candidate_label(candidates[0]) if candidates else ""


def _standard_line_reference() -> "pd.DataFrame | None":
    try:
        from prolibspector.analysis.element_database import get_database_path, load_element_database

        return load_element_database(get_database_path())
    except Exception:
        return None


def build_line_inventory(
    dataset: DelaySweepDataset,
    *,
    excluded_bands_nm: tuple[tuple[float, float], ...] | None = DEFAULT_EXCLUDED_WAVELENGTH_BANDS_NM,
    max_lines: int = 40,
) -> list[dict[str, Any]]:
    """Union of resolved lines across every sweep cell, labeled and tracked.

    Answers "which real lines does each setting keep or lose": peaks from all
    cells are clustered by wavelength, labeled against the persistent-lines
    database, flagged when they coincide with known air-plasma lines, and
    annotated with how many cells retain them and which cell shows them best.
    Only peaks that persist shot-to-shot (present in at least
    ``PEAK_MIN_PRESENCE_FRACTION`` of a cell's individual shots) are counted
    — a spike that averaging smeared into the mean spectrum is not a line.
    """
    censored = excluded_wavelength_mask(dataset.wavelengths, excluded_bands_nm)
    keep = ~censored
    if not keep.any():
        return []
    wavelengths = np.asarray(dataset.wavelengths, dtype=float)[keep]

    observations: list[tuple[float, float, float, float, float | None, float]] = []
    cell_count = 0
    for delay_us, integration_us, _full, stack, _mean, baseline, net, noise in _iter_cell_statistics(dataset, keep):
        cell_count += 1
        for peak in _analyze_cell_peaks(stack, baseline, net, noise):
            if not peak["persistent"]:
                continue
            observations.append(
                (float(wavelengths[peak["index"]]), peak["height"], float(peak["height"] / noise),
                 float(delay_us), None if integration_us is None else float(integration_us),
                 peak["presence"])
            )
    if not observations:
        return []

    observations.sort(key=lambda item: item[0])
    clusters: list[list[tuple[float, float, float, float, float | None]]] = [[observations[0]]]
    for observation in observations[1:]:
        if observation[0] - clusters[-1][-1][0] <= LINE_MATCH_TOLERANCE_NM:
            clusters[-1].append(observation)
        else:
            clusters.append([observation])

    reference = _line_reference()
    standard_reference = _standard_line_reference()
    air_lines = _atmospheric_lines()

    # Pass 1: persistent-line candidates per cluster; clusters with a single
    # unambiguous match vote for their element, building a sample prior.
    cluster_info = []
    symbol_votes: dict[str, int] = {}
    for cluster in clusters:
        center = float(sum(item[0] * item[1] for item in cluster) / sum(item[1] for item in cluster))
        candidates = _match_candidates(center, reference)
        cluster_info.append((cluster, center, candidates))
        if len({candidate["symbol"] for candidate in candidates}) == 1:
            symbol_votes[candidates[0]["symbol"]] = symbol_votes.get(candidates[0]["symbol"], 0) + 1

    def _choose(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidates:
            return None
        # Prefer elements already confirmed elsewhere in this spectrum (e.g.
        # Al I 394.40 over an exotic coincidence), then the closest match.
        return max(candidates, key=lambda c: (symbol_votes.get(c["symbol"], 0), -c["diff"]))

    inventory = []
    for cluster, center, candidates in cluster_info:
        best = max(cluster, key=lambda item: item[1])
        chosen = _choose(candidates)
        label = _candidate_label(chosen) if chosen else ""
        if not label:
            fallback = _choose(_match_candidates(center, standard_reference))
            if fallback is not None:
                label = _candidate_label(fallback) + " ?"
        air = next(
            (species for species, line_nm in air_lines if abs(line_nm - center) <= LINE_MATCH_TOLERANCE_NM),
            "",
        )
        inventory.append(
            {
                "wavelength_nm": round(center, 2),
                "label": label,
                "air_species": air,
                "best_height": round(best[1], 1),
                "best_snr": round(best[2], 1),
                "best_delay_us": best[3],
                "best_integration_us": best[4],
                "best_presence": round(best[5], 2),
                "cells_seen": len({(item[3], item[4]) for item in cluster}),
                "cells_total": cell_count,
            }
        )
    inventory.sort(key=lambda item: item["best_height"], reverse=True)
    return inventory[:max_lines]


def rolling_percentile_baseline(values: np.ndarray, *, window: int = 0, percentile: float = 20.0) -> np.ndarray:
    """Continuum estimate: a rolling low-percentile of the spectrum.

    A low percentile over a window much wider than a line profile tracks the
    continuum underneath the emission lines without fitting any peak model.
    """
    values = np.asarray(values, dtype=float)
    n = values.size
    if n == 0:
        return values
    if window <= 0:
        window = max(31, (n // 50) | 1)
    if window % 2 == 0:
        window += 1
    half = window // 2
    padded = np.pad(values, half, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, window)
    return np.percentile(windows, percentile, axis=1)


def compute_delay_sweep_metrics(
    dataset: DelaySweepDataset,
    *,
    excluded_bands_nm: tuple[tuple[float, float], ...] | None = DEFAULT_EXCLUDED_WAVELENGTH_BANDS_NM,
) -> pd.DataFrame:
    """Per-cell key facts: line signal, continuum, noise, SNR (+SE), SBR.

    Pixels inside ``excluded_bands_nm`` (default: the Nd:YAG 1064 nm scatter
    band) are censored from every metric, including saturation — otherwise
    elastic laser scatter would be scored as the strongest "line" and its
    saturation would disqualify every cell. Saturation inside the censored
    band is still reported separately as ``excluded_saturation_fraction``.

    One row per (delay, integration) cell:

    - ``continuum_level``: median of the rolling-percentile baseline of the
      cell's mean spectrum (counts).
    - ``line_signal``: net area above the baseline (counts, only pixels more
      than 3x the noise floor above it).
    - ``noise_level``: per-shot detection noise floor from quiet ACTIVE
      pixels (see ``_cell_noise_floor``) — detector/readout noise, not
      plasma shot-to-shot variation (that lives in the SE and RSD columns).
    - ``snr``: strongest PERSISTENT line height / noise floor, with
      ``snr_se`` from the shot-to-shot spread of that line's height. It is
      NaN when no line clears the shot-presence rule. ``raw_peak_to_noise``
      keeps the unfiltered mean-peak diagnostic without calling it SNR.
    - ``peak_wavelength_nm``: where that strongest persistent line sits.
    - ``sbr``: strongest net line height / continuum at that pixel.
    - ``line_count`` / ``line_count_all``: resolved peaks that persist in at
      least half the individual shots vs. every peak of the mean spectrum;
      ``information_score`` sums log1p(SNR) over the PERSISTENT peaks only.
    - ``median_line_sbr``: median height/continuum over the persistent
      peaks (NaN when the zero-clamped baseline makes SBR undefined).
    - ``saturation_fraction``: mean fraction of non-censored pixels at 98%+
      of full scale.
    """
    rows: list[dict[str, Any]] = []
    saturation_level = SATURATION_PIXEL_THRESHOLD * float(dataset.max_intensity)
    censored = excluded_wavelength_mask(dataset.wavelengths, excluded_bands_nm)
    keep = ~censored
    if not keep.any():
        raise ValueError("Every pixel falls inside an excluded wavelength band.")
    kept_wavelengths = np.asarray(dataset.wavelengths, dtype=float)[keep]
    for delay_us, integration_us, full_stack, stack, mean_spectrum, baseline, net, noise in _iter_cell_statistics(
        dataset, keep
    ):
        shot_count = int(stack.shape[0])
        peaks = _analyze_cell_peaks(stack, baseline, net, noise)
        persistent = [peak for peak in peaks if peak["persistent"]]
        # Information content: every PERSISTENT resolved line counts, with
        # diminishing returns per line (log), so a cell keeping ten real
        # lines beats one that keeps a single huge line while starving the
        # rest — and a single-shot spike smeared into the mean counts for
        # nothing.
        information_score = float(sum(math.log1p(peak["height"] / noise) for peak in persistent))
        information_score_se = float("nan")
        if persistent and shot_count >= 2:
            peak_indices = np.array([peak["index"] for peak in persistent])
            shot_scores = np.sum(
                np.log1p(np.clip((stack[:, peak_indices] - baseline[peak_indices]) / noise, 0.0, None)),
                axis=1,
            )
            information_score_se = float(np.nanstd(shot_scores, ddof=1) / math.sqrt(shot_count))
        line_sbrs = [
            peak["height"] / baseline[peak["index"]]
            for peak in persistent
            if baseline[peak["index"]] > 1e-6
        ]
        median_line_sbr = float(np.median(line_sbrs)) if line_sbrs else float("nan")

        raw_peak_index = int(np.nanargmax(net))
        raw_peak_height = float(net[raw_peak_index])
        raw_peak_presence = next(
            (float(peak["presence"]) for peak in peaks if int(peak["index"]) == raw_peak_index),
            0.0,
        )
        strongest_persistent = max(persistent, key=lambda peak: peak["height"]) if persistent else None
        peak_index = int(strongest_persistent["index"]) if strongest_persistent is not None else None
        peak_height = float(strongest_persistent["height"]) if strongest_persistent is not None else float("nan")
        # Zero-clamped detectors (the YSM-8111 firmware dark-subtracts and
        # clamps at 0) can report a baseline of exactly 0 under the line;
        # SBR is undefined there, not astronomically good.
        peak_continuum = float(baseline[peak_index]) if peak_index is not None else float("nan")
        # Replicate spread of the strongest line's height, for SE-based
        # tie-breaking between near-equal cells.
        if peak_index is not None:
            shot_heights = stack[:, peak_index] - baseline[peak_index]
            peak_height_se = (
                float(np.nanstd(shot_heights, ddof=1) / math.sqrt(shot_count))
                if shot_count >= 2
                else float("nan")
            )
        else:
            peak_height_se = float("nan")
        zero_frame_count = int(np.sum(np.all(full_stack == 0.0, axis=1)))
        zero_frame_fraction = float(zero_frame_count / shot_count) if shot_count else float("nan")
        line_pixels = net > (3.0 * noise)
        rows.append(
            {
                "delay_us": float(delay_us),
                "integration_time_us": float("nan") if integration_us is None else float(integration_us),
                "shots": shot_count,
                "total_counts": float(np.nansum(mean_spectrum)),
                "continuum_level": float(np.nanmedian(baseline)),
                "line_signal": float(np.nansum(net[line_pixels])) if line_pixels.any() else 0.0,
                "noise_level": noise,
                "peak_height": peak_height,
                "peak_height_se": peak_height_se,
                "peak_wavelength_nm": (
                    float(kept_wavelengths[peak_index]) if peak_index is not None else float("nan")
                ),
                "snr": (peak_height / noise) if peak_index is not None else float("nan"),
                "snr_se": (peak_height_se / noise) if math.isfinite(peak_height_se) else float("nan"),
                "sbr": (
                    (peak_height / peak_continuum)
                    if peak_index is not None and peak_continuum > 1e-6
                    else float("nan")
                ),
                "raw_peak_height": raw_peak_height,
                "raw_peak_wavelength_nm": float(kept_wavelengths[raw_peak_index]),
                "raw_peak_to_noise": raw_peak_height / noise,
                "raw_peak_presence": raw_peak_presence,
                "line_count": len(persistent),
                "line_count_all": len(peaks),
                "median_line_sbr": median_line_sbr,
                "information_score": information_score,
                "information_score_se": information_score_se,
                "has_signal": bool(np.nanmax(stack) > 0),
                "has_persistent_signal": bool(persistent),
                "zero_frame_count": zero_frame_count,
                "zero_frame_fraction": zero_frame_fraction,
                "nonzero_frame_fraction": 1.0 - zero_frame_fraction,
                "saturation_fraction": float(np.nanmean(stack >= saturation_level)),
                "excluded_saturation_fraction": (
                    float(np.nanmean(full_stack[:, censored] >= saturation_level)) if censored.any() else 0.0
                ),
            }
        )
    return pd.DataFrame(rows).sort_values(["delay_us", "integration_time_us"]).reset_index(drop=True)


SPATIAL_SENSITIVITY_MODELS = (
    ("linear", "Common linear XY plane"),
    ("quadratic", "Common quadratic XY surface"),
    ("run_linear", "Run-specific linear XY planes"),
    ("run_quadratic", "Run-specific quadratic XY surfaces"),
)


def compute_shot_information_table(
    dataset: DelaySweepDataset,
    *,
    excluded_bands_nm: tuple[tuple[float, float], ...] | None = DEFAULT_EXCLUDED_WAVELENGTH_BANDS_NM,
) -> pd.DataFrame:
    """Per-shot version of the persistent-line information score.

    Peak identities, the baseline, and the detector-noise floor are defined
    once per timing cell, exactly as they are for the headline metrics. Each
    shot is then scored at those persistent peak pixels. Keeping shot grain
    is necessary for a run/XY sensitivity model.
    """
    censored = excluded_wavelength_mask(dataset.wavelengths, excluded_bands_nm)
    keep = ~censored
    if not keep.any():
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for delay_us, integration_us, _full_stack, stack, _mean, baseline, net, noise in _iter_cell_statistics(
        dataset, keep
    ):
        peaks = _analyze_cell_peaks(stack, baseline, net, noise)
        persistent_indices = np.array(
            [peak["index"] for peak in peaks if peak["persistent"]], dtype=int
        )
        if persistent_indices.size:
            shot_scores = np.sum(
                np.log1p(
                    np.clip(
                        (stack[:, persistent_indices] - baseline[persistent_indices]) / noise,
                        0.0,
                        None,
                    )
                ),
                axis=1,
            )
        else:
            shot_scores = np.zeros(stack.shape[0], dtype=float)

        for shot, score in zip(dataset.shots_for_cell(delay_us, integration_us), shot_scores):
            rows.append(
                {
                    "delay_us": float(delay_us),
                    "integration_time_us": (
                        float("nan") if integration_us is None else float(integration_us)
                    ),
                    "run_id": str(shot.run_id or ""),
                    "x_mm": float("nan") if shot.x_mm is None else float(shot.x_mm),
                    "y_mm": float("nan") if shot.y_mm is None else float(shot.y_mm),
                    "source": str(shot.source or ""),
                    "information_score": float(score),
                    "log_information_score": float(math.log1p(max(0.0, float(score)))),
                }
            )
    return pd.DataFrame(rows)


def _fit_spatial_information_model(
    shot_table: pd.DataFrame,
    condition_keys: list[tuple[float, float]],
    model_name: str,
    *,
    reference_key: tuple[float, float] | None = None,
) -> tuple[dict[str, Any], dict[tuple[float, float], float]]:
    """Fit one OLS sensitivity model with XY-cluster-robust contrasts."""
    runs = sorted(str(value) for value in shot_table["run_id"].unique())
    n = len(shot_table)
    x = shot_table["x_mm"].to_numpy(dtype=float)
    y = shot_table["y_mm"].to_numpy(dtype=float)
    x_std = float(np.nanstd(x))
    y_std = float(np.nanstd(y))
    if x_std <= 0 or y_std <= 0:
        raise ValueError("XY coordinates do not span both axes.")
    xs = (x - float(np.nanmean(x))) / x_std
    ys = (y - float(np.nanmean(y))) / y_std

    columns: list[np.ndarray] = [np.ones(n, dtype=float)]
    condition_column: dict[tuple[float, float], int | None] = {condition_keys[0]: None}
    observed_conditions = list(
        zip(
            shot_table["delay_us"].to_numpy(dtype=float),
            shot_table["integration_time_us"].to_numpy(dtype=float),
        )
    )
    for key in condition_keys[1:]:
        condition_column[key] = len(columns)
        columns.append(np.array([value == key for value in observed_conditions], dtype=float))
    run_values = shot_table["run_id"].astype(str).to_numpy()
    for run in runs[1:]:
        columns.append((run_values == run).astype(float))

    base = np.column_stack(columns)
    if model_name == "linear":
        columns.extend((xs, ys))
    elif model_name == "quadratic":
        columns.extend((xs, ys, xs * xs, ys * ys, xs * ys))
    elif model_name in {"run_linear", "run_quadratic"}:
        for run in runs:
            mask = (run_values == run).astype(float)
            columns.extend((mask * xs, mask * ys))
            if model_name == "run_quadratic":
                columns.extend((mask * xs * xs, mask * ys * ys, mask * xs * ys))
    else:
        raise ValueError(f"Unknown spatial sensitivity model: {model_name}")

    design = np.column_stack(columns)
    rank = int(np.linalg.matrix_rank(design))
    if rank < design.shape[1]:
        raise ValueError(
            f"Design is rank-deficient ({rank}/{design.shape[1]}); "
            "timing and position cannot be separated for this model."
        )
    response = shot_table["log_information_score"].to_numpy(dtype=float)
    xtx_inverse = np.linalg.pinv(design.T @ design)
    beta = xtx_inverse @ design.T @ response
    fitted = design @ beta
    residual = response - fitted
    # Runs 1/2 revisit exact coordinates, so those observations cannot be
    # treated as independent. Cluster the sandwich covariance by physical XY
    # position; single-visit positions naturally remain one-row clusters.
    position_keys = list(zip(np.round(x, 6), np.round(y, 6)))
    clusters: dict[tuple[float, float], list[int]] = {}
    for index, key in enumerate(position_keys):
        clusters.setdefault(key, []).append(index)
    meat = np.zeros((design.shape[1], design.shape[1]), dtype=float)
    for indices in clusters.values():
        cluster_score = design[indices].T @ residual[indices]
        meat += np.outer(cluster_score, cluster_score)
    cluster_count = len(clusters)
    correction = 1.0
    if cluster_count > 1 and n > design.shape[1]:
        correction = (cluster_count / (cluster_count - 1.0)) * (
            (n - 1.0) / (n - design.shape[1])
        )
    covariance = correction * xtx_inverse @ meat @ xtx_inverse

    condition_effects: dict[tuple[float, float], float] = {}
    condition_vectors: dict[tuple[float, float], np.ndarray] = {}
    for key in condition_keys:
        vector = np.zeros(design.shape[1], dtype=float)
        vector[0] = 1.0
        column = condition_column[key]
        if column is not None:
            vector[column] = 1.0
        condition_vectors[key] = vector
        condition_effects[key] = float(vector @ beta)

    ordered = sorted(condition_keys, key=condition_effects.get, reverse=True)
    winner = ordered[0]
    runner_up = ordered[1] if len(ordered) > 1 else ordered[0]
    contrast = condition_vectors[winner] - condition_vectors[runner_up]
    delta = float(condition_effects[winner] - condition_effects[runner_up])
    delta_se = float(math.sqrt(max(0.0, float(contrast @ covariance @ contrast))))

    base_beta = np.linalg.lstsq(base, response, rcond=None)[0]
    base_residual = response - base @ base_beta
    base_rss = float(np.sum(base_residual * base_residual))
    rss = float(np.sum(residual * residual))
    partial_r2 = (base_rss - rss) / base_rss if base_rss > 0 else 0.0
    total_ss = float(np.sum((response - float(np.mean(response))) ** 2))
    r_squared = 1.0 - (rss / total_ss) if total_ss > 0 else 0.0

    result = {
        "model": model_name,
        "winner_delay_us": float(winner[0]),
        "winner_integration_us": float(winner[1]),
        "runner_up_delay_us": float(runner_up[0]),
        "runner_up_integration_us": float(runner_up[1]),
        "winner_vs_runner_log_difference": delta,
        "winner_vs_runner_ci_low": float(delta - 1.96 * delta_se),
        "winner_vs_runner_ci_high": float(delta + 1.96 * delta_se),
        "spatial_partial_r_squared": float(max(0.0, partial_r2)),
        "r_squared": float(r_squared),
        "observations": int(n),
        "parameters": int(design.shape[1]),
        "xy_clusters": int(cluster_count),
    }
    if reference_key is not None and reference_key in condition_vectors:
        reference_contrast = condition_vectors[winner] - condition_vectors[reference_key]
        reference_delta = float(condition_effects[winner] - condition_effects[reference_key])
        reference_se = float(
            math.sqrt(max(0.0, float(reference_contrast @ covariance @ reference_contrast)))
        )
        result.update(
            {
                "winner_vs_raw_log_difference": reference_delta,
                "winner_vs_raw_ci_low": float(reference_delta - 1.96 * reference_se),
                "winner_vs_raw_ci_high": float(reference_delta + 1.96 * reference_se),
            }
        )
    return result, condition_effects


def compute_spatial_sensitivity(
    dataset: DelaySweepDataset,
    metrics: pd.DataFrame,
    *,
    excluded_bands_nm: tuple[tuple[float, float], ...] | None = DEFAULT_EXCLUDED_WAVELENGTH_BANDS_NM,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Check whether plausible smooth XY alignment fields change the winner.

    This is a sensitivity analysis, not a claim that the block-confounded
    acquisition has been fully normalized.
    """
    unavailable = {
        "available": False,
        "conclusion": "Spatial sensitivity check unavailable.",
        "limitations": [
            "Requires at least two position-tagged mapping runs spanning both X and Y."
        ],
    }
    if dataset.run_type != "mapping":
        return unavailable, pd.DataFrame()

    shot_table = compute_shot_information_table(dataset, excluded_bands_nm=excluded_bands_nm)
    if shot_table.empty:
        return unavailable, pd.DataFrame()
    usable = shot_table[
        np.isfinite(shot_table["x_mm"].to_numpy(dtype=float))
        & np.isfinite(shot_table["y_mm"].to_numpy(dtype=float))
        & shot_table["run_id"].astype(str).ne("")
    ].copy()
    if usable["run_id"].nunique() < 2 or len(usable) < 20:
        return unavailable, pd.DataFrame()

    usable["x_key"] = usable["x_mm"].round(6)
    usable["y_key"] = usable["y_mm"].round(6)
    position_groups = usable.groupby(["x_key", "y_key"], dropna=False)
    repeated_positions = 0
    all_run_positions = 0
    crossover_positions = 0
    run_count = int(usable["run_id"].nunique())
    for _, group in position_groups:
        group_runs = int(group["run_id"].nunique())
        group_conditions = int(
            group[["delay_us", "integration_time_us"]].drop_duplicates().shape[0]
        )
        if group_runs >= 2:
            repeated_positions += 1
        if group_runs == run_count:
            all_run_positions += 1
        if group_runs >= 2 and group_conditions >= 2:
            crossover_positions += 1

    metric_rows = metrics[np.isfinite(metrics["information_score"].to_numpy(dtype=float))].copy()
    condition_keys = sorted(
        {
            (float(row["delay_us"]), float(row["integration_time_us"]))
            for _, row in metric_rows.iterrows()
            if math.isfinite(float(row["integration_time_us"]))
        }
    )
    observed_keys = {
        (float(row["delay_us"]), float(row["integration_time_us"]))
        for _, row in usable.iterrows()
    }
    condition_keys = [key for key in condition_keys if key in observed_keys]
    if len(condition_keys) < 2:
        return unavailable, pd.DataFrame()

    raw_scores = {
        (float(row["delay_us"]), float(row["integration_time_us"])): float(row["information_score"])
        for _, row in metric_rows.iterrows()
        if math.isfinite(float(row["integration_time_us"]))
    }
    raw_order = sorted(condition_keys, key=lambda key: raw_scores.get(key, float("-inf")), reverse=True)
    raw_winner = raw_order[0]
    raw_best = max(raw_scores.get(key, 0.0) for key in condition_keys) or 1.0
    ranking_rows: dict[tuple[float, float], dict[str, Any]] = {
        key: {
            "delay_us": key[0],
            "integration_time_us": key[1],
            "raw_information_score": raw_scores.get(key, float("nan")),
            "raw_index": 100.0 * raw_scores.get(key, 0.0) / raw_best,
            "raw_rank": raw_order.index(key) + 1,
        }
        for key in condition_keys
    }

    models: list[dict[str, Any]] = []
    errors: list[str] = []
    significant_changes = 0
    adjusted_winner_count = 0
    for model_name, model_label in SPATIAL_SENSITIVITY_MODELS:
        try:
            result, effects = _fit_spatial_information_model(
                usable, condition_keys, model_name, reference_key=raw_winner
            )
        except ValueError as error:
            errors.append(f"{model_label}: {error}")
            continue
        result["label"] = model_label
        winner = (result["winner_delay_us"], result["winner_integration_us"])
        adjusted_order = sorted(condition_keys, key=effects.get, reverse=True)
        adjusted_best = max(effects.values())
        for key in condition_keys:
            ranking_rows[key][f"{model_name}_index"] = 100.0 * math.exp(
                float(effects[key] - adjusted_best)
            )
            ranking_rows[key][f"{model_name}_rank"] = adjusted_order.index(key) + 1
        result["raw_winner_rank"] = int(adjusted_order.index(raw_winner) + 1)
        if winner == raw_winner:
            adjusted_winner_count += 1
            result["significant_change_from_raw"] = False
        else:
            significant = bool(result.get("winner_vs_raw_ci_low", float("-inf")) > 0.0)
            result["significant_change_from_raw"] = significant
            significant_changes += int(significant)
        models.append(result)

    if not models:
        unavailable["limitations"].extend(errors)
        return unavailable, pd.DataFrame()

    material_change = significant_changes > 0
    if material_change:
        conclusion = "The position adjustment produces a statistically supported change in the headline winner."
    elif adjusted_winner_count == len(models):
        conclusion = "No material change: every spatial model retains the headline winner."
    else:
        conclusion = (
            "No statistically supported winner change: some spatial models swap the top two, "
            "but none separates its alternative from the headline winner at the 95% level."
        )

    summary = {
        "available": True,
        "conclusion": conclusion,
        "material_change": bool(material_change),
        "raw_winner_delay_us": float(raw_winner[0]),
        "raw_winner_integration_us": float(raw_winner[1]),
        "models_retaining_raw_winner": int(adjusted_winner_count),
        "models_run": int(len(models)),
        "position_tagged_shots": int(len(usable)),
        "runs": run_count,
        "repeated_xy_positions": int(repeated_positions),
        "all_run_xy_positions": int(all_run_positions),
        "crossover_xy_positions": int(crossover_positions),
        "models": models,
        "limitations": [
            "This is a sensitivity analysis, not an exact position normalization: timing settings occupy fixed spatial blocks.",
            "The models assume mirror/alignment effects vary smoothly over XY and act approximately multiplicatively on the information score.",
            "Run order, crater reuse, surface changes, and timing-by-position interactions cannot be fully separated in these recorded runs.",
            "XY-cluster-robust intervals allow repeated coordinates to be correlated, but remain descriptive post-selection checks and do not correct for testing many candidate settings.",
        ] + errors,
    }
    ranking_table = pd.DataFrame(ranking_rows.values()).sort_values("raw_rank").reset_index(drop=True)
    return summary, ranking_table


TARGET_LINE_MIN_PRESENCE = 0.9


def derive_target_lines(inventory: list[dict[str, Any]], *, max_lines: int = 6) -> list[tuple[float, str]]:
    """Confidently identified sample lines from the inventory, for per-line QC.

    Skips air-plasma lines (they track the atmosphere, not the sample),
    tentative standard-database matches (trailing "?"), and lines missing
    from more than 10% of shots even at their best cell (a sporadic line
    cannot anchor a repeatability ranking). The inventory is height-sorted,
    so the picks are the strongest identified sample lines — on an Al plate
    these are the Al/Mg lines an operator would watch.
    """
    picks: list[tuple[float, str]] = []
    for entry in inventory:
        label = str(entry.get("label", ""))
        if not label or label.endswith("?") or entry.get("air_species"):
            continue
        presence = entry.get("best_presence")
        if presence is not None and float(presence) < TARGET_LINE_MIN_PRESENCE:
            continue
        picks.append((float(entry["wavelength_nm"]), label))
        if len(picks) >= max_lines:
            break
    return picks


def compute_target_line_table(
    dataset: DelaySweepDataset,
    target_lines: list[tuple[float, str]],
    *,
    excluded_bands_nm: tuple[tuple[float, float], ...] | None = DEFAULT_EXCLUDED_WAVELENGTH_BANDS_NM,
) -> pd.DataFrame:
    """Per-cell behaviour of specific known lines: height, local SBR, RSD.

    One row per (cell, target line): the strongest net pixel within
    ±``LINE_MATCH_TOLERANCE_NM`` of the line, its SNR against the detection
    floor, its LOCAL signal-to-background ratio (NaN when the zero-clamped
    baseline is 0 there), the shot-to-shot relative standard deviation of
    its height (plasma repeatability, separated from detector noise), and
    the fraction of individual shots in which the line is present.
    """
    censored = excluded_wavelength_mask(dataset.wavelengths, excluded_bands_nm)
    keep = ~censored
    if not keep.any():
        raise ValueError("Every pixel falls inside an excluded wavelength band.")
    wavelengths = np.asarray(dataset.wavelengths, dtype=float)[keep]
    rows: list[dict[str, Any]] = []
    for delay_us, integration_us, _full, stack, _mean, baseline, net, noise in _iter_cell_statistics(dataset, keep):
        shot_count = int(stack.shape[0])
        for line_nm, label in target_lines:
            window = np.flatnonzero(np.abs(wavelengths - float(line_nm)) <= LINE_MATCH_TOLERANCE_NM)
            if window.size == 0:
                continue
            index = int(window[np.argmax(net[window])])
            height = float(net[index])
            shot_heights = stack[:, index] - baseline[index]
            mean_height = float(np.nanmean(shot_heights))
            spread = float(np.nanstd(shot_heights, ddof=1)) if shot_count >= 2 else float("nan")
            rsd = (spread / mean_height) if (math.isfinite(spread) and mean_height > 0) else float("nan")
            local_continuum = float(baseline[index])
            rows.append(
                {
                    "delay_us": float(delay_us),
                    "integration_time_us": float("nan") if integration_us is None else float(integration_us),
                    "line_nm": float(line_nm),
                    "label": label,
                    "height": height,
                    "snr": height / noise,
                    "local_sbr": (height / local_continuum) if local_continuum > 1e-6 else float("nan"),
                    "rsd": rsd,
                    "presence": float(np.mean(shot_heights >= PEAK_PRESENCE_MIN_SNR * noise)),
                    "shots": shot_count,
                }
            )
    return pd.DataFrame(rows)


def _eligible_metrics(
    metrics: pd.DataFrame, *, saturation_max_fraction: float
) -> tuple[pd.DataFrame, list[str]]:
    """Shared exclusion gates for every recommendation flavour."""
    if metrics.empty:
        raise ValueError("No timing-sweep metrics to recommend from.")
    reasons: list[str] = []
    if "has_signal" in metrics.columns:
        dead = int((~metrics["has_signal"].astype(bool)).sum())
        if dead:
            reasons.append(
                f"{dead} setting(s) returned all-zero saved frames only (excluded; these are "
                "valid-length dark-clamped frames, not SDK no-data errors)."
            )
            metrics = metrics[metrics["has_signal"].astype(bool)]
            if metrics.empty:
                raise ValueError("Every timing-sweep cell recorded no signal.")
    if "line_count" in metrics.columns:
        intermittent = int((metrics["line_count"] <= 0).sum())
        if intermittent:
            reasons.append(
                f"{intermittent} additional setting(s) had some nonzero frames but no line present "
                f"in at least {PEAK_MIN_PRESENCE_FRACTION:.0%} of shots (excluded from recommendation)."
            )
            metrics = metrics[metrics["line_count"] > 0]
            if metrics.empty:
                raise ValueError("No timing-sweep cell retained a persistent line.")
    eligible = metrics[metrics["saturation_fraction"] <= saturation_max_fraction]
    if eligible.empty:
        eligible = metrics
        reasons.append(
            "Every setting shows saturation above the limit; recommendation ignores the saturation gate."
        )
    else:
        excluded = len(metrics) - len(eligible)
        if excluded:
            reasons.append(f"{excluded} setting(s) excluded for detector saturation.")
    return eligible, reasons


def _pick_setting_text(delay_us: float, integration_us: float | None) -> str:
    text = f"{delay_us:g} us delay"
    if integration_us is not None and math.isfinite(float(integration_us)):
        text += f" + {float(integration_us) / 1000.0:g} ms integration"
    return text


# RSD estimates carry sampling error themselves (relative error of a std is
# roughly 1/sqrt(2(n-1)): ~12% at n=36). Cells within this factor of the
# best RSD are statistical ties; the shortest integration then wins.
RSD_TIE_FACTOR = 1.25


def recommend_repeatable(
    metrics: pd.DataFrame,
    target_table: pd.DataFrame,
    *,
    saturation_max_fraction: float = DEFAULT_SATURATION_MAX_FRACTION,
) -> dict[str, Any]:
    """Most repeatable pick: keeps every target line at the lowest shot RSD.

    Ranks on the target lines' shot-to-shot relative standard deviation —
    plasma repeatability an operator would see on a known line — instead of
    the detection floor the information score uses.
    """
    if target_table is None or target_table.empty:
        raise ValueError("No target-line table to rank repeatability from.")
    eligible, reasons = _eligible_metrics(metrics, saturation_max_fraction=saturation_max_fraction)
    allowed = {
        (float(row["delay_us"]), float(row["integration_time_us"]) if math.isfinite(float(row["integration_time_us"])) else -1.0)
        for _, row in eligible.iterrows()
    }
    work = target_table.copy()
    work["_integration_sort"] = work["integration_time_us"].fillna(-1.0)
    work = work[[
        (float(row["delay_us"]), float(row["_integration_sort"])) in allowed for _, row in work.iterrows()
    ]]
    if work.empty:
        raise ValueError("No eligible cells left in the target-line table.")
    present = work[work["presence"] >= PEAK_MIN_PRESENCE_FRACTION]
    per_cell = present.groupby(["delay_us", "_integration_sort"]).agg(
        lines_present=("line_nm", "nunique"),
        median_rsd=("rsd", "median"),
        median_snr=("snr", "median"),
    ).reset_index()
    per_cell = per_cell[np.isfinite(per_cell["median_rsd"])]
    if per_cell.empty:
        raise ValueError("No cell keeps any target line with a measurable RSD.")
    n_targets = int(target_table["line_nm"].nunique())
    max_present = int(per_cell["lines_present"].max())
    complete = per_cell[per_cell["lines_present"] == max_present]
    if max_present == n_targets:
        reasons.append(
            f"{len(complete)} setting(s) keep all {n_targets} target line(s) in at least "
            f"{PEAK_MIN_PRESENCE_FRACTION:.0%} of shots."
        )
    else:
        reasons.append(
            f"No setting keeps all {n_targets} target line(s); ranking the "
            f"{len(complete)} setting(s) that keep {max_present}."
        )
    best_rsd = float(complete["median_rsd"].min())
    candidates = complete[complete["median_rsd"] <= best_rsd * RSD_TIE_FACTOR]
    ordered = candidates.sort_values(["_integration_sort", "median_rsd", "delay_us"], ascending=[True, True, True])
    winner = ordered.iloc[0]
    integration_us = float(winner["_integration_sort"])
    integration_value = integration_us if integration_us >= 0 else None
    reasons.append(
        f"Lowest median per-line shot RSD {float(winner['median_rsd']):.1%} "
        f"(best {best_rsd:.1%}; ties within x{RSD_TIE_FACTOR:g} go to the shortest integration)."
    )
    return {
        "delay_us": float(winner["delay_us"]),
        "integration_us": integration_value,
        "median_rsd": float(winner["median_rsd"]),
        "median_snr": float(winner["median_snr"]),
        "lines_present": int(winner["lines_present"]),
        "stat": (
            f"median line RSD {float(winner['median_rsd']):.1%} across "
            f"{int(winner['lines_present'])} target line(s)"
        ),
        "reasons": reasons,
    }


def recommend_compromise(
    metrics: pd.DataFrame,
    *,
    saturation_max_fraction: float = DEFAULT_SATURATION_MAX_FRACTION,
) -> dict[str, Any]:
    """Plateau pick: the cell whose timing NEIGHBORHOOD keeps the score high.

    An operational preset should sit on a broad stable plateau, not on one
    unusually lucky cell: each cell's score is averaged with its measured
    neighbors along both timing axes (delay one schedule step either way at
    the same integration, and vice versa) before ranking.
    """
    eligible, reasons = _eligible_metrics(metrics, saturation_max_fraction=saturation_max_fraction)
    use_information = "information_score" in eligible.columns and float(eligible["information_score"].max()) > 0
    score_column = "information_score" if use_information else "snr"
    work = eligible.copy()
    work["_integration_sort"] = work["integration_time_us"].fillna(-1.0)
    delays = sorted(work["delay_us"].unique())
    integrations = sorted(work["_integration_sort"].unique())
    scores = {
        (float(row["delay_us"]), float(row["_integration_sort"])): float(row[score_column])
        for _, row in work.iterrows()
        if math.isfinite(float(row[score_column]))
    }
    plateau_rows = []
    for (delay, integration), score in scores.items():
        d_index = delays.index(delay)
        i_index = integrations.index(integration)
        neighbor_keys = [(delay, integration)]
        if d_index > 0:
            neighbor_keys.append((delays[d_index - 1], integration))
        if d_index + 1 < len(delays):
            neighbor_keys.append((delays[d_index + 1], integration))
        if i_index > 0:
            neighbor_keys.append((delay, integrations[i_index - 1]))
        if i_index + 1 < len(integrations):
            neighbor_keys.append((delay, integrations[i_index + 1]))
        neighborhood = [scores[key] for key in neighbor_keys if key in scores]
        plateau_rows.append(
            {
                "delay_us": delay,
                "_integration_sort": integration,
                "cell_score": score,
                "plateau_score": float(np.mean(neighborhood)),
                "neighbors": len(neighborhood),
            }
        )
    if not plateau_rows:
        raise ValueError("No finite scores to build a plateau ranking from.")
    plateau = pd.DataFrame(plateau_rows)
    best_plateau = float(plateau["plateau_score"].max())
    # Same tie philosophy as the other picks: cells within 5% of the best
    # neighborhood average are equivalent, and the shortest integration wins.
    candidates = plateau[plateau["plateau_score"] >= best_plateau - 0.05 * abs(best_plateau)]
    ordered = candidates.sort_values(
        ["_integration_sort", "plateau_score", "delay_us"], ascending=[True, False, True]
    )
    winner = ordered.iloc[0]
    integration_us = float(winner["_integration_sort"])
    integration_value = integration_us if integration_us >= 0 else None
    label = score_column.replace("_", " ")
    reasons.append(
        f"Highest neighborhood-averaged {label} {float(winner['plateau_score']):.1f} over "
        f"{int(winner['neighbors'])} adjacent setting(s) — a broad plateau beats a single lucky cell."
    )
    return {
        "delay_us": float(winner["delay_us"]),
        "integration_us": integration_value,
        "plateau_score": float(winner["plateau_score"]),
        "cell_score": float(winner["cell_score"]),
        "neighbors": int(winner["neighbors"]),
        "stat": (
            f"neighborhood mean {label} {float(winner['plateau_score']):.1f} over "
            f"{int(winner['neighbors'])} cells (own score {float(winner['cell_score']):.1f})"
        ),
        "reasons": reasons,
    }


def build_recommendation_suite(
    metrics: pd.DataFrame,
    *,
    target_table: pd.DataFrame | None = None,
    snr_tolerance: float = DEFAULT_SNR_TOLERANCE,
    saturation_max_fraction: float = DEFAULT_SATURATION_MAX_FRACTION,
) -> dict[str, Any]:
    """Headline recommendation plus the repeatable and plateau alternatives.

    One number cannot answer "best for what?": the suite reports the
    information-rich pick (full line inventory, headline and backward
    compatible), the most repeatable pick (target-line shot RSD), and the
    plateau compromise (neighborhood-averaged score). When they disagree the
    disagreement itself is the finding — confirm on a fresh surface before
    adopting a preset.
    """
    suite = recommend_trigger_delay(
        metrics, snr_tolerance=snr_tolerance, saturation_max_fraction=saturation_max_fraction
    )
    headline_row = metrics[
        (metrics["delay_us"] == suite["recommended_delay_us"])
        & (
            metrics["integration_time_us"].fillna(-1.0)
            == (suite["recommended_integration_us"] if suite["recommended_integration_us"] is not None else -1.0)
        )
    ]
    info_stat = ""
    if not headline_row.empty:
        row = headline_row.iloc[0]
        info_stat = (
            f"information score {float(row['information_score']):.1f} · "
            f"{int(row['line_count'])} persistent line(s) · strongest-line SNR {float(row['snr']):.0f}"
        )
    picks: dict[str, dict[str, Any]] = {
        "information_rich": {
            "title": "Most information-rich",
            "delay_us": suite["recommended_delay_us"],
            "integration_us": suite["recommended_integration_us"],
            "stat": info_stat,
            "reasons": [reason for reason in suite["reasons"] if reason.startswith("Recommended")],
        }
    }
    if target_table is not None and not target_table.empty:
        try:
            picks["most_repeatable"] = {
                "title": "Most repeatable (target lines)",
                **recommend_repeatable(metrics, target_table, saturation_max_fraction=saturation_max_fraction),
            }
        except ValueError:
            pass
    try:
        picks["best_compromise"] = {
            "title": "Best compromise (plateau)",
            **recommend_compromise(metrics, saturation_max_fraction=saturation_max_fraction),
        }
    except ValueError:
        pass
    suite["picks"] = picks
    settings = {
        (pick["delay_us"], pick["integration_us"]) for pick in picks.values()
    }
    if len(settings) > 1:
        suite["reasons"].append(
            "The picks disagree — no single winner is established; confirm the shortlisted "
            "settings on a fresh surface with spatially interleaved positions before adopting a preset."
        )
    elif len(picks) > 1:
        suite["reasons"].append(
            "All criteria (information, repeatability, plateau stability) pick the same setting."
        )
    return suite


def recommend_trigger_delay(
    metrics: pd.DataFrame,
    *,
    snr_tolerance: float = DEFAULT_SNR_TOLERANCE,
    saturation_max_fraction: float = DEFAULT_SATURATION_MAX_FRACTION,
) -> dict[str, Any]:
    """Pick the best (delay, integration) cell.

    Saturated and unmeasured cells are excluded first. Cells are ranked by
    ``information_score`` — the log-summed SNR of EVERY resolved line, not
    just the strongest one — so settings that keep the full line inventory
    (matrix lines, coatings, unidentified-but-real peaks) beat settings that
    maximize a single dominant line while starving the rest. Every cell
    within the tolerance band of the best score is a candidate; the shortest
    integration wins (throughput, least dark accumulation), then the higher
    score, then the shortest delay.
    """
    eligible, reasons = _eligible_metrics(metrics, saturation_max_fraction=saturation_max_fraction)

    if "information_score" in eligible.columns and eligible["information_score"].max() > 0:
        score_column = "information_score"
        best_row = eligible.loc[eligible[score_column].idxmax()]
        best_score = float(best_row[score_column])
        best_se = float(best_row.get("information_score_se", float("nan")))
        if not math.isfinite(best_se):
            best_se = 0.0
        # Statistically indistinguishable = within the best cell's replicate
        # SE (with a 5% floor so a zero-variance cell still has a band).
        band = max(best_se, 0.05 * best_score)
        candidates = eligible[eligible[score_column] >= best_score - band].copy()
        reasons.append(
            f"{len(candidates)} setting(s) within the tolerance band of the best "
            f"information score ({best_score:.1f} +/- {band:.1f}; every persistent "
            "resolved line counts, not only the strongest)."
        )
    else:
        score_column = "snr"
        best_row = eligible.loc[eligible["snr"].idxmax()]
        best_snr = float(best_row["snr"])
        best_se = float(best_row["snr_se"]) if math.isfinite(float(best_row["snr_se"])) else 0.0
        band = max(best_se, (1.0 - snr_tolerance) * best_snr)
        candidates = eligible[eligible["snr"] >= best_snr - band].copy()
        reasons.append(
            f"{len(candidates)} setting(s) within the tolerance band of the best SNR "
            f"({best_snr:.1f} +/- {band:.1f})."
        )
    candidates["_integration_sort"] = candidates["integration_time_us"].fillna(-1.0)
    ordered = candidates.sort_values(
        ["_integration_sort", score_column, "delay_us"],
        ascending=[True, False, True],
    )
    recommended = ordered.iloc[0]
    integration_us = float(recommended["integration_time_us"])
    has_integration = math.isfinite(integration_us)
    setting = f"{recommended['delay_us']:g} us delay"
    if has_integration:
        setting += f" + {integration_us / 1000.0:g} ms integration"
    line_count_note = (
        f"{int(recommended['line_count'])} resolved line(s), " if "line_count" in recommended else ""
    )
    reasons.append(
        f"Recommended {setting}: {line_count_note}strongest-line SNR {recommended['snr']:.1f}, "
        f"saturation {recommended['saturation_fraction']:.2%}."
    )
    return {
        "recommended_delay_us": float(recommended["delay_us"]),
        "recommended_integration_us": integration_us if has_integration else None,
        "best_snr_delay_us": float(best_row["delay_us"]),
        "best_snr_integration_us": (
            float(best_row["integration_time_us"])
            if math.isfinite(float(best_row["integration_time_us"]))
            else None
        ),
        "reasons": reasons,
        "candidates": [
            {
                "delay_us": float(row["delay_us"]),
                "integration_us": float(row["integration_time_us"]) if math.isfinite(float(row["integration_time_us"])) else None,
                "snr": float(row["snr"]),
                "sbr": float(row["sbr"]),
                "line_count": int(row["line_count"]) if "line_count" in row else None,
                "information_score": float(row["information_score"]) if "information_score" in row else None,
            }
            for _, row in ordered.iterrows()
        ],
    }


def plot_spatial_sensitivity(
    ranking_table: pd.DataFrame,
    summary: dict[str, Any],
    output_directory: str,
) -> str:
    """Grouped rank-index chart for raw and XY-adjusted sensitivity models."""
    import matplotlib.pyplot as plt

    model_columns = [
        ("raw_index", "Raw"),
        *[
            (f"{model['model']}_index", str(model["label"]))
            for model in summary.get("models", [])
            if f"{model['model']}_index" in ranking_table.columns
        ],
    ]
    rank_columns = ["raw_rank"] + [column.replace("_index", "_rank") for column, _ in model_columns[1:]]
    work = ranking_table.copy()
    work["best_rank"] = work[rank_columns].min(axis=1)
    work["mean_index"] = work[[column for column, _ in model_columns]].mean(axis=1)
    shown = work.sort_values(["best_rank", "mean_index"], ascending=[True, False]).head(8)
    shown = shown.sort_values("mean_index", ascending=False).reset_index(drop=True)
    labels = [
        _pick_setting_text(float(row["delay_us"]), float(row["integration_time_us"])).replace("us", "µs")
        for _, row in shown.iterrows()
    ]

    figure_height = max(5.2, 0.72 * len(shown) + 2.2)
    fig, ax = plt.subplots(figsize=(12.5, figure_height))
    colors = plt.get_cmap("viridis")(np.linspace(0.12, 0.9, len(model_columns)))
    group_y = np.arange(len(shown), dtype=float)
    bar_height = min(0.16, 0.78 / max(1, len(model_columns)))
    offsets = (np.arange(len(model_columns)) - (len(model_columns) - 1) / 2.0) * bar_height
    for offset, (column, label), color in zip(offsets, model_columns, colors):
        values = shown[column].to_numpy(dtype=float)
        ax.barh(
            group_y + offset,
            values,
            height=bar_height * 0.92,
            label=label,
            color=color,
            edgecolor="#ffffff",
            linewidth=0.6,
        )

    ax.set_yticks(group_y, labels)
    ax.invert_yaxis()
    ax.set_xlim(0, 105)
    ax.set_xlabel("Within-method information index (best setting = 100)")
    ax.set_title("Raw and XY-adjusted timing rankings")
    ax.grid(axis="x", alpha=0.2)
    ax.legend(loc="lower right", fontsize=8, frameon=True)
    fig.text(
        0.01,
        0.01,
        "Indices compare rank robustness only; absolute values are not interchangeable across models.",
        fontsize=8,
        color="#5f6f7d",
    )
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    path = os.path.join(output_directory, "delay_sweep_spatial_sensitivity.png")
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return path


def save_delay_sweep_report(
    dataset: DelaySweepDataset,
    metrics: pd.DataFrame,
    recommendation: dict[str, Any],
    output_directory: str,
    *,
    excluded_bands_nm: tuple[tuple[float, float], ...] | None = DEFAULT_EXCLUDED_WAVELENGTH_BANDS_NM,
    inventory: list[dict[str, Any]] | None = None,
    target_table: pd.DataFrame | None = None,
    spatial_sensitivity: dict[str, Any] | None = None,
    spatial_ranking_table: pd.DataFrame | None = None,
) -> dict[str, str]:
    """Write the metrics CSV, recommendation JSON, and comparison plots."""
    os.makedirs(output_directory, exist_ok=True)
    paths: dict[str, str] = {}

    metrics_path = os.path.join(output_directory, "delay_sweep_metrics.csv")
    metrics.to_csv(metrics_path, index=False)
    paths["metrics_csv"] = metrics_path

    if target_table is not None and not target_table.empty:
        target_path = os.path.join(output_directory, "delay_sweep_target_lines.csv")
        target_table.to_csv(target_path, index=False)
        paths["target_lines_csv"] = target_path

    if spatial_ranking_table is not None and not spatial_ranking_table.empty:
        spatial_path = os.path.join(output_directory, "delay_sweep_spatial_sensitivity.csv")
        spatial_ranking_table.to_csv(spatial_path, index=False)
        paths["spatial_sensitivity_csv"] = spatial_path
        if spatial_sensitivity and spatial_sensitivity.get("available"):
            paths["spatial_sensitivity_png"] = plot_spatial_sensitivity(
                spatial_ranking_table, spatial_sensitivity, output_directory
            )

    recommendation_path = os.path.join(output_directory, "delay_sweep_recommendation.json")
    with open(recommendation_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "run_type": dataset.run_type,
                "run_directory": dataset.run_directory,
                "column_delays_us": list(dataset.column_delays_us),
                "integration_times_ms": list(dataset.integration_times_ms),
                **recommendation,
            },
            handle,
            indent=2,
            sort_keys=True,
        )
    paths["recommendation_json"] = recommendation_path

    paths.update(
        plot_delay_sweep(dataset, metrics, recommendation, output_directory, excluded_bands_nm=excluded_bands_nm)
    )
    try:
        paths["line_detail_png"] = plot_line_detail(
            dataset,
            metrics,
            recommendation,
            output_directory,
            excluded_bands_nm=excluded_bands_nm,
            inventory=inventory,
        )
    except Exception:
        pass
    paths["report_html"] = render_delay_sweep_report_html(
        dataset, metrics, recommendation, paths, output_directory,
        excluded_bands_nm=excluded_bands_nm, inventory=inventory, target_table=target_table,
        spatial_sensitivity=spatial_sensitivity,
    )
    return paths


def _embedded_image(path: str | None) -> str:
    if not path or not os.path.isfile(path):
        return ""
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return f'<img src="data:image/png;base64,{encoded}" alt="{html.escape(os.path.basename(path))}">'


VIEWER_MAX_POINTS = 1200


def _viewer_section_html(
    dataset: DelaySweepDataset,
    metrics: pd.DataFrame,
    recommendation: dict[str, Any],
    excluded_bands_nm: tuple[tuple[float, float], ...] | None,
    line_labels: list[dict[str, Any]] | None = None,
) -> str:
    """Interactive explorer: an information-colored cell grid; clicking a
    cell draws its mean spectrum on a canvas. Fully self-contained."""
    wavelengths = np.asarray(dataset.wavelengths, dtype=float)
    n = wavelengths.size
    if n == 0 or not dataset.shots:
        return ""
    step = max(1, int(math.ceil(n / VIEWER_MAX_POINTS)))
    pad = (-n) % step

    def _pool_max(values: np.ndarray) -> np.ndarray:
        padded = np.pad(np.asarray(values, dtype=float), (0, pad), constant_values=np.nan)
        return np.nanmax(padded.reshape(-1, step), axis=1)

    padded_wl = np.pad(wavelengths, (0, pad), constant_values=np.nan)
    wl_ds = np.nanmean(padded_wl.reshape(-1, step), axis=1)

    metrics_by_cell: dict[tuple[float, float], dict[str, float | int]] = {}
    for _, row in metrics.iterrows():
        integration = float(row["integration_time_us"])
        key = (float(row["delay_us"]), integration if math.isfinite(integration) else -1.0)
        information_score = float(row.get("information_score", float("nan")))
        metrics_by_cell[key] = {
            "snr": float(row["snr"]) if math.isfinite(float(row["snr"])) else 0.0,
            "line_count": int(row.get("line_count", 0)),
            "information_score": information_score if math.isfinite(information_score) else 0.0,
        }

    cells = []
    for delay_us, integration_us in dataset.cells():
        stack = np.vstack([shot.intensities for shot in dataset.shots_for_cell(delay_us, integration_us)])
        mean = np.nanmean(stack, axis=0)
        key = (float(delay_us), -1.0 if integration_us is None else float(integration_us))
        cell_metrics = metrics_by_cell.get(key, {})
        cells.append(
            {
                "delay_us": float(delay_us),
                "integration_us": None if integration_us is None else float(integration_us),
                "snr": round(float(cell_metrics.get("snr", 0.0)), 1),
                "line_count": int(cell_metrics.get("line_count", 0)),
                "information_score": round(float(cell_metrics.get("information_score", 0.0)), 1),
                "values": [round(float(v), 1) for v in _pool_max(mean)],
            }
        )
    if not cells:
        return ""

    payload = {
        "wavelengths": [round(float(w), 2) for w in wl_ds],
        "cells": cells,
        "excluded_bands_nm": [list(band) for band in (excluded_bands_nm or ())],
        "recommended": {
            "delay_us": recommendation.get("recommended_delay_us"),
            "integration_us": recommendation.get("recommended_integration_us"),
        },
        "lines": [
            {
                "nm": entry["wavelength_nm"],
                "label": entry["label"] or f"{entry['wavelength_nm']:g} nm ?",
                "is_aluminum": str(entry.get("label", "")).strip().startswith("Al "),
            }
            for entry in (line_labels or [])[:12]
        ],
    }

    delays = sorted({cell["delay_us"] for cell in cells})
    integrations = sorted(
        {(-1.0 if cell["integration_us"] is None else cell["integration_us"]) for cell in cells}
    )
    best_information = max((cell["information_score"] for cell in cells), default=1.0) or 1.0
    by_key = {
        (cell["delay_us"], -1.0 if cell["integration_us"] is None else cell["integration_us"]): (index, cell)
        for index, cell in enumerate(cells)
    }
    recommended_delay = recommendation.get("recommended_delay_us")
    recommended_integration = recommendation.get("recommended_integration_us")

    header = "<tr><th></th>" + "".join(f"<th>{delay:g} µs</th>" for delay in delays) + "</tr>"
    table_rows = []
    for integration in reversed(integrations):
        label = "n/a" if integration < 0 else _integration_label(integration)
        tds = [f"<th>{html.escape(label)}</th>"]
        for delay in delays:
            entry = by_key.get((delay, integration))
            if entry is None:
                tds.append("<td class='dead'>–</td>")
                continue
            index, cell = entry
            information_score = cell["information_score"]
            has_persistent_lines = cell["line_count"] > 0 and information_score > 0
            tone = max(0.0, min(1.0, information_score / best_information))
            recommended = (
                recommended_delay is not None
                and float(delay) == float(recommended_delay)
                and (
                    (recommended_integration is None and integration < 0)
                    or (recommended_integration is not None and integration >= 0 and float(integration) == float(recommended_integration))
                )
            )
            classes = "cell" + (" recommended-cell" if recommended else "")
            if has_persistent_lines:
                background, text_color = _viridis_cell_color(tone)
                display_value = f"{information_score:g}"
                title = f"Information score {information_score:g}; {cell['line_count']} persistent lines"
            else:
                background, text_color = "#e5e7eb", "#64748b"
                display_value = "0"
                title = "No persistent lines; information score 0"
            tds.append(
                f"<td class='{classes}' data-cell='{index}' "
                f"title='{html.escape(title)}' "
                f"style='background: {background}; color: {text_color};'>"
                f"{display_value}</td>"
            )
        table_rows.append("<tr>" + "".join(tds) + "</tr>")

    return f"""
<section id="explore">
<h2>Explore the grid — click a cell to see its mean spectrum</h2>
<p class="meta">Cell numbers and colors are persistent-line information score (viridis: dark = low, bright = high). Gray cells have no persistent lines and score zero. Rows: integration time. Columns: trigger delay.</p>
<table class="cellgrid"><thead>{header}</thead><tbody>{''.join(table_rows)}</tbody></table>
<div id="viewer-label" class="meta" style="margin-top: 0.6rem;"></div>
<canvas id="viewer" width="1000" height="460" style="width: 100%; border: 1px solid #d7dee8; border-radius: 4px;"></canvas>
<script>
const SWEEP = {json.dumps(payload, separators=(",", ":"))};
const canvas = document.getElementById("viewer");
const ctx = canvas.getContext("2d");
const PAD = {{left: 62, right: 14, top: 92, bottom: 34}};
function integrationLabel(us) {{
  if (us === null) return "n/a";
  return us >= 1000 ? (us / 1000) + " ms" : us + " µs";
}}
function nearestIndex(values, target) {{
  let best = 0, bestDistance = Infinity;
  for (let i = 0; i < values.length; i++) {{
    const distance = Math.abs(values[i] - target);
    if (distance < bestDistance) {{ best = i; bestDistance = distance; }}
  }}
  return best;
}}
function drawLineLabels(wl, values, x, y, ymax, plotWidth) {{
  const chartLeft = PAD.left, chartRight = PAD.left + plotWidth;
  const laneCount = 5, laneHeight = 15, labelGap = 6;
  const laneRight = Array(laneCount).fill(chartLeft - labelGap);
  ctx.textAlign = "left";
  ctx.textBaseline = "top";

  // The inventory is built across all cells. Only annotate lines that are
  // visibly present in the selected cell so absent late-plasma lines do not
  // consume label lanes.
  const items = SWEEP.lines.map(line => {{
    const index = nearestIndex(wl, line.nm);
    return {{...line, index, px: x(line.nm), peakY: y(values[index]), value: values[index]}};
  }}).filter(item =>
    item.px >= chartLeft && item.px <= chartRight && item.value >= Math.max(1, ymax * 0.01)
  ).sort((a, b) => a.px - b.px);

  for (const item of items) {{
    ctx.font = item.is_aluminum ? "bold 11px system-ui" : "11px system-ui";
    const width = ctx.measureText(item.label).width + 8;
    const desiredLeft = Math.max(chartLeft, Math.min(chartRight - width, item.px - width / 2));
    let lane = -1, left = desiredLeft;
    for (let candidate = 0; candidate < laneCount; candidate++) {{
      if (desiredLeft >= laneRight[candidate] + labelGap) {{ lane = candidate; break; }}
    }}
    // If every lane is occupied, omit the least-spaced label instead of
    // forcing an overlap. The full line remains available in the inventory.
    if (lane < 0) continue;
    laneRight[lane] = left + width;
    const top = 7 + lane * laneHeight;
    const labelCenter = left + width / 2;

    // Solid leader: actual peak -> annotation band -> collision-free label.
    ctx.setLineDash([]);
    ctx.strokeStyle = item.is_aluminum ? "#c76b00" : "rgba(30,42,51,0.58)";
    ctx.lineWidth = item.is_aluminum ? 1.7 : 0.9;
    ctx.beginPath();
    ctx.moveTo(item.px, item.peakY);
    ctx.lineTo(item.px, PAD.top - 6);
    ctx.lineTo(labelCenter, top + 12);
    ctx.stroke();
    ctx.fillStyle = item.is_aluminum ? "#c76b00" : "#1e2a33";
    ctx.beginPath(); ctx.arc(item.px, item.peakY, 1.8, 0, Math.PI * 2); ctx.fill();

    ctx.fillStyle = item.is_aluminum ? "rgba(255,239,194,0.96)" : "rgba(255,255,255,0.92)";
    ctx.fillRect(left - 2, top - 1, width + 4, 13);
    ctx.fillStyle = item.is_aluminum ? "#8a4700" : "#1e2a33";
    ctx.fillText(item.label, left + 2, top);
  }}
  ctx.textBaseline = "alphabetic";
}}
function drawCell(index) {{
  const cell = SWEEP.cells[index];
  const wl = SWEEP.wavelengths, values = cell.values;
  const w = canvas.width - PAD.left - PAD.right, h = canvas.height - PAD.top - PAD.bottom;
  const xmin = wl[0], xmax = wl[wl.length - 1];
  let ymax = Math.max(1, ...values) * 1.06;
  const x = v => PAD.left + (v - xmin) / (xmax - xmin) * w;
  const y = v => PAD.top + h - Math.max(0, v) / ymax * h;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
  for (const band of SWEEP.excluded_bands_nm) {{
    ctx.fillStyle = "rgba(180,35,24,0.08)";
    ctx.fillRect(x(band[0]), PAD.top, x(band[1]) - x(band[0]), h);
  }}
  ctx.strokeStyle = "#e3e8ee"; ctx.fillStyle = "#5b6b7c"; ctx.font = "12px system-ui";
  ctx.textAlign = "center";
  for (let nm = Math.ceil(xmin / 100) * 100; nm <= xmax; nm += 100) {{
    ctx.beginPath(); ctx.moveTo(x(nm), PAD.top); ctx.lineTo(x(nm), PAD.top + h); ctx.stroke();
    ctx.fillText(nm + " nm", x(nm), canvas.height - 12);
  }}
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {{
    const v = ymax * i / 4;
    ctx.beginPath(); ctx.moveTo(PAD.left, y(v)); ctx.lineTo(PAD.left + w, y(v)); ctx.stroke();
    ctx.fillText(Math.round(v).toLocaleString(), PAD.left - 6, y(v) + 4);
  }}
  ctx.strokeStyle = "#2f6db3"; ctx.lineWidth = 1.2; ctx.beginPath();
  for (let i = 0; i < wl.length; i++) {{
    const px = x(wl[i]), py = y(values[i]);
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }}
  ctx.stroke();
  drawLineLabels(wl, values, x, y, ymax, w);
  document.getElementById("viewer-label").textContent =
    "Delay " + cell.delay_us + " µs · integration " + integrationLabel(cell.integration_us) +
    " · SNR " + cell.snr + " · " + cell.line_count + " resolved lines" +
    " · information score " + cell.information_score +
    " · mean of the cell's shots (max-pooled for display)";
  document.querySelectorAll(".cellgrid td.selected").forEach(td => td.classList.remove("selected"));
  const td = document.querySelector(".cellgrid td[data-cell='" + index + "']");
  if (td) td.classList.add("selected");
}}
document.querySelectorAll(".cellgrid td[data-cell]").forEach(td => {{
  td.addEventListener("click", () => drawCell(parseInt(td.dataset.cell, 10)));
}});
(function () {{
  const rec = SWEEP.recommended;
  let start = 0;
  SWEEP.cells.forEach((cell, index) => {{
    if (rec.delay_us !== null && cell.delay_us === rec.delay_us &&
        String(cell.integration_us) === String(rec.integration_us)) start = index;
  }});
  drawCell(start);
}})();
</script>
"""


def render_delay_sweep_report_html(
    dataset: DelaySweepDataset,
    metrics: pd.DataFrame,
    recommendation: dict[str, Any],
    image_paths: dict[str, str],
    output_directory: str,
    *,
    excluded_bands_nm: tuple[tuple[float, float], ...] | None = DEFAULT_EXCLUDED_WAVELENGTH_BANDS_NM,
    inventory: list[dict[str, Any]] | None = None,
    target_table: pd.DataFrame | None = None,
    spatial_sensitivity: dict[str, Any] | None = None,
) -> str:
    """Write a single self-contained HTML report: stats, plots, raw spectra.

    Everything (including the PNGs) is embedded, so the one file can be
    mailed or archived with the run and opened in any browser.
    """
    recommended_delay = float(recommendation.get("recommended_delay_us", float("nan")))
    recommended_integration = recommendation.get("recommended_integration_us")
    joint = len(dataset.integration_values_us()) > 1
    setting = f"{recommended_delay:g} µs trigger delay"
    if recommended_integration is not None:
        setting += f" + {recommended_integration / 1000.0:g} ms integration"

    def _fmt(value, pattern="{:.1f}"):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return ""
        if not math.isfinite(value):
            return "–"
        return pattern.format(value)

    header_cells = ["Delay (µs)"]
    if joint:
        header_cells.append("Integration (ms)")
    header_cells += [
        "Shots", "Nonzero frames", "Continuum (counts)", "Noise (counts)", "Persistent peak (counts)",
        "Peak λ (nm)", "Lines (persist/all)", "Info score", "Persistent-line SNR ± SE", "SBR", "Line SBR (med)", "Saturation",
    ]
    body_rows = []
    for _, row in metrics.iterrows():
        integration_value = float(row["integration_time_us"])
        is_recommended = float(row["delay_us"]) == recommended_delay and (
            (recommended_integration is None and not math.isfinite(integration_value))
            or (
                recommended_integration is not None
                and math.isfinite(integration_value)
                and float(integration_value) == float(recommended_integration)
            )
        )
        cells = [f"{row['delay_us']:g}"]
        if joint:
            cells.append(_fmt(integration_value / 1000.0, "{:g}") if math.isfinite(integration_value) else "–")
        snr_se = _fmt(row["snr_se"])
        cells += [
            f"{int(row['shots'])}",
            _fmt(100.0 * float(row.get("nonzero_frame_fraction", 1.0)), "{:.0f}") + " %",
            _fmt(row["continuum_level"]),
            _fmt(row["noise_level"]),
            _fmt(row["peak_height"]),
            _fmt(row.get("peak_wavelength_nm")) if "peak_wavelength_nm" in row else "–",
            (
                f"{int(row['line_count'])}/{int(row.get('line_count_all', row['line_count']))}"
                if "line_count" in row
                else "–"
            ),
            _fmt(row.get("information_score")) if "information_score" in row else "–",
            f"{_fmt(row['snr'])}" + (f" ± {snr_se}" if snr_se and snr_se != "–" else ""),
            _fmt(row["sbr"], "{:.2f}"),
            _fmt(row.get("median_line_sbr"), "{:.2f}") if "median_line_sbr" in row else "–",
            _fmt(100.0 * float(row["saturation_fraction"]), "{:.2f}") + " %",
        ]
        css = ' class="recommended"' if is_recommended else ""
        body_rows.append(f"<tr{css}>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")

    reasons_html = "".join(f"<li>{html.escape(str(reason))}</li>" for reason in recommendation.get("reasons", []))
    if inventory is None:
        try:
            inventory = build_line_inventory(dataset, excluded_bands_nm=excluded_bands_nm)
        except Exception:
            inventory = []
    try:
        viewer_html = _viewer_section_html(dataset, metrics, recommendation, excluded_bands_nm, inventory)
    except Exception:
        viewer_html = ""

    picks = recommendation.get("picks") or {}
    picks_html = ""
    if picks:
        cards = []
        for key in ("most_repeatable", "information_rich", "best_compromise"):
            pick = picks.get(key)
            if not pick:
                continue
            headline = " headline" if key == "information_rich" else ""
            pick_setting = _pick_setting_text(pick["delay_us"], pick.get("integration_us")).replace("us", "µs")
            stat = html.escape(str(pick.get("stat", "")))
            cards.append(
                f"<div class='pick{headline}'><h3>{html.escape(pick['title'])}</h3>"
                f"<strong>{html.escape(pick_setting)}</strong>"
                f"<p class='meta'>{stat}</p></div>"
            )
        picks_html = f"<div class='picks'>{''.join(cards)}</div>"
    pick_settings = {
        (pick.get("delay_us"), pick.get("integration_us"))
        for pick in picks.values()
        if pick
    }
    summary_label = (
        "Provisional information-rich candidate"
        if len(pick_settings) > 1
        else "Recommended setting"
    )

    target_html = ""
    if target_table is not None and not target_table.empty:
        pick_cells = {
            (pick["delay_us"], pick.get("integration_us") if pick.get("integration_us") is not None else None)
            for pick in picks.values()
        } or {(recommended_delay, recommended_integration)}
        target_rows = []
        shown = target_table.copy()
        shown["_integration"] = shown["integration_time_us"].apply(
            lambda value: None if not math.isfinite(float(value)) else float(value)
        )
        shown = shown[[
            (float(row["delay_us"]), row["_integration"]) in pick_cells for _, row in shown.iterrows()
        ]].sort_values(["line_nm", "delay_us", "integration_time_us"])
        for _, row in shown.iterrows():
            cell_text = _pick_setting_text(float(row["delay_us"]), row["_integration"]).replace("us", "µs")
            rsd = float(row["rsd"])
            presence = float(row["presence"])
            target_rows.append(
                "<tr>"
                f"<td style='text-align: left;'>{html.escape(str(row['label']))}</td>"
                f"<td>{float(row['line_nm']):.2f}</td>"
                f"<td style='text-align: left;'>{html.escape(cell_text)}</td>"
                f"<td>{float(row['height']):,.0f}</td>"
                f"<td>{float(row['snr']):,.0f}</td>"
                f"<td>{_fmt(row['local_sbr'], '{:.2f}')}</td>"
                f"<td>{f'{rsd:.1%}' if math.isfinite(rsd) else '–'}</td>"
                f"<td>{f'{presence:.0%}' if math.isfinite(presence) else '–'}</td>"
                "</tr>"
            )
        target_lines_count = int(target_table["line_nm"].nunique())
        target_html = f"""
<section id="targets">
<h2>Target lines at the shortlisted settings</h2>
<p class="meta">The {target_lines_count} strongest confidently identified sample lines, evaluated
individually at each shortlisted setting: net height, SNR against the detection floor, LOCAL
signal-to-background (– when the zero-clamped baseline is 0 under the line), shot-to-shot RSD of
the line height (plasma repeatability — this is what "most repeatable" ranks), and the fraction of
individual shots in which the line is present. Full per-cell table:
<code>delay_sweep_target_lines.csv</code>.</p>
<div class="table-scroll">
<table>
  <thead><tr><th style='text-align: left;'>Line</th><th>λ (nm)</th><th style='text-align: left;'>Setting</th>
  <th>Height</th><th>SNR</th><th>Local SBR</th><th>Shot RSD</th><th>Presence</th></tr></thead>
  <tbody>{''.join(target_rows)}</tbody>
</table>
</div>
</section>
"""

    inventory_html = ""
    if inventory:
        inventory_rows = []
        for entry in inventory:
            integration_text = (
                _integration_label(entry["best_integration_us"])
                if entry["best_integration_us"] is not None
                else "n/a"
            )
            label = entry["label"] or "unidentified"
            if entry["air_species"]:
                label += " · air line"
            presence = entry.get("best_presence")
            presence_text = (
                f"{float(presence):.0%}" if isinstance(presence, (int, float)) and math.isfinite(float(presence)) else "–"
            )
            inventory_rows.append(
                "<tr>"
                f"<td>{entry['wavelength_nm']:.2f}</td>"
                f"<td style='text-align: left;'>{html.escape(label)}</td>"
                f"<td>{entry['best_height']:,.0f}</td>"
                f"<td>{entry['best_snr']:g}</td>"
                f"<td>{presence_text}</td>"
                f"<td>{entry['cells_seen']}/{entry['cells_total']}</td>"
                f"<td>{entry['best_delay_us']:g} µs + {html.escape(integration_text)}</td>"
                "</tr>"
            )
        inventory_html = f"""
<section id="lines">
<h2>Line inventory — every resolved line, across all settings</h2>
<p class="meta">Peaks ≥ 5× the noise floor, clustered across cells and matched against the
persistent-lines database (±{LINE_MATCH_TOLERANCE_NM:g} nm, same tolerance as the labeling tool);
elements already confirmed elsewhere in the spectrum are preferred when several lines match.
A trailing "?" marks a tentative match from the full standard database (no persistent line nearby)
— verify those in the analysis app before trusting them. "Air line" marks known air-plasma
wavelengths (H/N/O), which come from the atmosphere rather than the sample. Only lines present in
at least half of a cell's individual shots count ("Presence" is the shot fraction at the best
cell), so a single-shot spike smeared into the mean never enters the inventory. "Seen in" shows
how many sweep cells retain the line — settings that lose real lines lose information.</p>
<div class="table-scroll">
<table>
  <thead><tr><th>λ (nm)</th><th style='text-align: left;'>Identification</th><th>Best height</th>
  <th>Best SNR</th><th>Presence</th><th>Seen in</th><th>Best cell</th></tr></thead>
  <tbody>{''.join(inventory_rows)}</tbody>
</table>
</div>
</section>
"""
    capture_interpretation_html = ""
    if "zero_frame_fraction" in metrics.columns:
        late_example = metrics.sort_values(["delay_us", "integration_time_us"]).iloc[-1]
        example_shots = int(late_example["shots"])
        example_zero = int(late_example.get("zero_frame_count", 0))
        example_nonzero = example_shots - example_zero
        example_presence = float(late_example.get("raw_peak_presence", float("nan")))
        capture_interpretation_html = f"""
<section id="capture-interpretation">
<h2>Late-delay frames are intermittent, not uniformly line-free</h2>
<p>At {float(late_example['delay_us']):g} µs delay +
{float(late_example['integration_time_us']) / 1000.0:g} ms integration,
{example_zero}/{example_shots} saved arrays are exactly zero and {example_nonzero}/{example_shots}
contain at least one nonzero value. The strongest mean-spectrum peak appears in only
{example_presence:.0%} of shots, below the {PEAK_MIN_PRESENCE_FRACTION:.0%} persistence rule;
therefore the cell has no persistent line even though its averaged spectrum can look structured.</p>
<p><strong>The report retains the zero arrays.</strong> They have the expected pixel count and finite values and
were returned and indexed successfully; they are not the SDK <em>no-data</em> error that aborts a capture.
The YiXist firmware is known to dark-subtract and clamp dark frames to zero. Automatically deleting these
frames would inflate line presence, SNR, and repeatability.</p>
<p>The rare bright late frames are consistent with an additional optical pulse arriving after the trigger,
but the saved spectra alone cannot distinguish a laser pulse train from trigger/readout instability.
Confirm the pulse count and spacing on the PDA36A/oscilloscope before treating late-delay cells as plasma
decay measurements. Multi-pulse passively Q-switched Nd:YAG systems can produce several pulses over hundreds
of microseconds (<a href="https://doi.org/10.1016/j.optlastec.2011.06.005">example study</a>).</p>
</section>
"""

    sections = [
        (
            "capture-fraction",
            "Saved-frame capture fraction",
            image_paths.get("capture_fraction_png"),
            "Each cell shows the fraction of valid-length saved arrays containing any nonzero value. "
            "This is a completeness/intermittency diagnostic, not a line-quality score.",
        ),
        (
            "heatmap",
            "Persistent-line information over the sweep grid" if joint else None,
            image_paths.get("heatmap_png"),
            "Brightness reflects only peaks present in at least half of replicate shots. Gray cells may contain "
            "rare bright frames, but they do not retain a repeatable line.",
        ),
        (
            "keyfacts-figs",
            "Key facts vs delay",
            image_paths.get("metrics_png"),
            "SNR and SBR now refer to the strongest persistent line. Intermittent raw peaks remain in the CSV "
            "as explicitly named diagnostics and cannot win the recommendation.",
        ),
        (
            "spectra",
            "Mean spectrum by trigger delay",
            image_paths.get("spectra_png"),
            "These are absolute cell means. A recognizable late-delay trace can be produced by a few bright "
            "shots averaged together with many zero frames, so read this beside the capture-fraction matrix.",
        ),
        (
            "spectra-normalized",
            "Min–max-normalized spectrum shape by trigger delay",
            image_paths.get("normalized_spectra_png"),
            "Every condition is scaled independently outside the censored laser band. This reveals shape and "
            "noise but deliberately removes absolute intensity; dashed conditions have no persistent lines.",
        ),
        (
            "line-detail",
            "Strongest line, every replicate shot",
            image_paths.get("line_detail_png"),
            "The replicate traces expose intermittency directly. Raw peak/noise is shown only as an unscored "
            "diagnostic when the line fails the shot-presence threshold.",
        ),
    ]
    sections_html = "".join(
        f"<section id='{anchor}'><h2>{html.escape(title)}</h2>"
        f"<p>{html.escape(description)}</p>{_embedded_image(path)}</section>"
        for anchor, title, path, description in sections
        if title and path and os.path.isfile(path)
    )
    spatial_html = ""
    if spatial_sensitivity and spatial_sensitivity.get("available"):
        spatial_rows = []
        for model in spatial_sensitivity.get("models", []):
            winner_text = _pick_setting_text(
                float(model["winner_delay_us"]), float(model["winner_integration_us"])
            ).replace("us", "µs")
            runner_text = _pick_setting_text(
                float(model["runner_up_delay_us"]), float(model["runner_up_integration_us"])
            ).replace("us", "µs")
            ci_low = float(model["winner_vs_runner_ci_low"])
            ci_high = float(model["winner_vs_runner_ci_high"])
            spatial_rows.append(
                "<tr>"
                f"<td style='text-align: left;'>{html.escape(str(model['label']))}</td>"
                f"<td style='text-align: left;'>{html.escape(winner_text)}</td>"
                f"<td>{int(model['raw_winner_rank'])}</td>"
                f"<td style='text-align: left;'>{html.escape(runner_text)}</td>"
                f"<td>{float(model['spatial_partial_r_squared']):.1%}</td>"
                f"<td>{ci_low:.2f} to {ci_high:.2f}</td>"
                "</tr>"
            )
        limitations_html = "".join(
            f"<li>{html.escape(str(item))}</li>"
            for item in spatial_sensitivity.get("limitations", [])
        )
        spatial_image = _embedded_image(image_paths.get("spatial_sensitivity_png"))
        raw_setting = _pick_setting_text(
            float(spatial_sensitivity["raw_winner_delay_us"]),
            float(spatial_sensitivity["raw_winner_integration_us"]),
        ).replace("us", "µs")
        spatial_html = f"""
<section id="spatial-sensitivity">
<h2>XY sensitivity check — does position change the timing choice?</h2>
<div class="summary">
  <strong>{html.escape(str(spatial_sensitivity['conclusion']))}</strong>
  <p>The raw information winner is {html.escape(raw_setting)}. The check uses
  {int(spatial_sensitivity['position_tagged_shots']):,} position-tagged shots from
  {int(spatial_sensitivity['runs'])} runs; {int(spatial_sensitivity['repeated_xy_positions'])}
  XY coordinates repeat across runs, {int(spatial_sensitivity['all_run_xy_positions'])} occur in every run,
  and {int(spatial_sensitivity['crossover_xy_positions'])} repeated coordinates received more than one timing condition.</p>
</div>
<p>The chart rescales each method independently so its best setting equals 100. Agreement in rank—not
bar height across methods—is the evidence. Each model fits timing condition and run, then asks whether a
smooth common or run-specific XY alignment field changes the leading setting.</p>
{spatial_image}
<div class="table-scroll">
<table>
  <thead><tr><th style='text-align: left;'>Sensitivity model</th><th style='text-align: left;'>Adjusted winner</th>
  <th>Raw winner rank</th><th style='text-align: left;'>Runner-up</th><th>Residual variance absorbed by XY</th>
  <th>Winner margin, XY-cluster 95% CI (log score)</th></tr></thead>
  <tbody>{''.join(spatial_rows)}</tbody>
</table>
</div>
<p class="meta">A winner-margin interval crossing zero means the first- and second-ranked settings are not
cleanly separated under that model. Full per-setting ranks and indices are saved in
<code>delay_sweep_spatial_sensitivity.csv</code>.</p>
<h3>What this check cannot remove</h3>
<ul>{limitations_html}</ul>
</section>
"""
    nav_items = [("summary", "Summary")]
    if capture_interpretation_html:
        nav_items.append(("capture-interpretation", "Late-delay frames"))
    nav_items.append(("keyfacts", "Key facts"))
    if viewer_html:
        nav_items.append(("explore", "Explore"))
    if inventory_html:
        nav_items.append(("lines", "Line inventory"))
    if target_html:
        nav_items.append(("targets", "Target lines"))
    nav_items += [
        (anchor, title.split(" over ")[0].split(" vs ")[0])
        for anchor, title, path, _description in sections
        if title and path and os.path.isfile(path)
    ]
    if spatial_html:
        nav_items.append(("spatial-sensitivity", "XY sensitivity"))
    nav_html = "".join(f"<a href='#{anchor}'>{html.escape(label)}</a>" for anchor, label in nav_items)

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_shots = len(dataset.shots)
    censored_note = ""
    if excluded_bands_nm:
        bands = ", ".join(f"{low:g}–{high:g} nm" for low, high in excluded_bands_nm)
        censored_note = (
            f" · Censored from all metrics: {html.escape(bands)} (laser scatter)"
        )
    document = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Timing sweep report — {html.escape(dataset.run_type or 'run')}</title>
<style>
  :root {{
    --ink: #1e2a33; --muted: #5f6f7d; --line: #d9e0e7; --panel: #f4f6f8;
    --accent: #35608d; --accent-soft: #e8eef5; --flag: #b42318;
    --viridis-lo: #440154; --viridis-hi: #fde725;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: "Segoe UI", system-ui, sans-serif; margin: 0;
          color: var(--ink); background: #ffffff; }}
  header.page {{ background: linear-gradient(120deg, #2a2050 0%, #27538a 55%, #2e8b6f 100%);
                 color: #ffffff; padding: 1.6rem 1.5rem 1.3rem; }}
  header.page h1 {{ margin: 0 0 0.35rem; font-size: 1.45rem; letter-spacing: 0.01em; }}
  header.page .meta {{ color: rgba(255,255,255,0.85); }}
  nav.toc {{ position: sticky; top: 0; z-index: 5; background: #ffffff;
             border-bottom: 1px solid var(--line); padding: 0.45rem 1.5rem;
             display: flex; gap: 1.1rem; flex-wrap: wrap; }}
  nav.toc a {{ color: var(--accent); text-decoration: none; font-size: 0.88rem; font-weight: 600; }}
  nav.toc a:hover {{ text-decoration: underline; }}
  main {{ max-width: 1080px; margin: 0 auto; padding: 1.4rem 1.5rem 3rem; }}
  section {{ margin-top: 2.2rem; }}
  h2 {{ font-size: 1.12rem; margin: 0 0 0.6rem; padding-bottom: 0.3rem;
        border-bottom: 2px solid var(--accent-soft); }}
  .summary {{ border: 1px solid var(--line); border-left: 6px solid var(--accent);
              border-radius: 8px; padding: 0.9rem 1.2rem; background: var(--panel); }}
  .picks {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
            gap: 0.8rem; margin-bottom: 0.9rem; }}
  .pick {{ border: 1px solid var(--line); border-radius: 8px; padding: 0.75rem 1rem;
           background: #ffffff; }}
  .pick.headline {{ border-left: 6px solid var(--accent); background: var(--accent-soft); }}
  .pick h3 {{ margin: 0 0 0.3rem; font-size: 0.8rem; text-transform: uppercase;
              letter-spacing: 0.04em; color: var(--muted); }}
  .pick strong {{ font-size: 1.02rem; }}
  .pick p {{ margin: 0.35rem 0 0; }}
  .summary strong {{ font-size: 1.12rem; }}
  .summary ul {{ margin: 0.5rem 0 0; }}
  table {{ border-collapse: collapse; margin-top: 0.8rem; font-size: 0.88rem; width: 100%;
           font-variant-numeric: tabular-nums; }}
  th, td {{ border: 1px solid var(--line); padding: 0.35rem 0.6rem; text-align: right; }}
  th {{ background: var(--panel); }}
  tbody tr:nth-child(even) td {{ background: #fafbfc; }}
  td:first-child, th:first-child {{ text-align: left; }}
  tr.recommended td {{ background: var(--accent-soft) !important; font-weight: 600; }}
  img {{ max-width: 100%; height: auto; border: 1px solid var(--line); border-radius: 6px; margin-top: 0.4rem; }}
  .meta {{ color: var(--muted); font-size: 0.85rem; }}
  .table-scroll {{ overflow-x: auto; }}
  table.cellgrid td.cell {{ cursor: pointer; text-align: center; min-width: 3.2rem; }}
  table.cellgrid td.cell:hover {{ outline: 2px solid var(--ink); outline-offset: -2px; }}
  table.cellgrid td.selected {{ outline: 3px solid var(--ink); outline-offset: -3px; font-weight: 700; }}
  table.cellgrid td.recommended-cell {{ box-shadow: inset 0 0 0 3px var(--flag); }}
  table.cellgrid td.dead {{ color: #9aa7b4; text-align: center; }}
</style>
</head>
<body>
<header class="page">
<h1>Timing sweep report</h1>
<p class="meta">Run: {html.escape(dataset.run_directory or '(in-memory dataset)')} ·
Type: {html.escape(dataset.run_type or 'unknown')} · Shots: {total_shots} · Generated: {generated}{censored_note}</p>
</header>
<nav class="toc">{nav_html}</nav>
<main>
<section id="summary">
{picks_html}
<div class="summary">
  <strong>{html.escape(summary_label)}: {html.escape(setting)}</strong>
  <ul>{reasons_html}</ul>
</div>
</section>
{capture_interpretation_html}
<section id="keyfacts">
<h2>Per-setting key facts</h2>
<p class="meta">One row per delay × integration cell. “Nonzero frames” counts valid-length saved arrays
that contain any nonzero value. Peak, SNR, and SBR refer only to the strongest line present in at least
{PEAK_MIN_PRESENCE_FRACTION:.0%} of shots; unfiltered mean-peak diagnostics remain in the CSV under
<code>raw_peak_*</code> columns.</p>
<div class="table-scroll">
<table>
  <thead><tr>{''.join(f'<th>{html.escape(cell)}</th>' for cell in header_cells)}</tr></thead>
  <tbody>{''.join(body_rows)}</tbody>
</table>
</div>
</section>
{viewer_html}
{inventory_html}
{target_html}
{sections_html}
{spatial_html}
</main>
</body>
</html>
"""
    os.makedirs(output_directory, exist_ok=True)
    report_path = os.path.join(output_directory, "delay_sweep_report.html")
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(document)
    return report_path


def _sequential_color_map(values: list, ramp_name: str = "viridis"):
    """Perceptually uniform ramp: dark = small value, bright = large value."""
    import matplotlib
    import matplotlib.colors as mcolors

    ramp = matplotlib.colormaps[ramp_name]
    # Stop short of the pale yellow top end so lines stay visible on white.
    positions = np.linspace(0.0, 0.85, max(len(values), 2))
    return {value: mcolors.to_hex(ramp(pos)) for value, pos in zip(values, positions)}


def _viridis_cell_color(fraction: float) -> tuple[str, str]:
    """Viridis background + a text color chosen by the cell's luminance."""
    import matplotlib

    r, g, b, _a = matplotlib.colormaps["viridis"](max(0.0, min(1.0, fraction)))
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    import matplotlib.colors as mcolors

    return mcolors.to_hex((r, g, b)), ("#1a2733" if luminance > 0.55 else "#ffffff")


def _style_axis(ax):
    ax.grid(True, axis="both", color="0.9", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def _style_delay_axis(ax, delays: list[float], linthresh: float):
    """Symlog delay axis ticked at the actual sweep values (0 included)."""
    from matplotlib.ticker import NullLocator

    ax.set_xscale("symlog", linthresh=linthresh)
    ax.set_xticks(delays)
    ax.set_xticklabels([f"{value:g}" for value in delays])
    ax.xaxis.set_minor_locator(NullLocator())
    ax.set_xlim(left=-linthresh * 0.4)


def _integration_label(integration_us: float | None) -> str:
    if integration_us is None or not math.isfinite(integration_us):
        return "n/a"
    return f"{integration_us / 1000.0:g} ms"


def plot_delay_sweep(
    dataset: DelaySweepDataset,
    metrics: pd.DataFrame,
    recommendation: dict[str, Any],
    output_directory: str,
    *,
    excluded_bands_nm: tuple[tuple[float, float], ...] | None = DEFAULT_EXCLUDED_WAVELENGTH_BANDS_NM,
) -> dict[str, str]:
    """Comparison figures: spectra per delay, key facts, and (2D) a heatmap."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(output_directory, exist_ok=True)
    paths: dict[str, str] = {}
    delays = dataset.delays()
    integrations = dataset.integration_values_us()
    joint = len(integrations) > 1
    delay_colors = _sequential_color_map(delays)
    recommended_delay = float(recommendation.get("recommended_delay_us", float("nan")))
    recommended_integration = recommendation.get("recommended_integration_us")
    pick_settings = {
        (pick.get("delay_us"), pick.get("integration_us"))
        for pick in (recommendation.get("picks") or {}).values()
        if pick
    }
    selection_label = "information-rich candidate" if len(pick_settings) > 1 else "recommended"
    linthresh = min((d for d in delays if d > 0), default=1.0)

    # ── Figure 1: mean spectrum per delay ──────────────────────────────
    # For a joint sweep the overlay is restricted to the recommended
    # integration band so the delay comparison is apples-to-apples.
    spectra_integration = recommended_integration if joint else integrations[0]
    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=150)
    plotted_mean_spectra: list[np.ndarray] = []
    for delay_us in delays:
        cell_shots = dataset.shots_for_cell(delay_us, spectra_integration)
        if not cell_shots:
            continue
        stack = np.vstack([shot.intensities for shot in cell_shots])
        label = f"{delay_us:g} µs"
        if delay_us == recommended_delay:
            label += f" ({selection_label})"
        mean_spectrum = np.nanmean(stack, axis=0)
        plotted_mean_spectra.append(mean_spectrum)
        ax.plot(
            dataset.wavelengths,
            mean_spectrum,
            color=delay_colors[delay_us],
            linewidth=2.2 if delay_us == recommended_delay else 1.2,
            label=label,
            zorder=3 if delay_us == recommended_delay else 2,
        )
    for band_index, (band_low, band_high) in enumerate(excluded_bands_nm or ()):
        ax.axvspan(
            band_low,
            band_high,
            color="#f6dedc",
            zorder=1,
            label="censored (laser scatter)" if band_index == 0 else None,
        )
    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel("Mean intensity (counts)")
    title = "Mean spectrum by trigger delay"
    if joint:
        title += f" (at {_integration_label(spectra_integration)} integration)"
    ax.set_title(title)
    if plotted_mean_spectra:
        display_peak = spectrum_autoscale_peak(
            dataset.wavelengths,
            np.vstack(plotted_mean_spectra),
            excluded_bands_nm=excluded_bands_nm,
        )
        ax.set_ylim(0.0, max(1.0, display_peak * 1.10))
    _style_axis(ax)
    ax.legend(title="Trigger delay", fontsize=8, title_fontsize=9, ncol=2, frameon=False)
    fig.tight_layout()
    spectra_path = os.path.join(output_directory, "delay_sweep_spectra.png")
    fig.savefig(spectra_path)
    plt.close(fig)
    paths["spectra_png"] = spectra_path

    # Same condition overlay with each mean spectrum scaled independently.
    # The censored laser-scatter band is excluded from both the scale and the
    # displayed trace so it cannot flatten the sample-emission comparison.
    excluded = excluded_wavelength_mask(dataset.wavelengths, excluded_bands_nm)
    scale_mask = ~excluded
    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=150)
    for delay_us in delays:
        cell_shots = dataset.shots_for_cell(delay_us, spectra_integration)
        if not cell_shots:
            continue
        stack = np.vstack([shot.intensities for shot in cell_shots])
        mean_spectrum = np.nanmean(stack, axis=0)
        finite_scale = scale_mask & np.isfinite(mean_spectrum)
        if not finite_scale.any():
            continue
        scale_min = float(np.nanmin(mean_spectrum[finite_scale]))
        scale_max = float(np.nanmax(mean_spectrum[finite_scale]))
        scale_span = scale_max - scale_min
        if not math.isfinite(scale_span) or scale_span <= 0:
            normalized = np.zeros_like(mean_spectrum, dtype=float)
        else:
            normalized = (mean_spectrum - scale_min) / scale_span
        normalized = np.where(excluded, np.nan, normalized)
        label = f"{delay_us:g} µs"
        if delay_us == recommended_delay:
            label += f" ({selection_label})"
        metric_row = metrics[metrics["delay_us"] == delay_us]
        if spectra_integration is not None:
            metric_row = metric_row[
                np.isclose(metric_row["integration_time_us"], float(spectra_integration))
            ]
        has_persistent_lines = not metric_row.empty and int(metric_row.iloc[0].get("line_count", 0)) > 0
        if not has_persistent_lines:
            label += " (no persistent lines)"
        ax.plot(
            dataset.wavelengths,
            normalized,
            color=delay_colors[delay_us],
            linewidth=2.2 if delay_us == recommended_delay else 1.2,
            linestyle="-" if has_persistent_lines else "--",
            alpha=1.0 if has_persistent_lines else 0.75,
            label=label,
            zorder=3 if delay_us == recommended_delay else 2,
        )
    for band_index, (band_low, band_high) in enumerate(excluded_bands_nm or ()):
        ax.axvspan(
            band_low,
            band_high,
            color="#f6dedc",
            zorder=1,
            label="excluded from normalization (laser scatter)" if band_index == 0 else None,
        )
    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel("Min–max normalized mean intensity")
    normalized_title = "Normalized mean spectrum by trigger delay"
    if joint:
        normalized_title += f" (at {_integration_label(spectra_integration)} integration)"
    ax.set_title(normalized_title)
    ax.set_ylim(-0.02, 1.05)
    _style_axis(ax)
    ax.legend(title="Trigger delay", fontsize=8, title_fontsize=9, ncol=2, frameon=False)
    fig.text(
        0.01,
        0.01,
        "Each condition is scaled independently using uncensored wavelengths; compare shape and noise, not absolute signal strength.",
        fontsize=8,
        color="#5f6f7d",
    )
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    normalized_spectra_path = os.path.join(output_directory, "delay_sweep_spectra_normalized.png")
    fig.savefig(normalized_spectra_path)
    plt.close(fig)
    paths["normalized_spectra_png"] = normalized_spectra_path

    if not joint:
        # ── 1D: key facts vs delay (small multiples, one axis each) ────
        panels = (
            ("line_signal", "Net line signal (counts)"),
            ("continuum_level", "Continuum background (counts)"),
            ("snr", "Signal-to-noise ratio"),
            ("sbr", "Signal-to-background ratio"),
        )
        fig, axes = plt.subplots(2, 2, figsize=(11, 7), dpi=150, sharex=True)
        series_color = delay_colors[delays[-1]]
        for (column, title), ax in zip(panels, axes.ravel()):
            ax.plot(metrics["delay_us"], metrics[column], color=series_color, linewidth=2, marker="o", markersize=5)
            if math.isfinite(recommended_delay):
                ax.axvline(recommended_delay, color="0.45", linewidth=1, linestyle="--")
                row = metrics[metrics["delay_us"] == recommended_delay]
                if not row.empty:
                    ax.plot(
                        [recommended_delay],
                        [float(row.iloc[0][column])],
                        marker="o",
                        markersize=9,
                        markerfacecolor="none",
                        markeredgewidth=2,
                        color="0.25",
                    )
            _style_delay_axis(ax, delays, linthresh)
            ax.set_title(title, fontsize=10)
            _style_axis(ax)
        for ax in axes[1]:
            ax.set_xlabel("Trigger delay (µs)")
        label = f"recommended {recommended_delay:g} µs" if math.isfinite(recommended_delay) else ""
        fig.suptitle(f"Trigger-delay sweep key facts ({label})", fontsize=12)
        fig.tight_layout(rect=(0, 0, 1, 0.96))
        metrics_path = os.path.join(output_directory, "delay_sweep_metrics.png")
        fig.savefig(metrics_path)
        plt.close(fig)
        paths["metrics_png"] = metrics_path
        return paths

    # ── 2D: SNR vs delay, one line per integration ─────────────────────
    integration_colors = _sequential_color_map(integrations)
    fig, (ax_snr, ax_sbr) = plt.subplots(1, 2, figsize=(12, 5), dpi=150, sharex=True)
    for integration_us in integrations:
        subset = metrics[
            metrics["integration_time_us"].apply(
                lambda value: (integration_us is None and not math.isfinite(value))
                or (integration_us is not None and math.isfinite(value) and float(value) == float(integration_us))
            )
        ].sort_values("delay_us")
        if subset.empty:
            continue
        recommended_series = (
            recommended_integration is not None
            and integration_us is not None
            and float(integration_us) == float(recommended_integration)
        )
        for ax, column in ((ax_snr, "snr"), (ax_sbr, "sbr")):
            ax.plot(
                subset["delay_us"],
                subset[column],
                color=integration_colors[integration_us],
                linewidth=2.2 if recommended_series else 1.4,
                marker="o",
                markersize=5 if recommended_series else 4,
                label=_integration_label(integration_us) + (f" ({selection_label})" if recommended_series else ""),
                zorder=3 if recommended_series else 2,
            )
    for ax, title in ((ax_snr, "Signal-to-noise ratio"), (ax_sbr, "Signal-to-background ratio")):
        if math.isfinite(recommended_delay):
            ax.axvline(recommended_delay, color="0.45", linewidth=1, linestyle="--")
        _style_delay_axis(ax, delays, linthresh)
        ax.set_xlabel("Trigger delay (µs)")
        ax.set_title(title, fontsize=11)
        _style_axis(ax)
    ax_snr.legend(title="Integration", fontsize=8, title_fontsize=9, frameon=False)
    setting = f"{recommended_delay:g} µs + {_integration_label(recommended_integration)}"
    fig.suptitle(f"Joint timing sweep ({selection_label}: {setting})", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    metrics_path = os.path.join(output_directory, "delay_sweep_metrics.png")
    fig.savefig(metrics_path)
    plt.close(fig)
    paths["metrics_png"] = metrics_path

    # ── 2D: persistent-line information over the full grid ─────────────
    finite_integrations = [value for value in integrations if value is not None]
    grid = np.full((len(finite_integrations), len(delays)), np.nan)
    observed = np.zeros(grid.shape, dtype=bool)
    line_counts = np.zeros(grid.shape, dtype=int)
    for _, row in metrics.iterrows():
        integration_value = float(row["integration_time_us"])
        if not math.isfinite(integration_value):
            continue
        try:
            i = finite_integrations.index(integration_value)
            j = delays.index(float(row["delay_us"]))
        except ValueError:
            continue
        observed[i, j] = True
        line_counts[i, j] = int(row.get("line_count", 0))
        information_score = float(row.get("information_score", float("nan")))
        if line_counts[i, j] > 0 and math.isfinite(information_score) and information_score > 0:
            grid[i, j] = information_score

    fig, ax = plt.subplots(figsize=(10, 4.8), dpi=150)
    cmap = matplotlib.colormaps["viridis"].copy()
    cmap.set_bad("#e5e7eb")
    mesh = ax.imshow(np.ma.masked_invalid(grid), aspect="auto", cmap=cmap, origin="lower")
    ax.set_xticks(range(len(delays)), [f"{value:g}" for value in delays])
    ax.set_yticks(range(len(finite_integrations)), [_integration_label(value) for value in finite_integrations])
    ax.set_xlabel("Trigger delay (µs)")
    ax.set_ylabel("Integration time")
    ax.set_title("Persistent-line information score by delay × integration")
    finite_grid = grid[np.isfinite(grid)]
    grid_min = float(np.nanmin(finite_grid)) if finite_grid.size else 0.0
    grid_max = float(np.nanmax(finite_grid)) if finite_grid.size else 1.0
    span = (grid_max - grid_min) or 1.0
    for i in range(grid.shape[0]):
        for j in range(grid.shape[1]):
            if not math.isfinite(grid[i, j]):
                if observed[i, j] and line_counts[i, j] == 0:
                    ax.text(j, i, "no lines", ha="center", va="center", fontsize=7, color="#64748b")
                continue
            _bg, text_color = _viridis_cell_color((grid[i, j] - grid_min) / span)
            ax.text(
                j,
                i,
                f"{grid[i, j]:.0f}",
                ha="center",
                va="center",
                fontsize=8,
                color=text_color,
            )
    if (
        math.isfinite(recommended_delay)
        and recommended_integration is not None
        and recommended_delay in delays
        and float(recommended_integration) in finite_integrations
    ):
        j = delays.index(recommended_delay)
        i = finite_integrations.index(float(recommended_integration))
        ax.add_patch(
            plt.Rectangle((j - 0.5, i - 0.5), 1.0, 1.0, fill=False, edgecolor="#B42318", linewidth=2)
        )
    fig.colorbar(mesh, ax=ax, label="Information score (persistent lines)")
    fig.tight_layout()
    heatmap_path = os.path.join(output_directory, "delay_sweep_heatmap.png")
    fig.savefig(heatmap_path)
    plt.close(fig)
    paths["heatmap_png"] = heatmap_path

    # Frame-level completeness: an all-zero, valid-length array is retained
    # as an observed dark frame. This matrix exposes intermittent capture
    # instead of letting a few bright shots hide inside the cell mean.
    capture_grid = np.full((len(finite_integrations), len(delays)), np.nan)
    for _, row in metrics.iterrows():
        integration_value = float(row["integration_time_us"])
        if not math.isfinite(integration_value):
            continue
        try:
            i = finite_integrations.index(integration_value)
            j = delays.index(float(row["delay_us"]))
        except ValueError:
            continue
        capture_grid[i, j] = float(row.get("nonzero_frame_fraction", 1.0))

    fig, ax = plt.subplots(figsize=(10, 4.8), dpi=150)
    mesh = ax.imshow(capture_grid, aspect="auto", cmap="Blues", origin="lower", vmin=0.0, vmax=1.0)
    ax.set_xticks(range(len(delays)), [f"{value:g}" for value in delays])
    ax.set_yticks(
        range(len(finite_integrations)),
        [_integration_label(value) for value in finite_integrations],
    )
    ax.set_xlabel("Trigger delay (µs)")
    ax.set_ylabel("Integration time")
    ax.set_title("Nonzero saved-frame fraction by delay × integration")
    for i in range(capture_grid.shape[0]):
        for j in range(capture_grid.shape[1]):
            if not math.isfinite(capture_grid[i, j]):
                continue
            ax.text(
                j,
                i,
                f"{capture_grid[i, j]:.0%}",
                ha="center",
                va="center",
                fontsize=8,
                color="#ffffff" if capture_grid[i, j] >= 0.58 else "#1e2a33",
            )
    if (
        math.isfinite(recommended_delay)
        and recommended_integration is not None
        and recommended_delay in delays
        and float(recommended_integration) in finite_integrations
    ):
        j = delays.index(recommended_delay)
        i = finite_integrations.index(float(recommended_integration))
        ax.add_patch(
            plt.Rectangle((j - 0.5, i - 0.5), 1.0, 1.0, fill=False, edgecolor="#B42318", linewidth=2)
        )
    fig.colorbar(mesh, ax=ax, label="Fraction of saved arrays containing any nonzero value")
    fig.tight_layout()
    capture_path = os.path.join(output_directory, "delay_sweep_capture_fraction.png")
    fig.savefig(capture_path)
    plt.close(fig)
    paths["capture_fraction_png"] = capture_path
    return paths


def _comparison_cells(dataset: DelaySweepDataset, recommendation: dict[str, Any]) -> list[tuple[float, float | None]]:
    """Short / recommended / long delay, all at the reference integration."""
    delays = dataset.delays()
    integrations = dataset.integration_values_us()
    recommended_integration = recommendation.get("recommended_integration_us")
    integration = recommended_integration if len(integrations) > 1 else integrations[0]
    chosen: list[float] = []
    for delay in (delays[0], recommendation.get("recommended_delay_us"), delays[-1]):
        if delay is None:
            continue
        delay = float(delay)
        if delay in delays and delay not in chosen:
            chosen.append(delay)
    return [(delay, integration) for delay in chosen if dataset.shots_for_cell(delay, integration)]


def plot_line_detail(
    dataset: DelaySweepDataset,
    metrics: pd.DataFrame,
    recommendation: dict[str, Any],
    output_directory: str,
    *,
    window_nm: float = 4.0,
    excluded_bands_nm: tuple[tuple[float, float], ...] | None = DEFAULT_EXCLUDED_WAVELENGTH_BANDS_NM,
    inventory: list[dict[str, Any]] | None = None,
) -> str:
    """Actual replicate spectra around the strongest line, per setting.

    One panel per compared setting (shortest delay, recommended, longest
    delay), each showing every replicate shot plus the mean, so shot noise
    and the line/continuum trade-off are visible in the raw data rather
    than only in summary numbers.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(output_directory, exist_ok=True)
    cells = _comparison_cells(dataset, recommendation)
    if not cells:
        raise ValueError("No comparison cells available for the line-detail plot.")
    recommended_delay = float(recommendation.get("recommended_delay_us", float("nan")))

    # Line center from the recommended cell's net spectrum.
    reference_delay, reference_integration = next(
        (cell for cell in cells if cell[0] == recommended_delay), cells[-1]
    )
    reference_stack = np.vstack(
        [shot.intensities for shot in dataset.shots_for_cell(reference_delay, reference_integration)]
    )
    reference_mean = np.nanmean(reference_stack, axis=0)
    net = reference_mean - rolling_percentile_baseline(reference_mean)
    censored = excluded_wavelength_mask(dataset.wavelengths, excluded_bands_nm)
    if censored.any() and not censored.all():
        # Never center the raw-data panel on laser scatter.
        net = np.where(censored, -np.inf, net)
    center_nm = float(dataset.wavelengths[int(np.nanargmax(net))])
    center_label = ""
    if inventory:
        closest = min(inventory, key=lambda entry: abs(float(entry["wavelength_nm"]) - center_nm))
        if abs(float(closest["wavelength_nm"]) - center_nm) <= LINE_MATCH_TOLERANCE_NM:
            center_label = str(closest.get("label", "")).strip()
    mask = np.abs(dataset.wavelengths - center_nm) <= float(window_nm)
    if not mask.any():
        mask = np.ones_like(dataset.wavelengths, dtype=bool)

    delay_colors = _sequential_color_map(dataset.delays())
    fig, axes = plt.subplots(1, len(cells), figsize=(4.6 * len(cells), 5.0), dpi=150)
    axes = np.atleast_1d(axes)
    for (delay_us, integration_us), ax in zip(cells, axes):
        shots = dataset.shots_for_cell(delay_us, integration_us)
        stack = np.vstack([shot.intensities for shot in shots])
        color = delay_colors[delay_us]
        for shot_values in stack:
            ax.plot(dataset.wavelengths[mask], shot_values[mask], color=color, linewidth=0.7, alpha=0.35)
        ax.plot(dataset.wavelengths[mask], np.nanmean(stack, axis=0)[mask], color=color, linewidth=2.2)
        row = metrics[metrics["delay_us"] == delay_us]
        if integration_us is not None:
            row = row[np.isclose(row["integration_time_us"], float(integration_us))]
        metric_text = ""
        if not row.empty:
            line_count = int(row.iloc[0].get("line_count", 0))
            raw_peak_snr = float(row.iloc[0]["snr"])
            if line_count > 0:
                metric_text = f"{len(shots)} shots · {line_count} persistent lines\nstrongest-line SNR {raw_peak_snr:.1f}"
            else:
                raw_peak_to_noise = float(row.iloc[0].get("raw_peak_to_noise", float("nan")))
                raw_text = f"{raw_peak_to_noise:.1f}" if math.isfinite(raw_peak_to_noise) else "n/a"
                metric_text = f"{len(shots)} shots · no persistent lines\nraw peak/noise {raw_text} (not scored)"
        title = f"{delay_us:g} µs"
        if delay_us == recommended_delay:
            title += " (recommended)"
        ax.set_title(f"{title}\n{metric_text}", fontsize=9.5)
        ax.set_xlabel("Wavelength (nm)")
        _style_axis(ax)
    axes[0].set_ylabel("Intensity (counts)")
    suffix = f" at {_integration_label(cells[0][1])} integration" if len(dataset.integration_values_us()) > 1 else ""
    line_identity = f" — {center_label}" if center_label else ""
    fig.suptitle(
        f"Strongest line ({center_nm:.1f} nm{line_identity}) — every replicate shot{suffix}",
        fontsize=12,
        color="#8a4700" if center_label.startswith("Al ") else "#1e2a33",
        y=0.99,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.87))
    path = os.path.join(output_directory, "delay_sweep_line_detail.png")
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path
