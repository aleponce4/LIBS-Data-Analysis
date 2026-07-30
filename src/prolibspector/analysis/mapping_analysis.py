"""2D mapping analysis helpers for acquisition mapping runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from prolibspector.analysis.adjust_plot import normalize_min_max, normalize_total_intensity
from prolibspector.analysis.adjust_spectrum import apply_baseline_removal, apply_smoothing
from prolibspector.analysis.element_database import DEFAULT_DATABASE_LABEL, get_database_path, load_element_database
from prolibspector.analysis.spectrum_io import read_spectrum_file, write_spectrum_file
from prolibspector.acquisition.mapping_spectrum_store import (
    binary_row_is_written,
    load_binary_written_mask,
    mapping_spectrum_store_dir,
    mapping_spectrum_store_manifest_path,
    read_binary_mapping_spectrum,
)

MAPPING_INDEX_FILENAME = "_mapping_grid_index.csv"
MAPPING_MANIFEST_FILENAME = "_mapping_grid_manifest.json"
MAPPING_STATE_FILENAME = "_mapping_grid_state.json"
MAPPING_ANALYSIS_CACHE_DIRNAME = "_mapping_analysis_cache"
MAPPING_ANALYSIS_RESULTS_DIRNAME = "results"
MAPPING_ANALYSIS_LINE_FEATURES_DIRNAME = "line_features"
MAPPING_ANALYSIS_CANDIDATE_SCANS_DIRNAME = "candidate_scans"

MAPPING_PALETTES = ("viridis", "cividis", "plasma", "inferno", "magma", "turbo", "gray")
EPS = 1e-12
DETECTION_METHOD_VERSION = "multi-element-targeted-v11"
DETECTION_STATE_DETECTED = "detected"
DETECTION_STATE_UNCERTAIN = "uncertain"
DETECTION_STATE_NOT_DETECTED = "not_detected"
# A database line of another element only counts as an overlap threat when its
# intensity is at least this fraction of that element's strongest in-range line.
OVERLAP_SIGNIFICANCE_RATIO = 0.02
# How many of each other selected element's lines count as "dominant" for
# strong-line exclusion, ranked by tabulated intensity.
#
# Rank, not a fraction of the element's maximum. A fraction fails in both
# directions: zinc's catalogued maximum is 213.86 nm where this spectrometer has
# little sensitivity, so the lines that dominate a real zinc spectrum (481.05,
# 334.50) sit at 1-2% of it - while a 1% cut applied to a dense list such as
# iron or titanium promotes hundreds of lines and excluded the good Ni 351.5 and
# Co 357.54 outright. Taking a fixed number of strongest lines per element is
# bounded by construction and behaves the same for sparse and dense line lists.
STRONG_LINE_DOMINANT_COUNT = 8

# Species emitted by the ambient-air plasma itself. Their lines fire at every
# grid point regardless of sample composition, so they carry no elemental
# discrimination and must not count as evidence for other elements.
ATMOSPHERIC_SPECIES = ("H", "N", "O", "C", "Ar")
# Curated strong air-plasma lines (nm) observed even when the species entry is
# missing from the loaded database: H Balmer, O I / N I multiplets, N II.
ATMOSPHERIC_LINES_NM = (
    486.13,
    656.28,
    500.52,
    567.96,
    742.36,
    744.23,
    746.83,
    777.19,
    777.42,
    777.54,
    818.49,
    818.80,
    820.04,
    821.07,
    821.63,
    822.31,
    824.24,
    844.64,
    856.77,
    859.40,
    862.92,
    868.03,
    868.34,
    870.33,
    871.17,
    872.89,
    926.60,
    938.68,
    939.28,
)
# Isolated, always-present air-plasma lines used to measure the per-run
# wavelength offset (exact NIST vacuum-corrected air wavelengths, nm).
WAVELENGTH_CALIBRATION_ANCHORS_NM = (500.515, 567.956, 656.279, 746.831, 844.636)
# Internal-standard line for "Air-plasma reference" normalization: the O I 777
# triplet, integrated as one blend. Air chemistry is identical at every point,
# so this area tracks laser-plasma coupling shot by shot.
AIR_REFERENCE_LINE_NM = 777.417
AIR_REFERENCE_HALF_WINDOW_NM = 0.7

MULTI_ELEMENT_DETECTION_PRESETS: dict[str, dict[str, Any]] = {
    "Exploratory": {
        "detected_threshold": 0.65,
        "uncertain_threshold": 0.35,
        "min_line_z": 3.0,
        "min_line_snr": 3.0,
        "spatial_smoothing_strength": 0.30,
        "require_multi_line_agreement": False,
    },
    "Loose": {
        "detected_threshold": 0.65,
        "uncertain_threshold": 0.35,
        "min_line_z": 3.2,
        "min_line_snr": 4.0,
        "spatial_smoothing_strength": 0.30,
    },
    "Normal": {
        "detected_threshold": 0.75,
        "uncertain_threshold": 0.45,
        "min_line_z": 4.0,
        "min_line_snr": 5.0,
        "spatial_smoothing_strength": 0.25,
    },
    "Strict": {
        "detected_threshold": 0.85,
        "uncertain_threshold": 0.55,
        "min_line_z": 5.0,
        "min_line_snr": 7.0,
        "spatial_smoothing_strength": 0.18,
    },
}

MULTI_ELEMENT_COLORS = (
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
    "#4c78a8",
    "#f58518",
    "#54a24b",
    "#e45756",
    "#72b7b2",
    "#b279a2",
    "#ff9da6",
    "#9d755d",
    "#bab0ac",
    "#6b6ecf",
)

REQUIRED_INDEX_COLUMNS = {
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
    "integration_time_us",
}


@dataclass(frozen=True)
class MappingRunData:
    folder: Path
    index: pd.DataFrame
    manifest: dict[str, Any] = field(default_factory=dict)
    sample_label: str = ""
    grid_rows: int = 0
    grid_columns: int = 0
    row_indices: tuple[int, ...] = ()
    column_indices: tuple[int, ...] = ()
    x_coords: tuple[float, ...] = ()
    y_coords: tuple[float, ...] = ()
    missing_spectra: int = 0
    resolved_spectra: int = 0
    storage_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def points_total(self) -> int:
        return int(self.index["point_key"].nunique()) if not self.index.empty else 0

    @property
    def shots_total(self) -> int:
        return int(len(self.index))

    @property
    def binary_backed_spectra(self) -> int:
        if "binary_available" not in self.index.columns:
            return 0
        return int(self.index["binary_available"].sum())

    @property
    def csv_backed_spectra(self) -> int:
        if "csv_file_exists" not in self.index.columns:
            return int(self.resolved_spectra)
        return int(self.index["csv_file_exists"].sum())

    @property
    def missing_binary_spectra(self) -> int:
        if "storage_kind" not in self.index.columns:
            return 0
        binary_rows = self.index["storage_kind"].fillna("").astype(str).str.lower() == "binary_npy"
        if "binary_available" not in self.index.columns:
            return int(binary_rows.sum())
        return int((binary_rows & ~self.index["binary_available"].astype(bool)).sum())

    @property
    def missing_csv_spectra(self) -> int:
        if "storage_kind" not in self.index.columns:
            return int(self.missing_spectra)
        storage = self.index["storage_kind"].fillna("").astype(str).str.lower()
        csv_rows = (storage == "") | (storage == "csv")
        if "csv_file_exists" not in self.index.columns:
            return int(csv_rows.sum())
        return int((csv_rows & ~self.index["csv_file_exists"].astype(bool)).sum())


@dataclass(frozen=True)
class MappingPreprocessConfig:
    background_enabled: bool = False
    background_reference_path: str | None = None
    # Smoothing defaults off for mapping: at typical mapping spectrograph
    # sampling (~0.2 nm/px) the SavGol window smears emission lines wider
    # than the peak-integration window, destroying line/sideband separation.
    # Median shot aggregation plus net-area integration do the denoising.
    smoothing_enabled: bool = False
    smoothing_method: str = "Savitzky-Golay filter"
    smoothing_strength: int = 21
    baseline_enabled: bool = False
    baseline_lam: float = 1e4
    baseline_p: float = 0.001
    baseline_niter: int = 10
    normalization: str = "Air-plasma reference"
    aggregate_shots: str = "median"
    drift_correction_enabled: bool = False
    wavelength_calibration_enabled: bool = True
    parallel_workers: int = 0
    chunk_size_spectra: int = 2048


@dataclass(frozen=True)
class ElementMapConfig:
    selected_element: str = ""
    selected_elements: tuple[str, ...] = ()
    database_label: str = DEFAULT_DATABASE_LABEL
    database_path: str | None = None
    ionization_levels: tuple[bool, bool, bool] = (True, True, True)
    tolerance_nm: float = 0.3
    # Same measured default as MultiElementMapConfig: the GUI already passes
    # 0.2 from the shared sidebar variable, so this only aligns programmatic use.
    peak_window_nm: float = 0.2
    colormap: str = "viridis"
    color_min: float | None = None
    color_max: float | None = None
    candidate_pruning_enabled: bool = True
    max_candidate_lines: int = 4
    min_prominence_sigma: float = 1.0
    atmospheric_filter_enabled: bool = True

    @property
    def element_symbols(self) -> tuple[str, ...]:
        if self.selected_elements:
            return tuple(str(symbol).strip() for symbol in self.selected_elements if str(symbol).strip())
        if self.selected_element:
            return (str(self.selected_element).strip(),)
        return ()


@dataclass
class ElementMapResult:
    run_data: MappingRunData
    preprocess_config: MappingPreprocessConfig
    element_config: ElementMapConfig
    point_table: pd.DataFrame
    grid_matrix: pd.DataFrame
    line_scores: pd.DataFrame
    value_grid: np.ndarray
    x_coords: tuple[float, ...]
    y_coords: tuple[float, ...]
    x_edges: np.ndarray
    y_edges: np.ndarray
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def element_label(self) -> str:
        symbols = self.element_config.element_symbols
        return symbols[0] if symbols else "Element"

    @property
    def accepted_line_count(self) -> int:
        if self.line_scores.empty or "accepted" not in self.line_scores.columns:
            return 0
        return int(self.line_scores["accepted"].sum())

    def manifest_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_folder": str(self.run_data.folder),
            "source_sample_label": self.run_data.sample_label,
            "source_manifest_summary": _manifest_summary(self.run_data.manifest),
            "preprocess_config": asdict(self.preprocess_config),
            "element_config": asdict(self.element_config),
            "points_total": int(len(self.point_table)),
            "accepted_line_count": self.accepted_line_count,
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True)
class MultiElementMapConfig:
    selected_elements: tuple[str, ...] = ()
    database_label: str = DEFAULT_DATABASE_LABEL
    database_path: str | None = None
    # All ionization stages. Restricting to neutral lines scored better on both
    # benchmark samples, but both have transition-metal positives whose strong
    # lines are neutral; it makes elements whose PRINCIPAL lines are ionic
    # unfindable (Eu II, and routinely Ca II 393/397, Ba II, Sr II, Mg II), and
    # the pipeline then latches onto weak neutral lines that match by accident -
    # europium reached 0.99 confidence on a tile that contains none.
    ionization_levels: tuple[bool, bool, bool] = (True, True, True)
    tolerance_nm: float = 0.3
    # Measured against ground truth rather than reasoned about: 0.35 nm reached
    # far enough into neighbouring lines to invert the nickel map on a 2 euro
    # coin. See docs/mapping_analysis_methods.md.
    peak_window_nm: float = 0.2
    detected_threshold: float = 0.75
    uncertain_threshold: float = 0.45
    min_line_z: float = 4.0
    min_line_snr: float = 5.0
    require_multi_line_agreement: bool = True
    spatial_smoothing_strength: float = 0.25
    preset: str = "Normal"
    view: str = "overview"
    detail_element: str = ""
    candidate_pruning_enabled: bool = True
    max_candidate_lines: int = 4
    min_prominence_sigma: float = 1.0
    atmospheric_filter_enabled: bool = True
    # Drop candidates whose integration window reaches a principal line of
    # another selected element. 0 disables it, which is the historical
    # behaviour; a dominant neighbour otherwise leaks in through its wings even
    # when the line centres are further apart than tolerance_nm.
    strong_line_exclusion_nm: float = 0.0
    # Discount overlapped lines when building the intensity map, the way the
    # confidence path already does.
    intensity_overlap_weighting: bool = False
    # Local-baseline sidebands. 0 keeps the derived geometry
    # (inner = max(2w, w+0.15), outer = inner + 1.2).
    sideband_inner_nm: float = 0.0
    sideband_outer_nm: float = 0.0
    # A database line of another element only counts as a misattribution
    # threat when it is at least this fraction of that element's strongest
    # in-range line; 0 restores the historical every-line-counts behaviour.
    # Configurable so the benchmark can measure it instead of assuming it.
    overlap_significance_ratio: float = OVERLAP_SIGNIFICANCE_RATIO

    @property
    def element_symbols(self) -> tuple[str, ...]:
        return tuple(str(symbol).strip() for symbol in self.selected_elements if str(symbol).strip())

    @classmethod
    def from_preset(
        cls,
        selected_elements: Sequence[str],
        *,
        preset: str = "Normal",
        database_label: str = DEFAULT_DATABASE_LABEL,
        database_path: str | None = None,
        # Keep in step with the dataclass defaults above - this constructor is
        # the path the GUI and the benchmark both use, so a default set only on
        # the dataclass would never take effect.
        ionization_levels: tuple[bool, bool, bool] = (True, True, True),
        tolerance_nm: float = 0.3,
        peak_window_nm: float = 0.2,
        require_multi_line_agreement: bool = True,
        view: str = "overview",
        detail_element: str = "",
        candidate_pruning_enabled: bool = True,
        max_candidate_lines: int = 4,
        min_prominence_sigma: float = 1.0,
        atmospheric_filter_enabled: bool = True,
    ) -> "MultiElementMapConfig":
        preset_values = MULTI_ELEMENT_DETECTION_PRESETS.get(preset, MULTI_ELEMENT_DETECTION_PRESETS["Normal"])
        return cls(
            selected_elements=tuple(str(symbol).strip() for symbol in selected_elements if str(symbol).strip()),
            database_label=database_label,
            database_path=database_path,
            ionization_levels=ionization_levels,
            tolerance_nm=tolerance_nm,
            peak_window_nm=peak_window_nm,
            detected_threshold=float(preset_values["detected_threshold"]),
            uncertain_threshold=float(preset_values["uncertain_threshold"]),
            min_line_z=float(preset_values["min_line_z"]),
            min_line_snr=float(preset_values["min_line_snr"]),
            require_multi_line_agreement=bool(preset_values.get("require_multi_line_agreement", require_multi_line_agreement)),
            spatial_smoothing_strength=float(preset_values["spatial_smoothing_strength"]),
            preset=preset if preset in MULTI_ELEMENT_DETECTION_PRESETS else "Normal",
            view=view,
            detail_element=detail_element,
            candidate_pruning_enabled=candidate_pruning_enabled,
            max_candidate_lines=max_candidate_lines,
            min_prominence_sigma=min_prominence_sigma,
            atmospheric_filter_enabled=atmospheric_filter_enabled,
        )


class MappingAnalysisCancelled(Exception):
    """Raised when a long mapping-analysis job is cancelled by the UI."""


ProgressCallback = Callable[[str, int, int, str], None]
BinarySpectrumArrays = tuple[np.ndarray, np.ndarray, np.ndarray]


@dataclass
class MappingAnalysisCache:
    run_data: MappingRunData
    preprocess_config: MappingPreprocessConfig
    signature: str
    cache_dir: Path
    wavelengths: np.ndarray
    intensities: np.ndarray
    shot_table: pd.DataFrame
    diagnostics: pd.DataFrame
    cache_hit: bool = False
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def readable_shots(self) -> int:
        return int(self.intensities.shape[0])

    @property
    def stage_timings(self) -> dict[str, float]:
        value = self.manifest.get("stage_timings", {}) if isinstance(self.manifest, dict) else {}
        return value if isinstance(value, dict) else {}

    def close(self) -> None:
        mmap_obj = getattr(self.intensities, "_mmap", None)
        if mmap_obj is not None:
            try:
                mmap_obj.close()
            except OSError:
                pass


@dataclass(frozen=True)
class ElementDetectionEvidence:
    element_symbol: str
    point_key: str
    row_index: int
    column_index: int
    x_mm: float
    y_mm: float
    line_id: str
    wavelength: float
    ionization_level: int
    peak_area: float
    null_median: float
    null_mad: float
    robust_z: float
    empirical_p: float
    local_snr: float
    overlap_penalty: float
    shape_score: float
    alignment_error_nm: float
    line_confidence: float


@dataclass
class MultiElementMapResult:
    run_data: MappingRunData
    preprocess_config: MappingPreprocessConfig
    config: MultiElementMapConfig
    point_element_table: pd.DataFrame
    wide_point_table: pd.DataFrame
    line_evidence_table: pd.DataFrame
    element_grids: dict[str, np.ndarray]
    state_grids: dict[str, np.ndarray]
    x_coords: tuple[float, ...]
    y_coords: tuple[float, ...]
    x_edges: np.ndarray
    y_edges: np.ndarray
    summary_table: pd.DataFrame
    diagnostics: dict[str, Any] = field(default_factory=dict)
    element_colors: dict[str, str] = field(default_factory=dict)
    element_intensity_grids: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def is_multi_element(self) -> bool:
        return True

    @property
    def element_label(self) -> str:
        count = len(self.config.element_symbols)
        return f"{count} elements" if count != 1 else self.config.element_symbols[0]

    @property
    def accepted_line_count(self) -> int:
        if self.line_evidence_table.empty or "line_usable" not in self.line_evidence_table.columns:
            return 0
        usable = self.line_evidence_table[self.line_evidence_table["line_usable"]]
        return int(usable[["element_symbol", "line_id"]].drop_duplicates().shape[0])

    def manifest_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "detection_method_version": DETECTION_METHOD_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_folder": str(self.run_data.folder),
            "source_sample_label": self.run_data.sample_label,
            "source_manifest_summary": _manifest_summary(self.run_data.manifest),
            "preprocess_config": asdict(self.preprocess_config),
            "multi_element_config": asdict(self.config),
            "selected_elements": list(self.config.element_symbols),
            "points_total": int(self.run_data.points_total),
            "point_element_rows": int(len(self.point_element_table)),
            "accepted_line_count": self.accepted_line_count,
            "summary": self.summary_table.to_dict(orient="records"),
            "diagnostics": dict(self.diagnostics),
        }


def _manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    plan = manifest.get("plan") if isinstance(manifest, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    summary = plan.get("summary") if isinstance(plan.get("summary"), dict) else {}
    config = plan.get("config") if isinstance(plan.get("config"), dict) else {}
    return {
        "run_type": manifest.get("run_type") or plan.get("run_type"),
        "run_mode": manifest.get("run_mode"),
        "experiment_name": plan.get("experiment_name") or config.get("experiment_name"),
        "rows": summary.get("rows"),
        "columns": summary.get("columns"),
        "points": summary.get("points"),
        "targets_total": manifest.get("targets_total") or summary.get("target_count"),
    }


def _load_manifest(folder: Path) -> dict[str, Any]:
    manifest_path = folder / MAPPING_MANIFEST_FILENAME
    if not manifest_path.exists():
        return {}
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _sample_label_from_manifest(folder: Path, manifest: dict[str, Any]) -> str:
    plan = manifest.get("plan") if isinstance(manifest, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    config = plan.get("config") if isinstance(plan.get("config"), dict) else {}
    for value in (
        plan.get("experiment_name"),
        config.get("experiment_name"),
        manifest.get("experiment_name") if isinstance(manifest, dict) else None,
    ):
        text = str(value or "").strip()
        if text:
            return text
    return folder.name


def _resolve_spectrum_path(folder: Path, row: pd.Series) -> Path:
    raw_filepath = str(row.get("filepath", "") or "").strip()
    filename = str(row.get("filename", "") or "").strip()

    if raw_filepath:
        candidate = Path(raw_filepath)
        if candidate.exists():
            return candidate
        if not filename:
            filename = candidate.name

    if filename:
        candidate = folder / filename
        if candidate.exists():
            return candidate
        return candidate

    return folder / raw_filepath


def _row_storage_kind(row: pd.Series | dict[str, Any]) -> str:
    try:
        value = row.get("storage_kind", "")
    except AttributeError:
        value = ""
    return str(value or "").strip().lower()


def _row_binary_index(row: pd.Series | dict[str, Any]) -> int | None:
    try:
        value = row.get("binary_row_index", "")
    except AttributeError:
        value = ""
    if value is None or value == "":
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_uses_binary_store(row: pd.Series | dict[str, Any]) -> bool:
    return _row_storage_kind(row) == "binary_npy" and _row_binary_index(row) is not None


def _binary_source_uri(folder: Path, row: pd.Series | dict[str, Any]) -> str:
    row_index = _row_binary_index(row)
    return f"{mapping_spectrum_store_dir(folder) / 'intensities.npy'}#{row_index}"


def _load_binary_spectrum_arrays(folder: Path) -> BinarySpectrumArrays | None:
    store_dir = mapping_spectrum_store_dir(folder)
    wavelengths_path = store_dir / "wavelengths.npy"
    intensities_path = store_dir / "intensities.npy"
    mask = load_binary_written_mask(folder)
    if mask is None or not wavelengths_path.exists() or not intensities_path.exists():
        return None
    try:
        wavelengths = np.load(wavelengths_path, allow_pickle=False)
        intensities = np.load(intensities_path, mmap_mode="r", allow_pickle=False)
    except Exception:
        return None
    return wavelengths, intensities, mask


def _close_binary_spectrum_arrays(binary_arrays: BinarySpectrumArrays | None) -> None:
    if binary_arrays is None:
        return
    _wavelengths, intensities, _mask = binary_arrays
    mmap = getattr(intensities, "_mmap", None)
    if mmap is not None:
        try:
            mmap.close()
        except Exception:
            pass


def load_mapping_run(folder: str | os.PathLike[str]) -> MappingRunData:
    run_folder = Path(folder)
    index_path = run_folder / MAPPING_INDEX_FILENAME
    if not run_folder.exists() or not run_folder.is_dir():
        raise FileNotFoundError(f"Mapping folder does not exist: {run_folder}")
    if not index_path.exists():
        raise ValueError(f"Mapping folder is missing {MAPPING_INDEX_FILENAME}.")

    index = pd.read_csv(index_path)
    missing_columns = sorted(REQUIRED_INDEX_COLUMNS.difference(index.columns))
    if missing_columns:
        raise ValueError(f"Mapping index is missing required column(s): {', '.join(missing_columns)}")

    index = index.copy()
    # Acquisition appends rows for fired-but-unmatched stream shots
    # (exclude_reason="stream_frame_mismatch") with no shot_index and no
    # stored spectrum; they document craters, not loadable data. Drop them
    # before numeric validation or one discarded segment fails the whole run.
    if "exclude_reason" in index.columns:
        excluded = index["exclude_reason"].fillna("").astype(str).str.strip() != ""
        index = index.loc[~excluded].copy()
    for column in ("row_index", "column_index", "shot_number", "shot_index"):
        index[column] = pd.to_numeric(index[column], errors="coerce")
    for column in ("x_mm", "y_mm", "save_ms", "integration_time_us"):
        if column in index.columns:
            index[column] = pd.to_numeric(index[column], errors="coerce")

    required_numeric = ["row_index", "column_index", "shot_number", "shot_index", "x_mm", "y_mm"]
    invalid_numeric = [column for column in required_numeric if index[column].isna().any()]
    if invalid_numeric:
        raise ValueError(f"Mapping index has invalid numeric value(s): {', '.join(invalid_numeric)}")

    for column in ("row_index", "column_index", "shot_number", "shot_index"):
        index[column] = index[column].astype(int)
    index["point_key"] = index["point_key"].fillna("").astype(str)
    empty_point = index["point_key"].str.strip() == ""
    if empty_point.any():
        index.loc[empty_point, "point_key"] = index.loc[empty_point].apply(
            lambda row: f"R{int(row['row_index']):03d}:C{int(row['column_index']):03d}",
            axis=1,
        )
    index["target_key"] = index["target_key"].fillna("").astype(str)
    empty_target = index["target_key"].str.strip() == ""
    if empty_target.any():
        index.loc[empty_target, "target_key"] = index.loc[empty_target].apply(
            lambda row: f"{row['point_key']}:S{int(row['shot_number']):02d}",
            axis=1,
        )

    resolved = [_resolve_spectrum_path(run_folder, row) for _, row in index.iterrows()]
    csv_exists = [path.exists() for path in resolved]
    binary_mask = load_binary_written_mask(run_folder)
    binary_available = [
        binary_row_is_written(run_folder, _row_binary_index(row), binary_mask) if _row_uses_binary_store(row) else False
        for _, row in index.iterrows()
    ]
    index["resolved_filepath"] = [str(path) for path in resolved]
    index["csv_file_exists"] = csv_exists
    index["binary_available"] = binary_available
    index["source_uri"] = [
        _binary_source_uri(run_folder, row) if available else str(path)
        for available, path, (_, row) in zip(binary_available, resolved, index.iterrows())
    ]
    index["file_exists"] = [bool(csv_ok or binary_ok) for csv_ok, binary_ok in zip(csv_exists, binary_available)]
    index["captured_at_dt"] = pd.to_datetime(index["captured_at"], errors="coerce")
    index = index.sort_values(["row_index", "column_index", "shot_number", "shot_index"], kind="stable").reset_index(drop=True)

    row_positions = index.drop_duplicates("row_index").sort_values("row_index")
    column_positions = index.drop_duplicates("column_index").sort_values("column_index")
    row_indices = tuple(int(value) for value in row_positions["row_index"].tolist())
    column_indices = tuple(int(value) for value in column_positions["column_index"].tolist())
    y_coords = tuple(float(value) for value in row_positions["y_mm"].tolist())
    x_coords = tuple(float(value) for value in column_positions["x_mm"].tolist())

    manifest = _load_manifest(run_folder)
    store_manifest = mapping_spectrum_store_manifest_path(run_folder)
    if store_manifest.exists():
        try:
            with store_manifest.open("r", encoding="utf-8") as handle:
                manifest["spectrum_store"] = json.load(handle)
        except Exception:
            manifest["spectrum_store"] = {"load_error": str(store_manifest)}
    storage_summary = {
        "storage_kind": "mixed" if any(binary_available) and any(csv_exists) else ("binary_npy" if any(binary_available) else "csv"),
        "spectra_total": int(len(index)),
        "resolved_spectra": int(index["file_exists"].sum()),
        "csv_backed_spectra": int(sum(csv_exists)),
        "binary_backed_spectra": int(sum(binary_available)),
        "missing_spectra": int((~index["file_exists"]).sum()),
        "missing_binary_spectra": int(
            (
                (index.get("storage_kind", pd.Series("", index=index.index)).fillna("").astype(str).str.lower() == "binary_npy")
                & ~index["binary_available"].astype(bool)
            ).sum()
        ),
        "missing_csv_spectra": int(
            (
                (
                    index.get("storage_kind", pd.Series("", index=index.index)).fillna("").astype(str).str.lower().isin(("", "csv"))
                )
                & ~index["csv_file_exists"].astype(bool)
            ).sum()
        ),
    }
    return MappingRunData(
        folder=run_folder,
        index=index,
        manifest=manifest,
        sample_label=_sample_label_from_manifest(run_folder, manifest),
        grid_rows=len(row_indices),
        grid_columns=len(column_indices),
        row_indices=row_indices,
        column_indices=column_indices,
        x_coords=x_coords,
        y_coords=y_coords,
        missing_spectra=int((~index["file_exists"]).sum()),
        resolved_spectra=int(index["file_exists"].sum()),
        storage_summary=storage_summary,
    )


def materialize_mapping_spectra_to_csv(
    run_data: MappingRunData,
    output_dir: str | os.PathLike[str] | None = None,
    *,
    row_indices: Sequence[int] | None = None,
) -> list[Path]:
    """Write selected mapping spectra to standard CSV/TSV files for debug or external tools."""
    destination = Path(output_dir) if output_dir is not None else run_data.folder / "_materialized_spectra"
    if row_indices is None:
        selected = run_data.index.index.tolist()
    else:
        selected = [int(item) for item in row_indices]
    written: list[Path] = []
    for row_number in selected:
        row = run_data.index.iloc[row_number]
        filename = str(row.get("filename") or f"mapping_spectrum_{row_number + 1:06d}.csv")
        output_path = destination / filename
        if _row_uses_binary_store(row):
            binary_index = _row_binary_index(row)
            if binary_index is None or not binary_row_is_written(run_data.folder, binary_index):
                continue
            wavelengths, intensities = read_binary_mapping_spectrum(run_data.folder, binary_index)
        else:
            source = Path(str(row.get("resolved_filepath") or ""))
            if not source.exists():
                continue
            wavelengths, intensities = read_spectrum_file(source)
        written.append(write_spectrum_file(output_path, wavelengths, intensities))
    return written


def _load_background_reference(config: MappingPreprocessConfig) -> tuple[np.ndarray, np.ndarray] | None:
    if not config.background_enabled:
        return None
    path_text = str(config.background_reference_path or "").strip()
    if not path_text:
        return None
    wavelengths, intensities = read_spectrum_file(path_text)
    wavelengths = np.asarray(wavelengths, dtype=float)
    intensities = np.asarray(intensities, dtype=float)
    valid = np.isfinite(wavelengths) & np.isfinite(intensities)
    wavelengths = wavelengths[valid]
    intensities = intensities[valid]
    if wavelengths.size == 0:
        raise ValueError(f"Background reference has no numeric spectrum rows: {path_text}")
    order = np.argsort(wavelengths, kind="stable")
    return wavelengths[order], intensities[order]


def _first_run_wavelength_axis(run_data: MappingRunData) -> np.ndarray | None:
    """Load the wavelength axis of the first available shot in the run."""
    for _, row in run_data.index.iterrows():
        try:
            if _row_uses_binary_store(row):
                binary_index = _row_binary_index(row)
                if binary_index is None or not binary_row_is_written(run_data.folder, binary_index):
                    continue
                wavelengths, _ = read_binary_mapping_spectrum(run_data.folder, binary_index)
            else:
                source = Path(str(row.get("resolved_filepath") or ""))
                if not source.exists():
                    continue
                wavelengths, _ = read_spectrum_file(source)
        except Exception:
            continue
        wavelengths = np.asarray(wavelengths, dtype=float)
        wavelengths = wavelengths[np.isfinite(wavelengths)]
        if wavelengths.size:
            return wavelengths
    return None


def validate_background_reference(run_data: MappingRunData, path: str) -> list[str]:
    """Check a background reference against the mapping run's acquisition axis.

    Returns human-readable warnings (empty list when everything matches).
    Mismatches do not block subtraction — the caller decides how to surface
    them — but silent interpolation over a mismatched axis or a different
    integration time makes the subtraction questionable.
    """
    warnings: list[str] = []
    path_text = str(path or "").strip()
    if not path_text:
        return warnings
    if not os.path.isfile(path_text):
        return [f"Background reference file not found: {path_text}"]
    try:
        ref_wavelengths, _ = read_spectrum_file(path_text)
        ref_wavelengths = np.asarray(ref_wavelengths, dtype=float)
        ref_wavelengths = ref_wavelengths[np.isfinite(ref_wavelengths)]
    except Exception as exc:
        return [f"Background reference could not be read: {exc}"]
    if ref_wavelengths.size == 0:
        return [f"Background reference has no numeric spectrum rows: {path_text}"]

    run_wavelengths = _first_run_wavelength_axis(run_data)
    if run_wavelengths is not None and run_wavelengths.size:
        if ref_wavelengths.size != run_wavelengths.size:
            warnings.append(
                "Background reference pixel count differs from the mapping run "
                f"({ref_wavelengths.size} vs {run_wavelengths.size} points)."
            )
        run_min, run_max = float(run_wavelengths.min()), float(run_wavelengths.max())
        ref_min, ref_max = float(ref_wavelengths.min()), float(ref_wavelengths.max())
        if ref_min > run_min + 0.5 or ref_max < run_max - 0.5:
            warnings.append(
                "Background reference wavelength range "
                f"({ref_min:.1f}-{ref_max:.1f} nm) does not cover the run range "
                f"({run_min:.1f}-{run_max:.1f} nm); edges would be flat-extrapolated."
            )

    # Compare integration times when the run recorded a reference capture.
    reference_info = {}
    manifest_block = run_data.manifest.get("background_reference")
    if isinstance(manifest_block, dict):
        reference_info = manifest_block
    ref_integration = reference_info.get("integration_time_us")
    if ref_integration is not None and "integration_time_us" in run_data.index.columns:
        run_integrations = (
            pd.to_numeric(run_data.index["integration_time_us"], errors="coerce").dropna().unique()
        )
        try:
            ref_integration = float(ref_integration)
        except (TypeError, ValueError):
            ref_integration = None
        if ref_integration is not None and run_integrations.size:
            if not any(abs(float(value) - ref_integration) < 1.0 for value in run_integrations):
                run_text = ", ".join(f"{float(value):g}" for value in run_integrations[:4])
                warnings.append(
                    "Background reference integration time "
                    f"({ref_integration:g} us) differs from the run's shots ({run_text} us)."
                )
    return warnings


def _preprocess_spectrum(
    wavelengths: np.ndarray,
    intensities: np.ndarray,
    config: MappingPreprocessConfig,
    background_reference: tuple[np.ndarray, np.ndarray] | None = None,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    wavelengths = np.asarray(wavelengths, dtype=float)
    values = np.asarray(intensities, dtype=float)
    sample_size = min(wavelengths.size, values.size)
    wavelengths = wavelengths[:sample_size]
    values = values[:sample_size]
    valid = np.isfinite(wavelengths) & np.isfinite(values)
    wavelengths = wavelengths[valid]
    values = values[valid]
    order = np.argsort(wavelengths, kind="stable")
    wavelengths = wavelengths[order]
    values = values[order]

    diagnostics = {
        "raw_total": float(np.nansum(np.clip(values, 0, None))) if values.size else 0.0,
        "background_subtracted": False,
        "smoothing_applied": False,
        "baseline_applied": False,
        "normalization": config.normalization,
    }

    processed = values.astype(float, copy=True)
    if config.background_enabled and processed.size:
        try:
            if background_reference is None:
                background_reference = _load_background_reference(config)
            if background_reference is not None:
                bg_wavelengths, bg_intensities = background_reference
                background = np.interp(
                    wavelengths,
                    np.asarray(bg_wavelengths, dtype=float),
                    np.asarray(bg_intensities, dtype=float),
                    left=float(bg_intensities[0]),
                    right=float(bg_intensities[-1]),
                )
                processed = processed - background
                diagnostics["background_subtracted"] = True
                diagnostics["background_reference_path"] = str(config.background_reference_path or "")
        except Exception as exc:
            diagnostics["background_error"] = str(exc)

    if config.smoothing_enabled and processed.size >= 11 and config.smoothing_strength > 0:
        try:
            processed = apply_smoothing(
                processed,
                config.smoothing_method,
                int(config.smoothing_strength),
                clip_negative=False,
            )
            diagnostics["smoothing_applied"] = True
        except Exception as exc:
            diagnostics["smoothing_error"] = str(exc)

    if config.baseline_enabled and processed.size >= 3:
        try:
            processed = apply_baseline_removal(
                processed,
                lam=float(config.baseline_lam),
                p=float(config.baseline_p),
                niter=max(1, int(config.baseline_niter)),
                clip=False,
            )
            diagnostics["baseline_applied"] = True
        except Exception as exc:
            diagnostics["baseline_error"] = str(exc)

    diagnostics["pre_normalization_total"] = float(np.nansum(np.clip(processed, 0, None))) if processed.size else 0.0

    if config.normalization == "Total Intensity":
        # Scale by the positive-part total (same scale as normalize_total_intensity)
        # but keep negative noise so downstream MAD/null statistics stay symmetric.
        positive_total = float(np.nansum(np.clip(processed, 0, None)))
        processed = processed / (positive_total + 1e-12)
    elif config.normalization == "Air-plasma reference":
        # Divide by the O I 777 net area: the ambient air is chemically
        # identical at every point, so this line is a per-shot internal
        # standard for laser-plasma coupling. Total intensity cannot do this —
        # a stronger air plasma inflates the total and *suppresses* the
        # sample's share. Falls back to total intensity when the air line is
        # absent (e.g. vacuum/purged measurements).
        reference_area, _, _ = _net_line_area(
            wavelengths, processed, AIR_REFERENCE_LINE_NM, AIR_REFERENCE_HALF_WINDOW_NM
        )
        if np.isfinite(reference_area) and reference_area > EPS:
            processed = processed / reference_area
            diagnostics["air_reference_area"] = float(reference_area)
        else:
            positive_total = float(np.nansum(np.clip(processed, 0, None)))
            processed = processed / (positive_total + 1e-12)
            diagnostics["air_reference_fallback"] = "total_intensity"
    elif config.normalization == "Min-Max":
        processed = normalize_min_max(processed)
    elif config.normalization == "None":
        processed = processed.astype(float, copy=True)
    else:
        raise ValueError(f"Unsupported mapping normalization: {config.normalization}")

    diagnostics["processed_total"] = float(np.nansum(np.clip(processed, 0, None))) if processed.size else 0.0
    return wavelengths, processed, diagnostics


def preprocess_spectrum(
    wavelengths: Sequence[float],
    intensities: Sequence[float],
    config: MappingPreprocessConfig | None = None,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Apply mapping preprocessing defaults to one spectrum."""
    resolved_config = config or MappingPreprocessConfig()
    return _preprocess_spectrum(
        np.asarray(wavelengths, dtype=float),
        np.asarray(intensities, dtype=float),
        resolved_config,
        _load_background_reference(resolved_config),
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return str(value)


def _file_signature(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _mapping_cache_root(run_data: MappingRunData) -> Path:
    return run_data.folder / MAPPING_ANALYSIS_CACHE_DIRNAME


def mapping_analysis_cache_size(run_data: MappingRunData) -> int:
    root = _mapping_cache_root(run_data)
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                total += int(path.stat().st_size)
            except OSError:
                pass
    return total


def clear_mapping_analysis_cache(run_data: MappingRunData) -> int:
    root = _mapping_cache_root(run_data)
    size = mapping_analysis_cache_size(run_data)
    if not root.exists():
        return 0
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass
    return size


def mapping_analysis_cache_summary(run_data: MappingRunData | None) -> dict[str, Any]:
    if run_data is None:
        return {"exists": False, "size_bytes": 0, "entries": 0, "updated_at": ""}
    root = _mapping_cache_root(run_data)
    if not root.exists():
        return {"exists": False, "size_bytes": 0, "entries": 0, "updated_at": ""}
    mtimes: list[float] = []
    entries = 0
    for path in root.rglob("*"):
        if path.is_file():
            entries += 1
            try:
                mtimes.append(path.stat().st_mtime)
            except OSError:
                pass
    updated_at = datetime.fromtimestamp(max(mtimes)).isoformat(timespec="seconds") if mtimes else ""
    return {
        "exists": True,
        "size_bytes": mapping_analysis_cache_size(run_data),
        "entries": entries,
        "updated_at": updated_at,
    }


def _config_database_signature(config: ElementMapConfig | MultiElementMapConfig) -> dict[str, Any]:
    try:
        path = Path(_database_path(config))
    except Exception:
        return {"path": "", "exists": False}
    return _file_signature(path)


def _analysis_result_signature(
    run_data: MappingRunData,
    preprocess_config: MappingPreprocessConfig,
    config: ElementMapConfig | MultiElementMapConfig,
    result_type: str,
) -> str:
    analysis_config = asdict(config)
    for display_only_key in ("colormap", "color_min", "color_max", "view", "detail_element"):
        analysis_config.pop(display_only_key, None)
    payload = {
        "version": 6,
        "method_version": DETECTION_METHOD_VERSION,
        "result_type": result_type,
        "preprocess_signature": _preprocess_cache_signature(run_data, preprocess_config),
        "preprocess_config": asdict(preprocess_config),
        "analysis_config": analysis_config,
        "database": _config_database_signature(config),
        # A result is assembled from the line-feature and candidate-scan stages,
        # so it must be keyed by their signatures too. Without this, fixing a
        # bug in an upstream key still returned the result computed from the
        # bad cache: the upstream stage rebuilt correctly, and the stale result
        # was handed back before that rebuild was ever consulted.
        "line_feature_signature": _line_feature_cache_signature(
            run_data, preprocess_config, config),
        "candidate_scan_signature": (
            _candidate_scan_cache_signature(run_data, preprocess_config, config)
            if isinstance(config, MultiElementMapConfig) else ""
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _analysis_result_cache_dir(run_data: MappingRunData, signature: str) -> Path:
    return _mapping_cache_root(run_data) / MAPPING_ANALYSIS_RESULTS_DIRNAME / signature


def _safe_cache_token(value: str) -> str:
    token = "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in str(value).strip())
    return token or "cache"


def _line_feature_cache_signature(
    run_data: MappingRunData,
    preprocess_config: MappingPreprocessConfig,
    config: ElementMapConfig | MultiElementMapConfig,
) -> str:
    payload = {
        "version": 3,
        "method_version": DETECTION_METHOD_VERSION,
        "preprocess_signature": _preprocess_cache_signature(run_data, preprocess_config),
        "database": _config_database_signature(config),
        "ionization_levels": tuple(bool(value) for value in config.ionization_levels),
        "tolerance_nm": float(config.tolerance_nm),
        "peak_window_nm": float(config.peak_window_nm),
        "aggregate_shots": str(preprocess_config.aggregate_shots),
        # The sidebands set the local baseline and the noise scale, so they
        # change peak_area/noise_area/robust_z in this very table. Leaving them
        # out let two sideband geometries share one cache directory, where
        # whichever ran last silently rewrote the other's features.
        "sideband_inner_nm": float(getattr(config, "sideband_inner_nm", 0.0) or 0.0),
        "sideband_outer_nm": float(getattr(config, "sideband_outer_nm", 0.0) or 0.0),
    }
    encoded = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _line_feature_cache_dir(
    run_data: MappingRunData,
    preprocess_config: MappingPreprocessConfig,
    config: ElementMapConfig | MultiElementMapConfig,
) -> Path:
    signature = _line_feature_cache_signature(run_data, preprocess_config, config)
    return _mapping_cache_root(run_data) / MAPPING_ANALYSIS_LINE_FEATURES_DIRNAME / signature


def _candidate_scan_cache_signature(
    run_data: MappingRunData,
    preprocess_config: MappingPreprocessConfig,
    config: MultiElementMapConfig,
) -> str:
    payload = {
        "version": 5,
        "method_version": DETECTION_METHOD_VERSION,
        "preprocess_signature": _preprocess_cache_signature(run_data, preprocess_config),
        "database": _config_database_signature(config),
        "selected_elements": tuple(config.element_symbols),
        "ionization_levels": tuple(bool(value) for value in config.ionization_levels),
        "tolerance_nm": float(config.tolerance_nm),
        "peak_window_nm": float(config.peak_window_nm),
        "aggregate_shots": str(preprocess_config.aggregate_shots),
        "candidate_pruning_enabled": bool(config.candidate_pruning_enabled),
        "max_candidate_lines": int(config.max_candidate_lines),
        "min_prominence_sigma": float(config.min_prominence_sigma),
        "atmospheric_filter_enabled": bool(config.atmospheric_filter_enabled),
        "strong_line_exclusion_nm": float(getattr(config, "strong_line_exclusion_nm", 0.0) or 0.0),
        "sideband_inner_nm": float(getattr(config, "sideband_inner_nm", 0.0) or 0.0),
        "sideband_outer_nm": float(getattr(config, "sideband_outer_nm", 0.0) or 0.0),
        # Changes which database lines count as overlap threats, which feeds
        # the pruning order - so it changes the cached scan itself.
        "overlap_significance_ratio": float(
            getattr(config, "overlap_significance_ratio", OVERLAP_SIGNIFICANCE_RATIO)),
    }
    encoded = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _candidate_scan_cache_dir(
    run_data: MappingRunData,
    preprocess_config: MappingPreprocessConfig,
    config: MultiElementMapConfig,
) -> Path:
    signature = _candidate_scan_cache_signature(run_data, preprocess_config, config)
    return _mapping_cache_root(run_data) / MAPPING_ANALYSIS_CANDIDATE_SCANS_DIRNAME / signature


def _candidate_scan_cache_path(cache_dir: Path, element: str) -> Path:
    return cache_dir / f"{_safe_cache_token(element)}_candidates.csv"


def _normalize_cached_candidate_scan(candidates: pd.DataFrame) -> pd.DataFrame:
    normalized = candidates.copy()
    for column in (
        "is_overlapped",
        "overlaps_selected_element",
        "line_usable",
        "candidate_selected",
        "atmospheric_excluded",
        "measurable",
    ):
        if column in normalized.columns:
            normalized[column] = normalized[column].map(
                lambda value: str(value).strip().lower() in {"true", "1", "yes"}
            )
    for column in (
        "Wavelength",
        "database_intensity",
        "database_intensity_norm",
        "measured_snr",
        "p95_intensity",
        "median_intensity",
        "raw_weight",
        "measured_priority",
        "overlap_penalty",
        "positive_point_fraction",
        "spatial_consistency",
        "noise_floor",
        "prominence_p95",
    ):
        if column in normalized.columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    if "Ionization Level" in normalized.columns:
        normalized["Ionization Level"] = pd.to_numeric(normalized["Ionization Level"], errors="coerce").fillna(0).astype(int)
    return normalized


def _line_feature_cache_path(cache_dir: Path, line: pd.Series) -> Path:
    line_id = str(line.get("line_id", "line"))
    wavelength = float(line.get("Wavelength", line.get("wavelength", 0.0)))
    digest = hashlib.sha256(f"{line_id}:{wavelength:.8f}".encode("utf-8")).hexdigest()[:12]
    return cache_dir / f"{_safe_cache_token(line_id)}_{digest}.pkl"


def _read_result_manifest(cache_dir: Path) -> dict[str, Any] | None:
    manifest_path = cache_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _load_cached_element_result(
    run_data: MappingRunData,
    preprocess_config: MappingPreprocessConfig,
    element_config: ElementMapConfig,
    signature: str,
) -> ElementMapResult | None:
    cache_dir = _analysis_result_cache_dir(run_data, signature)
    manifest = _read_result_manifest(cache_dir)
    if not manifest or manifest.get("result_type") != "single":
        return None
    try:
        point_table = pd.read_csv(cache_dir / "point_table.csv")
        grid_matrix = pd.read_csv(cache_dir / "grid_matrix.csv", index_col=0)
        line_scores = pd.read_csv(cache_dir / "line_scores.csv")
        value_grid = np.load(cache_dir / "value_grid.npy", allow_pickle=False)
    except Exception:
        return None
    diagnostics = manifest.get("diagnostics", {})
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    diagnostics.update(
        {
            "result_cache_hit": True,
            "result_cache_signature": signature,
            "result_cache_dir": str(cache_dir),
        }
    )
    return ElementMapResult(
        run_data=run_data,
        preprocess_config=preprocess_config,
        element_config=element_config,
        point_table=point_table,
        grid_matrix=grid_matrix,
        line_scores=line_scores,
        value_grid=value_grid,
        x_coords=tuple(run_data.x_coords),
        y_coords=tuple(run_data.y_coords),
        x_edges=_coordinate_edges(run_data.x_coords),
        y_edges=_coordinate_edges(run_data.y_coords),
        diagnostics=diagnostics,
    )


def _save_element_result_cache(result: ElementMapResult, signature: str) -> None:
    cache_dir = _analysis_result_cache_dir(result.run_data, signature)
    cache_dir.mkdir(parents=True, exist_ok=True)
    result.point_table.to_csv(cache_dir / "point_table.csv", index=False)
    result.grid_matrix.to_csv(cache_dir / "grid_matrix.csv")
    result.line_scores.to_csv(cache_dir / "line_scores.csv", index=False)
    np.save(cache_dir / "value_grid.npy", np.asarray(result.value_grid, dtype=float), allow_pickle=False)
    manifest = {
        "version": 1,
        "result_type": "single",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "diagnostics": dict(result.diagnostics),
        "preprocess_config": asdict(result.preprocess_config),
        "element_config": asdict(result.element_config),
    }
    with (cache_dir / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True, default=_json_default)


def _load_cached_multi_result(
    run_data: MappingRunData,
    preprocess_config: MappingPreprocessConfig,
    config: MultiElementMapConfig,
    signature: str,
) -> MultiElementMapResult | None:
    cache_dir = _analysis_result_cache_dir(run_data, signature)
    manifest = _read_result_manifest(cache_dir)
    if not manifest or manifest.get("result_type") != "multi":
        return None
    try:
        point_element_table = pd.read_csv(cache_dir / "point_element_table.csv")
        wide_point_table = pd.read_csv(cache_dir / "wide_point_table.csv")
        line_evidence_table = pd.read_csv(cache_dir / "line_evidence_table.csv")
        summary_table = pd.read_csv(cache_dir / "summary_table.csv")
        grids_npz = np.load(cache_dir / "element_grids.npz", allow_pickle=False)
        element_grids = {element: grids_npz[element] for element in config.element_symbols if element in grids_npz.files}
    except Exception:
        return None
    state_grids = {
        element: _state_grid_from_points(
            point_element_table[point_element_table["element_symbol"] == element],
            run_data.row_indices,
            run_data.column_indices,
        )
        for element in config.element_symbols
    }
    element_intensity_grids: dict[str, np.ndarray] = {}
    if "normalized_intensity" in point_element_table.columns:
        element_intensity_grids = {
            element: _grid_from_value_column(
                point_element_table[point_element_table["element_symbol"] == element],
                run_data.row_indices,
                run_data.column_indices,
                "normalized_intensity",
            )
            for element in config.element_symbols
        }
    diagnostics = manifest.get("diagnostics", {})
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    diagnostics.update(
        {
            "result_cache_hit": True,
            "result_cache_signature": signature,
            "result_cache_dir": str(cache_dir),
        }
    )
    return MultiElementMapResult(
        run_data=run_data,
        preprocess_config=preprocess_config,
        config=config,
        point_element_table=point_element_table,
        wide_point_table=wide_point_table,
        line_evidence_table=line_evidence_table,
        element_grids=element_grids,
        state_grids=state_grids,
        x_coords=tuple(run_data.x_coords),
        y_coords=tuple(run_data.y_coords),
        x_edges=_coordinate_edges(run_data.x_coords),
        y_edges=_coordinate_edges(run_data.y_coords),
        summary_table=summary_table,
        diagnostics=diagnostics,
        element_colors=_element_color_map(config.element_symbols),
        element_intensity_grids=element_intensity_grids,
    )


def _save_multi_result_cache(result: MultiElementMapResult, signature: str) -> None:
    cache_dir = _analysis_result_cache_dir(result.run_data, signature)
    cache_dir.mkdir(parents=True, exist_ok=True)
    result.point_element_table.to_csv(cache_dir / "point_element_table.csv", index=False)
    result.wide_point_table.to_csv(cache_dir / "wide_point_table.csv", index=False)
    result.line_evidence_table.to_csv(cache_dir / "line_evidence_table.csv", index=False)
    result.summary_table.to_csv(cache_dir / "summary_table.csv", index=False)
    np.savez(
        cache_dir / "element_grids.npz",
        **{element: np.asarray(grid, dtype=float) for element, grid in result.element_grids.items()},
    )
    manifest = {
        "version": 1,
        "result_type": "multi",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "diagnostics": dict(result.diagnostics),
        "preprocess_config": asdict(result.preprocess_config),
        "multi_element_config": asdict(result.config),
    }
    with (cache_dir / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True, default=_json_default)


def _preprocess_cache_signature(run_data: MappingRunData, config: MappingPreprocessConfig) -> str:
    index_path = run_data.folder / MAPPING_INDEX_FILENAME
    store_dir = mapping_spectrum_store_dir(run_data.folder)
    background_path = Path(str(config.background_reference_path or "")) if config.background_reference_path else None
    payload = {
        "version": 5,
        "preprocess_config": asdict(config),
        "background_reference": _file_signature(background_path) if background_path is not None else {"path": "", "exists": False},
        "index": _file_signature(index_path),
        "store": {
            "manifest": _file_signature(store_dir / "manifest.json"),
            "wavelengths": _file_signature(store_dir / "wavelengths.npy"),
            "intensities": _file_signature(store_dir / "intensities.npy"),
            "written_mask": _file_signature(store_dir / "written_mask.npy"),
        },
        "spectra": [
            (
                {
                    "storage_kind": "binary_npy",
                    "binary_row_index": _row_binary_index(row),
                    "binary_available": bool(row.get("binary_available", False)),
                }
                if _row_uses_binary_store(row)
                else _file_signature(Path(str(row["resolved_filepath"])))
            )
            for _, row in run_data.index.sort_values(["row_index", "column_index", "shot_number", "shot_index"], kind="stable").iterrows()
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _cache_paths(cache_dir: Path) -> dict[str, Path]:
    return {
        "wavelengths": cache_dir / "wavelengths.npy",
        "intensities": cache_dir / "preprocessed_intensities.npy",
        "shot_table": cache_dir / "shot_table.csv",
        "diagnostics": cache_dir / "diagnostics.csv",
        "manifest": cache_dir / "manifest.json",
    }


def _resolve_parallel_workers(value: int) -> int:
    try:
        workers = int(value)
    except (TypeError, ValueError):
        workers = 0
    if workers <= 0:
        cpu_count = os.cpu_count() or 2
        return max(1, min(cpu_count - 1, 8))
    return max(1, workers)


def _resolve_chunk_size(value: int) -> int:
    try:
        chunk_size = int(value)
    except (TypeError, ValueError):
        chunk_size = 2048
    return max(1, chunk_size)


def _load_processed_cache(
    run_data: MappingRunData,
    config: MappingPreprocessConfig,
    signature: str,
    cache_dir: Path,
) -> MappingAnalysisCache | None:
    paths = _cache_paths(cache_dir)
    if not all(path.exists() for path in paths.values()):
        return None
    try:
        wavelengths = np.load(paths["wavelengths"], allow_pickle=False)
        intensities = np.load(paths["intensities"], mmap_mode="r", allow_pickle=False)
        shot_table = pd.read_csv(paths["shot_table"])
        diagnostics = pd.read_csv(paths["diagnostics"])
        with paths["manifest"].open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except Exception:
        return None
    if wavelengths.ndim != 1 or intensities.ndim != 2 or intensities.shape[1] != wavelengths.size:
        return None
    return MappingAnalysisCache(
        run_data=run_data,
        preprocess_config=config,
        signature=signature,
        cache_dir=cache_dir,
        wavelengths=wavelengths.astype(float, copy=False),
        intensities=intensities,
        shot_table=shot_table,
        diagnostics=diagnostics,
        cache_hit=True,
        manifest=manifest if isinstance(manifest, dict) else {},
    )


def _preprocess_mapping_file(
    row_position: int,
    row_info: dict[str, Any],
    resolved_path: Path,
    config: MappingPreprocessConfig,
    base_wavelengths: np.ndarray | None,
    run_folder: Path | None = None,
    binary_arrays: BinarySpectrumArrays | None = None,
    background_reference: tuple[np.ndarray, np.ndarray] | None = None,
) -> dict[str, Any]:
    storage_kind = _row_storage_kind(row_info)
    binary_index = _row_binary_index(row_info)
    source_exists = bool(row_info.get("file_exists", False))
    diagnostic_row = {
        "target_key": row_info.get("target_key", ""),
        "point_key": row_info.get("point_key", ""),
        "filename": row_info.get("filename", ""),
        "resolved_filepath": str(row_info.get("source_uri") or resolved_path),
        "file_exists": source_exists,
        "storage_kind": storage_kind or "csv",
        "binary_row_index": "" if binary_index is None else binary_index,
        "read_error": "",
        "interpolated": False,
    }
    if not source_exists:
        diagnostic_row["read_error"] = "missing_file"
        return {
            "row_position": row_position,
            "row_info": row_info,
            "wavelengths": None,
            "intensities": None,
            "diagnostics": diagnostic_row,
            "success": False,
        }
    try:
        if storage_kind == "binary_npy" and binary_index is not None:
            if binary_arrays is not None:
                binary_wavelengths, binary_intensities, binary_mask = binary_arrays
                if binary_index < 0 or binary_index >= binary_intensities.shape[0]:
                    raise IndexError(f"Mapping binary row index out of range: {binary_index}")
                if binary_index >= binary_mask.shape[0] or not bool(binary_mask[binary_index]):
                    raise ValueError(f"Mapping binary row is not marked written: {binary_index}")
                wavelengths = np.asarray(binary_wavelengths, dtype=np.float64)
                intensities = np.array(binary_intensities[binary_index, :], dtype=np.float32, copy=True)
            else:
                wavelengths, intensities = read_binary_mapping_spectrum(run_folder or resolved_path.parent, binary_index)
        else:
            wavelengths, intensities = read_spectrum_file(resolved_path)
        wavelengths, processed, processing_diag = _preprocess_spectrum(wavelengths, intensities, config, background_reference)
        diagnostic_row.update(processing_diag)
        if base_wavelengths is not None and (
            wavelengths.size != base_wavelengths.size
            or not np.allclose(wavelengths, base_wavelengths, rtol=0, atol=1e-6)
        ):
            processed = np.interp(base_wavelengths, wavelengths, processed, left=np.nan, right=np.nan)
            wavelengths = base_wavelengths
            diagnostic_row["interpolated"] = True
        return {
            "row_position": row_position,
            "row_info": row_info,
            "wavelengths": wavelengths,
            "intensities": np.asarray(processed, dtype=np.float32),
            "diagnostics": diagnostic_row,
            "success": True,
        }
    except Exception as exc:
        diagnostic_row["read_error"] = str(exc)
        return {
            "row_position": row_position,
            "row_info": row_info,
            "wavelengths": None,
            "intensities": None,
            "diagnostics": diagnostic_row,
            "success": False,
        }


def _air_reference_prescan(binary_arrays: BinarySpectrumArrays | None, sample_limit: int = 64) -> dict[str, Any]:
    """Decide once per run whether the O I 777 air line is reliable enough to
    normalize by. Mixing per-shot fallbacks with air-referenced shots puts two
    scales orders of magnitude apart into one map, so the decision must be
    global: gated acquisitions with a quenched air line use Total Intensity.
    """
    result: dict[str, Any] = {"reliable": True, "sampled": 0, "with_line": 0}
    if binary_arrays is None:
        # Legacy CSV runs predate gated acquisition; keep the configured mode.
        result["reason"] = "no_binary_store"
        return result
    wavelengths, intensities, mask = binary_arrays
    written = np.flatnonzero(np.asarray(mask, dtype=bool))
    if written.size == 0:
        result["reason"] = "no_written_rows"
        return result
    sample_rows = written[:: max(1, written.size // int(sample_limit))][: int(sample_limit)]
    wavelengths = np.asarray(wavelengths, dtype=float)
    with_line = 0
    for row_index in sample_rows:
        row = np.asarray(intensities[int(row_index)], dtype=float)
        area, _, noise_area = _net_line_area(
            wavelengths, row, AIR_REFERENCE_LINE_NM, AIR_REFERENCE_HALF_WINDOW_NM
        )
        if np.isfinite(area) and area > max(4.0 * noise_area, EPS):
            with_line += 1
    result["sampled"] = int(sample_rows.size)
    result["with_line"] = int(with_line)
    result["reliable"] = bool(sample_rows.size and with_line / sample_rows.size >= 0.95)
    if not result["reliable"]:
        result["reason"] = "air_line_missing_or_weak"
    return result


def _fit_wavelength_calibration(
    wavelengths: np.ndarray,
    mean_spectrum: np.ndarray,
    *,
    anchors: Sequence[float] = WAVELENGTH_CALIBRATION_ANCHORS_NM,
    search_window_nm: float = 0.8,
    max_offset_nm: float = 0.5,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Measure and remove the per-run wavelength offset using air-plasma anchors.

    Each anchor line's observed centroid is compared with its exact reference
    wavelength; a robust linear offset model (constant if fewer than three
    anchors survive) is subtracted from the axis. Returns the corrected axis
    and a diagnostics payload; the axis is returned unchanged when no anchor
    yields a trustworthy centroid.
    """
    wavelengths = np.asarray(wavelengths, dtype=float)
    spectrum = np.nan_to_num(np.asarray(mean_spectrum, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    diagnostics: dict[str, Any] = {"applied": False, "anchors": []}
    measured: list[tuple[float, float]] = []
    for anchor in anchors:
        window = (wavelengths >= anchor - search_window_nm) & (wavelengths <= anchor + search_window_nm)
        if int(window.sum()) < 5:
            continue
        x_values = wavelengths[window]
        segment = spectrum[window]
        edge_count = max(2, segment.size // 4)
        edges = np.concatenate([segment[:edge_count], segment[-edge_count:]])
        baseline = float(np.median(edges))
        noise = 1.4826 * float(np.median(np.abs(edges - baseline)))
        net = np.clip(segment - baseline, 0, None)
        prominence = float(net.max())
        if prominence <= max(4.0 * noise, EPS) or float(net.sum()) <= EPS:
            continue
        centroid = float(np.sum(x_values * net) / np.sum(net))
        offset = centroid - float(anchor)
        if abs(offset) > max_offset_nm:
            continue
        measured.append((float(anchor), offset))
        diagnostics["anchors"].append(
            {"anchor_nm": float(anchor), "observed_nm": centroid, "offset_nm": offset}
        )
    # A single anchor cannot be distinguished from a coincidental sample line
    # near the reference wavelength, and disagreeing anchors mean at least one
    # centroid is contaminated — require two consistent anchors to correct.
    if len(measured) < 2:
        diagnostics["reason"] = "insufficient_anchor_lines"
        return wavelengths, diagnostics
    spread = max(o for _, o in measured) - min(o for _, o in measured)
    if spread > 0.3:
        diagnostics["reason"] = "anchor_offsets_disagree"
        diagnostics["offset_spread_nm"] = float(spread)
        return wavelengths, diagnostics

    anchor_x = np.asarray([a for a, _ in measured], dtype=float)
    anchor_offset = np.asarray([o for _, o in measured], dtype=float)
    if anchor_x.size >= 3:
        slope, intercept = np.polyfit(anchor_x, anchor_offset, 1)
        residuals = anchor_offset - (slope * anchor_x + intercept)
        keep = np.abs(residuals) <= max(0.05, 2.0 * 1.4826 * float(np.median(np.abs(residuals))))
        if keep.sum() >= 3 and keep.sum() < anchor_x.size:
            slope, intercept = np.polyfit(anchor_x[keep], anchor_offset[keep], 1)
        # Interpolate only within the anchor span; outside it hold the edge
        # value — a linear dispersion model extrapolated hundreds of nm past
        # the anchors can overshoot the true offset and pull line windows off
        # their peaks at the spectrum edges.
        clamped = np.clip(wavelengths, float(anchor_x.min()), float(anchor_x.max()))
        model = slope * clamped + intercept
        diagnostics["model"] = {
            "kind": "linear_clamped",
            "slope": float(slope),
            "intercept_nm": float(intercept),
            "anchor_span_nm": [float(anchor_x.min()), float(anchor_x.max())],
        }
    else:
        constant = float(np.median(anchor_offset))
        model = np.full_like(wavelengths, constant)
        diagnostics["model"] = {"kind": "constant", "offset_nm": constant}
    diagnostics["applied"] = True
    diagnostics["median_offset_nm"] = float(np.median(anchor_offset))
    return wavelengths - model, diagnostics


def load_or_build_processed_mapping_run(
    run_data: MappingRunData,
    config: MappingPreprocessConfig,
    *,
    force_rebuild: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> MappingAnalysisCache:
    """Read, preprocess, normalize, and cache all readable spectra once."""
    started = time.perf_counter()
    stage_timings: dict[str, float] = {}
    signature = _preprocess_cache_signature(run_data, config)
    cache_dir = _mapping_cache_root(run_data) / signature
    if not force_rebuild:
        cache_load_started = time.perf_counter()
        cached = _load_processed_cache(run_data, config, signature, cache_dir)
        if cached is not None:
            cached.manifest.setdefault("stage_timings", {})
            if isinstance(cached.manifest["stage_timings"], dict):
                cached.manifest["stage_timings"]["preprocess_cache_load_seconds"] = float(time.perf_counter() - cache_load_started)
            if progress_callback:
                progress_callback("cache", cached.readable_shots, run_data.shots_total, "Using cached preprocessing")
            return cached

    if progress_callback:
        progress_callback("preprocessing", 0, run_data.shots_total, "Reading and preprocessing spectra")

    cache_dir.mkdir(parents=True, exist_ok=True)
    paths = _cache_paths(cache_dir)
    row_items: list[tuple[int, dict[str, Any], Path]] = [
        (position, row.to_dict(), Path(str(row["resolved_filepath"])))
        for position, (_, row) in enumerate(run_data.index.iterrows())
    ]
    background_reference = _load_background_reference(config)
    binary_arrays = _load_binary_spectrum_arrays(run_data.folder) if bool(run_data.index.get("binary_available", pd.Series(dtype=bool)).any()) else None
    air_prescan: dict[str, Any] | None = None
    if config.normalization == "Air-plasma reference":
        air_prescan = _air_reference_prescan(binary_arrays)
        if not air_prescan.get("reliable", True):
            config = replace(config, normalization="Total Intensity")
    first_result: dict[str, Any] | None = None
    first_index: int | None = None
    first_started = time.perf_counter()
    try:
        for index, (position, row_info, resolved_path) in enumerate(row_items):
            result = _preprocess_mapping_file(position, row_info, resolved_path, config, None, run_data.folder, binary_arrays, background_reference)
            if result["success"]:
                first_result = result
                first_index = index
                break
    except Exception:
        _close_binary_spectrum_arrays(binary_arrays)
        raise
    stage_timings["preprocess_first_spectrum_seconds"] = float(time.perf_counter() - first_started)
    if first_result is None or first_index is None:
        _close_binary_spectrum_arrays(binary_arrays)
        raise ValueError("No readable mapping spectra were found in this folder.")
    base_wavelengths = np.asarray(first_result["wavelengths"], dtype=float)
    existing_count = max(1, int(run_data.index["file_exists"].sum()))
    intensity_path = paths["intensities"]
    if intensity_path.exists():
        try:
            intensity_path.unlink()
        except OSError:
            pass
    intensities_writer = np.lib.format.open_memmap(
        intensity_path,
        mode="w+",
        dtype=np.float32,
        shape=(existing_count, base_wavelengths.size),
    )
    shot_rows: list[dict[str, Any]] = []
    diagnostics_by_position: dict[int, dict[str, Any]] = {}
    write_index = 0
    skipped_before_first = 0
    try:
        for index, (position, row_info, resolved_path) in enumerate(row_items[:first_index]):
            missing = _preprocess_mapping_file(position, row_info, resolved_path, config, base_wavelengths, run_data.folder, binary_arrays, background_reference)
            diagnostics_by_position[position] = missing["diagnostics"]
            skipped_before_first += 1
    except Exception:
        _close_binary_spectrum_arrays(binary_arrays)
        raise
    intensities_writer[write_index, :] = np.asarray(first_result["intensities"], dtype=np.float32)
    shot_rows.append(first_result["row_info"])
    diagnostics_by_position[int(first_result["row_position"])] = first_result["diagnostics"]
    write_index += 1

    remaining = row_items[first_index + 1 :]
    worker_count = _resolve_parallel_workers(config.parallel_workers)
    chunk_size = _resolve_chunk_size(config.chunk_size_spectra)
    processing_started = time.perf_counter()
    completed = first_index + 1

    def handle_result(result: dict[str, Any]) -> None:
        nonlocal write_index
        diagnostics_by_position[int(result["row_position"])] = result["diagnostics"]
        if result["success"]:
            if write_index >= intensities_writer.shape[0]:
                raise RuntimeError("Preprocessed spectrum count exceeded allocated cache size.")
            intensities_writer[write_index, :] = np.asarray(result["intensities"], dtype=np.float32)
            shot_rows.append(result["row_info"])
            write_index += 1

    try:
        if worker_count <= 1 or len(remaining) <= 1:
            for position, row_info, resolved_path in remaining:
                handle_result(_preprocess_mapping_file(position, row_info, resolved_path, config, base_wavelengths, run_data.folder, binary_arrays, background_reference))
                completed += 1
                if progress_callback and (completed == run_data.shots_total or completed % 25 == 0):
                    progress_callback("preprocessing", completed, run_data.shots_total, "Reading and preprocessing spectra")
        else:
            for chunk_start in range(0, len(remaining), chunk_size):
                chunk = remaining[chunk_start : chunk_start + chunk_size]
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = [
                        executor.submit(_preprocess_mapping_file, position, row_info, resolved_path, config, base_wavelengths, run_data.folder, binary_arrays, background_reference)
                        for position, row_info, resolved_path in chunk
                    ]
                    for future in as_completed(futures):
                        handle_result(future.result())
                        completed += 1
                        if progress_callback and (completed == run_data.shots_total or completed % 25 == 0):
                            progress_callback("preprocessing", completed, run_data.shots_total, "Reading and preprocessing spectra")
    finally:
        _close_binary_spectrum_arrays(binary_arrays)
    stage_timings["preprocess_read_process_seconds"] = float(time.perf_counter() - processing_started)

    del intensities_writer
    if write_index <= 0:
        raise ValueError("No readable mapping spectra were found in this folder.")
    if write_index < existing_count:
        trim_started = time.perf_counter()
        trim_path = cache_dir / "preprocessed_intensities_trimmed.npy"
        source = np.load(intensity_path, mmap_mode="r+", allow_pickle=False)
        trimmed = np.lib.format.open_memmap(trim_path, mode="w+", dtype=np.float32, shape=(write_index, base_wavelengths.size))
        for chunk_start in range(0, write_index, chunk_size):
            chunk_end = min(write_index, chunk_start + chunk_size)
            trimmed[chunk_start:chunk_end, :] = source[chunk_start:chunk_end, :]
        del source
        del trimmed
        try:
            intensity_path.unlink()
            trim_path.replace(intensity_path)
        except OSError:
            np.save(intensity_path, np.asarray(np.load(trim_path, mmap_mode="r", allow_pickle=False)), allow_pickle=False)
        stage_timings["preprocess_trim_seconds"] = float(time.perf_counter() - trim_started)

    diagnostics_rows = [
        diagnostics_by_position[position]
        for position in sorted(diagnostics_by_position)
    ]
    shot_table = pd.DataFrame(shot_rows).reset_index(drop=True)
    diagnostics = pd.DataFrame(diagnostics_rows)

    calibration_diagnostics: dict[str, Any] = {"enabled": bool(config.wavelength_calibration_enabled), "applied": False}
    if config.wavelength_calibration_enabled:
        calibration_started = time.perf_counter()
        stored = np.load(intensity_path, mmap_mode="r", allow_pickle=False)
        mean_spectrum = np.zeros(base_wavelengths.size, dtype=np.float64)
        for chunk_start in range(0, stored.shape[0], chunk_size):
            chunk_end = min(stored.shape[0], chunk_start + chunk_size)
            mean_spectrum += np.asarray(stored[chunk_start:chunk_end, :], dtype=np.float64).sum(axis=0)
        mean_spectrum /= max(stored.shape[0], 1)
        del stored
        base_wavelengths, fit_diagnostics = _fit_wavelength_calibration(base_wavelengths, mean_spectrum)
        calibration_diagnostics.update(fit_diagnostics)
        stage_timings["preprocess_wavelength_calibration_seconds"] = float(time.perf_counter() - calibration_started)

    np.save(paths["wavelengths"], base_wavelengths.astype(float, copy=False), allow_pickle=False)
    shot_table.to_csv(paths["shot_table"], index=False)
    diagnostics.to_csv(paths["diagnostics"], index=False)
    stage_timings["preprocess_total_seconds"] = float(time.perf_counter() - started)
    manifest = {
        "version": 3,
        "signature": signature,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_folder": str(run_data.folder),
        "preprocess_config": asdict(config),
        "spectra_total": int(run_data.shots_total),
        "spectra_readable": int(len(shot_table)),
        "storage_mode": "npy_memmap",
        "parallel_workers": int(worker_count),
        "chunk_size_spectra": int(chunk_size),
        "build_seconds": float(time.perf_counter() - started),
        "stage_timings": stage_timings,
        "wavelength_calibration": calibration_diagnostics,
        "normalization_effective": config.normalization,
        "air_reference_prescan": air_prescan,
    }
    with paths["manifest"].open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True, default=_json_default)
    if progress_callback:
        progress_callback("preprocessing", run_data.shots_total, run_data.shots_total, "Preprocessing cached")
    intensities_matrix = np.load(paths["intensities"], mmap_mode="r", allow_pickle=False)
    return MappingAnalysisCache(
        run_data=run_data,
        preprocess_config=config,
        signature=signature,
        cache_dir=cache_dir,
        wavelengths=base_wavelengths.astype(float, copy=False),
        intensities=intensities_matrix,
        shot_table=shot_table,
        diagnostics=diagnostics,
        cache_hit=False,
        manifest=manifest,
    )


def _processed_shots(run_data: MappingRunData, config: MappingPreprocessConfig) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    processed = load_or_build_processed_mapping_run(run_data, config)
    shots = [
        {
            "index": row.to_dict(),
            "wavelengths": processed.wavelengths,
            "intensities": processed.intensities[index].astype(float, copy=False),
            "diagnostics": {},
        }
        for index, (_, row) in enumerate(processed.shot_table.iterrows())
    ]
    return shots, processed.diagnostics.copy()


def _database_path(config: ElementMapConfig | MultiElementMapConfig) -> str:
    if config.database_path:
        return str(config.database_path)
    return get_database_path(config.database_label)


def _levels_to_include(config: ElementMapConfig | MultiElementMapConfig) -> list[int]:
    return [index + 1 for index, enabled in enumerate(config.ionization_levels) if enabled]


def _load_candidate_database(config: ElementMapConfig | MultiElementMapConfig) -> pd.DataFrame:
    element_df = load_element_database(_database_path(config))
    element_df["Wavelength"] = pd.to_numeric(element_df["Wavelength"], errors="coerce")
    element_df = element_df.dropna(subset=["Wavelength"]).copy()
    levels = _levels_to_include(config)
    if levels:
        element_df = element_df[element_df["Ionization Level"].isin(levels)].copy()
    return element_df


def _atmospheric_reference_lines(element_df: pd.DataFrame) -> np.ndarray:
    """Wavelengths of air-plasma emission: the curated list plus any database
    lines of atmospheric species at >=1e-3 of that species' strongest line."""
    lines: list[float] = list(ATMOSPHERIC_LINES_NM)
    if not element_df.empty and "Symbol" in element_df.columns:
        for species in ATMOSPHERIC_SPECIES:
            species_rows = element_df[element_df["Symbol"].astype(str) == species]
            if species_rows.empty:
                continue
            intensities = np.asarray(
                [_database_intensity(row) for _, row in species_rows.iterrows()], dtype=float
            )
            species_max = float(np.nanmax(intensities)) if intensities.size else 0.0
            if species_max <= 0:
                continue
            strong = intensities >= 1e-3 * species_max
            lines.extend(float(value) for value in species_rows["Wavelength"].to_numpy(dtype=float)[strong])
    return np.unique(np.asarray(lines, dtype=float))


def _candidate_lines_for_symbol(
    element_df: pd.DataFrame,
    symbol: str,
    wavelengths: np.ndarray,
    *,
    tolerance_nm: float,
    peak_window_nm: float,
    selected_symbols: Sequence[str] = (),
    atmospheric_filter: bool = False,
    strong_line_exclusion_nm: float = 0.0,
    overlap_significance_ratio: float = OVERLAP_SIGNIFICANCE_RATIO,
) -> pd.DataFrame:
    selected = element_df[element_df["Symbol"] == symbol].copy()
    if wavelengths.size:
        selected = selected[
            selected["Wavelength"].between(
                float(np.nanmin(wavelengths)) - peak_window_nm,
                float(np.nanmax(wavelengths)) + peak_window_nm,
            )
        ].copy()
    if selected.empty:
        return selected

    selected["_database_intensity_for_dedup"] = [
        _database_intensity(row)
        for _, row in selected.iterrows()
    ]
    dedup_bucket_nm = max(0.001, min(0.01, float(tolerance_nm) / 50.0))
    selected["_dedup_bucket"] = (
        (selected["Wavelength"].astype(float) / dedup_bucket_nm).round().astype(int)
    )
    selected = selected.sort_values(
        ["_database_intensity_for_dedup", "Wavelength"],
        ascending=[False, True],
        kind="stable",
    )
    selected["duplicate_database_line"] = selected.duplicated(
        subset=["Symbol", "Ionization Level", "_dedup_bucket"],
        keep="first",
    )
    selected = selected[~selected["duplicate_database_line"]].copy()

    selected_symbol_set = {str(item).strip() for item in selected_symbols if str(item).strip()}
    # An overlapping database row is only a real misattribution threat when it
    # is a significant line OF THE OTHER element (relative to that element's
    # strongest in-range line). Dense line lists (Fe, Ti, ...) put a faint line
    # within tolerance of almost any wavelength; flagging those pinned every
    # element's confidence at the overlap cap and flattened the maps.
    db_intensity = _database_intensity_series(element_df)
    db_wavelengths = pd.to_numeric(element_df["Wavelength"], errors="coerce").to_numpy(dtype=float)
    if wavelengths.size:
        in_range = (
            np.isfinite(db_wavelengths)
            & (db_wavelengths >= float(np.nanmin(wavelengths)) - float(peak_window_nm))
            & (db_wavelengths <= float(np.nanmax(wavelengths)) + float(peak_window_nm))
        )
    else:
        in_range = np.isfinite(db_wavelengths)
    symbol_peak = (
        pd.Series(np.where(in_range, db_intensity, np.nan), index=element_df.index)
        .groupby(element_df["Symbol"].astype(str))
        .transform("max")
        .to_numpy(dtype=float)
    )
    significant_overlap_source = np.zeros(len(element_df), dtype=bool)
    valid_peak = np.isfinite(symbol_peak) & (symbol_peak > 0)
    significant_overlap_source[valid_peak] = (
        db_intensity[valid_peak] >= float(overlap_significance_ratio) * symbol_peak[valid_peak]
    )
    overlap_symbols: list[str] = []
    overlap_flags: list[bool] = []
    selected_overlap_flags: list[bool] = []
    overlap_penalties: list[float] = []
    for _, candidate in selected.iterrows():
        diff = np.abs(element_df["Wavelength"] - float(candidate["Wavelength"]))
        overlaps = element_df[
            (diff <= float(tolerance_nm))
            & (element_df["Symbol"].astype(str) != str(candidate["Symbol"]))
            & significant_overlap_source
        ]
        symbol_set = set(overlaps["Symbol"].astype(str).tolist())
        symbols_text = ", ".join(sorted(symbol_set))
        selected_overlap = bool(symbol_set.intersection(selected_symbol_set.difference({symbol})))
        overlap_symbols.append(symbols_text)
        overlap_flags.append(bool(symbols_text))
        selected_overlap_flags.append(selected_overlap)
        if selected_overlap:
            overlap_penalties.append(0.55)
        elif symbols_text:
            overlap_penalties.append(0.35)
        else:
            overlap_penalties.append(0.0)

    selected["overlap_symbols"] = overlap_symbols
    selected["is_overlapped"] = overlap_flags
    selected["overlaps_selected_element"] = selected_overlap_flags
    selected["overlap_penalty"] = overlap_penalties
    selected["line_usable"] = True
    selected["candidate_reason"] = np.where(selected["is_overlapped"], "overlapped_retained", "candidate")

    selected["atmospheric_excluded"] = False
    if atmospheric_filter and symbol not in ATMOSPHERIC_SPECIES:
        atmospheric_lines = _atmospheric_reference_lines(element_df)
        if atmospheric_lines.size:
            candidate_wavelengths = selected["Wavelength"].to_numpy(dtype=float)
            min_distance = np.min(
                np.abs(candidate_wavelengths[:, None] - atmospheric_lines[None, :]), axis=1
            )
            # Exclude any candidate whose integration window reaches into the
            # atmospheric line's core: these strong air-plasma lines have
            # wings, so a window merely touching them inherits their signal.
            exclusion_radius = max(float(tolerance_nm), 0.3) + float(peak_window_nm)
            excluded = min_distance <= exclusion_radius
            selected["atmospheric_excluded"] = excluded
            selected.loc[excluded, "line_usable"] = False
            selected.loc[excluded, "candidate_reason"] = "atmospheric_overlap"

    # Strong-line exclusion. The overlap penalty above handles a line sitting on
    # another element's line, but a dominant line also contaminates its
    # neighbourhood well beyond `tolerance_nm`: on a copper coin the Ni lines at
    # 324.85/325.07 nm read the wing of Cu I 324.75 and invert the nickel map,
    # and on glaze the Ca II 393/397 wings bleed into everything nearby. Here a
    # candidate is dropped outright when its integration window reaches a
    # PRINCIPAL line of another selected element.
    selected["strong_line_excluded"] = False
    if strong_line_exclusion_nm > 0 and selected_symbol_set:
        others = selected_symbol_set.difference({symbol})
        principal: list[float] = []
        for other in others:
            rows = element_df["Symbol"].astype(str) == other
            if not rows.any():
                continue
            intensity = np.where(in_range & rows.to_numpy(), db_intensity, np.nan)
            finite = np.flatnonzero(np.isfinite(intensity) & (np.nan_to_num(intensity) > 0))
            if finite.size == 0:
                continue
            strongest = finite[np.argsort(intensity[finite])[::-1][:STRONG_LINE_DOMINANT_COUNT]]
            principal.extend(db_wavelengths[strongest].tolist())
        if principal:
            strong = np.asarray(sorted(set(principal)), dtype=float)
            candidate_wavelengths = selected["Wavelength"].to_numpy(dtype=float)
            distance = np.min(np.abs(candidate_wavelengths[:, None] - strong[None, :]), axis=1)
            radius = float(strong_line_exclusion_nm) + float(peak_window_nm)
            excluded = distance <= radius
            selected["strong_line_excluded"] = excluded
            selected.loc[excluded, "line_usable"] = False
            selected.loc[excluded, "candidate_reason"] = "strong_line_overlap"

    selected = selected.sort_values(
        ["is_overlapped", "_database_intensity_for_dedup", "Wavelength"],
        ascending=[True, False, True],
        kind="stable",
    )
    selected = selected.reset_index(drop=True)
    selected["line_id"] = [
        f"{symbol}_{float(wl):.4f}_{int(level)}"
        for wl, level in zip(selected["Wavelength"], selected["Ionization Level"])
    ]
    selected = selected.drop(columns=[column for column in ("_database_intensity_for_dedup", "_dedup_bucket") if column in selected.columns])
    return selected


def _candidate_lines(config: ElementMapConfig, wavelengths: np.ndarray) -> pd.DataFrame:
    symbols = config.element_symbols
    if len(symbols) != 1:
        raise ValueError("Select exactly one element for Single Element Map.")

    element_df = _load_candidate_database(config)
    selected = _candidate_lines_for_symbol(
        element_df,
        symbols[0],
        wavelengths,
        tolerance_nm=float(config.tolerance_nm),
        peak_window_nm=float(config.peak_window_nm),
        selected_symbols=symbols,
        atmospheric_filter=bool(config.atmospheric_filter_enabled),
        strong_line_exclusion_nm=float(getattr(config, "strong_line_exclusion_nm", 0.0) or 0.0),
        overlap_significance_ratio=float(
            getattr(config, "overlap_significance_ratio", OVERLAP_SIGNIFICANCE_RATIO)),
    )
    if selected.empty:
        raise ValueError(f"No spectral database lines found for {symbols[0]} in the loaded wavelength range.")
    return selected


def _database_intensity(row: pd.Series) -> float:
    # Prefer the relative-intensity columns; fall back to the Einstein A
    # coefficient only when neither is available, so lines within one element
    # are ranked on a single consistent metric.
    intensity_values = []
    for column in ("Intensity (long)", "Intensity (short)"):
        if column in row.index:
            value = pd.to_numeric(pd.Series([row[column]]), errors="coerce").iloc[0]
            if pd.notna(value) and float(value) > 0:
                intensity_values.append(float(value))
    if intensity_values:
        return max(intensity_values)
    if "A (10^8 s^-1)" in row.index:
        value = pd.to_numeric(pd.Series([row["A (10^8 s^-1)"]]), errors="coerce").iloc[0]
        if pd.notna(value) and float(value) > 0:
            return float(value)
    return 1.0


def _database_intensity_series(element_df: pd.DataFrame) -> np.ndarray:
    """Vectorized `_database_intensity` over a whole database frame."""
    best = np.full(len(element_df), np.nan, dtype=float)
    for column in ("Intensity (long)", "Intensity (short)"):
        if column in element_df.columns:
            values = pd.to_numeric(element_df[column], errors="coerce").to_numpy(dtype=float)
            best = np.fmax(best, np.where(values > 0, values, np.nan))
    if "A (10^8 s^-1)" in element_df.columns:
        fallback = pd.to_numeric(element_df["A (10^8 s^-1)"], errors="coerce").to_numpy(dtype=float)
        best = np.where(np.isnan(best), np.where(fallback > 0, fallback, np.nan), best)
    return np.where(np.isnan(best), 1.0, best)


def _log_database_intensity_norm(intensity: float, element_max: float, decades: float = 6.0) -> float:
    """Per-element line-strength weight compressed to a log scale.

    Database intensities span tens of decades; a linear max-normalization
    pins everything but the strongest line to ~0. One unit of weight per
    ``decades`` orders of magnitude keeps strong lines dominant while still
    grading the rest.
    """
    if not np.isfinite(intensity) or intensity <= 0 or not np.isfinite(element_max) or element_max <= 0:
        return 0.05
    ratio = min(1.0, float(intensity) / float(element_max))
    return float(np.clip(1.0 + math.log10(ratio) / float(decades), 0.05, 1.0))


def _local_wavelength_spacing(wavelengths: np.ndarray, index: int) -> float:
    values = np.asarray(wavelengths, dtype=float)
    if values.size < 2:
        return 1.0
    if index <= 0:
        spacing = values[1] - values[0]
    elif index >= values.size - 1:
        spacing = values[-1] - values[-2]
    else:
        spacing = (values[index + 1] - values[index - 1]) / 2.0
    if not np.isfinite(spacing) or spacing <= EPS:
        finite_diff = np.diff(values[np.isfinite(values)])
        finite_diff = finite_diff[finite_diff > EPS]
        return float(np.nanmedian(finite_diff)) if finite_diff.size else 1.0
    return float(spacing)


# Sideband geometry override, set for the duration of a build. A module-level
# value rather than a threaded parameter because _net_line_area_matrix is called
# from a dozen places that have no access to the analysis config; the builder
# sets it once and restores it in a finally block.
_SIDEBAND_OVERRIDE: tuple[float, float] = (0.0, 0.0)


def set_sideband_geometry(inner_nm: float, outer_nm: float) -> tuple[float, float]:
    """Override the local-baseline sideband edges. Returns the previous value."""
    global _SIDEBAND_OVERRIDE
    previous = _SIDEBAND_OVERRIDE
    _SIDEBAND_OVERRIDE = (float(inner_nm or 0.0), float(outer_nm or 0.0))
    return previous


def _integrate_line(wavelengths: np.ndarray, intensities: np.ndarray, center_nm: float, half_window_nm: float) -> float:
    mask = (
        np.isfinite(wavelengths)
        & np.isfinite(intensities)
        & (wavelengths >= center_nm - half_window_nm)
        & (wavelengths <= center_nm + half_window_nm)
    )
    if not mask.any():
        return math.nan
    x_values = wavelengths[mask]
    y_values = np.clip(intensities[mask], 0, None)
    if y_values.size == 1:
        source_index = int(np.flatnonzero(mask)[0])
        return float(y_values[0] * _local_wavelength_spacing(wavelengths, source_index))
    trapezoid = getattr(np, "trapezoid", None) or getattr(np, "trapz", None)
    return float(trapezoid(y_values, x_values))


def _sideband_mask(wavelengths: np.ndarray, center_nm: float, half_window_nm: float) -> np.ndarray:
    """Wavelength mask for the local-baseline sidebands flanking a line window.

    The inner edge stays clear of the smoothing-broadened line core; the outer
    edge is wide enough to give a stable median even on a curved continuum.

    The geometry can be overridden per build via `set_sideband_geometry`, which
    exists so the benchmark can sweep it. The default reproduces the derived
    values exactly.
    """
    override_inner, override_outer = _SIDEBAND_OVERRIDE
    if override_inner > 0 and override_outer > override_inner:
        inner, outer = float(override_inner), float(override_outer)
    else:
        inner = max(2.0 * float(half_window_nm), float(half_window_nm) + 0.15)
        outer = inner + 1.2
    return (
        np.isfinite(wavelengths)
        & (
            ((wavelengths >= center_nm - outer) & (wavelengths <= center_nm - inner))
            | ((wavelengths >= center_nm + inner) & (wavelengths <= center_nm + outer))
        )
    )


def _net_line_area_matrix(
    wavelengths: np.ndarray,
    intensities: np.ndarray,
    center_nm: float,
    half_window_nm: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Local-baseline-subtracted line area per spectrum row.

    Returns ``(net_area, sideband_median, noise_area)`` where the baseline is
    the median of the flanking sidebands and ``noise_area`` is the sideband
    MAD expressed in the same integrated-area units as ``net_area``. This is
    what keeps a zero-gate-delay continuum from counting as line signal.
    """
    values = np.asarray(intensities, dtype=float)
    row_count = values.shape[0] if values.ndim == 2 else 0
    target_mask = (
        np.isfinite(wavelengths)
        & (wavelengths >= center_nm - half_window_nm)
        & (wavelengths <= center_nm + half_window_nm)
    )
    if values.ndim != 2 or not target_mask.any():
        return (
            np.full(row_count, np.nan, dtype=float),
            np.zeros(row_count, dtype=float),
            np.zeros(row_count, dtype=float),
        )
    side_mask = _sideband_mask(wavelengths, center_nm, half_window_nm)
    if side_mask.any():
        side = values[:, side_mask]
        baseline = np.nanmedian(side, axis=1)
        mad = np.nanmedian(np.abs(side - baseline[:, None]), axis=1)
    else:
        baseline = np.zeros(row_count, dtype=float)
        mad = np.zeros(row_count, dtype=float)
    baseline = np.nan_to_num(baseline, nan=0.0, posinf=0.0, neginf=0.0)
    mad = np.nan_to_num(mad, nan=0.0, posinf=0.0, neginf=0.0)
    net = np.nan_to_num(
        np.clip(values[:, target_mask] - baseline[:, None], 0, None),
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    )
    source_index = int(np.flatnonzero(target_mask)[0])
    pixel_spacing = _local_wavelength_spacing(wavelengths, source_index)
    if net.shape[1] == 1:
        area = net[:, 0] * pixel_spacing
    else:
        x_values = np.asarray(wavelengths[target_mask], dtype=float)
        trapezoid = getattr(np, "trapezoid", None) or getattr(np, "trapz", None)
        area = trapezoid(net, x_values, axis=1)
    # Area noise for independent per-pixel noise scales with sqrt(N) pixels,
    # not the full window width (which would assume perfectly correlated noise).
    noise_area = 1.4826 * mad * max(pixel_spacing, EPS) * math.sqrt(max(net.shape[1], 1))
    return area, baseline, noise_area


def _net_line_area(
    wavelengths: np.ndarray,
    intensities: np.ndarray,
    center_nm: float,
    half_window_nm: float,
) -> tuple[float, float, float]:
    """Scalar wrapper around :func:`_net_line_area_matrix` for one spectrum."""
    values = np.asarray(intensities, dtype=float)
    area, baseline, noise_area = _net_line_area_matrix(
        np.asarray(wavelengths, dtype=float),
        values[None, :],
        float(center_nm),
        float(half_window_nm),
    )
    return float(area[0]), float(baseline[0]), float(noise_area[0])


def _peak_shape_metrics_matrix(
    wavelengths: np.ndarray,
    intensities: np.ndarray,
    mask: np.ndarray,
    center_nm: float,
    half_window_nm: float,
    tolerance_nm: float,
) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray(intensities, dtype=float)
    if not mask.any() or values.ndim != 2:
        row_count = values.shape[0] if values.ndim == 2 else 0
        return np.zeros(row_count, dtype=float), np.full(row_count, np.nan, dtype=float)
    x_values = np.asarray(wavelengths[mask], dtype=float)
    target = np.nan_to_num(values[:, mask], nan=-np.inf, posinf=-np.inf, neginf=-np.inf)
    finite_rows = np.isfinite(target).any(axis=1)
    if target.shape[1] == 0:
        return np.zeros(values.shape[0], dtype=float), np.full(values.shape[0], np.nan, dtype=float)
    peak_indices = np.argmax(target, axis=1)
    alignment_error = np.abs(x_values[peak_indices] - float(center_nm))
    alignment_error[~finite_rows] = np.nan
    alignment_scale = max(float(tolerance_nm), float(half_window_nm), EPS)
    alignment_score = np.clip(1.0 - np.nan_to_num(alignment_error, nan=alignment_scale) / alignment_scale, 0.0, 1.0)
    positive = np.nan_to_num(np.clip(values[:, mask], 0, None), nan=0.0, posinf=0.0, neginf=0.0)
    positive_total = np.sum(positive, axis=1)
    apex = np.max(positive, axis=1) if positive.shape[1] else np.zeros(values.shape[0], dtype=float)
    apex_fraction = np.divide(apex, positive_total + EPS, out=np.zeros_like(apex, dtype=float), where=positive_total > EPS)
    compactness_score = np.clip(apex_fraction * max(3.0, positive.shape[1] / 2.0), 0.20, 1.0)
    shape_score = alignment_score * compactness_score
    shape_score[positive_total <= EPS] = 0.0
    return np.clip(shape_score, 0.0, 1.0), alignment_error


def _decoy_centers_for_line(
    wavelengths: np.ndarray,
    center_nm: float,
    half_window_nm: float,
    known_database_lines: np.ndarray,
    tolerance_nm: float,
) -> list[float]:
    if wavelengths.size == 0:
        return []
    spacing = max(float(half_window_nm) * 4.0, float(tolerance_nm) * 1.75, 0.35)
    min_wavelength = float(np.nanmin(wavelengths)) + half_window_nm
    max_wavelength = float(np.nanmax(wavelengths)) - half_window_nm
    known = np.asarray(known_database_lines, dtype=float)
    known = known[np.isfinite(known)]
    centers: list[float] = []
    for multiplier in (-4, -3, -2, -1, 1, 2, 3, 4):
        decoy_center = float(center_nm) + multiplier * spacing
        if decoy_center < min_wavelength or decoy_center > max_wavelength:
            continue
        if known.size and np.nanmin(np.abs(known - decoy_center)) <= max(float(tolerance_nm), float(half_window_nm)):
            continue
        centers.append(decoy_center)
    return centers


def _soft_cap_quality_z(
    values: np.ndarray | float, knee: float = 8.0, ceiling: float = 16.0, floor: float = -4.0
) -> np.ndarray:
    """Compress quality z-scores above ``knee`` instead of hard-clipping.

    A hard clip pins every strong line to the same value, so saturated points
    become indistinguishable and maps go flat; tanh keeps ordering while
    bounding the contribution at ``ceiling``. Negative z (line region below
    the empirical null — evidence of absence) is kept, bounded at ``floor``,
    so elements with many silent lines cannot accumulate rectified noise.
    """
    array = np.asarray(values, dtype=float)
    span = max(float(ceiling) - float(knee), EPS)
    compressed = np.where(array > knee, knee + span * np.tanh((array - knee) / span), array)
    return np.clip(compressed, float(floor), float(ceiling))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z_value = math.exp(-value)
        return float(1.0 / (1.0 + z_value))
    z_value = math.exp(value)
    return float(z_value / (1.0 + z_value))


def _confidence_z_scale(config: MultiElementMapConfig) -> float:
    """Sigmoid width for mapping evidence z to confidence.

    Combined z on real runs spans ~0-30; a fixed width of 1.25 saturated the
    sigmoid by z~=10, so every point with any signal collapsed to the same
    confidence and maps went flat. Tying the width to ``min_line_z`` keeps
    gradation across the observed range while z at the threshold still maps
    to 0.5.
    """
    return max(1.25, float(config.min_line_z))


def _specificity_limited_confidence(combined_z: float, config: MultiElementMapConfig) -> float:
    lower = max(0.0, min(1.0, float(config.uncertain_threshold)))
    upper = max(lower, min(1.0, float(config.detected_threshold) - 0.02))
    evidence_excess = max(0.0, float(combined_z) - float(config.min_line_z))
    scale = max(6.0, float(config.min_line_z) * 3.0)
    score = 1.0 - math.exp(-evidence_excess / scale)
    return float(lower + (upper - lower) * max(0.0, min(1.0, score)))


def _aggregate(values: Sequence[float], method: str) -> float:
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    if array.size == 0:
        return math.nan
    if method == "mean":
        return float(np.nanmean(array))
    return float(np.nanmedian(array))


def _point_metadata(run_data: MappingRunData) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for point_key, group in run_data.index.groupby("point_key", sort=False):
        group = group.sort_values(["shot_number", "shot_index"], kind="stable")
        rows.append(
            {
                "point_key": point_key,
                "row_index": int(group["row_index"].iloc[0]),
                "column_index": int(group["column_index"].iloc[0]),
                "x_mm": float(group["x_mm"].iloc[0]),
                "y_mm": float(group["y_mm"].iloc[0]),
                "shots_expected": int(len(group)),
                "source_files": ";".join(str(item) for item in group["filename"].tolist()),
                "source_filepaths": ";".join(
                    str(item)
                    for item in group.get("source_uri", group["resolved_filepath"]).tolist()
                ),
                "captured_at_min": _datetime_min(group["captured_at_dt"]),
                "captured_at_max": _datetime_max(group["captured_at_dt"]),
            }
        )
    return pd.DataFrame(rows).sort_values(["row_index", "column_index"], kind="stable").reset_index(drop=True)


def _datetime_min(values: pd.Series) -> str:
    valid = values.dropna()
    return "" if valid.empty else valid.min().isoformat()


def _datetime_max(values: pd.Series) -> str:
    valid = values.dropna()
    return "" if valid.empty else valid.max().isoformat()


def _line_signal_metrics(values: np.ndarray, noise_floor: float = 0.0) -> tuple[float, float, float]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return math.nan, math.nan, 0.0
    p95 = float(np.nanpercentile(finite, 95))
    median = float(np.nanmedian(finite))
    mad = float(np.nanmedian(np.abs(finite - median)))
    # The spectral noise floor keeps the denominator physical: map-level MAD
    # collapses toward zero in dark regions and would otherwise explode SNR.
    scale = max(1.4826 * mad, float(noise_floor) if np.isfinite(noise_floor) else 0.0, EPS)
    snr = max(0.0, (p95 - median) / scale)
    return p95, median, snr


def _line_spatial_consistency(values: np.ndarray, p95: float, median: float) -> tuple[float, float]:
    finite = values[np.isfinite(values)]
    if finite.size == 0 or not np.isfinite(p95) or p95 <= EPS:
        return 0.0, 0.05
    positive_fraction = float(np.count_nonzero(finite > EPS) / finite.size)
    median_fraction = max(0.0, float(median)) / max(float(p95), EPS) if np.isfinite(median) else 0.0
    consistency = max(0.05, min(1.0, 0.5 * positive_fraction + 0.5 * min(1.0, median_fraction)))
    return positive_fraction, consistency


def _aggregate_line_values_by_point(
    shot_table: pd.DataFrame,
    values: np.ndarray,
    point_keys: Sequence[str],
    method: str,
) -> np.ndarray:
    table = pd.DataFrame(
        {
            "point_key": shot_table["point_key"].astype(str).to_numpy(),
            "value": np.asarray(values, dtype=float),
        }
    )
    if method == "mean":
        aggregate = table.groupby("point_key", sort=False)["value"].mean()
    else:
        aggregate = table.groupby("point_key", sort=False)["value"].median()
    return np.asarray([aggregate.get(str(point_key), math.nan) for point_key in point_keys], dtype=float)


def _aggregate_metric_table_by_point(
    shot_table: pd.DataFrame,
    metrics: dict[str, np.ndarray],
    point_metadata: pd.DataFrame,
    aggregate_method: str,
) -> pd.DataFrame:
    table = pd.DataFrame({"point_key": shot_table["point_key"].astype(str).to_numpy()})
    if "shot_index" in shot_table.columns:
        table["shot_index"] = pd.to_numeric(shot_table["shot_index"], errors="coerce")
    else:
        table["shot_index"] = np.arange(1, len(table) + 1)
    for column, values in metrics.items():
        table[column] = np.asarray(values, dtype=float)
    grouped = table.groupby("point_key", sort=False)
    if aggregate_method == "mean":
        aggregated = grouped[list(metrics.keys())].mean()
    else:
        aggregated = grouped[list(metrics.keys())].median()
    if "empirical_p" in aggregated.columns:
        aggregated["empirical_p"] = grouped["empirical_p"].min()
    aggregated["shots_used"] = grouped["shot_index"].nunique().astype(int)
    result = point_metadata[["point_key", "row_index", "column_index", "x_mm", "y_mm"]].copy()
    result = result.merge(aggregated.reset_index(), on="point_key", how="left")
    return result


def _compute_line_feature_table(
    processed: MappingAnalysisCache,
    point_metadata: pd.DataFrame,
    line: pd.Series,
    *,
    peak_window_nm: float,
    tolerance_nm: float,
    known_database_lines: np.ndarray,
    aggregate_method: str,
    chunk_size: int,
) -> pd.DataFrame:
    wavelengths = processed.wavelengths
    center_nm = float(line["Wavelength"])
    target_mask = (
        np.isfinite(wavelengths)
        & (wavelengths >= center_nm - float(peak_window_nm))
        & (wavelengths <= center_nm + float(peak_window_nm))
    )
    decoy_centers = _decoy_centers_for_line(
        wavelengths,
        center_nm,
        float(peak_window_nm),
        known_database_lines,
        float(tolerance_nm),
    )
    total_rows = processed.intensities.shape[0]
    arrays: dict[str, np.ndarray] = {
        "peak_area": np.zeros(total_rows, dtype=float),
        "null_median": np.zeros(total_rows, dtype=float),
        "null_mad": np.zeros(total_rows, dtype=float),
        "robust_z": np.zeros(total_rows, dtype=float),
        "empirical_p": np.ones(total_rows, dtype=float),
        "local_snr": np.zeros(total_rows, dtype=float),
        "shape_score": np.zeros(total_rows, dtype=float),
        "alignment_error_nm": np.full(total_rows, np.nan, dtype=float),
        "noise_area": np.zeros(total_rows, dtype=float),
    }
    for chunk_start in range(0, total_rows, max(1, int(chunk_size))):
        chunk_end = min(total_rows, chunk_start + max(1, int(chunk_size)))
        values = np.asarray(processed.intensities[chunk_start:chunk_end, :], dtype=float)
        peak_area, _, sideband_scale = _net_line_area_matrix(wavelengths, values, center_nm, float(peak_window_nm))
        peak_area = np.nan_to_num(np.maximum(peak_area, 0.0), nan=0.0, posinf=0.0, neginf=0.0)
        if decoy_centers:
            decoy_values = np.column_stack(
                [
                    np.nan_to_num(
                        _net_line_area_matrix(wavelengths, values, decoy_center, float(peak_window_nm))[0],
                        nan=0.0,
                        posinf=0.0,
                        neginf=0.0,
                    )
                    for decoy_center in decoy_centers
                ]
            )
            null_median = np.nanmedian(decoy_values, axis=1)
            null_mad = np.nanmedian(np.abs(decoy_values - null_median[:, None]), axis=1)
            null_scale = np.maximum.reduce([1.4826 * null_mad, sideband_scale, np.full_like(sideband_scale, EPS)])
            empirical_p = (1.0 + np.sum(decoy_values >= peak_area[:, None], axis=1)) / (decoy_values.shape[1] + 1.0)
        else:
            null_median = np.zeros_like(peak_area)
            null_scale = np.maximum(sideband_scale, EPS)
            null_mad = null_scale / 1.4826
            robust_z_for_p = np.maximum(0.0, (peak_area - null_median) / null_scale)
            empirical_p = 0.5 * np.vectorize(math.erfc)(robust_z_for_p / math.sqrt(2.0))
        robust_z = (peak_area - null_median) / null_scale
        local_snr = np.divide(peak_area, null_scale, out=np.zeros_like(peak_area), where=null_scale > EPS)
        shape_score, alignment_error = _peak_shape_metrics_matrix(
            wavelengths,
            values,
            target_mask,
            center_nm,
            float(peak_window_nm),
            float(tolerance_nm),
        )
        segment = slice(chunk_start, chunk_end)
        arrays["peak_area"][segment] = peak_area
        arrays["null_median"][segment] = np.nan_to_num(null_median, nan=0.0)
        arrays["null_mad"][segment] = np.nan_to_num(null_mad, nan=0.0)
        arrays["robust_z"][segment] = np.nan_to_num(robust_z, nan=0.0)
        arrays["empirical_p"][segment] = np.nan_to_num(empirical_p, nan=1.0)
        arrays["local_snr"][segment] = np.nan_to_num(local_snr, nan=0.0)
        arrays["shape_score"][segment] = np.nan_to_num(shape_score, nan=0.0)
        arrays["alignment_error_nm"][segment] = alignment_error
        arrays["noise_area"][segment] = np.nan_to_num(sideband_scale, nan=0.0)
    return _aggregate_metric_table_by_point(processed.shot_table, arrays, point_metadata, aggregate_method)


def _decorate_line_feature_table(raw_table: pd.DataFrame, line: pd.Series, config: MultiElementMapConfig) -> pd.DataFrame:
    table = raw_table.copy()
    robust_z = pd.to_numeric(table["robust_z"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    shape_score = pd.to_numeric(table["shape_score"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    overlap_penalty = float(line.get("overlap_penalty", 0.0))
    quality_z = robust_z * np.clip(shape_score, 0.0, 1.0) * max(0.0, 1.0 - overlap_penalty)
    quality_z = _soft_cap_quality_z(quality_z)
    table["element_symbol"] = str(line["Symbol"])
    table["line_id"] = str(line["line_id"])
    table["wavelength"] = float(line["Wavelength"])
    table["ionization_level"] = int(line["Ionization Level"])
    table["database_intensity"] = float(line["database_intensity"])
    table["database_intensity_norm"] = float(line["database_intensity_norm"])
    table["is_overlapped"] = bool(line["is_overlapped"])
    table["overlaps_selected_element"] = bool(line.get("overlaps_selected_element", False))
    table["overlap_symbols"] = str(line.get("overlap_symbols", "") or "")
    table["overlap_penalty"] = overlap_penalty
    table["line_usable"] = bool(line["line_usable"])
    table["candidate_selected"] = bool(line["candidate_selected"])
    table["candidate_reason"] = str(line.get("candidate_reason", ""))
    table["candidate_measured_snr"] = float(line.get("measured_snr", 0.0))
    table["candidate_p95_intensity"] = float(line.get("p95_intensity", 0.0))
    table["quality_z"] = quality_z
    table["line_confidence"] = [
        _sigmoid((float(value) - float(config.min_line_z)) / _confidence_z_scale(config))
        for value in quality_z
    ]
    ordered = [
        "element_symbol",
        "point_key",
        "row_index",
        "column_index",
        "x_mm",
        "y_mm",
        "line_id",
        "wavelength",
        "ionization_level",
        "database_intensity",
        "database_intensity_norm",
        "is_overlapped",
        "overlaps_selected_element",
        "overlap_symbols",
        "overlap_penalty",
        "line_usable",
        "candidate_selected",
        "candidate_reason",
        "candidate_measured_snr",
        "candidate_p95_intensity",
        "shots_used",
        "peak_area",
        "null_median",
        "null_mad",
        "robust_z",
        "empirical_p",
        "local_snr",
        "shape_score",
        "alignment_error_nm",
        "line_confidence",
        "quality_z",
    ]
    return table[[column for column in ordered if column in table.columns]]


def _load_or_build_line_feature_table(
    processed: MappingAnalysisCache,
    point_metadata: pd.DataFrame,
    line: pd.Series,
    preprocess_config: MappingPreprocessConfig,
    config: MultiElementMapConfig,
    known_database_lines: np.ndarray,
    *,
    force_rebuild: bool = False,
) -> tuple[pd.DataFrame, bool]:
    cache_dir = _line_feature_cache_dir(processed.run_data, preprocess_config, config)
    cache_path = _line_feature_cache_path(cache_dir, line)
    raw_table: pd.DataFrame | None = None
    cache_hit = False
    if not force_rebuild and cache_path.exists():
        try:
            raw_table = pd.read_pickle(cache_path)
            cache_hit = True
        except Exception:
            raw_table = None
            cache_hit = False
    if raw_table is None and not force_rebuild:
        legacy_csv_path = cache_path.with_suffix(".csv")
        if legacy_csv_path.exists():
            try:
                raw_table = pd.read_csv(legacy_csv_path)
                cache_hit = True
            except Exception:
                raw_table = None
                cache_hit = False
    if raw_table is None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        raw_table = _compute_line_feature_table(
            processed,
            point_metadata,
            line,
            peak_window_nm=float(config.peak_window_nm),
            tolerance_nm=float(config.tolerance_nm),
            known_database_lines=known_database_lines,
            aggregate_method=preprocess_config.aggregate_shots,
            chunk_size=_resolve_chunk_size(preprocess_config.chunk_size_spectra),
        )
        # Write through a per-process temp file: benchmark sweeps run several
        # configurations at once, and configurations that differ only after the
        # line-feature stage share this cache directory, so two processes can
        # target the same path. os.replace is atomic, so a concurrent reader
        # sees either the old file or the complete new one, never a partial.
        tmp_path = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
        raw_table.to_pickle(tmp_path)
        os.replace(tmp_path, cache_path)
    return _decorate_line_feature_table(raw_table, line, config), cache_hit


def _scan_candidate_lines(
    candidates: pd.DataFrame,
    processed: MappingAnalysisCache,
    point_metadata: pd.DataFrame,
    *,
    peak_window_nm: float,
    aggregate_method: str,
    pruning_enabled: bool,
    max_candidate_lines: int,
    min_prominence_sigma: float = 1.0,
) -> tuple[pd.DataFrame, dict[str, np.ndarray]]:
    if candidates.empty:
        return candidates.copy(), {}
    scanned = candidates.copy().reset_index(drop=True)
    point_keys = point_metadata["point_key"].astype(str).tolist()
    db_intensities = np.asarray([_database_intensity(row) for _, row in scanned.iterrows()], dtype=float)
    db_max = float(np.nanmax(db_intensities)) if db_intensities.size and np.nanmax(db_intensities) > 0 else 1.0
    line_values: dict[str, np.ndarray] = {}
    p95_values: list[float] = []
    median_values: list[float] = []
    snr_values: list[float] = []
    raw_weights: list[float] = []
    db_norm_values: list[float] = []
    positive_fraction_values: list[float] = []
    consistency_values: list[float] = []
    measured_priority_values: list[float] = []
    noise_floor_values: list[float] = []
    prominence_values: list[float] = []

    for candidate_index, (_, line) in enumerate(scanned.iterrows()):
        line_id = str(line["line_id"])
        shot_areas, _, shot_noise_areas = _net_line_area_matrix(
            processed.wavelengths,
            processed.intensities,
            float(line["Wavelength"]),
            float(peak_window_nm),
        )
        point_values = _aggregate_line_values_by_point(
            processed.shot_table,
            shot_areas,
            point_keys,
            aggregate_method,
        )
        point_noise = _aggregate_line_values_by_point(
            processed.shot_table,
            shot_noise_areas,
            point_keys,
            aggregate_method,
        )
        finite_noise = point_noise[np.isfinite(point_noise)]
        noise_floor = float(np.nanmedian(finite_noise)) if finite_noise.size else 0.0
        line_values[line_id] = point_values
        p95, median, snr = _line_signal_metrics(point_values, noise_floor)
        prominence_p95 = max(0.0, (p95 - median)) if np.isfinite(p95) and np.isfinite(median) else 0.0
        positive_fraction, consistency_factor = _line_spatial_consistency(point_values, p95, median)
        db_intensity_norm = _log_database_intensity_norm(float(db_intensities[candidate_index]), db_max)
        overlap_factor = max(0.05, 1.0 - float(line.get("overlap_penalty", 0.0)) * 0.65)
        signal_factor = max(float(p95) if np.isfinite(p95) else 0.0, EPS)
        finite_snr = max(float(snr) if np.isfinite(snr) else 0.0, 0.0)
        bounded_snr = min(finite_snr, 25.0)
        snr_factor = 1.0 + math.log1p(bounded_snr)
        raw_weight = (
            snr_factor
            * db_intensity_norm
            * overlap_factor
            * math.log1p(signal_factor * 1000.0)
            * consistency_factor
        )
        measured_priority = bounded_snr * math.log1p(signal_factor * 1000.0) * overlap_factor
        p95_values.append(p95)
        median_values.append(median)
        snr_values.append(snr)
        raw_weights.append(raw_weight)
        db_norm_values.append(db_intensity_norm)
        positive_fraction_values.append(positive_fraction)
        consistency_values.append(consistency_factor)
        measured_priority_values.append(measured_priority)
        noise_floor_values.append(noise_floor)
        prominence_values.append(prominence_p95)

    scanned["database_intensity"] = db_intensities
    scanned["database_intensity_norm"] = db_norm_values
    scanned["element_symbol"] = scanned["Symbol"].astype(str)
    scanned["wavelength"] = scanned["Wavelength"].astype(float)
    scanned["ionization_level"] = scanned["Ionization Level"].astype(int)
    scanned["measured_snr"] = snr_values
    scanned["p95_intensity"] = p95_values
    scanned["median_intensity"] = median_values
    scanned["raw_weight"] = raw_weights
    scanned["measured_priority"] = measured_priority_values
    scanned["positive_point_fraction"] = positive_fraction_values
    scanned["spatial_consistency"] = consistency_values
    scanned["noise_floor"] = noise_floor_values
    scanned["prominence_p95"] = prominence_values
    scanned["candidate_selected"] = False
    scanned["candidate_reason"] = "low_measured_snr"

    # Gate on the line's net signal versus its own noise floor. The net area
    # is already local-baseline-subtracted, so a uniformly present element
    # (no spatial contrast) still passes while dark-region artifacts do not.
    p95_series = scanned["p95_intensity"].fillna(0.0).astype(float)
    noise_floor_series = scanned["noise_floor"].fillna(0.0).astype(float)
    measurable = (p95_series > 0.0) & (p95_series >= float(min_prominence_sigma) * noise_floor_series)
    if "atmospheric_excluded" in scanned.columns:
        atmospheric = scanned["atmospheric_excluded"].astype(bool)
        measurable = measurable & ~atmospheric
    if "strong_line_excluded" in scanned.columns:
        # Same route as the atmospheric filter: folding the exclusion into
        # `measurable` keeps the line out of the ranking pool and out of
        # `line_usable` below. Setting line_usable at candidate-construction
        # time is not enough - it is recomputed from the selection here.
        measurable = measurable & ~scanned["strong_line_excluded"].astype(bool)
    scanned["measurable"] = measurable
    max_lines = max(1, int(max_candidate_lines))
    if (not pruning_enabled) or len(scanned) <= max_lines:
        selected_index = list(scanned.index)
    else:
        measurable_scanned = scanned[measurable]
        ranked = measurable_scanned.sort_values(
            ["raw_weight", "measured_snr", "p95_intensity", "database_intensity_norm"],
            ascending=[False, False, False, False],
            kind="stable",
        )
        if ranked.empty:
            ranked = scanned.sort_values(
                ["raw_weight", "database_intensity_norm"],
                ascending=[False, False],
                kind="stable",
            )
            selected_index = list(ranked.head(max_lines).index)
        else:
            measured_reserve = 0 if max_lines <= 1 else min(max_lines, max(1, max_lines // 3))
            measured_ranked = measurable_scanned.sort_values(
                ["measured_priority", "measured_snr", "p95_intensity"],
                ascending=[False, False, False],
                kind="stable",
            )
            selected_index = []
            for index in measured_ranked.head(measured_reserve).index:
                if index not in selected_index:
                    selected_index.append(index)
            for index in ranked.index:
                if index not in selected_index:
                    selected_index.append(index)
                if len(selected_index) >= max_lines:
                    break

    scanned.loc[selected_index, "candidate_selected"] = True
    scanned.loc[selected_index, "candidate_reason"] = "selected_measured_candidate"
    if pruning_enabled and len(scanned) > max_lines:
        strong_measured_index = [
            index
            for index in selected_index[: max(1, max_lines // 3)]
            if index in scanned.index and bool(measurable.loc[index])
        ]
        scanned.loc[strong_measured_index, "candidate_reason"] = "selected_strong_measured_line"
    scanned.loc[~measurable, "candidate_reason"] = "low_measured_signal"
    scanned.loc[scanned["is_overlapped"].astype(bool) & scanned["candidate_selected"], "candidate_reason"] = "overlapped_retained_with_penalty"
    if "atmospheric_excluded" in scanned.columns:
        scanned.loc[scanned["atmospheric_excluded"].astype(bool), "candidate_reason"] = "atmospheric_overlap"
    if "strong_line_excluded" in scanned.columns:
        scanned.loc[scanned["strong_line_excluded"].astype(bool), "candidate_reason"] = "strong_line_overlap"
    scanned["line_usable"] = scanned["candidate_selected"].astype(bool) & measurable
    scanned = scanned.sort_values(
        ["candidate_selected", "raw_weight", "measured_snr", "Wavelength"],
        ascending=[False, False, False, True],
        kind="stable",
    ).reset_index(drop=True)
    return scanned, line_values


def _accepted_line_mask(line_scores: pd.DataFrame) -> pd.Series:
    if "measurable" in line_scores.columns:
        has_signal = line_scores["measurable"].astype(bool)
    else:
        has_signal = line_scores["p95_intensity"].fillna(0.0) > 0
    if "candidate_selected" in line_scores.columns:
        return line_scores["candidate_selected"].astype(bool) & has_signal
    return has_signal


def _apply_optional_drift_correction(point_table: pd.DataFrame, config: MappingPreprocessConfig) -> tuple[pd.DataFrame, dict[str, Any]]:
    diagnostics = {"drift_correction_applied": False, "drift_correction_reason": "disabled"}
    if not config.drift_correction_enabled:
        return point_table, diagnostics

    corrected = point_table.copy()
    times = pd.to_datetime(corrected["captured_at_min"], errors="coerce")
    values = pd.to_numeric(corrected["normalized_element_intensity"], errors="coerce").to_numpy(dtype=float)
    valid = times.notna().to_numpy() & np.isfinite(values) & (values > 0)
    if valid.sum() < 3:
        diagnostics["drift_correction_reason"] = "not_enough_valid_time_points"
        return corrected, diagnostics

    seconds = (times[valid] - times[valid].min()).dt.total_seconds().to_numpy(dtype=float)
    valid_values = values[valid]
    try:
        slope, intercept = np.polyfit(seconds, valid_values, 1)
        trend = slope * seconds + intercept
    except Exception as exc:
        diagnostics["drift_correction_reason"] = str(exc)
        return corrected, diagnostics

    median_trend = float(np.nanmedian(trend))
    if not np.isfinite(median_trend) or median_trend <= EPS or not np.all(np.isfinite(trend)) or np.any(trend <= EPS):
        diagnostics["drift_correction_reason"] = "invalid_trend"
        return corrected, diagnostics

    adjusted_values = valid_values / (trend / median_trend)
    corrected.loc[valid, "normalized_element_intensity"] = adjusted_values
    diagnostics.update(
        {
            "drift_correction_applied": True,
            "drift_correction_reason": "linear_value_time_trend",
            "drift_slope": float(slope),
            "drift_intercept": float(intercept),
        }
    )
    return corrected, diagnostics


def _coordinate_edges(values: Sequence[float]) -> np.ndarray:
    coords = np.asarray(values, dtype=float)
    if coords.size == 0:
        return np.asarray([0.0, 1.0], dtype=float)
    if coords.size == 1:
        return np.asarray([coords[0] - 0.5, coords[0] + 0.5], dtype=float)
    midpoints = (coords[:-1] + coords[1:]) / 2.0
    first = coords[0] - (midpoints[0] - coords[0])
    last = coords[-1] + (coords[-1] - midpoints[-1])
    return np.concatenate([[first], midpoints, [last]])


def _grid_from_points(
    point_table: pd.DataFrame,
    row_indices: Sequence[int],
    column_indices: Sequence[int],
) -> tuple[np.ndarray, pd.DataFrame]:
    row_lookup = {int(row): index for index, row in enumerate(row_indices)}
    column_lookup = {int(column): index for index, column in enumerate(column_indices)}
    grid = np.full((len(row_indices), len(column_indices)), np.nan, dtype=float)
    for _, row in point_table.iterrows():
        row_index = row_lookup.get(int(row["row_index"]))
        column_index = column_lookup.get(int(row["column_index"]))
        if row_index is not None and column_index is not None:
            grid[row_index, column_index] = float(row.get("normalized_element_intensity", math.nan))
    grid_df = pd.DataFrame(
        grid,
        index=[f"R{int(row):03d}" for row in row_indices],
        columns=[f"C{int(column):03d}" for column in column_indices],
    )
    return grid, grid_df


def neighbor_correlation(grid: np.ndarray) -> float:
    """Mean Pearson correlation between horizontally and vertically adjacent
    cells - a quick spatial-coherence score (noise ~0, real structure >0)."""
    matrix = np.asarray(grid, dtype=float)
    if matrix.ndim != 2 or min(matrix.shape) < 2:
        return float("nan")
    correlations = []
    for left, right in ((matrix[:, :-1], matrix[:, 1:]), (matrix[:-1, :], matrix[1:, :])):
        a = left.ravel()
        b = right.ravel()
        valid = np.isfinite(a) & np.isfinite(b)
        if valid.sum() >= 3 and np.std(a[valid]) > 0 and np.std(b[valid]) > 0:
            correlations.append(float(np.corrcoef(a[valid], b[valid])[0, 1]))
    return float(np.mean(correlations)) if correlations else float("nan")


def compute_pulse_element_intensity_grid(
    run_data: MappingRunData,
    preprocess_config: MappingPreprocessConfig,
    lines: pd.DataFrame,
    pulse: int | None,
    *,
    peak_window_nm: float = 0.35,
    aggregate_method: str = "median",
) -> np.ndarray:
    """Per-point net line intensity grid for one pulse number.

    Powers the depth slider: successive pulses at a point sample deeper into
    the crater, so restricting aggregation to a single pulse number turns the
    stored shots into a depth profile. ``pulse=None`` uses every shot (the
    regular aggregate view). ``lines`` needs a wavelength column plus an
    optional weight (``raw_weight`` or ``database_intensity_norm``). Reads the
    preprocessed cache directly, so no detection-pipeline rebuild is needed.
    """
    processed = load_or_build_processed_mapping_run(run_data, preprocess_config)
    point_metadata = _point_metadata(run_data)
    point_keys = point_metadata["point_key"].astype(str).tolist()
    shot_table = processed.shot_table
    if pulse is not None:
        shot_numbers = pd.to_numeric(shot_table["shot_number"], errors="coerce")
        subset_rows = np.flatnonzero((shot_numbers == int(pulse)).to_numpy())
        if subset_rows.size == 0:
            return np.full((len(run_data.row_indices), len(run_data.column_indices)), np.nan)
        shot_table = shot_table.iloc[subset_rows].reset_index(drop=True)
        intensities = np.asarray(processed.intensities[subset_rows, :], dtype=float)
    else:
        intensities = processed.intensities

    weighted = np.zeros(len(point_keys), dtype=float)
    weight_sum = np.zeros(len(point_keys), dtype=float)
    for _, line in lines.iterrows():
        center = line.get("wavelength", line.get("Wavelength", math.nan))
        if not np.isfinite(float(center)):
            continue
        areas, _, _ = _net_line_area_matrix(processed.wavelengths, intensities, float(center), float(peak_window_nm))
        point_values = _aggregate_line_values_by_point(shot_table, areas, point_keys, aggregate_method)
        finite = point_values[np.isfinite(point_values)]
        if finite.size == 0:
            continue
        scale = float(np.nanpercentile(finite, 95))
        if not np.isfinite(scale) or scale <= EPS:
            continue
        weight = line.get("raw_weight", line.get("database_intensity_norm", 1.0))
        try:
            weight = max(float(weight), 0.05)
        except (TypeError, ValueError):
            weight = 1.0
        valid = np.isfinite(point_values)
        weighted[valid] += (point_values[valid] / scale) * weight
        weight_sum[valid] += weight

    values = np.divide(weighted, weight_sum, out=np.full_like(weighted, np.nan), where=weight_sum > 0)
    point_table = point_metadata[["point_key", "row_index", "column_index"]].copy()
    point_table["pulse_intensity"] = values
    return _grid_from_value_column(point_table, run_data.row_indices, run_data.column_indices, "pulse_intensity")


def _grid_from_value_column(
    point_table: pd.DataFrame,
    row_indices: Sequence[int],
    column_indices: Sequence[int],
    value_column: str,
) -> np.ndarray:
    row_lookup = {int(row): index for index, row in enumerate(row_indices)}
    column_lookup = {int(column): index for index, column in enumerate(column_indices)}
    grid = np.full((len(row_indices), len(column_indices)), np.nan, dtype=float)
    for _, row in point_table.iterrows():
        row_index = row_lookup.get(int(row["row_index"]))
        column_index = column_lookup.get(int(row["column_index"]))
        if row_index is not None and column_index is not None:
            grid[row_index, column_index] = float(row.get(value_column, math.nan))
    return grid


def _state_grid_from_points(
    point_table: pd.DataFrame,
    row_indices: Sequence[int],
    column_indices: Sequence[int],
) -> np.ndarray:
    row_lookup = {int(row): index for index, row in enumerate(row_indices)}
    column_lookup = {int(column): index for index, column in enumerate(column_indices)}
    grid = np.full((len(row_indices), len(column_indices)), DETECTION_STATE_NOT_DETECTED, dtype=object)
    for _, row in point_table.iterrows():
        row_index = row_lookup.get(int(row["row_index"]))
        column_index = column_lookup.get(int(row["column_index"]))
        if row_index is not None and column_index is not None:
            grid[row_index, column_index] = str(row.get("state", DETECTION_STATE_NOT_DETECTED))
    return grid


def _detection_state(confidence: float, config: MultiElementMapConfig) -> str:
    if not np.isfinite(confidence):
        return DETECTION_STATE_NOT_DETECTED
    if confidence >= float(config.detected_threshold):
        return DETECTION_STATE_DETECTED
    if confidence >= float(config.uncertain_threshold):
        return DETECTION_STATE_UNCERTAIN
    return DETECTION_STATE_NOT_DETECTED


def _element_color_map(elements: Sequence[str]) -> dict[str, str]:
    return {
        str(symbol): MULTI_ELEMENT_COLORS[index % len(MULTI_ELEMENT_COLORS)]
        for index, symbol in enumerate(elements)
    }


def _aggregate_detection_evidence(evidence_table: pd.DataFrame, aggregate_method: str) -> pd.DataFrame:
    if evidence_table.empty:
        return evidence_table.copy()
    rows: list[dict[str, Any]] = []
    group_columns = ["element_symbol", "point_key", "line_id"]
    for (_element, _point_key, _line_id), group in evidence_table.groupby(group_columns, sort=False):
        first = group.iloc[0]
        rows.append(
            {
                "element_symbol": first["element_symbol"],
                "point_key": first["point_key"],
                "row_index": int(first["row_index"]),
                "column_index": int(first["column_index"]),
                "x_mm": float(first["x_mm"]),
                "y_mm": float(first["y_mm"]),
                "line_id": first["line_id"],
                "wavelength": float(first["wavelength"]),
                "ionization_level": int(first["ionization_level"]),
                "database_intensity": float(first["database_intensity"]),
                "database_intensity_norm": float(first["database_intensity_norm"]),
                "is_overlapped": bool(first["is_overlapped"]),
                "overlaps_selected_element": bool(first.get("overlaps_selected_element", False)),
                "overlap_symbols": str(first.get("overlap_symbols", "") or ""),
                "overlap_penalty": float(first["overlap_penalty"]),
                "line_usable": bool(first["line_usable"]),
                "candidate_selected": bool(first.get("candidate_selected", True)),
                "candidate_reason": str(first.get("candidate_reason", "")),
                "candidate_measured_snr": float(first.get("candidate_measured_snr", math.nan)),
                "candidate_p95_intensity": float(first.get("candidate_p95_intensity", math.nan)),
                "shots_used": int(group["shot_index"].nunique()),
                "peak_area": _aggregate(group["peak_area"].tolist(), aggregate_method),
                "null_median": _aggregate(group["null_median"].tolist(), aggregate_method),
                "null_mad": _aggregate(group["null_mad"].tolist(), aggregate_method),
                "robust_z": _aggregate(group["robust_z"].tolist(), aggregate_method),
                "empirical_p": float(np.nanmin(group["empirical_p"].to_numpy(dtype=float))),
                "local_snr": _aggregate(group["local_snr"].tolist(), aggregate_method),
                "shape_score": _aggregate(group["shape_score"].tolist(), aggregate_method),
                "alignment_error_nm": _aggregate(group["alignment_error_nm"].tolist(), aggregate_method),
                "line_confidence": _aggregate(group["line_confidence"].tolist(), aggregate_method),
                "quality_z": _aggregate(group["quality_z"].tolist(), aggregate_method),
            }
        )
    return pd.DataFrame(rows)


def _element_intensity_by_point(line_evidence_table: pd.DataFrame,
                                *, overlap_weighting: bool = False) -> pd.DataFrame:
    """Per-point net line intensity per element.

    Each usable line's point-aggregated net ``peak_area`` is normalized by
    that line's p95 across the map (making lines comparable), then combined
    as a database-intensity-weighted mean. Unlike the saturating confidence,
    this quantity keeps varying with signal, so it drives the detail heatmap.
    """
    empty = pd.DataFrame(columns=["element_symbol", "point_key", "normalized_intensity"])
    required = {"element_symbol", "point_key", "line_id", "peak_area", "line_usable"}
    if line_evidence_table.empty or not required.issubset(line_evidence_table.columns):
        return empty
    usable = line_evidence_table[line_evidence_table["line_usable"].astype(bool)].copy()
    if usable.empty:
        return empty
    usable["_area"] = pd.to_numeric(usable["peak_area"], errors="coerce")

    def _p95(series: pd.Series) -> float:
        finite = series[np.isfinite(series)]
        return float(np.nanpercentile(finite, 95)) if finite.size else math.nan

    line_scale = usable.groupby(["element_symbol", "line_id"], sort=False)["_area"].transform(_p95)
    usable["_scaled"] = usable["_area"] / line_scale.where(line_scale > EPS)
    if "database_intensity_norm" in usable.columns:
        weights = pd.to_numeric(usable["database_intensity_norm"], errors="coerce")
    else:
        weights = pd.Series(1.0, index=usable.index)
    if overlap_weighting and "overlap_penalty" in usable.columns:
        # The confidence path already discounts lines that sit on another
        # element's line, but the intensity map did not, so a contaminated line
        # contributed at full weight - which is how titanium ended up tracking
        # zinc on the coin. Reuse the penalty that is already computed.
        penalty = pd.to_numeric(usable["overlap_penalty"], errors="coerce").fillna(0.0)
        weights = weights * (1.0 - penalty).clip(lower=0.05, upper=1.0)
    usable["_weight"] = weights.clip(lower=0.05).fillna(0.05)
    valid = usable[np.isfinite(usable["_scaled"])]
    if valid.empty:
        return empty
    numerator = (valid["_scaled"] * valid["_weight"]).groupby(
        [valid["element_symbol"], valid["point_key"]], sort=False
    ).sum()
    denominator = valid.groupby(["element_symbol", "point_key"], sort=False)["_weight"].sum()
    intensity = (numerator / denominator).rename("normalized_intensity").reset_index()
    intensity.columns = ["element_symbol", "point_key", "normalized_intensity"]
    return intensity


def _combine_line_evidence_for_point(
    line_rows: pd.DataFrame,
    config: MultiElementMapConfig,
) -> dict[str, Any]:
    if line_rows.empty or "line_usable" not in line_rows.columns:
        return {
            "raw_confidence": 0.0,
            "combined_z": 0.0,
            "supporting_lines": 0,
            "clean_supporting_lines": 0,
            "generic_overlap_supporting_lines": 0,
            "selected_overlap_supporting_lines": 0,
            "supporting_line_ids": "",
            "usable_lines": 0,
            "best_line": "",
            "best_wavelength": math.nan,
            "best_snr": 0.0,
            "best_robust_z": 0.0,
            "evidence_depth": "no_candidate_lines",
            "overlap_warnings": "",
            "principal_line_support": 0.0,
        }
    usable = line_rows[line_rows["line_usable"].astype(bool)].copy()
    if usable.empty:
        return {
            "raw_confidence": 0.0,
            "combined_z": 0.0,
            "supporting_lines": 0,
            "clean_supporting_lines": 0,
            "generic_overlap_supporting_lines": 0,
            "selected_overlap_supporting_lines": 0,
            "supporting_line_ids": "",
            "usable_lines": 0,
            "best_line": "",
            "best_wavelength": math.nan,
            "best_snr": 0.0,
            "best_robust_z": 0.0,
            "evidence_depth": "no_usable_lines",
            "overlap_warnings": "",
            "principal_line_support": 0.0,
        }

    usable = usable.copy()
    quality_z = np.asarray(usable["quality_z"], dtype=float)
    snr = np.asarray(usable["local_snr"], dtype=float)
    shape = np.asarray(usable["shape_score"], dtype=float)
    db_norm = np.asarray(usable["database_intensity_norm"], dtype=float)
    overlap_penalty = np.asarray(usable["overlap_penalty"], dtype=float)
    weights = (
        np.clip(db_norm, 0.05, None)
        * np.clip(shape, 0.10, 1.0)
        * np.clip(1.0 - overlap_penalty, 0.05, 1.0)
        * np.sqrt(np.clip(snr / max(float(config.min_line_snr), EPS), 0.10, 8.0))
    )
    finite = np.isfinite(quality_z) & np.isfinite(weights) & (weights > 0)
    if not finite.any():
        combined_z = 0.0
    else:
        combined_z = float(np.nansum(weights[finite] * quality_z[finite]) / math.sqrt(float(np.nansum(weights[finite] ** 2)) + EPS))

    supporting_mask = (
        (quality_z >= float(config.min_line_z))
        & (snr >= float(config.min_line_snr))
        & (shape >= 0.35)
        & usable["line_usable"].astype(bool).to_numpy()
    )
    supporting = usable[supporting_mask].copy()
    supporting_count = int(len(supporting))
    generic_overlap_supporting_count = 0
    selected_overlap_supporting_count = 0
    if supporting.empty:
        clean_supporting_count = 0
    elif "overlaps_selected_element" in supporting.columns:
        selected_overlap = supporting["overlaps_selected_element"].astype(bool)
        selected_overlap_supporting_count = int(selected_overlap.sum())
        if "overlap_penalty" in supporting.columns:
            overlap = pd.to_numeric(supporting["overlap_penalty"], errors="coerce").fillna(0.0)
            clean_supporting_count = int((~selected_overlap & (overlap <= 0.05)).sum())
            generic_overlap_supporting_count = int((~selected_overlap & (overlap > 0.05)).sum())
        else:
            clean_supporting_count = int((~selected_overlap).sum())
    elif "overlap_penalty" in supporting.columns:
        overlap = pd.to_numeric(supporting["overlap_penalty"], errors="coerce").fillna(0.0)
        clean_supporting_count = int((overlap <= 0.05).sum())
        generic_overlap_supporting_count = int((overlap > 0.05).sum())
    else:
        clean_supporting_count = supporting_count
    raw_confidence = _sigmoid((combined_z - float(config.min_line_z)) / _confidence_z_scale(config))

    # Principal-line consistency: if an element's strongest database lines in
    # range show no signal while weaker lines "match", the match is almost
    # certainly coincidental (dense line lists always hit something). Scale
    # confidence by the fraction of principal lines with positive evidence.
    finite_norm = db_norm[np.isfinite(db_norm)]
    principal_support = 1.0
    if finite_norm.size:
        principal_mask = np.isfinite(db_norm) & (db_norm >= 0.75 * float(np.nanmax(finite_norm)))
        if principal_mask.any():
            principal_support = float(np.mean(quality_z[principal_mask] >= 1.0))
    raw_confidence *= 0.5 + 0.5 * principal_support

    usable_count = int(len(usable))
    # Caps below are multiplicative rather than constant clamps: the state
    # guarantee (stays below the uncertain threshold) is preserved while the
    # per-point confidence keeps varying with evidence, so maps retain
    # spatial structure instead of flattening to a single value.
    uncertain_cap = max(0.0, float(config.uncertain_threshold) - 0.02)
    evidence_depth = "none"
    if supporting_count >= 2:
        evidence_depth = "multi_line"
    elif supporting_count == 1:
        evidence_depth = "single_line_limited" if usable_count >= 2 else "single_strong_line"
        if bool(config.require_multi_line_agreement) and usable_count >= 2:
            raw_confidence *= 0.78
    else:
        raw_confidence *= uncertain_cap
    if supporting_count > 0 and clean_supporting_count == 0:
        if selected_overlap_supporting_count == supporting_count:
            evidence_depth = "selected_overlap_only"
            raw_confidence *= uncertain_cap
        elif generic_overlap_supporting_count > 0:
            evidence_depth = "generic_overlap_only"
            raw_confidence = min(raw_confidence, _specificity_limited_confidence(combined_z, config))
        else:
            evidence_depth = "overlapped_only"
            raw_confidence *= uncertain_cap

    best = usable.sort_values(["quality_z", "local_snr", "database_intensity_norm"], ascending=[False, False, False], kind="stable").iloc[0]
    overlap_warnings = "; ".join(
        sorted(
            {
                str(value)
                for value in usable.loc[usable["overlap_symbols"].astype(str).str.strip() != "", "overlap_symbols"].tolist()
                if str(value).strip()
            }
        )
    )
    return {
        "raw_confidence": float(max(0.0, min(1.0, raw_confidence))),
        "combined_z": combined_z,
        "supporting_lines": supporting_count,
        "clean_supporting_lines": clean_supporting_count,
        "generic_overlap_supporting_lines": generic_overlap_supporting_count,
        "selected_overlap_supporting_lines": selected_overlap_supporting_count,
        "supporting_line_ids": ";".join(str(value) for value in supporting["line_id"].tolist()),
        "usable_lines": usable_count,
        "best_line": str(best["line_id"]),
        "best_wavelength": float(best["wavelength"]),
        "best_snr": float(best["local_snr"]),
        "best_robust_z": float(best["robust_z"]),
        "evidence_depth": evidence_depth,
        "overlap_warnings": overlap_warnings,
        "principal_line_support": float(principal_support),
    }


def _apply_spatial_consistency(
    point_element_table: pd.DataFrame,
    run_data: MappingRunData,
    config: MultiElementMapConfig,
) -> pd.DataFrame:
    if point_element_table.empty:
        return point_element_table.copy()
    adjusted = point_element_table.copy()
    adjusted["confidence"] = adjusted["raw_confidence"].astype(float)
    strength = max(0.0, min(1.0, float(config.spatial_smoothing_strength)))
    if strength <= EPS:
        adjusted["state"] = [_detection_state(value, config) for value in adjusted["confidence"].tolist()]
        return adjusted

    row_lookup = {int(row): index for index, row in enumerate(run_data.row_indices)}
    column_lookup = {int(column): index for index, column in enumerate(run_data.column_indices)}
    for element in config.element_symbols:
        mask = adjusted["element_symbol"] == element
        element_rows = adjusted[mask].copy()
        if element_rows.empty:
            continue
        grid = _grid_from_value_column(element_rows, run_data.row_indices, run_data.column_indices, "raw_confidence")
        new_values: dict[str, float] = {}
        for _, row in element_rows.iterrows():
            raw_value = float(row["raw_confidence"])
            if raw_value >= float(config.detected_threshold) + 0.10:
                new_values[str(row["point_key"])] = raw_value
                continue
            if raw_value <= max(0.0, float(config.uncertain_threshold) - 0.15):
                new_values[str(row["point_key"])] = raw_value
                continue
            row_pos = row_lookup.get(int(row["row_index"]))
            col_pos = column_lookup.get(int(row["column_index"]))
            if row_pos is None or col_pos is None:
                new_values[str(row["point_key"])] = raw_value
                continue
            r0 = max(0, row_pos - 1)
            r1 = min(grid.shape[0], row_pos + 2)
            c0 = max(0, col_pos - 1)
            c1 = min(grid.shape[1], col_pos + 2)
            neighborhood = grid[r0:r1, c0:c1].astype(float, copy=True).ravel()
            center_index = (row_pos - r0) * (c1 - c0) + (col_pos - c0)
            if 0 <= center_index < neighborhood.size:
                neighborhood = np.delete(neighborhood, center_index)
            neighborhood = neighborhood[np.isfinite(neighborhood)]
            if neighborhood.size == 0:
                new_values[str(row["point_key"])] = raw_value
                continue
            neighbor_mean = float(np.nanmean(neighborhood))
            corrected = raw_value + strength * (neighbor_mean - raw_value)
            new_values[str(row["point_key"])] = float(max(0.0, min(1.0, corrected)))
        for point_key, value in new_values.items():
            adjusted.loc[mask & (adjusted["point_key"] == point_key), "confidence"] = value

    adjusted["state"] = [_detection_state(value, config) for value in adjusted["confidence"].tolist()]
    return adjusted


def _build_wide_point_table(point_metadata: pd.DataFrame, point_element_table: pd.DataFrame, elements: Sequence[str]) -> pd.DataFrame:
    wide = point_metadata.copy()
    for element in elements:
        subset = point_element_table[point_element_table["element_symbol"] == element][
            ["point_key", "confidence", "raw_confidence", "state", "supporting_lines", "best_line", "best_snr"]
        ].copy()
        subset = subset.rename(
            columns={
                "confidence": f"{element}_confidence",
                "raw_confidence": f"{element}_raw_confidence",
                "state": f"{element}_state",
                "supporting_lines": f"{element}_supporting_lines",
                "best_line": f"{element}_best_line",
                "best_snr": f"{element}_best_snr",
            }
        )
        wide = wide.merge(subset, on="point_key", how="left")
    return wide


def _build_multi_summary(point_element_table: pd.DataFrame, line_evidence_table: pd.DataFrame, elements: Sequence[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for element in elements:
        subset = point_element_table[point_element_table["element_symbol"] == element]
        if not line_evidence_table.empty and {"element_symbol", "line_usable", "line_id"}.issubset(line_evidence_table.columns):
            lines = line_evidence_table[
                (line_evidence_table["element_symbol"] == element)
                & line_evidence_table["line_usable"].astype(bool)
            ]
        else:
            lines = pd.DataFrame()
        detected = int((subset["state"] == DETECTION_STATE_DETECTED).sum()) if not subset.empty else 0
        uncertain = int((subset["state"] == DETECTION_STATE_UNCERTAIN).sum()) if not subset.empty else 0
        rows.append(
            {
                "element": element,
                "detected_points": detected,
                "uncertain_points": uncertain,
                "not_detected_points": int((subset["state"] == DETECTION_STATE_NOT_DETECTED).sum()) if not subset.empty else 0,
                "lines_used": int(lines["line_id"].nunique()) if not lines.empty else 0,
                "median_confidence": float(np.nanmedian(subset["confidence"])) if not subset.empty else 0.0,
                "max_confidence": float(np.nanmax(subset["confidence"])) if not subset.empty else 0.0,
            }
        )
    return pd.DataFrame(rows)


def build_multi_element_map(
    run_data: MappingRunData,
    preprocess_config: MappingPreprocessConfig,
    config: MultiElementMapConfig,
    *,
    force_rebuild_cache: bool = False,
    force_result_rebuild: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> MultiElementMapResult:
    elements = config.element_symbols
    if len(elements) < 2:
        raise ValueError("Select two or more elements for Multi-element Map.")
    if float(config.detected_threshold) <= float(config.uncertain_threshold):
        raise ValueError("Detected threshold must be greater than uncertainty threshold.")

    previous_sidebands = set_sideband_geometry(
        float(getattr(config, "sideband_inner_nm", 0.0) or 0.0),
        float(getattr(config, "sideband_outer_nm", 0.0) or 0.0),
    )
    try:
        return _build_multi_element_map(
            run_data, preprocess_config, config,
            force_rebuild_cache=force_rebuild_cache,
            force_result_rebuild=force_result_rebuild,
            progress_callback=progress_callback,
        )
    finally:
        set_sideband_geometry(*previous_sidebands)


def _build_multi_element_map(
    run_data: MappingRunData,
    preprocess_config: MappingPreprocessConfig,
    config: MultiElementMapConfig,
    *,
    force_rebuild_cache: bool = False,
    force_result_rebuild: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> MultiElementMapResult:
    elements = config.element_symbols
    overall_started = time.perf_counter()
    stage_timings: dict[str, float] = {}
    preprocess_started = time.perf_counter()
    processed = load_or_build_processed_mapping_run(
        run_data,
        preprocess_config,
        force_rebuild=force_rebuild_cache,
        progress_callback=progress_callback,
    )
    stage_timings["preprocess_load_or_build_seconds"] = float(time.perf_counter() - preprocess_started)
    result_signature = _analysis_result_signature(run_data, preprocess_config, config, "multi")
    if not force_rebuild_cache and not force_result_rebuild:
        result_load_started = time.perf_counter()
        cached_result = _load_cached_multi_result(run_data, preprocess_config, config, result_signature)
        if cached_result is not None:
            cached_result.diagnostics.setdefault("stage_timings", {})
            if isinstance(cached_result.diagnostics["stage_timings"], dict):
                cached_result.diagnostics["stage_timings"]["result_cache_load_seconds"] = float(time.perf_counter() - result_load_started)
            if progress_callback:
                progress_callback("cache", 1, 1, "Using cached multi-element result")
            return cached_result
    base_wavelengths = processed.wavelengths
    element_df = _load_candidate_database(config)
    known_database_lines = element_df["Wavelength"].to_numpy(dtype=float)
    point_metadata = _point_metadata(run_data)
    shots_used_by_point: dict[str, int] = {}
    for _, shot_row in processed.shot_table.iterrows():
        point_key = str(shot_row["point_key"])
        shots_used_by_point[point_key] = shots_used_by_point.get(point_key, 0) + 1

    line_feature_tables: list[pd.DataFrame] = []
    candidate_counts: dict[str, int] = {}
    line_counts: dict[str, int] = {}
    selected_candidate_counts: dict[str, int] = {}
    line_feature_cache_hits = 0
    line_feature_cache_misses = 0
    candidate_scan_cache_hits = 0
    candidate_scan_cache_misses = 0
    candidate_scan_dir = _candidate_scan_cache_dir(run_data, preprocess_config, config)
    candidate_started = time.perf_counter()
    for element_index, element in enumerate(elements, start=1):
        if progress_callback:
            progress_callback("candidate scan", element_index - 1, len(elements), f"Scanning {element} candidate lines")
        candidates = _candidate_lines_for_symbol(
            element_df,
            element,
            base_wavelengths,
            tolerance_nm=float(config.tolerance_nm),
            peak_window_nm=float(config.peak_window_nm),
            selected_symbols=elements,
            atmospheric_filter=bool(config.atmospheric_filter_enabled),
            strong_line_exclusion_nm=float(getattr(config, "strong_line_exclusion_nm", 0.0) or 0.0),
            overlap_significance_ratio=float(
                getattr(config, "overlap_significance_ratio", OVERLAP_SIGNIFICANCE_RATIO)),
        )
        candidate_counts[element] = int(len(candidates))
        if candidates.empty:
            line_counts[element] = 0
            selected_candidate_counts[element] = 0
            continue
        line_scan_started = time.perf_counter()
        scan_cache_path = _candidate_scan_cache_path(candidate_scan_dir, element)
        scanned_from_cache = False
        if not force_rebuild_cache and scan_cache_path.exists():
            try:
                candidates = _normalize_cached_candidate_scan(pd.read_csv(scan_cache_path))
                scanned_from_cache = True
                candidate_scan_cache_hits += 1
            except Exception:
                scanned_from_cache = False
        if not scanned_from_cache:
            candidates, _line_values = _scan_candidate_lines(
                candidates,
                processed,
                point_metadata,
                peak_window_nm=float(config.peak_window_nm),
                aggregate_method=preprocess_config.aggregate_shots,
                pruning_enabled=bool(config.candidate_pruning_enabled),
                max_candidate_lines=int(config.max_candidate_lines),
                min_prominence_sigma=float(config.min_prominence_sigma),
            )
            candidate_scan_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(scan_cache_path, index=False)
            candidate_scan_cache_misses += 1
        stage_timings[f"candidate_scan_{element}_seconds"] = float(time.perf_counter() - line_scan_started)
        selected_candidates = candidates[candidates["candidate_selected"].astype(bool)].copy()
        selected_candidate_counts[element] = int(len(selected_candidates))
        line_counts[element] = int(selected_candidates["line_usable"].sum()) if not selected_candidates.empty else 0
        evidence_started = time.perf_counter()
        for line_number, (_, line) in enumerate(selected_candidates.iterrows(), start=1):
            table, cache_hit = _load_or_build_line_feature_table(
                processed,
                point_metadata,
                line,
                preprocess_config,
                config,
                known_database_lines,
                force_rebuild=force_rebuild_cache,
            )
            line_feature_tables.append(table)
            if cache_hit:
                line_feature_cache_hits += 1
            else:
                line_feature_cache_misses += 1
            if progress_callback:
                progress_callback("line features", line_number, len(selected_candidates), f"{element}: cached {line_feature_cache_hits}, built {line_feature_cache_misses}")
        stage_timings[f"line_features_{element}_seconds"] = float(time.perf_counter() - evidence_started)
        if progress_callback:
            progress_callback("evidence", element_index, len(elements), f"Built evidence for {element}")
    stage_timings["candidate_and_line_feature_seconds"] = float(time.perf_counter() - candidate_started)

    fusion_started = time.perf_counter()
    line_evidence_table = pd.concat(line_feature_tables, ignore_index=True) if line_feature_tables else pd.DataFrame()
    evidence_lookup: dict[tuple[str, str], pd.DataFrame] = {}
    if not line_evidence_table.empty:
        evidence_lookup = {
            (str(element), str(point_key)): group
            for (element, point_key), group in line_evidence_table.groupby(["element_symbol", "point_key"], sort=False)
        }
    point_rows: list[dict[str, Any]] = []
    for element in elements:
        for point in point_metadata.itertuples(index=False):
            point_key = str(point.point_key)
            line_rows = evidence_lookup.get((str(element), point_key), pd.DataFrame())
            combined = _combine_line_evidence_for_point(line_rows, config)
            point_rows.append(
                {
                    "point_key": point_key,
                    "row_index": int(point.row_index),
                    "column_index": int(point.column_index),
                    "x_mm": float(point.x_mm),
                    "y_mm": float(point.y_mm),
                    "element_symbol": element,
                    "raw_confidence": float(combined["raw_confidence"]),
                    "combined_z": float(combined["combined_z"]),
                    "supporting_lines": int(combined["supporting_lines"]),
                    "clean_supporting_lines": int(combined.get("clean_supporting_lines", 0)),
                    "generic_overlap_supporting_lines": int(combined.get("generic_overlap_supporting_lines", 0)),
                    "selected_overlap_supporting_lines": int(combined.get("selected_overlap_supporting_lines", 0)),
                    "supporting_line_ids": str(combined["supporting_line_ids"]),
                    "usable_lines": int(combined["usable_lines"]),
                    "best_line": str(combined["best_line"]),
                    "best_wavelength": combined["best_wavelength"],
                    "best_snr": float(combined["best_snr"]),
                    "best_robust_z": float(combined["best_robust_z"]),
                    "evidence_depth": str(combined["evidence_depth"]),
                    "overlap_warnings": str(combined["overlap_warnings"]),
                    "principal_line_support": float(combined.get("principal_line_support", 1.0)),
                    "shots_used": int(shots_used_by_point.get(point_key, 0)),
                    "source_files": point.source_files,
                    "source_filepaths": point.source_filepaths,
                    "captured_at_min": point.captured_at_min,
                    "captured_at_max": point.captured_at_max,
                }
            )
    point_element_table = pd.DataFrame(point_rows)
    intensity_table = _element_intensity_by_point(
        line_evidence_table,
        overlap_weighting=bool(getattr(config, "intensity_overlap_weighting", False)),
    )
    point_element_table = point_element_table.merge(
        intensity_table, on=["element_symbol", "point_key"], how="left"
    )
    if "normalized_intensity" not in point_element_table.columns:
        point_element_table["normalized_intensity"] = math.nan
    point_element_table = _apply_spatial_consistency(point_element_table, run_data, config)
    wide_point_table = _build_wide_point_table(point_metadata, point_element_table, elements)

    element_grids: dict[str, np.ndarray] = {}
    element_intensity_grids: dict[str, np.ndarray] = {}
    state_grids: dict[str, np.ndarray] = {}
    for element in elements:
        subset = point_element_table[point_element_table["element_symbol"] == element]
        element_grids[element] = _grid_from_value_column(subset, run_data.row_indices, run_data.column_indices, "confidence")
        element_intensity_grids[element] = _grid_from_value_column(
            subset, run_data.row_indices, run_data.column_indices, "normalized_intensity"
        )
        state_grids[element] = _state_grid_from_points(subset, run_data.row_indices, run_data.column_indices)

    summary_table = _build_multi_summary(point_element_table, line_evidence_table, elements)
    stage_timings["evidence_fusion_seconds"] = float(time.perf_counter() - fusion_started)
    stage_timings["total_build_seconds"] = float(time.perf_counter() - overall_started)
    diagnostics = {
        "spectra_total": run_data.shots_total,
        "spectra_readable": processed.readable_shots,
        "spectra_missing": run_data.missing_spectra,
        "spectra_failed": int((processed.diagnostics["read_error"].fillna("") != "").sum()),
        "candidate_line_counts": candidate_counts,
        "selected_candidate_line_counts": selected_candidate_counts,
        "usable_line_counts": line_counts,
        "detection_method_version": DETECTION_METHOD_VERSION,
        "shot_diagnostics": processed.diagnostics.to_dict(orient="records"),
        "line_evidence_rows": int(len(line_evidence_table)),
        "line_feature_cache_hits": int(line_feature_cache_hits),
        "line_feature_cache_misses": int(line_feature_cache_misses),
        "candidate_scan_cache_hits": int(candidate_scan_cache_hits),
        "candidate_scan_cache_misses": int(candidate_scan_cache_misses),
        "candidate_scan_cache_signature": _candidate_scan_cache_signature(run_data, preprocess_config, config),
        "candidate_scan_cache_dir": str(candidate_scan_dir),
        "line_feature_cache_signature": _line_feature_cache_signature(run_data, preprocess_config, config),
        "line_feature_cache_dir": str(_line_feature_cache_dir(run_data, preprocess_config, config)),
        "preprocess_cache_hit": bool(processed.cache_hit),
        "preprocess_cache_signature": processed.signature,
        "preprocess_cache_dir": str(processed.cache_dir),
        "result_cache_hit": False,
        "result_cache_signature": result_signature,
        "result_cache_dir": str(_analysis_result_cache_dir(run_data, result_signature)),
        "candidate_pruning_enabled": bool(config.candidate_pruning_enabled),
        "max_candidate_lines": int(config.max_candidate_lines),
        "stage_timings": stage_timings,
        "preprocessing": {
            "smoothing_method": preprocess_config.smoothing_method if preprocess_config.smoothing_enabled else "None",
            "smoothing_strength": preprocess_config.smoothing_strength,
            "baseline_enabled": preprocess_config.baseline_enabled,
            "normalization": preprocess_config.normalization,
            "aggregate_shots": preprocess_config.aggregate_shots,
        },
    }
    result = MultiElementMapResult(
        run_data=run_data,
        preprocess_config=preprocess_config,
        config=config,
        point_element_table=point_element_table,
        wide_point_table=wide_point_table,
        line_evidence_table=line_evidence_table,
        element_grids=element_grids,
        state_grids=state_grids,
        x_coords=tuple(run_data.x_coords),
        y_coords=tuple(run_data.y_coords),
        x_edges=_coordinate_edges(run_data.x_coords),
        y_edges=_coordinate_edges(run_data.y_coords),
        summary_table=summary_table,
        diagnostics=diagnostics,
        element_colors=_element_color_map(elements),
        element_intensity_grids=element_intensity_grids,
    )
    result_write_started = time.perf_counter()
    _save_multi_result_cache(result, result_signature)
    result.diagnostics.setdefault("stage_timings", stage_timings)
    result.diagnostics["stage_timings"]["result_cache_write_seconds"] = float(time.perf_counter() - result_write_started)
    return result


def build_element_map(
    run_data: MappingRunData,
    preprocess_config: MappingPreprocessConfig,
    element_config: ElementMapConfig,
    *,
    force_rebuild_cache: bool = False,
    force_result_rebuild: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> ElementMapResult:
    symbols = element_config.element_symbols
    if len(symbols) != 1:
        raise ValueError("Multi-element maps are not implemented yet. Select one element for V1.")
    if element_config.colormap not in MAPPING_PALETTES:
        raise ValueError(f"Unsupported mapping palette: {element_config.colormap}")

    overall_started = time.perf_counter()
    stage_timings: dict[str, float] = {}
    preprocess_started = time.perf_counter()
    processed = load_or_build_processed_mapping_run(
        run_data,
        preprocess_config,
        force_rebuild=force_rebuild_cache,
        progress_callback=progress_callback,
    )
    stage_timings["preprocess_load_or_build_seconds"] = float(time.perf_counter() - preprocess_started)
    result_signature = _analysis_result_signature(run_data, preprocess_config, element_config, "single")
    if not force_rebuild_cache and not force_result_rebuild:
        result_load_started = time.perf_counter()
        cached_result = _load_cached_element_result(run_data, preprocess_config, element_config, result_signature)
        if cached_result is not None:
            cached_result.diagnostics.setdefault("stage_timings", {})
            if isinstance(cached_result.diagnostics["stage_timings"], dict):
                cached_result.diagnostics["stage_timings"]["result_cache_load_seconds"] = float(time.perf_counter() - result_load_started)
            if progress_callback:
                progress_callback("cache", 1, 1, "Using cached element map")
            return cached_result
    base_wavelengths = processed.wavelengths
    candidates = _candidate_lines(element_config, base_wavelengths)
    point_table = _point_metadata(run_data)
    if progress_callback:
        progress_callback("candidate scan", 0, len(candidates), f"Scanning {symbols[0]} candidate lines")
    candidate_started = time.perf_counter()
    line_scores, line_value_columns = _scan_candidate_lines(
        candidates,
        processed,
        point_table,
        peak_window_nm=float(element_config.peak_window_nm),
        aggregate_method=preprocess_config.aggregate_shots,
        pruning_enabled=bool(element_config.candidate_pruning_enabled),
        max_candidate_lines=int(element_config.max_candidate_lines),
        min_prominence_sigma=float(element_config.min_prominence_sigma),
    )
    stage_timings["candidate_scan_seconds"] = float(time.perf_counter() - candidate_started)
    if progress_callback:
        progress_callback("candidate scan", len(candidates), len(candidates), "Candidate scan complete")

    shots_used_by_point: dict[str, int] = {}
    for _, shot_row in processed.shot_table.iterrows():
        point_key = str(shot_row["point_key"])
        shots_used_by_point[point_key] = shots_used_by_point.get(point_key, 0) + 1

    for line_id, values in line_value_columns.items():
        point_table[f"line_{line_id}_area"] = values

    line_scores["accepted"] = _accepted_line_mask(line_scores)
    line_scores["reason"] = line_scores.get("candidate_reason", "candidate")
    line_scores.loc[line_scores["accepted"], "reason"] = "accepted"
    line_scores.loc[~line_scores["accepted"] & line_scores["candidate_selected"].astype(bool), "reason"] = "selected_but_low_signal"
    line_scores.loc[
        ~line_scores["accepted"]
        & ~line_scores["candidate_selected"].astype(bool)
        & line_scores["is_overlapped"].astype(bool),
        "reason",
    ] = "overlapped_pruned_by_measured_score"
    line_scores.loc[~line_scores["accepted"] & (line_scores["p95_intensity"].fillna(0.0) <= 0), "reason"] = "no_signal"

    accepted = line_scores[line_scores["accepted"]].copy()
    if accepted.empty:
        raise ValueError(f"No usable spectral lines found for {symbols[0]} in this mapping run.")

    weighted_values = np.zeros(len(point_table), dtype=float)
    weight_sum = np.zeros(len(point_table), dtype=float)
    accepted_weights = []
    for _, line in accepted.iterrows():
        line_id = str(line["line_id"])
        values = np.asarray(line_value_columns[line_id], dtype=float)
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            continue
        scale = float(np.nanpercentile(finite, 95))
        if not np.isfinite(scale) or scale <= EPS:
            scale = float(np.nanmax(finite)) if finite.size else 1.0
        if not np.isfinite(scale) or scale <= EPS:
            scale = 1.0
        normalized_values = values / scale
        weight = max(float(line["raw_weight"]), EPS)
        finite_mask = np.isfinite(normalized_values)
        weighted_values[finite_mask] += normalized_values[finite_mask] * weight
        weight_sum[finite_mask] += weight
        accepted_weights.append((line_id, weight))

    final_values = np.full(len(point_table), np.nan, dtype=float)
    valid_weight = weight_sum > 0
    final_values[valid_weight] = weighted_values[valid_weight] / weight_sum[valid_weight]
    point_table["normalized_element_intensity"] = final_values
    point_table["shots_used"] = [int(shots_used_by_point.get(point_key, 0)) for point_key in point_table["point_key"].tolist()]

    point_table, drift_diagnostics = _apply_optional_drift_correction(point_table, preprocess_config)
    value_grid, grid_matrix = _grid_from_points(point_table, run_data.row_indices, run_data.column_indices)
    stage_timings["evidence_fusion_seconds"] = float(time.perf_counter() - overall_started - stage_timings["preprocess_load_or_build_seconds"] - stage_timings["candidate_scan_seconds"])
    stage_timings["total_build_seconds"] = float(time.perf_counter() - overall_started)

    diagnostics = {
        "spectra_total": run_data.shots_total,
        "spectra_readable": processed.readable_shots,
        "spectra_missing": run_data.missing_spectra,
        "spectra_failed": int((processed.diagnostics["read_error"].fillna("") != "").sum()),
        "accepted_line_ids": [line_id for line_id, _weight in accepted_weights],
        "line_count": int(len(line_scores)),
        "accepted_line_count": int(line_scores["accepted"].sum()),
        "overlap_warning": bool((line_scores["accepted"] & line_scores["is_overlapped"]).any()),
        "preprocess_cache_hit": bool(processed.cache_hit),
        "preprocess_cache_signature": processed.signature,
        "preprocess_cache_dir": str(processed.cache_dir),
        "result_cache_hit": False,
        "result_cache_signature": result_signature,
        "result_cache_dir": str(_analysis_result_cache_dir(run_data, result_signature)),
        "candidate_pruning_enabled": bool(element_config.candidate_pruning_enabled),
        "max_candidate_lines": int(element_config.max_candidate_lines),
        "stage_timings": stage_timings,
        "preprocessing": {
            "smoothing_method": preprocess_config.smoothing_method if preprocess_config.smoothing_enabled else "None",
            "smoothing_strength": preprocess_config.smoothing_strength,
            "baseline_enabled": preprocess_config.baseline_enabled,
            "normalization": preprocess_config.normalization,
            "aggregate_shots": preprocess_config.aggregate_shots,
        },
        "shot_diagnostics": processed.diagnostics.to_dict(orient="records"),
    }
    diagnostics.update(drift_diagnostics)

    result = ElementMapResult(
        run_data=run_data,
        preprocess_config=preprocess_config,
        element_config=element_config,
        point_table=point_table,
        grid_matrix=grid_matrix,
        line_scores=line_scores,
        value_grid=value_grid,
        x_coords=tuple(run_data.x_coords),
        y_coords=tuple(run_data.y_coords),
        x_edges=_coordinate_edges(run_data.x_coords),
        y_edges=_coordinate_edges(run_data.y_coords),
        diagnostics=diagnostics,
    )
    result_write_started = time.perf_counter()
    _save_element_result_cache(result, result_signature)
    result.diagnostics.setdefault("stage_timings", stage_timings)
    result.diagnostics["stage_timings"]["result_cache_write_seconds"] = float(time.perf_counter() - result_write_started)
    return result


def _safe_export_token(value: str) -> str:
    token = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in str(value).strip())
    return token or "element"


def _safe_sheet_name(value: str, used: set[str]) -> str:
    base = _safe_export_token(value)[:28] or "sheet"
    candidate = base
    suffix = 1
    while candidate in used:
        suffix_text = f"_{suffix}"
        candidate = f"{base[:31 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used.add(candidate)
    return candidate


def _grid_matrix_dataframe(grid: np.ndarray, row_indices: Sequence[int], column_indices: Sequence[int]) -> pd.DataFrame:
    return pd.DataFrame(
        np.asarray(grid, dtype=float),
        index=[f"R{int(row):03d}" for row in row_indices],
        columns=[f"C{int(column):03d}" for column in column_indices],
    )


def _optional_parquet_exports(tables: dict[str, pd.DataFrame], base: Path, enabled: bool) -> list[Path]:
    if not enabled:
        return []
    written: list[Path] = []
    try:
        import pyarrow  # noqa: F401
    except Exception:
        return []
    for name, table in tables.items():
        path = Path(f"{base}_{_safe_export_token(name)}.parquet")
        try:
            table.to_parquet(path, index=False)
        except Exception:
            continue
        written.append(path)
    return written


def _export_multi_element_result(result: MultiElementMapResult, file_path: str | os.PathLike[str], *, include_parquet: bool = False) -> list[Path]:
    output_path = Path(file_path)
    suffix = output_path.suffix.lower()
    base = output_path.with_suffix("")
    written: list[Path] = []

    if suffix == ".xlsx":
        used_sheets: set[str] = set()
        with pd.ExcelWriter(output_path) as writer:
            result.point_element_table.to_excel(writer, sheet_name=_safe_sheet_name("point_element", used_sheets), index=False)
            result.wide_point_table.to_excel(writer, sheet_name=_safe_sheet_name("wide_points", used_sheets), index=False)
            result.line_evidence_table.to_excel(writer, sheet_name=_safe_sheet_name("line_evidence", used_sheets), index=False)
            result.summary_table.to_excel(writer, sheet_name=_safe_sheet_name("summary", used_sheets), index=False)
            for element in result.config.element_symbols:
                grid_df = _grid_matrix_dataframe(result.element_grids[element], result.run_data.row_indices, result.run_data.column_indices)
                grid_df.to_excel(writer, sheet_name=_safe_sheet_name(f"grid_{element}", used_sheets))
            pd.DataFrame([result.manifest_payload()]).to_excel(writer, sheet_name=_safe_sheet_name("manifest", used_sheets), index=False)
        written.append(output_path)
    elif suffix == ".csv":
        result.point_element_table.to_csv(output_path, index=False)
        written.append(output_path)
        wide_path = Path(f"{base}_wide.csv")
        lines_path = Path(f"{base}_lines.csv")
        summary_path = Path(f"{base}_summary.csv")
        result.wide_point_table.to_csv(wide_path, index=False)
        result.line_evidence_table.to_csv(lines_path, index=False)
        result.summary_table.to_csv(summary_path, index=False)
        written.extend([wide_path, lines_path, summary_path])
        for element in result.config.element_symbols:
            grid_path = Path(f"{base}_grid_{_safe_export_token(element)}.csv")
            grid_df = _grid_matrix_dataframe(result.element_grids[element], result.run_data.row_indices, result.run_data.column_indices)
            grid_df.to_csv(grid_path)
            written.append(grid_path)
    else:
        raise ValueError(f"Unsupported mapping export format: {suffix}")

    manifest_path = Path(f"{base}_manifest.json")
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(result.manifest_payload(), handle, indent=2, sort_keys=True)
    written.append(manifest_path)
    written.extend(
        _optional_parquet_exports(
            {
                "point_element": result.point_element_table,
                "wide_points": result.wide_point_table,
                "line_evidence": result.line_evidence_table,
                "summary": result.summary_table,
            },
            base,
            include_parquet,
        )
    )
    return written


def export_mapping_result(
    result: ElementMapResult | MultiElementMapResult,
    file_path: str | os.PathLike[str],
    *,
    include_parquet: bool = False,
) -> list[Path]:
    if isinstance(result, MultiElementMapResult):
        return _export_multi_element_result(result, file_path, include_parquet=include_parquet)

    output_path = Path(file_path)
    suffix = output_path.suffix.lower()
    base = output_path.with_suffix("")
    written: list[Path] = []

    if suffix == ".xlsx":
        with pd.ExcelWriter(output_path) as writer:
            result.point_table.to_excel(writer, sheet_name="point_map", index=False)
            result.grid_matrix.to_excel(writer, sheet_name="grid_matrix")
            result.line_scores.to_excel(writer, sheet_name="line_scores", index=False)
            pd.DataFrame([result.manifest_payload()]).to_excel(writer, sheet_name="manifest", index=False)
        written.append(output_path)
    elif suffix == ".csv":
        result.point_table.to_csv(output_path, index=False)
        written.append(output_path)
        grid_path = Path(f"{base}_grid.csv")
        lines_path = Path(f"{base}_lines.csv")
        result.grid_matrix.to_csv(grid_path)
        result.line_scores.to_csv(lines_path, index=False)
        written.extend([grid_path, lines_path])
    else:
        raise ValueError(f"Unsupported mapping export format: {suffix}")

    manifest_path = Path(f"{base}_manifest.json")
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(result.manifest_payload(), handle, indent=2, sort_keys=True)
    written.append(manifest_path)
    written.extend(
        _optional_parquet_exports(
            {
                "point_map": result.point_table,
                "line_scores": result.line_scores,
                "grid_matrix": result.grid_matrix.reset_index(),
            },
            base,
            include_parquet,
        )
    )
    return written
