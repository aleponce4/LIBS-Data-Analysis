"""End-to-end automated acquisition against the bundled simulators.

This is the test the automation stack exists for: a full mapping run driven by
``AutomatedAcquisitionWorker`` with no hardware attached, no serial port opened
and no vendor SDK installed. It uses the real simulated spectrometer and the
real ``SimulatedGrblLaserController`` rather than stand-ins, so the actual
motion, triggering and storage code paths execute.

It also pins the run-directory contract. The binary spectrum store, the grid
index and the manifest are what the analysis half of the application reads back,
so their names and shapes are an interface, not an implementation detail.
"""

import csv
import json

import numpy as np
import pytest

from prolibspector.acquisition.automated_worker import (
    RUN_MODE_SIMULATION,
    AutomatedAcquisitionWorker,
)
from prolibspector.acquisition.automation_mapping import (
    DEFAULT_MAPPING_MOVE_SPEED_MM_MIN,
    MappingGridConfig,
)
from prolibspector.hardware.grbl_laser import SimulatedGrblLaserController
from prolibspector.hardware.ocean_optics import SpectrometerModule


@pytest.fixture
def mapping_run(tmp_path):
    """Run a 2x2 simulated mapping grid and return its output directory."""
    spectrometer = SpectrometerModule()
    spectrometer.connect_simulated("Generic")
    laser = SimulatedGrblLaserController()
    laser.connect()

    worker = AutomatedAcquisitionWorker(spectrometer, laser)
    # The run is deterministic; the interval waits only make it slow.
    worker._wait_for_interruptible_interval = lambda _interval: None

    config = MappingGridConfig(
        experiment_name="GateRun",
        run_directory=str(tmp_path),
        x_length_mm=1.0,
        y_length_mm=1.0,
        step_mm=1.0,
        shots_per_point=1,
        laser_power_percent=10.0,
        pulse_ms=1.0,
        settle_ms=0.0,
        capture_timeout_s=1.0,
    )
    worker.configure_mapping(
        config,
        safety_checklist={"cover_closed": True},
        run_mode=RUN_MODE_SIMULATION,
    )
    # A real run refuses to start until a dry run of the same plan has passed.
    worker.completed_dry_run_signature = config.dry_run_signature()

    worker._set_state(worker.STATE_AUTOMATED)
    worker._run_mapping()

    try:
        yield tmp_path, worker
    finally:
        laser.disconnect()
        spectrometer.disconnect()


def test_simulated_mapping_run_writes_the_full_run_directory(mapping_run):
    run_dir, _worker = mapping_run
    store = run_dir / "_mapping_spectrum_store"

    for relative in (
        "_mapping_spectrum_store/wavelengths.npy",
        "_mapping_spectrum_store/intensities.npy",
        "_mapping_spectrum_store/written_mask.npy",
        "_mapping_grid_index.csv",
        "_mapping_grid_manifest.json",
    ):
        assert (run_dir / relative).is_file(), f"{relative} was not written"

    # Spectra go into the binary store, not one CSV per shot: a full grid is
    # tens of thousands of shots and a file each makes the run undeployable.
    assert list(run_dir.glob("GateRun_R*_C*_shot*.csv")) == []

    written = np.load(store / "written_mask.npy")
    intensities = np.load(store / "intensities.npy")
    wavelengths = np.load(store / "wavelengths.npy")
    assert written.sum() == 4, "every target of the 2x2 grid should be written"
    assert intensities.shape[0] == 4
    assert intensities.shape[1] == wavelengths.shape[0]
    assert np.isfinite(intensities).all()


def test_mapping_index_locates_every_spectrum_in_the_binary_store(mapping_run):
    """The index is the only thing tying a grid position to a stored row."""
    run_dir, _worker = mapping_run
    with (run_dir / "_mapping_grid_index.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 4
    assert [row["target_key"] for row in rows] == [
        "R001:C001:S01",
        "R001:C002:S01",
        "R002:C001:S01",
        "R002:C002:S01",
    ]

    first = rows[0]
    assert first["row_index"] == "1"
    assert first["column_index"] == "1"
    assert first["x_mm"] == "0.000000"
    assert first["y_mm"] == "0.000000"
    assert first["storage_kind"] == "binary_npy"
    assert first["store_dir"] == "_mapping_spectrum_store"
    assert first["binary_written"] == "true"

    # Every row must point at a distinct, actually-written store row.
    binary_rows = [int(row["binary_row_index"]) for row in rows]
    assert sorted(binary_rows) == [0, 1, 2, 3]
    written = np.load(run_dir / "_mapping_spectrum_store" / "written_mask.npy")
    assert all(written[index] for index in binary_rows)


def test_mapping_manifest_records_what_the_run_actually_did(mapping_run):
    run_dir, _worker = mapping_run
    manifest = json.loads((run_dir / "_mapping_grid_manifest.json").read_text(encoding="utf-8"))

    assert manifest["run_type"] == "mapping"
    assert manifest["targets_total"] == 4
    assert manifest["shots_saved"] == 4
    assert manifest["plan"]["summary"]["rows"] == 2
    assert manifest["plan"]["summary"]["columns"] == 2
    # Requested and effective speed are recorded separately so a run that was
    # silently clamped by the controller's max feed is visible afterwards.
    assert manifest["requested_move_speed_mm_min"] == DEFAULT_MAPPING_MOVE_SPEED_MM_MIN
    assert manifest["effective_move_speed_mm_min"] == DEFAULT_MAPPING_MOVE_SPEED_MM_MIN


def test_simulated_run_never_opens_a_serial_port(monkeypatch, tmp_path):
    """The simulation path must not touch pyserial, even if it is installed.

    A simulated run that quietly fell through to the hardware path would be
    both a lie and, with a real stage attached, a moving-parts hazard.
    """
    import prolibspector.hardware.grbl_laser as grbl

    def _explode(*_args, **_kwargs):
        raise AssertionError("simulated run attempted to open a serial port")

    monkeypatch.setattr(grbl, "_probe_serial_factory", _explode)

    spectrometer = SpectrometerModule()
    spectrometer.connect_simulated("Generic")
    laser = SimulatedGrblLaserController()
    laser.connect()
    assert laser.is_simulated

    worker = AutomatedAcquisitionWorker(spectrometer, laser)
    worker._wait_for_interruptible_interval = lambda _interval: None
    config = MappingGridConfig(
        experiment_name="NoSerial",
        run_directory=str(tmp_path),
        x_length_mm=1.0,
        y_length_mm=1.0,
        step_mm=1.0,
        shots_per_point=1,
        laser_power_percent=10.0,
        pulse_ms=1.0,
        settle_ms=0.0,
        capture_timeout_s=1.0,
    )
    worker.configure_mapping(
        config, safety_checklist={"cover_closed": True}, run_mode=RUN_MODE_SIMULATION
    )
    worker.completed_dry_run_signature = config.dry_run_signature()
    worker._set_state(worker.STATE_AUTOMATED)
    worker._run_mapping()

    assert (tmp_path / "_mapping_grid_index.csv").is_file()
