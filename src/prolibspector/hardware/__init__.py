"""Hardware abstraction package: spectrometer backends and the GRBL stage.

``spectrometer`` holds the interfaces and the simulated device; each vendor
backend lives in its own module and subclasses ``SimulatedBackedModule``, so a
missing vendor runtime costs the simulated path nothing.
"""

from prolibspector.hardware.brokered_spectrometer import BrokeredSpectrometerClient
from prolibspector.hardware.ocean_optics import SpectrometerModule
from prolibspector.hardware.spectrometer import (
    DeviceCapabilities,
    ModuleCapabilities,
    NoDeviceError,
    SimulatedBackedModule,
    SimulatedSpectrometer,
    Spectrometer,
    SpectrometerBase,
    SpectrometerError,
    apply_simulated_trigger_delay_response,
    simulated_trigger_delay_factors,
)
from prolibspector.hardware.thorlabs_ccs import ThorlabsCCSModule

__all__ = [
    "BrokeredSpectrometerClient",
    "DeviceCapabilities",
    "ModuleCapabilities",
    "NoDeviceError",
    "SimulatedBackedModule",
    "SimulatedSpectrometer",
    "Spectrometer",
    "SpectrometerBase",
    "SpectrometerError",
    "SpectrometerModule",
    "ThorlabsCCSModule",
    "apply_simulated_trigger_delay_response",
    "simulated_trigger_delay_factors",
]
