"""Shared resource and writable-path helpers."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


def source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return source_root()


def working_root() -> Path:
    if getattr(sys, "frozen", False):
        if platform.system() == "Linux":
            xdg_data_home = os.environ.get("XDG_DATA_HOME")
            base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
            return base / "prolibspector"
        return Path(sys.executable).resolve().parent
    return source_root()


def resource_path(*parts: str) -> str:
    return str(resource_root().joinpath(*parts))


def working_path(*parts: str) -> str:
    return str(working_root().joinpath(*parts))


def settings_dir() -> Path:
    if getattr(sys, "frozen", False):
        if platform.system() == "Linux":
            return working_root() / ".libs_settings"
        return Path.home() / ".libs_settings"
    return source_root() / ".libs_settings"


def ensure_parent(path: str | Path) -> Path:
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def prepare_frozen_runtime() -> None:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        os.chdir(sys._MEIPASS)

