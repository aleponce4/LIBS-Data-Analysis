"""Tests for the vendor spectrometer backends.

Neither vendor runtime is present in CI, so these cover the two things that can
be checked without hardware and that break most quietly:

1. The simulated path inherited from ``SimulatedBackedModule`` still works
   through a vendor subclass. That is the whole point of the split — a missing
   SDK must cost the simulated pipeline nothing.
2. A missing runtime fails as "no device", naming what to install, rather than
   as an ImportError or a silent fallback to a fake device.
"""

import sys

import numpy as np
import pytest

from prolibspector.hardware.ocean_optics import SEABREEZE_MISSING_REASON, SpectrometerModule
from prolibspector.hardware.spectrometer import (
    SIMULATION_PROFILES,
    NoDeviceError,
    SpectrometerError,
    trigger_mode_fields,
)
from prolibspector.hardware.thorlabs_ccs import CCS_PIXEL_COUNT, ThorlabsCCSModule


# ── The simulated path survives the vendor subclass ──────────────────────


def test_ocean_module_runs_the_simulated_pipeline():
    module = SpectrometerModule()
    status = module.connect_simulated()

    assert module.is_connected
    assert "SIMULATED" in status.upper() or "Simulated" in status

    wavelengths, intensities = module.get_spectrum()
    assert len(wavelengths) == len(intensities) == 2048
    assert np.all(np.isfinite(intensities))
    assert np.max(intensities) > 100.0

    module.disconnect()
    assert not module.is_connected


def test_ocean_module_reports_simulated_brand_not_the_vendor_brand():
    """A simulated device must never be labelled as real Ocean Optics hardware.

    ``_finish_connection`` tags the status bar from this field, and saved
    manifests carry it, so a wrong brand here mislabels recorded data.
    """
    module = SpectrometerModule()
    module.connect_simulated()
    assert module.capabilities.brand == "simulated"


def test_thorlabs_module_defaults_to_the_ccs175_profile():
    module = ThorlabsCCSModule()
    module.connect_simulated()

    capabilities = module.capabilities
    assert capabilities.model == "CCS175-SIM"
    assert capabilities.pixel_count == CCS_PIXEL_COUNT
    # The CCS DLL returns normalised intensities, so the simulated CCS must too.
    assert capabilities.max_intensity == 1.0
    assert not capabilities.has_external_trigger


def test_simulated_ccs_spectrum_stays_inside_the_normalised_range():
    """Guard the ADC rescale.

    The synthetic spectrum is generated in ADC counts — dark counts and the
    readout floor are absolute quantities — and only rescaled at the end. Get
    that order wrong and a 0.0–1.0 device saturates on every pixel.
    """
    module = ThorlabsCCSModule()
    module.connect_simulated()
    intensities = module.get_intensities()

    assert intensities.max() <= 1.0
    assert intensities.min() >= 0.0
    # Emission lines must still stand out, not be flattened against the ceiling.
    assert intensities.max() > 10 * float(np.median(intensities))
    assert np.count_nonzero(intensities >= 1.0) < intensities.size // 100


# ── Trigger delay is reported honestly per profile ───────────────────────

def test_generic_profile_supports_a_programmable_delay():
    module = SpectrometerModule()
    module.connect_simulated("Generic")

    assert module.capabilities.supports_trigger_delay
    module.set_trigger_delay(25.0)
    assert module.trigger_delay_us == 25.0


@pytest.mark.parametrize("profile_name", ["USB4000", "QEPro", "HDX", "CCS175", "CCS200"])
def test_brand_profiles_refuse_a_delay_their_hardware_lacks(profile_name):
    """A sweep over a device with no gate must fail, not record phantom delays."""
    module = SpectrometerModule()
    module.connect_simulated(profile_name)

    assert not module.capabilities.supports_trigger_delay
    with pytest.raises(SpectrometerError):
        module.set_trigger_delay(25.0)


def test_every_simulation_profile_declares_its_trigger_numbers():
    for name, profile in SIMULATION_PROFILES.items():
        modes = profile["trigger_modes"]
        assert "normal" in modes, f"{name} has no normal trigger mode"
        assert all(isinstance(value, int) for value in modes.values()), name


def test_trigger_mode_fields_cannot_disagree_with_the_map():
    fields = trigger_mode_fields({"normal": 0, "external": 3})
    assert fields["normal_trigger_mode"] == fields["trigger_modes"]["normal"]
    assert fields["external_trigger_mode"] == fields["trigger_modes"]["external"]

    no_external = trigger_mode_fields({"normal": 0})
    assert no_external["external_trigger_mode"] is None


# ── A missing vendor runtime fails as "no device" ────────────────────────

def test_ocean_connect_without_seabreeze_names_the_missing_package(monkeypatch):
    # A None entry in sys.modules makes `import seabreeze` raise ImportError,
    # so this is deterministic whether or not seabreeze is installed here.
    monkeypatch.setitem(sys.modules, "seabreeze", None)

    module = SpectrometerModule()
    with pytest.raises(NoDeviceError) as excinfo:
        module.connect()

    assert "seabreeze" in str(excinfo.value)
    assert str(excinfo.value) == SEABREEZE_MISSING_REASON


def test_thorlabs_connect_without_the_dll_names_thorspectra(monkeypatch):
    monkeypatch.setattr(
        "prolibspector.hardware.thorlabs_ccs.supports_thorlabs_ccs", lambda: True
    )
    monkeypatch.setattr("prolibspector.hardware.thorlabs_ccs._find_tlccs_dll", lambda: None)

    import ctypes

    def _refuse(_name):
        raise OSError("TLCCS_64.dll not on PATH")

    monkeypatch.setattr(ctypes.cdll, "LoadLibrary", _refuse)

    module = ThorlabsCCSModule()
    with pytest.raises(NoDeviceError) as excinfo:
        module.connect()

    assert "TLCCS_64.dll" in str(excinfo.value)
    assert "ThorSpectra" in str(excinfo.value)


def test_thorlabs_refuses_on_platforms_without_visa(monkeypatch):
    monkeypatch.setattr(
        "prolibspector.hardware.thorlabs_ccs.supports_thorlabs_ccs", lambda: False
    )

    module = ThorlabsCCSModule()
    with pytest.raises(NoDeviceError):
        module.connect()
    assert module.list_available_devices() == []


# ── Diagnostics reports keep the shape the dialog reads ──────────────────

def test_ocean_diagnose_reports_the_keys_the_dialog_renders(monkeypatch):
    monkeypatch.setitem(sys.modules, "seabreeze", None)

    report = SpectrometerModule.diagnose()
    for key in (
        "backend", "platform", "seabreeze_installed", "seabreeze_backend",
        "seabreeze_backend_fail_reason", "usb_devices", "seabreeze_devices",
        "per_device_errors", "driver_warnings", "notes",
    ):
        assert key in report, f"diagnostics dialog reads {key!r}"

    assert report["seabreeze_installed"] is False
    assert any("seabreeze" in note for note in report["notes"])


def test_thorlabs_diagnose_reports_the_keys_the_dialog_renders(monkeypatch):
    monkeypatch.setattr(
        "prolibspector.hardware.thorlabs_ccs.supports_thorlabs_ccs", lambda: False
    )

    report = ThorlabsCCSModule.diagnose()
    for key in (
        "backend", "platform", "supported", "dll_found", "dll_path",
        "visa_installed", "visa_resources", "usb_devices", "notes",
    ):
        assert key in report, f"diagnostics dialog reads {key!r}"

    assert report["supported"] is False
    assert report["notes"]
