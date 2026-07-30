"""Helpers for selecting and filtering bundled spectral line databases."""

from __future__ import annotations

from typing import Iterable

import pandas as pd

from prolibspector.core.paths import resource_path

DEFAULT_DATABASE_LABEL = "Standard database"
PERSISTENT_DATABASE_LABEL = "Persistent Lines database"

DATABASE_OPTIONS = {
    DEFAULT_DATABASE_LABEL: "element_database.csv",
    PERSISTENT_DATABASE_LABEL: "persistent_lines.csv",
}


def database_option_names() -> tuple[str, ...]:
    return tuple(DATABASE_OPTIONS.keys())


def get_database_path(selection: str = DEFAULT_DATABASE_LABEL) -> str:
    file_name = DATABASE_OPTIONS.get(selection, DATABASE_OPTIONS[DEFAULT_DATABASE_LABEL])
    return resource_path(file_name)


def load_element_database(path: str) -> pd.DataFrame:
    element_df = pd.read_csv(path)
    element_df["Ionization Level"] = pd.to_numeric(element_df["Ionization Level"], errors="coerce")
    element_df = element_df.dropna(subset=["Ionization Level"]).copy()
    element_df["Ionization Level"] = element_df["Ionization Level"].astype(int)
    element_df["Symbol"] = element_df["Symbol"].astype(str).str.strip()
    element_df = element_df[~element_df["Symbol"].isin(("", "nan"))]
    element_df["Wavelength"] = pd.to_numeric(element_df["Wavelength"], errors="coerce")
    element_df = element_df[element_df["Wavelength"] > 0].copy()
    return element_df


def filter_element_database(
    path: str,
    selected_symbols: Iterable[str],
    ionization_levels: Iterable[bool],
) -> pd.DataFrame:
    element_df = load_element_database(path)
    normalized_symbols = {str(symbol).strip() for symbol in selected_symbols}
    levels_to_include = [index + 1 for index, enabled in enumerate(ionization_levels) if enabled]

    filtered = element_df[element_df["Symbol"].isin(normalized_symbols)]
    if levels_to_include:
        filtered = filtered[filtered["Ionization Level"].isin(levels_to_include)]
    return filtered.copy()
