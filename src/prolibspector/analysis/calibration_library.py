"""Helpers for reading and writing the local calibration library."""

from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from prolibspector.core.paths import resource_path, working_path

CALIBRATION_LIBRARY_COLUMNS = [
    "wavelength",
    "element_symbol",
    "ionization_level",
    "relative_intensity",
    "concentration",
    "units",
]


def calibration_library_path() -> Path:
    return Path(working_path("calibration_data_library.csv"))


def bundled_calibration_library_path() -> Path:
    return Path(resource_path("calibration_data_library.csv"))


def ensure_calibration_library() -> Path:
    library_path = calibration_library_path()
    if library_path.exists():
        return library_path

    bundled_path = bundled_calibration_library_path()
    if getattr(sys, "frozen", False) and bundled_path.exists():
        library_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled_path, library_path)

    return library_path


def calibration_library_exists() -> bool:
    return ensure_calibration_library().exists()


def load_calibration_library() -> pd.DataFrame:
    return pd.read_csv(ensure_calibration_library())


def append_calibration_entries(entries: Iterable[Sequence[object]]) -> Path:
    library_path = ensure_calibration_library()
    library_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = (not library_path.exists()) or library_path.stat().st_size == 0

    with library_path.open("a", newline="", encoding="utf-8") as csvfile:
        csv_writer = csv.writer(csvfile)
        if write_header:
            csv_writer.writerow(CALIBRATION_LIBRARY_COLUMNS)
        for entry in entries:
            csv_writer.writerow(entry)

    return library_path
