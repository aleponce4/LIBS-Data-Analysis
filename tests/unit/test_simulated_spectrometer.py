"""Tests for SimulatedSpectrometer hardware engine."""

import numpy as np
import pytest
from prolibspector.hardware.spectrometer import SimulatedSpectrometer, SIMULATION_PROFILES


def test_simulated_spectrometer_initialization(sim_spectrometer):
    assert sim_spectrometer.is_connected is True
    assert sim_spectrometer.model == "GENERIC-SIM"
    assert sim_spectrometer.serial_number == "SIM00001"
    caps = sim_spectrometer.capabilities
    assert caps.pixels == 2048
    assert caps.wl_min_nm == 200.0
    assert caps.wl_max_nm == 1000.0


def test_simulated_spectrometer_acquisition(sim_spectrometer):
    wl = sim_spectrometer.get_wavelengths()
    intensities = sim_spectrometer.get_intensities()
    assert len(wl) == 2048
    assert len(intensities) == 2048
    assert np.all(np.isfinite(wl))
    assert np.all(np.isfinite(intensities))
    assert np.all(intensities >= 0.0)
    assert np.max(intensities) > 100.0  # Synthetic emission lines present


def test_simulated_spectrometer_trigger_delay(sim_spectrometer):
    sim_spectrometer.set_trigger_delay(10.0)
    assert sim_spectrometer.trigger_delay_us == 10.0
    delayed_intensities = sim_spectrometer.get_intensities()
    assert len(delayed_intensities) == 2048
    assert np.all(np.isfinite(delayed_intensities))


def test_simulated_spectrometer_disconnect(sim_spectrometer):
    sim_spectrometer.disconnect()
    assert sim_spectrometer.is_connected is False
    with pytest.raises(Exception):
        sim_spectrometer.get_intensities()
