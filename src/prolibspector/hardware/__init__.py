"""Hardware abstraction package for generic and simulated spectrometers."""

from prolibspector.hardware.spectrometer import (
    DeviceCapabilities,
    ModuleCapabilities,
    NoDeviceError,
    Spectrometer,
    SpectrometerBase,
    SpectrometerError,
    SpectrometerModule,
    SimulatedSpectrometer,
    ThorlabsCCSModule,
    simulated_trigger_delay_factors,
    apply_simulated_trigger_delay_response,
)

__all__ = [
    "DeviceCapabilities",
    "ModuleCapabilities",
    "NoDeviceError",
    "Spectrometer",
    "SpectrometerBase",
    "SpectrometerError",
    "SpectrometerModule",
    "SimulatedSpectrometer",
    "ThorlabsCCSModule",
    "simulated_trigger_delay_factors",
    "apply_simulated_trigger_delay_response",
]
