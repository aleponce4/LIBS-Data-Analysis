"""Pytest configuration and shared fixtures for public edition tests."""

import pytest
from prolibspector.hardware.spectrometer import SimulatedSpectrometer, SIMULATION_PROFILES


@pytest.fixture
def sim_spectrometer():
    """Return a fresh SimulatedSpectrometer instance using Generic profile."""
    return SimulatedSpectrometer(profile=SIMULATION_PROFILES["Generic"], seed=42)
