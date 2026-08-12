"""Unit tests for the public-edition plate autosave module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prolibspector.acquisition.plate_autosave import (
    ORDER_COLUMN,
    ORDER_ROW,
    PLATE_FORMATS,
    PlateAutosaveConfig,
    PlateRunState,
    discover_resumable_plate_runs,
    sanitize_filename_part,
    save_plate_run_state,
    unique_output_path,
    well_names,
)


def test_plate_formats_cover_documented_layouts():
    assert PLATE_FORMATS[96] == (8, 12)
    for well_count, (rows, columns) in PLATE_FORMATS.items():
        assert rows * columns == well_count


def test_well_names_row_and_column_order():
    assert well_names(2, 3, ORDER_ROW) == ["A1", "A2", "A3", "B1", "B2", "B3"]
    assert well_names(2, 3, ORDER_COLUMN) == ["A1", "B1", "A2", "B2", "A3", "B3"]


def test_sanitize_filename_part_falls_back():
    assert sanitize_filename_part("Plate 1/A*") == "Plate_1_A"
    assert sanitize_filename_part("   ") == "Sample"
    assert sanitize_filename_part(None, fallback="Plate1") == "Plate1"


def test_unique_output_path_avoids_overwriting(tmp_path: Path):
    first = Path(unique_output_path(tmp_path, "shot.csv"))
    first.write_text("x", encoding="utf-8")
    second = Path(unique_output_path(tmp_path, "shot.csv"))
    assert second.name == "shot_2.csv"


def test_config_from_mapping_coerces_strings_and_keeps_extras():
    config = PlateAutosaveConfig.from_mapping(
        {
            "plate_type": "96",
            "plate_name": " My Plate ",
            "shots_per_well": "3",
            "order_mode": ORDER_COLUMN,
            "sample_name": "Steel",
        }
    )
    assert config.plate_type == 96
    assert config.plate_name == "My Plate"
    assert config.safe_plate_name == "My_Plate"
    assert config.shots_per_well == 3
    assert config.total_shots == 96 * 3
    assert config.runtime["sample_name"] == "Steel"


@pytest.mark.parametrize(
    "values",
    [
        {"plate_type": 7},
        {"shots_per_well": 0},
        {"order_mode": "diagonal"},
    ],
)
def test_config_rejects_invalid_settings(values):
    with pytest.raises(ValueError):
        PlateAutosaveConfig.from_mapping({"plate_type": 96, **values})


def _fill_plate(state: PlateRunState, tmp_path: Path) -> None:
    while True:
        assignment = state.next_assignment()
        if assignment is None:
            return
        well, shot = assignment
        path = tmp_path / f"{well}_shot{shot:02d}.csv"
        path.write_text("Wavelength\tIntensity\n200\t1\n", encoding="utf-8")
        state.record_saved(str(path))


def test_shot_assignment_follows_plate_order(tmp_path: Path):
    state = PlateRunState(PlateAutosaveConfig(plate_type=6, shots_per_well=2))
    assert state.next_assignment() == ("A1", 1)
    (tmp_path / "a.csv").write_text("x", encoding="utf-8")
    state.record_saved(str(tmp_path / "a.csv"))
    assert state.next_assignment() == ("A1", 2)


def test_run_completes_and_reports_progress(tmp_path: Path):
    state = PlateRunState(PlateAutosaveConfig(plate_type=6, shots_per_well=1))
    _fill_plate(state, tmp_path)
    payload = state.progress_payload()
    assert payload["complete"] is True
    assert payload["current_well"] is None
    assert payload["saved_shots"] == 6
    assert payload["complete_wells"] == payload["total_wells"] == 6
    # Payload shape is a contract with acquisition/app.py.
    for key in ("rows", "columns", "shots_by_well", "order_label", "qc_by_well", "repair_queue"):
        assert key in payload


def test_record_saved_refuses_extra_shots(tmp_path: Path):
    state = PlateRunState(PlateAutosaveConfig(plate_type=6, shots_per_well=1))
    _fill_plate(state, tmp_path)
    with pytest.raises(RuntimeError):
        state.record_saved(str(tmp_path / "extra.csv"))


def test_discard_last_moves_file_and_rolls_back(tmp_path: Path):
    state = PlateRunState(PlateAutosaveConfig(plate_type=6, shots_per_well=1))
    shot = tmp_path / "A1.csv"
    shot.write_text("x", encoding="utf-8")
    state.record_saved(str(shot))

    discarded = tmp_path / "Discarded"
    record, payload = state.discard_last(discarded)
    assert record is not None and record.well == "A1"
    assert not shot.exists()
    assert (discarded / "A1.csv").exists()
    assert payload["saved_shots"] == 0
    assert payload["current_well"] == "A1"


def test_start_repair_requeues_wells_and_moves_files(tmp_path: Path):
    state = PlateRunState(PlateAutosaveConfig(plate_type=6, shots_per_well=1))
    _fill_plate(state, tmp_path)

    discarded = tmp_path / "Discarded"
    removed, payload = state.start_repair(["a3", "A1"], discarded)
    assert {record.well for record in removed} == {"A1", "A3"}
    assert payload["repair_active"] is True
    assert payload["repair_queue"] == ["A1", "A3"]  # re-ordered into plate order
    assert payload["current_well"] == "A1"
    assert len(list(discarded.iterdir())) == 2

    # Refilling the queued wells clears the repair and marks them repaired.
    again = tmp_path / "again"
    again.mkdir()
    _fill_plate(state, again)
    payload = state.progress_payload()
    assert payload["repair_active"] is False
    assert set(payload["repaired_wells"]) == {"A1", "A3"}


def test_start_repair_rejects_unknown_and_empty_wells(tmp_path: Path):
    state = PlateRunState(PlateAutosaveConfig(plate_type=6, shots_per_well=1))
    _fill_plate(state, tmp_path)
    with pytest.raises(ValueError):
        state.start_repair(["Z9"], tmp_path / "Discarded")
    with pytest.raises(ValueError):
        state.start_repair([], tmp_path / "Discarded")


def test_state_round_trips_through_mapping(tmp_path: Path):
    state = PlateRunState(PlateAutosaveConfig(plate_type=6, plate_name="Round Trip", shots_per_well=1))
    shot = tmp_path / "A1.csv"
    shot.write_text("x", encoding="utf-8")
    state.record_saved(str(shot))

    restored = PlateRunState.from_mapping(json.loads(json.dumps(state.to_mapping())))
    assert restored.config.plate_name == "Round Trip"
    assert restored.saved_shots == 1
    assert restored.progress_payload() == state.progress_payload()


def test_from_records_renumbers_and_rejects_overflow():
    config = PlateAutosaveConfig(plate_type=6, shots_per_well=1)
    state = PlateRunState.from_records(config, [{"well": "A2", "filepath": "a.csv"}])
    assert state.records[0].shot_number == 1
    assert state.current_well == "A1"

    with pytest.raises(ValueError):
        PlateRunState.from_records(config, [{"well": "A1"}, {"well": "A1"}])
    with pytest.raises(ValueError):
        PlateRunState.from_records(config, [{"well": "Z9"}])


def test_discover_resumable_runs_from_state_file(tmp_path: Path):
    config = PlateAutosaveConfig(plate_type=6, plate_name="Partial", shots_per_well=1)
    state = PlateRunState(config)
    plate_dir = tmp_path / config.safe_plate_name
    plate_dir.mkdir()
    shot = plate_dir / "Partial_A1_shot01_20240101_000000_000_001.csv"
    shot.write_text("Wavelength\tIntensity\n200\t1\n", encoding="utf-8")
    state.record_saved(str(shot))
    save_plate_run_state(plate_dir, state)

    found = discover_resumable_plate_runs(tmp_path)
    assert len(found) == 1
    assert found[0]["source_label"] == "state file"
    assert found[0]["needs_confirmation"] is False
    assert found[0]["payload"]["current_well"] == "A2"


def test_discover_resumable_runs_from_file_scan_needs_confirmation(tmp_path: Path):
    plate_dir = tmp_path / "Scanned"
    plate_dir.mkdir()
    for well in ("A1", "A2"):
        (plate_dir / f"Scanned_{well}_shot01_20240101_000000_000_001.csv").write_text("x", encoding="utf-8")

    found = discover_resumable_plate_runs(tmp_path)
    assert len(found) == 1
    assert found[0]["needs_confirmation"] is True
    assert found[0]["source_label"] == "file scan"
    assert found[0]["payload"]["saved_shots"] == 2


def test_discover_skips_complete_plates(tmp_path: Path):
    config = PlateAutosaveConfig(plate_type=6, plate_name="Done", shots_per_well=1)
    state = PlateRunState(config)
    plate_dir = tmp_path / config.safe_plate_name
    plate_dir.mkdir()
    _fill_plate(state, plate_dir)
    save_plate_run_state(plate_dir, state)

    assert discover_resumable_plate_runs(tmp_path) == []


def test_discover_handles_missing_directory():
    assert discover_resumable_plate_runs("") == []
