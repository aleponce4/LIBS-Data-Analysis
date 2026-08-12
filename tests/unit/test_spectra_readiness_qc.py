"""Unit tests for the public-edition spectra readiness QC scorer."""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pytest

from prolibspector.acquisition.plate_autosave import PlateAutosaveConfig, PlateRunState
from prolibspector.acquisition.spectra_readiness_qc import (
    GATE_FAIL,
    GATE_PASS,
    GATE_WARN,
    HeuristicSpectraQC,
    apply_spectra_qc_to_plate_states,
    load_default_spectra_qc,
    run_high_throughput_spectra_qc,
)

WAVELENGTHS = np.linspace(200.0, 1000.0, 2048)


def _good_spectrum() -> np.ndarray:
    rng = np.random.default_rng(7)
    peak = 20_000.0 * np.exp(-0.5 * ((WAVELENGTHS - 400.0) / 0.6) ** 2)
    return 500.0 + peak + rng.normal(0.0, 5.0, WAVELENGTHS.size)


def _saturated_spectrum() -> np.ndarray:
    return np.full_like(WAVELENGTHS, 65_000.0)


def _flat_spectrum() -> np.ndarray:
    return np.full_like(WAVELENGTHS, 100.0)


def test_load_default_qc_is_available_and_names_its_scorer():
    result = load_default_spectra_qc()
    assert result.available is True
    assert result.qc is not None
    # The message must not imply a trained calibration is in use.
    assert "heuristic" in result.message.lower()
    assert "private edition" in result.message.lower()


def test_good_spectrum_passes():
    row = HeuristicSpectraQC().score_spectrum(WAVELENGTHS, _good_spectrum())
    assert row["gate_status"] == GATE_PASS
    assert row["failure_reasons"] == []
    assert row["readiness_score"] > 50.0


def test_saturated_spectrum_fails_with_reason():
    row = HeuristicSpectraQC().score_spectrum(WAVELENGTHS, _saturated_spectrum())
    assert row["gate_status"] == GATE_FAIL
    assert any("saturat" in reason for reason in row["failure_reasons"])
    assert row["readiness_score"] == 0.0


def test_flat_spectrum_fails_and_does_not_score_high():
    """A dead detector has zero noise; it must not read as infinite SNR."""
    row = HeuristicSpectraQC().score_spectrum(WAVELENGTHS, _flat_spectrum())
    assert row["gate_status"] == GATE_FAIL
    assert any("flat trace" in reason for reason in row["failure_reasons"])
    assert row["readiness_score"] == 0.0


def test_empty_spectrum_fails_rather_than_raising():
    row = HeuristicSpectraQC().score_spectrum(np.array([]), np.array([]))
    assert row["gate_status"] == GATE_FAIL
    assert row["readiness_score"] == 0.0


def _plate_with_shots(tmp_path: Path, profiles: list[np.ndarray]) -> tuple[PlateRunState, Path]:
    config = PlateAutosaveConfig(plate_type=6, plate_name="QC Plate", shots_per_well=1)
    state = PlateRunState(config)
    plate_dir = tmp_path / config.safe_plate_name
    plate_dir.mkdir(parents=True)
    for profile in profiles:
        well, shot = state.next_assignment()
        path = plate_dir / f"{well}_shot{shot:02d}.csv"
        np.savetxt(
            path,
            np.column_stack((WAVELENGTHS, profile)),
            delimiter="\t",
            header="Wavelength\tIntensity",
            comments="",
            fmt="%.6f",
        )
        state.record_saved(str(path))
    return state, plate_dir


def test_run_qc_scores_every_shot_and_writes_csv(tmp_path: Path):
    state, plate_dir = _plate_with_shots(
        tmp_path, [_good_spectrum(), _saturated_spectrum(), _flat_spectrum()]
    )
    result = run_high_throughput_spectra_qc(str(tmp_path), state)
    mapping = result.to_mapping()

    assert mapping["pass_count"] == 1
    assert mapping["fail_count"] == 2
    assert len(mapping["rows"]) == 3
    # The public edition writes no PDF and must not claim one.
    assert mapping["pdf_path"] == ""

    csv_path = Path(mapping["csv_path"])
    assert csv_path.parent == plate_dir
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert {row["well"] for row in rows} == {"A1", "A2", "A3"}


def test_unreadable_shot_is_reported_not_skipped(tmp_path: Path):
    state, _plate_dir = _plate_with_shots(tmp_path, [_good_spectrum()])
    state.records[0].filepath = str(tmp_path / "does_not_exist.csv")

    result = run_high_throughput_spectra_qc(str(tmp_path), state)
    mapping = result.to_mapping()
    assert mapping["fail_count"] == 1
    assert "could not read spectrum" in mapping["rows"][0]["top_failure_reasons"]


def test_apply_qc_uses_worst_status_per_well(tmp_path: Path):
    config = PlateAutosaveConfig(plate_type=6, plate_name="Worst", shots_per_well=2)
    state = PlateRunState(config)
    plate_dir = tmp_path / config.safe_plate_name
    plate_dir.mkdir(parents=True)
    for profile in (_good_spectrum(), _saturated_spectrum()):
        well, shot = state.next_assignment()
        path = plate_dir / f"{well}_shot{shot:02d}.csv"
        np.savetxt(
            path,
            np.column_stack((WAVELENGTHS, profile)),
            delimiter="\t",
            header="Wavelength\tIntensity",
            comments="",
            fmt="%.6f",
        )
        state.record_saved(str(path))

    result = run_high_throughput_spectra_qc(str(tmp_path), state)
    apply_spectra_qc_to_plate_states(result, state)

    # Both shots landed in A1; one passed and one failed.
    assert state.qc_by_well["A1"]["status"] == GATE_FAIL
    assert state.qc_by_well["A1"]["shots_scored"] == 2
    assert state.progress_payload()["qc_by_well"]["A1"]["status"] == GATE_FAIL


@pytest.mark.parametrize(
    "noise_scale,expected",
    [
        # Peak sits ~20 000 counts above baseline, so SNR ~= 20000 / noise_scale.
        (5.0, GATE_PASS),  # SNR ~4000
        (2_000.0, GATE_WARN),  # SNR ~10, between warn (15) and fail (5)
        (8_000.0, GATE_FAIL),  # SNR ~2.5, below the fail threshold
    ],
)
def test_snr_gate_responds_to_noise(noise_scale, expected):
    rng = np.random.default_rng(11)
    peak = 20_000.0 * np.exp(-0.5 * ((WAVELENGTHS - 400.0) / 0.6) ** 2)
    spectrum = 500.0 + peak + rng.normal(0.0, noise_scale, WAVELENGTHS.size)
    assert HeuristicSpectraQC().score_spectrum(WAVELENGTHS, spectrum)["gate_status"] == expected
