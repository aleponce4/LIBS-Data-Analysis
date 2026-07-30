"""Helpers for importing and exporting analysis spectra."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


def reset_analysis_data(app) -> None:
    app.data = pd.DataFrame()
    app.x_data = pd.Series()
    app.y_data = pd.Series()
    app._original_y_data = None


def _valid_spectrum_arrays(df: pd.DataFrame, path: Path) -> tuple[np.ndarray, np.ndarray]:
    if df.shape[1] < 2:
        raise ValueError(f"Spectrum file has only {df.shape[1]} column(s), need at least 2: {path}")

    columns_by_lower = {str(column).strip().lower(): column for column in df.columns}
    wavelength_column = columns_by_lower.get("wavelength") or columns_by_lower.get("wavelength_nm") or df.columns[0]
    intensity_column = columns_by_lower.get("intensity") or columns_by_lower.get("intensities") or df.columns[1]
    wavelengths = pd.to_numeric(df[wavelength_column], errors="coerce").to_numpy(dtype=float)
    intensities = pd.to_numeric(df[intensity_column], errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(wavelengths) & np.isfinite(intensities)
    if not valid.any():
        raise ValueError(f"Spectrum file has no numeric wavelength/intensity rows: {path}")
    wavelengths = wavelengths[valid]
    intensities = intensities[valid]
    order = np.argsort(wavelengths, kind="stable")
    return wavelengths[order], intensities[order]


def read_spectrum_file(path: str | os.PathLike[str]) -> tuple[np.ndarray, np.ndarray]:
    """Read a two-column spectrum file as wavelength and intensity arrays."""
    spectrum_path = Path(path)
    try:
        with spectrum_path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = handle.readline()
        columns = [column.strip().lower() for column in header.rstrip("\r\n").split("\t")]
        if len(columns) >= 2 and columns[0] == "wavelength" and columns[1] == "intensity":
            data = np.loadtxt(spectrum_path, delimiter="\t", skiprows=1, usecols=(0, 1), dtype=float, ndmin=2)
            return _valid_spectrum_arrays(pd.DataFrame(data, columns=["Wavelength", "Intensity"]), spectrum_path)
    except (OSError, UnicodeDecodeError, ValueError):
        pass

    try:
        df = pd.read_csv(spectrum_path, sep="\t")
        if df.shape[1] < 2:
            df = pd.read_csv(spectrum_path)
    except Exception:
        df = pd.read_csv(spectrum_path, sep=None, engine="python")
    return _valid_spectrum_arrays(df, spectrum_path)


def write_spectrum_file(path: str | os.PathLike[str], wavelengths: np.ndarray, intensities: np.ndarray) -> Path:
    """Write a two-column spectrum file in the app's standard tab-delimited format."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = np.column_stack((np.asarray(wavelengths, dtype=float), np.asarray(intensities, dtype=float)))
    tmp_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    try:
        np.savetxt(tmp_path, data, delimiter="\t", header="Wavelength\tIntensity", comments="", fmt="%.6f")
        os.replace(tmp_path, output_path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    return output_path


def load_plaintext_spectra(file_paths: Iterable[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    all_data = pd.DataFrame()
    replicate_data = pd.DataFrame()

    for index, path in enumerate(file_paths):
        with open(path, "r", encoding="utf-8") as file:
            file_content = file.read()

        decimal_separator = "," if "," in file_content and "." not in file_content else "."
        delimiter = "\t" if "\t" in file_content else r"\s+"
        data = pd.read_csv(
            path,
            sep=delimiter,
            engine="python",
            header=None,
            decimal=decimal_separator,
            skiprows=1,
        )
        data = data.iloc[:, :2].copy()
        data.columns = ["Wavelength", f"Intensity_{index + 1}"]

        all_data = pd.concat([all_data, data], axis=0)
        if replicate_data.empty:
            replicate_data = data.copy()
        else:
            replicate_data = pd.merge(replicate_data, data, on="Wavelength", how="outer")

    averaged_data = all_data.groupby("Wavelength").mean().reset_index() if not all_data.empty else pd.DataFrame()
    return averaged_data, replicate_data


def spectrum_title_from_paths(file_paths: Iterable[str]) -> str:
    return ", ".join(os.path.basename(path) for path in file_paths)


def export_processed_spectrum(x_data, y_data, peak_data, file_path: str) -> list[Path]:
    file_extension = os.path.splitext(file_path)[1]
    base_name = os.path.splitext(file_path)[0]
    output_path = Path(file_path)
    written_paths = [output_path]

    spectrum_df = pd.DataFrame({"wavelength": x_data, "intensity": y_data})
    if file_extension == ".csv":
        spectrum_df.to_csv(output_path, index=False)
    elif file_extension == ".xlsx":
        spectrum_df.to_excel(output_path, index=False)
    else:
        raise ValueError(f"Unsupported export format: {file_extension}")

    if peak_data:
        peak_output_path = Path(f"{base_name}_peaks{file_extension}")
        peak_df = pd.DataFrame(
            peak_data,
            columns=["wavelength", "element_symbol", "ionization_level", "relative_intensity"],
        )
        if file_extension == ".csv":
            peak_df.to_csv(peak_output_path, index=False)
        elif file_extension == ".xlsx":
            peak_df.to_excel(peak_output_path, index=False)
        written_paths.append(peak_output_path)

    return written_paths
