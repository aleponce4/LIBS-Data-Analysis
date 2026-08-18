"""Tests for SimulatedSpectrometer hardware engine."""

import numpy as np
import pytest


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


def test_live_view_is_not_saturated_before_a_delay_is_configured(sim_spectrometer):
    """With no gate configured, the spectrum must look like a live view.

    The gated response models a window opening at t=0, where the continuum
    genuinely swamps the detector. Applying it when no delay has been set at
    all clips the whole blue end and makes every simulated spectrum unusable.
    """
    intensities = sim_spectrometer.get_intensities()
    ceiling = sim_spectrometer.capabilities.max_intensity

    assert np.count_nonzero(intensities >= ceiling) == 0
    assert intensities.max() > 20 * float(np.median(intensities))


def test_configuring_a_delay_switches_on_the_gated_response(sim_spectrometer):
    """A zero-delay gate adds the early-time continuum the live view omits."""
    live_view = sim_spectrometer.get_intensities()
    sim_spectrometer.set_trigger_delay(0.0)
    gated = sim_spectrometer.get_intensities()

    blue_end = slice(0, 200)
    assert gated[blue_end].mean() > live_view[blue_end].mean()


def test_simulated_spectrometer_disconnect(sim_spectrometer):
    sim_spectrometer.disconnect()
    assert sim_spectrometer.is_connected is False
    with pytest.raises(Exception):
        sim_spectrometer.get_intensities()
