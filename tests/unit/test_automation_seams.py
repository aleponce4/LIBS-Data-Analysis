"""Regression tests for the automation stack's boundary with the plate layer.

The automation modules and the plate/QC modules were written independently, so
they share names without automatically sharing meaning. Every test here pins one
seam that was silently wrong: the code imported cleanly, ran, and produced the
wrong result. Import checks cannot catch any of these.
"""

import json

import numpy as np
import pytest

from prolibspector.acquisition.plate_autosave import (
    ORDER_ROW,
    PlateAutosaveConfig,
    PlateRunState,
    load_plate_run_state,
    ordered_wells,
    records_from_plate_folder,
    save_plate_run_state,
    well_names,
)
from prolibspector.acquisition.spectra_readiness_qc import (
    GATE_FAIL,
    GATE_PASS,
    AutomatedQCWellRow,
    AutomatedSpectraQCResult,
    apply_spectra_qc_to_plate_states,
)


@pytest.fixture
def plate_config():
    return PlateAutosaveConfig(plate_type=96, plate_name="P1", shots_per_well=1, order_mode=ORDER_ROW)


# ── Well ordering is one function under two names ────────────────────────

def test_ordered_wells_is_the_same_function_as_well_names():
    """A plate written by one workflow is resumed by the other.

    If these ever diverge, an automated run and a manual run disagree about
    which well is next, and a resume silently shoots the wrong one.
    """
    assert ordered_wells is well_names
    assert ordered_wells(8, 12, ORDER_ROW)[:3] == ["A1", "A2", "A3"]
    assert ordered_wells(8, 12, "column")[:3] == ["A1", "B1", "C1"]


# ── Per-shot provenance survives the state round trip ────────────────────

def test_record_saved_keeps_per_shot_exposure(plate_config, tmp_path):
    """An automated run varies exposure per target, so the plate config cannot
    describe any individual shot. The record has to carry it."""
    state = PlateRunState(plate_config)
    state.record_saved(
        str(tmp_path / "shot.csv"),
        0,
        integration_time_ms=12.5,
        integration_time_us=12500,
        trigger_delay_us=3.0,
    )

    save_plate_run_state(tmp_path, state)
    reloaded = load_plate_run_state(tmp_path)
    record = reloaded.records[0]
    assert record.integration_time_ms == 12.5
    assert record.integration_time_us == 12500
    assert record.trigger_delay_us == 3.0


def test_manual_shots_omit_exposure_keys_entirely(plate_config):
    """A manual run must keep writing exactly the state shape it always wrote."""
    state = PlateRunState(plate_config)
    state.record_saved("shot.csv", 0)
    payload = state.records[0].to_mapping()

    assert "integration_time_ms" not in payload
    assert "trigger_delay_us" not in payload


def test_save_plate_run_state_merges_extra_metadata(plate_config, tmp_path):
    """Schedules that vary within a plate have nowhere else to live."""
    state = PlateRunState(plate_config)
    save_plate_run_state(tmp_path, state, extra={"column_delays_us": {"1": 2.0}})

    payload = json.loads((tmp_path / "plate_run_state.json").read_text(encoding="utf-8"))
    assert payload["column_delays_us"] == {"1": 2.0}
    assert payload["progress"], "extra must not displace the normal payload"


# ── Resume must not re-shoot burned wells ────────────────────────────────

def test_collision_suffixed_shots_are_still_found_on_resume(plate_config, tmp_path):
    """``unique_output_path`` appends ``_2`` on a filename collision.

    The scanner used to reject exactly the names this module generates, so a
    resumed run re-shot wells whose spectra were already on disk - destroying
    sample material that cannot be recovered.
    """
    plate_dir = tmp_path / "P1"
    plate_dir.mkdir()
    for name in (
        "P1_A1_shot01_20260814_120000_000000_001.csv",
        "P1_A2_shot01_20260814_120000_000001_002_2.csv",
    ):
        (plate_dir / name).write_text("Wavelength\tIntensity\n200\t1\n", encoding="utf-8")

    records = records_from_plate_folder(plate_dir)
    assert sorted(record.well for record in records) == ["A1", "A2"]


def test_plate_state_rebuilds_from_files_when_the_state_file_is_gone(plate_config, tmp_path):
    plate_dir = tmp_path / "P1"
    plate_dir.mkdir()
    for index, well in enumerate(("A1", "A2", "A3")):
        (plate_dir / f"P1_{well}_shot01_20260814_120000_00000{index}_00{index}.csv").write_text(
            "Wavelength\tIntensity\n200\t1\n", encoding="utf-8"
        )

    state = PlateRunState.from_records(plate_config, records_from_plate_folder(plate_dir))
    assert state.saved_shots == 3
    assert state.complete_wells == 3
    # A4 is next, not A1: the first three wells are already burned.
    assert state.next_assignment() == ("A4", 1)


# ── QC folds onto the right plate ────────────────────────────────────────

def _well_row(plate_index, well, status):
    return AutomatedQCWellRow(
        plate_index=plate_index, plate_name=f"P{plate_index}", well=well, gate_status=status
    )


def test_qc_applies_each_plates_own_wells_only(plate_config):
    """Folding one plate's rows onto another marks wells that were never shot."""
    states = {1: PlateRunState(plate_config), 2: PlateRunState(plate_config)}
    result = AutomatedSpectraQCResult(
        shot_rows=[
            {"plate_index": 1, "well": "A1", "gate_status": GATE_PASS, "failure_reasons": []},
            {"plate_index": 2, "well": "B2", "gate_status": GATE_FAIL, "failure_reasons": ["dark"]},
        ]
    )
    # The worker hands the whole {plate_index: state} mapping in one call.
    apply_spectra_qc_to_plate_states(
        type("R", (), {"rows": result.shot_rows})(), states
    )

    assert set(states[1].qc_by_well) == {"A1"}
    assert set(states[2].qc_by_well) == {"B2"}
    assert states[2].qc_by_well["B2"]["gate_status"] == GATE_FAIL


def test_single_plate_state_still_works(plate_config):
    """The manual workflow passes one state, not a mapping."""
    state = PlateRunState(plate_config)
    apply_spectra_qc_to_plate_states(
        type("R", (), {"rows": [{"well": "A1", "gate_status": GATE_PASS, "failure_reasons": []}]})(),
        state,
    )
    assert state.qc_by_well["A1"]["gate_status"] == GATE_PASS


def test_failed_well_targets_carry_what_a_repeat_needs():
    """A repeat has to drive back to the well, so it needs plate index and coordinates."""
    result = AutomatedSpectraQCResult(
        well_rows=[
            _well_row(1, "A1", GATE_PASS),
            AutomatedQCWellRow(
                plate_index=2,
                plate_name="P2",
                well="B2",
                gate_status=GATE_FAIL,
                target_x_mm=10.0,
                target_y_mm=20.0,
                failure_reasons=("low signal",),
            ),
        ]
    )

    assert result.pass_count == 1
    assert result.fail_count == 1
    failed = result.failed_well_targets
    assert len(failed) == 1
    assert failed[0]["plate_index"] == 2
    assert failed[0]["well"] == "B2"
    assert failed[0]["target_x_mm"] == 10.0
    assert failed[0]["top_failure_reasons"] == "low signal"


# ── The simulation reference spectrum must actually load ─────────────────

def test_simulation_reference_spectrum_resolves_under_the_src_layout():
    """Resolving this from the repo root missed by the ``src/`` directory.

    The failure was swallowed, so a simulated run silently produced a flat
    fallback spectrum instead of the recorded reference.
    """
    from prolibspector.acquisition.automated_worker import _load_simulation_reference_spectrum

    wavelengths, intensities = _load_simulation_reference_spectrum()
    assert wavelengths.size > 1000
    assert wavelengths.size == intensities.size
    assert np.all(np.diff(wavelengths) >= 0), "reference spectrum must be wavelength-sorted"
    assert intensities.max() > 0
