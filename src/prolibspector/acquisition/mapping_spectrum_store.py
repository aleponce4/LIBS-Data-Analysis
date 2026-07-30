"""Binary spectrum storage for 2D mapping acquisition runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

import numpy as np


MAPPING_SPECTRUM_STORE_DIRNAME = "_mapping_spectrum_store"
MAPPING_SPECTRUM_STORE_VERSION = 1
MAPPING_SPECTRUM_MANIFEST_WRITE_INTERVAL = 100
# Flushing msyncs the full memmap, which dominated per-shot save cost; batching
# bounds the power-loss window to the rows since the last flush (resume then
# re-shoots them). Clean stops always flush via write_manifest()/close().
MAPPING_SPECTRUM_FLUSH_ROW_INTERVAL = 25
MAPPING_SPECTRUM_FLUSH_MAX_AGE_S = 2.0


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return str(value)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=_json_default)
    os.replace(tmp_path, path)
    return str(path)


def mapping_spectrum_store_dir(run_directory: str | os.PathLike[str]) -> Path:
    return Path(run_directory) / MAPPING_SPECTRUM_STORE_DIRNAME


def mapping_spectrum_store_manifest_path(run_directory: str | os.PathLike[str]) -> Path:
    return mapping_spectrum_store_dir(run_directory) / "manifest.json"


def binary_row_index_for_target(
    *,
    row_index: int,
    column_index: int,
    shot_number: int,
    columns: int,
    shots_per_point: int,
) -> int:
    return ((int(row_index) - 1) * int(columns) + (int(column_index) - 1)) * int(shots_per_point) + (
        int(shot_number) - 1
    )


@dataclass
class MappingSpectrumStore:
    """Append-by-row binary store for raw mapping shot spectra."""

    run_directory: Path
    targets_total: int
    config_summary: dict[str, Any]
    wavelength_count: int | None = None
    wavelengths: np.ndarray | None = None
    intensities: np.memmap | None = None
    written_mask: np.memmap | None = None
    manifest: dict[str, Any] | None = None
    _completed_count: int | None = None
    _manifest_dirty_rows: int = 0
    # Rows whose intensities are written in memory but whose mask bit is
    # deferred until the next flush(), so a durable mask bit always implies
    # durable intensity data (mask bits enter the page cache only after the
    # intensities flush returned).
    _pending_mask_rows: set = field(default_factory=set)
    _rows_since_flush: int = 0
    _last_flush_at: float | None = None
    _closed: bool = False
    # The save-writer thread owns write_shot, but the acquisition thread also
    # flushes via write_manifest/close at run boundaries and abort paths while
    # writer jobs may still be draining; every state-touching method locks.
    _lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def store_dir(self) -> Path:
        return mapping_spectrum_store_dir(self.run_directory)

    @property
    def wavelengths_path(self) -> Path:
        return self.store_dir / "wavelengths.npy"

    @property
    def intensities_path(self) -> Path:
        return self.store_dir / "intensities.npy"

    @property
    def written_mask_path(self) -> Path:
        return self.store_dir / "written_mask.npy"

    @property
    def manifest_path(self) -> Path:
        return self.store_dir / "manifest.json"

    @classmethod
    def open(
        cls,
        run_directory: str | os.PathLike[str],
        *,
        targets_total: int,
        config_summary: dict[str, Any],
    ) -> "MappingSpectrumStore":
        store = cls(Path(run_directory), int(targets_total), dict(config_summary))
        store.store_dir.mkdir(parents=True, exist_ok=True)
        store._load_existing_manifest()
        return store

    def _load_existing_manifest(self) -> None:
        if not self.manifest_path.exists():
            return
        try:
            with self.manifest_path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except Exception:
            return
        if isinstance(manifest, dict):
            self.manifest = manifest
            try:
                self.wavelength_count = int(manifest.get("wavelength_count") or 0) or None
            except (TypeError, ValueError):
                self.wavelength_count = None
            try:
                self._completed_count = int(manifest.get("completed_row_count") or 0)
            except (TypeError, ValueError):
                self._completed_count = None

    def ensure_initialized(self, wavelengths: np.ndarray) -> None:
        axis = np.asarray(wavelengths, dtype=np.float64)
        if axis.ndim != 1 or axis.size <= 0:
            raise ValueError("Mapping binary store requires a non-empty 1D wavelength axis.")
        if self.wavelengths is not None and self.intensities is not None and self.written_mask is not None:
            if self.wavelengths.size != axis.size or not np.allclose(self.wavelengths, axis, rtol=0, atol=1e-9):
                raise ValueError("Captured wavelength axis changed during mapping binary save.")
            return

        existing_ready = (
            self.wavelengths_path.exists()
            and self.intensities_path.exists()
            and self.written_mask_path.exists()
            and self.wavelength_count == axis.size
        )
        if existing_ready:
            existing_axis = np.load(self.wavelengths_path, allow_pickle=False)
            if existing_axis.size == axis.size and np.allclose(existing_axis, axis, rtol=0, atol=1e-9):
                self.wavelengths = existing_axis.astype(np.float64, copy=False)
                self.intensities = np.load(self.intensities_path, mmap_mode="r+", allow_pickle=False)
                self.written_mask = np.load(self.written_mask_path, mmap_mode="r+", allow_pickle=False)
                if self.intensities.shape == (self.targets_total, axis.size) and self.written_mask.shape == (self.targets_total,):
                    if self._completed_count is None:
                        self._completed_count = int(np.count_nonzero(self.written_mask))
                    return

        np.save(self.wavelengths_path, axis, allow_pickle=False)
        self.intensities = np.lib.format.open_memmap(
            self.intensities_path,
            mode="w+",
            dtype=np.float32,
            shape=(self.targets_total, axis.size),
        )
        self.written_mask = np.lib.format.open_memmap(
            self.written_mask_path,
            mode="w+",
            dtype=np.bool_,
            shape=(self.targets_total,),
        )
        self.written_mask[:] = False
        self.written_mask.flush()
        self.wavelengths = axis
        self.wavelength_count = int(axis.size)
        self._completed_count = 0
        self.write_manifest(force=True)

    def write_shot(self, row_index: int, wavelengths: np.ndarray, intensities: np.ndarray, *, force_manifest: bool = False) -> str:
        with self._lock:
            if self._closed:
                # A late save-writer job after close() must fail loudly, not
                # silently reopen the memmaps: the reopened row's mask bit
                # would sit pending forever and could be claimed by state.
                raise RuntimeError("Mapping spectrum store is closed.")
            self.ensure_initialized(wavelengths)
            assert self.intensities is not None
            assert self.written_mask is not None
            row_index = int(row_index)
            if row_index < 0 or row_index >= self.targets_total:
                raise IndexError(f"Mapping binary row index out of range: {row_index}")
            values = np.asarray(intensities, dtype=np.float32)
            if values.ndim != 1 or values.size != self.intensities.shape[1]:
                raise ValueError("Captured intensity axis does not match mapping binary store.")
            was_written = bool(self.written_mask[row_index]) or row_index in self._pending_mask_rows
            self.intensities[row_index, :] = values
            self._pending_mask_rows.add(row_index)
            self._rows_since_flush += 1
            if not was_written:
                self._completed_count = int(self._completed_count or 0) + 1
                self._manifest_dirty_rows += 1
            if force_manifest or self._manifest_dirty_rows >= MAPPING_SPECTRUM_MANIFEST_WRITE_INTERVAL:
                self.write_manifest(force=True)
            elif self._flush_due():
                self.flush()
            return f"{MAPPING_SPECTRUM_STORE_DIRNAME}/intensities.npy#{row_index}"

    def _flush_due(self) -> bool:
        if self._rows_since_flush >= MAPPING_SPECTRUM_FLUSH_ROW_INTERVAL:
            return True
        if self._rows_since_flush <= 0:
            return False
        if self._last_flush_at is None:
            return True
        return (time.monotonic() - self._last_flush_at) >= MAPPING_SPECTRUM_FLUSH_MAX_AGE_S

    def flush(self) -> None:
        """Flush intensities to disk, then publish and flush the mask bits."""
        with self._lock:
            if self.intensities is None or self.written_mask is None:
                return
            if self._rows_since_flush > 0 or self._pending_mask_rows:
                self.intensities.flush()
                for row in self._pending_mask_rows:
                    self.written_mask[row] = True
                self._pending_mask_rows.clear()
                self.written_mask.flush()
            self._rows_since_flush = 0
            self._last_flush_at = time.monotonic()

    def is_row_written(self, row_index: int) -> bool:
        with self._lock:
            try:
                row_index = int(row_index)
                if row_index in self._pending_mask_rows:
                    return True
                mask = self.written_mask
                if mask is None:
                    if not self.written_mask_path.exists():
                        return False
                    mask = np.load(self.written_mask_path, allow_pickle=False)
                    if not self._closed:
                        self.written_mask = mask
                return 0 <= row_index < mask.shape[0] and bool(mask[row_index])
            except Exception:
                return False

    def completed_count(self) -> int:
        with self._lock:
            self.flush()
            mask = self.written_mask
            if mask is None:
                if not self.written_mask_path.exists():
                    return 0
                mask = np.load(self.written_mask_path, allow_pickle=False)
                if not self._closed:
                    self.written_mask = mask
            self._completed_count = int(np.count_nonzero(mask))
            return int(self._completed_count)

    def write_manifest(self, *, force: bool = False) -> str:
        with self._lock:
            return self._write_manifest_locked(force=force)

    def _write_manifest_locked(self, *, force: bool = False) -> str:
        if not force and self._manifest_dirty_rows <= 0 and self.manifest_path.exists():
            return str(self.manifest_path)
        # The manifest's completed_row_count must never claim rows whose bytes
        # are not yet durable.
        self.flush()
        completed = int(self._completed_count or 0)
        payload = {
            "version": MAPPING_SPECTRUM_STORE_VERSION,
            "created_or_updated_at": datetime.now(timezone.utc).isoformat(),
            "storage_kind": "binary_npy",
            "dtype": "float32",
            "wavelength_dtype": "float64",
            "targets_total": int(self.targets_total),
            "wavelength_count": int(self.wavelength_count or 0),
            "shape": [int(self.targets_total), int(self.wavelength_count or 0)],
            "completed_row_count": int(completed),
            "files": {
                "wavelengths": "wavelengths.npy",
                "intensities": "intensities.npy",
                "written_mask": "written_mask.npy",
            },
            "config": dict(self.config_summary),
        }
        self.manifest = payload
        self._manifest_dirty_rows = 0
        return _write_json_atomic(self.manifest_path, payload)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.flush()
            self.write_manifest(force=True)
            self._closed = True
            for attr in ("intensities", "written_mask"):
                array = getattr(self, attr)
                if array is not None:
                    try:
                        array.flush()
                    except Exception:
                        pass
                    mmap = getattr(array, "_mmap", None)
                    if mmap is not None:
                        try:
                            mmap.close()
                        except Exception:
                            pass
                setattr(self, attr, None)
            self.wavelengths = None


def binary_row_is_written(
    run_directory: str | os.PathLike[str],
    row_index: int | str | None,
    written_mask: np.ndarray | None = None,
) -> bool:
    if row_index is None or row_index == "":
        return False
    try:
        row = int(row_index)
    except (TypeError, ValueError):
        return False
    try:
        mask = written_mask
        if mask is None:
            mask = load_binary_written_mask(run_directory)
        if mask is None:
            return False
        return 0 <= row < mask.shape[0] and bool(mask[row])
    except Exception:
        return False


def mapping_spectrum_store_summary(
    run_directory: str | os.PathLike[str],
    *,
    targets_total: int | None = None,
    csv_compatibility_enabled: bool | None = None,
) -> dict[str, Any]:
    store_dir = mapping_spectrum_store_dir(run_directory)
    manifest_path = store_dir / "manifest.json"
    mask = load_binary_written_mask(run_directory)
    completed = int(np.count_nonzero(mask)) if mask is not None else 0
    mask_total = int(mask.shape[0]) if mask is not None else 0
    total = int(targets_total if targets_total is not None else mask_total)
    return {
        "storage_kind": "binary_npy",
        "store_dir": MAPPING_SPECTRUM_STORE_DIRNAME,
        "store_path": str(store_dir),
        "manifest_path": str(manifest_path),
        "manifest_exists": manifest_path.exists(),
        "csv_compatibility_enabled": bool(csv_compatibility_enabled) if csv_compatibility_enabled is not None else None,
        "binary_completed_rows": completed,
        "binary_targets_total": total,
        "binary_store_complete": bool(total > 0 and completed >= total),
        "binary_missing_rows": max(0, total - completed),
        "written_mask_exists": mask is not None,
    }


def load_binary_written_mask(run_directory: str | os.PathLike[str]) -> np.ndarray | None:
    mask_path = mapping_spectrum_store_dir(run_directory) / "written_mask.npy"
    if not mask_path.exists():
        return None
    try:
        return np.load(mask_path, allow_pickle=False)
    except Exception:
        return None


def read_binary_mapping_spectrum(
    run_directory: str | os.PathLike[str],
    row_index: int | str,
) -> tuple[np.ndarray, np.ndarray]:
    row = int(row_index)
    store_dir = mapping_spectrum_store_dir(run_directory)
    wavelengths = np.load(store_dir / "wavelengths.npy", allow_pickle=False)
    intensities = np.load(store_dir / "intensities.npy", mmap_mode="r", allow_pickle=False)
    mask = load_binary_written_mask(run_directory)
    try:
        if row < 0 or row >= intensities.shape[0]:
            raise IndexError(f"Mapping binary row index out of range: {row}")
        if mask is None or row >= mask.shape[0] or not bool(mask[row]):
            raise ValueError(f"Mapping binary row is not marked written: {row}")
        return np.asarray(wavelengths, dtype=np.float64), np.array(intensities[row, :], dtype=np.float32, copy=True)
    finally:
        mmap = getattr(intensities, "_mmap", None)
        if mmap is not None:
            try:
                mmap.close()
            except Exception:
                pass
