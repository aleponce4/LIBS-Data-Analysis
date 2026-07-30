"""Integration smoke check for application initialization."""

import numpy as np
from prolibspector.app.bootstrap import smoke_check
from prolibspector.hardware.spectrometer import SimulatedSpectrometer


def test_app_smoke_check():
    info = smoke_check()
    assert "resource_root" in info
    assert "error_log" in info


def test_simulated_backend_e2e_acquisition():
    sim = SimulatedSpectrometer()
    sim.connect()
    wl, intensities = sim.get_spectrum()
    assert len(wl) == len(intensities)
    assert len(wl) > 1000
    assert np.max(intensities) > 0.0
    sim.disconnect()
