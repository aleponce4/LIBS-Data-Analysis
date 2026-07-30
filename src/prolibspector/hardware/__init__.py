"""Hardware abstraction package for generic and simulated spectrometers."""

from prolibspector.hardware.spectrometer import (
    DeviceCapabilities,
    Spectrometer,
    SpectrometerBase,
    SpectrometerError,
    SimulatedSpectrometer,
    simulated_trigger_delay_factors,
    apply_simulated_trigger_delay_response,
)

__all__ = [
    "DeviceCapabilities",
    "Spectrometer",
    "SpectrometerBase",
    "SpectrometerError",
    "SimulatedSpectrometer",
    "simulated_trigger_delay_factors",
    "apply_simulated_trigger_delay_response",
]
