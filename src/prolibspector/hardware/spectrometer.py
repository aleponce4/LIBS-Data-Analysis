"""Generic spectrometer interface and simulation backend.

Provides a unified interface (`SpectrometerBase`) and a simulated spectrometer
(`SimulatedSpectrometer`) capable of generating realistic LIBS spectra, dark noise,
trigger delays, and integration time responses without physical hardware.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import logging
import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_INTEGRATION_TIME_US = 50_000
SIMULATION_REFERENCE_INTEGRATION_TIME_US = 100_000


class SpectrometerError(Exception):
    """Base exception for spectrometer initialization or communication failures."""
    pass


class NoDeviceError(SpectrometerError):
    """Raised when no matching spectrometer could be found or opened.

    The acquisition view treats this distinctly from a generic
    ``SpectrometerError``: it is the "nothing to connect to" case that opens the
    diagnostics dialog with a Simulation fallback rather than reporting a bug.
    """
    pass


@dataclass
class DeviceCapabilities:
    """Read-only description of hardware feature support."""
    model: str = "Simulated"
    pixels: int = 2048
    wl_min_nm: float = 200.0
    wl_max_nm: float = 1000.0
    max_intensity: float = 65535.0
    int_min_us: int = 1_000
    int_max_us: int = 60_000_000
    trigger_modes: dict[str, int] = field(default_factory=lambda: {"normal": 0, "external": 1})
    supports_dark_correction: bool = True
    supports_nonlinearity_correction: bool = True
    supports_trigger_delay: bool = True
    trigger_delay_max_us: float = 1_000_000.0


class SpectrometerBase(ABC):
    """Abstract base class for spectrometer backends."""

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @property
    @abstractmethod
    def capabilities(self) -> DeviceCapabilities: ...

    @property
    @abstractmethod
    def integration_time_us(self) -> int: ...

    @property
    @abstractmethod
    def current_trigger_mode(self) -> int: ...

    @property
    @abstractmethod
    def model(self) -> str: ...

    @property
    @abstractmethod
    def serial_number(self) -> str: ...

    @abstractmethod
    def connect(self, device_index: int = 0) -> str: ...

    @abstractmethod
    def connect_simulated(self, profile_name: str = "Generic") -> str: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def set_integration_time(self, microseconds: int) -> None: ...

    @abstractmethod
    def set_trigger_mode(self, mode: int) -> None: ...

    @property
    def trigger_delay_us(self) -> float:
        return 0.0

    def set_trigger_delay(self, microseconds: float) -> None:
        raise SpectrometerError(f"{self.model} does not support a programmable trigger delay.")

    @abstractmethod
    def get_wavelengths(self) -> np.ndarray: ...

    @abstractmethod
    def get_intensities(self, correct_dark_counts: bool = False, correct_nonlinearity: bool = False) -> np.ndarray: ...

    def get_spectrum(self) -> tuple[np.ndarray, np.ndarray]:
        return self.get_wavelengths(), self.get_intensities()

    def drain_buffered_frames(self, max_frames: int = 8, time_budget_s: float = 2.5) -> int:
        return 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False


# Each profile mirrors one real instrument's geometry, timing limits and
# trigger-mode numbers, so a simulated connection behaves like the device the
# user actually picked. Only "Generic" models a gated detector with a
# programmable trigger delay; the brand-named profiles report the delay support
# their hardware really has, which for all of them is none.
SIMULATION_PROFILES = {
    "Generic": {
        "model": "GENERIC-SIM",
        "pixels": 2048,
        "wl_min": 200.0,
        "wl_max": 1000.0,
        "max_intensity": 65535,
        "int_min_us": 1_000,
        "int_max_us": 60_000_000,
        "trigger_modes": {"normal": 0, "external": 3},
        "trigger_delay_max_us": 1_000_000,
    },
    # A gated detector: microsecond gating together with a programmable trigger
    # delay. LIBS needs both at once, because the useful signal sits in a window
    # that opens after the continuum has decayed and closes while the lines are
    # still bright. None of the Ocean Optics or Thorlabs parts below can do it --
    # they integrate from the trigger edge -- so exercising the timing work
    # against a simulated device needs a profile that can.
    "GatedCCD": {
        "model": "GATED-CCD-SIM",
        "pixels": 4096,
        "wl_min": 200.0,
        "wl_max": 1100.0,
        "max_intensity": 65535,
        "int_min_us": 9,
        "int_max_us": 1_000_000,
        "trigger_modes": {"normal": 0, "external": 3},
        "trigger_delay_max_us": 1_000_000,
    },
    # ── Ocean Optics ─────────────────────────────────────────────
    # OOI-protocol devices number external *edge* trigger 3; OBP devices
    # (HDX and the Ocean ST/SR line) number it 1.
    "USB4000": {
        "model": "USB4000-SIM",
        "pixels": 3648,
        "wl_min": 200.0,
        "wl_max": 1100.0,
        "max_intensity": 65535,
        "int_min_us": 10,
        "int_max_us": 65_535_000,
        "trigger_modes": {"normal": 0, "external": 3},
    },
    "QEPro": {
        "model": "QEPRO-SIM",
        "pixels": 1044,
        "wl_min": 200.0,
        "wl_max": 950.0,
        "max_intensity": 262143,
        "int_min_us": 8_000,
        "int_max_us": 1_600_000_000,
        "trigger_modes": {"normal": 0, "external": 3},
    },
    "HDX": {
        "model": "HDX-SIM",
        "pixels": 2068,
        "wl_min": 200.0,
        "wl_max": 800.0,
        "max_intensity": 65535,
        "int_min_us": 6_000,
        "int_max_us": 10_000_000,
        "trigger_modes": {"normal": 0, "external": 1},
    },
    # ── Thorlabs CCS ─────────────────────────────────────────────
    # The CCS DLL returns normalised 0.0–1.0 intensities rather than ADC
    # counts, and exposes no external hardware trigger at all.
    "CCS175": {
        "model": "CCS175-SIM",
        "pixels": 3648,
        "wl_min": 500.0,
        "wl_max": 1000.0,
        "max_intensity": 1.0,
        "int_min_us": 10,
        "int_max_us": 60_000_000,
        "trigger_modes": {"normal": 0},
    },
    "CCS200": {
        "model": "CCS200-SIM",
        "pixels": 3648,
        "wl_min": 200.0,
        "wl_max": 1000.0,
        "max_intensity": 1.0,
        "int_min_us": 10,
        "int_max_us": 60_000_000,
        "trigger_modes": {"normal": 0},
    },
}

#: Full-scale ADC counts the synthetic spectrum is generated against. Profiles
#: whose devices report a different range are rescaled from this at the end, so
#: absolute quantities like dark counts stay in counts throughout.
SIMULATION_ADC_FULL_SCALE = 65535.0

SIMULATED_CONTINUUM_DECAY_US = 1.6
SIMULATED_LINE_DECAY_US = 18.0
SIMULATED_CONTINUUM_TO_LINE_RATIO = 6.0
SIMULATED_DARK_COUNTS_PER_MS = 50.0


def _captured_fraction(delay_us: float, integration_us: float | None, decay_us: float) -> float:
    delay = max(0.0, float(delay_us))
    start = float(np.exp(-delay / decay_us))
    if integration_us is None:
        return start
    length = max(0.0, float(integration_us))
    return start * float(1.0 - np.exp(-length / decay_us))


def simulated_trigger_delay_factors(delay_us: float, integration_us: float | None = None) -> tuple[float, float]:
    continuum_scale = _captured_fraction(delay_us, integration_us, SIMULATED_CONTINUUM_DECAY_US)
    line_scale = _captured_fraction(delay_us, integration_us, SIMULATED_LINE_DECAY_US)
    return continuum_scale, line_scale


def apply_simulated_trigger_delay_response(
    wavelengths, intensities, delay_us: float, *, integration_us: float | None = None, max_intensity: float = 65535.0, rng=None
) -> np.ndarray:
    values = np.asarray(intensities, dtype=float).copy()
    axis = np.asarray(wavelengths, dtype=float)
    if values.size == 0 or axis.size != values.size:
        return values
    if rng is None:
        rng = np.random.default_rng()

    max_intensity = float(max_intensity) if max_intensity and max_intensity > 0 else 65535.0
    continuum_scale, line_scale = simulated_trigger_delay_factors(delay_us, integration_us)

    finite = values[np.isfinite(values)]
    line_peak = float(np.nanmax(finite)) if finite.size else 0.0

    span = max(axis[-1] - axis[0], 1e-9)
    shape = np.exp(-(axis - axis[0]) / (span * 0.6))
    continuum_zero_delay = line_peak * SIMULATED_CONTINUUM_TO_LINE_RATIO
    continuum = continuum_zero_delay * shape * continuum_scale
    continuum *= rng.uniform(0.97, 1.03)

    delayed = values * line_scale + continuum
    if integration_us is not None:
        dark_counts = SIMULATED_DARK_COUNTS_PER_MS * max(0.0, float(integration_us)) / 1000.0
        delayed += dark_counts
    delayed += np.sqrt(np.maximum(delayed, 0.0)) * rng.standard_normal(values.size) * 0.5
    delayed += rng.standard_normal(values.size) * (max_intensity * 0.00012)
    return np.clip(delayed, 0.0, max_intensity)


class SimulatedSpectrometer(SpectrometerBase):
    """Simulated spectrometer backend for hardware-free testing and GUI interaction."""

    def __init__(self, profile: dict | None = None, seed: int | None = 42):
        if profile is None:
            profile = SIMULATION_PROFILES["Generic"]

        self._is_connected = True
        self.model_name = profile.get("model", "GENERIC-SIM")
        self._serial_number = "SIM00001"
        self._integration_time_us = DEFAULT_INTEGRATION_TIME_US
        self._trigger_mode = 0
        self._trigger_delay_max_us = float(profile.get("trigger_delay_max_us", 0) or 0)
        self._trigger_delay_us = 0.0
        # The gated-detector response only takes over once a delay has actually
        # been set. Applying it unconditionally would put the zero-delay,
        # continuum-swamped spectrum on screen during ordinary live view, which
        # is physically right for a gate held open at t=0 but is not what an
        # operator is looking at when no gate is configured at all.
        self._trigger_delay_configured = False
        self._pixels = profile.get("pixels", 2048)
        self._max_intensity = profile.get("max_intensity", 65535)
        self._wl_min = profile.get("wl_min", 200.0)
        self._wl_max = profile.get("wl_max", 1000.0)
        self._int_min_us = profile.get("int_min_us", 1_000)
        self._int_max_us = profile.get("int_max_us", 60_000_000)
        self._trigger_modes = profile.get("trigger_modes", {"normal": 0, "external": 3})
        self._wavelengths = np.linspace(self._wl_min, self._wl_max, self._pixels)
        self._rng = np.random.default_rng(seed)

        self._emission_lines = [
            (238.20, 0.45, 0.15), (239.56, 0.50, 0.15), (240.49, 0.35, 0.15),
            (248.33, 0.40, 0.15), (252.28, 0.38, 0.15), (259.94, 0.55, 0.15),
            (358.12, 0.60, 0.18), (371.99, 0.75, 0.18), (373.49, 0.65, 0.18),
            (385.99, 0.80, 0.18), (393.37, 0.90, 0.20), (396.85, 0.85, 0.20),
            (422.67, 0.70, 0.20), (588.99, 0.95, 0.22), (589.59, 0.90, 0.22),
            (766.49, 0.60, 0.25), (769.90, 0.55, 0.25), (854.21, 0.40, 0.25),
        ]

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(
            model=self.model_name,
            pixels=self._pixels,
            wl_min_nm=self._wl_min,
            wl_max_nm=self._wl_max,
            max_intensity=self._max_intensity,
            int_min_us=self._int_min_us,
            int_max_us=self._int_max_us,
            trigger_modes=self._trigger_modes,
            supports_dark_correction=True,
            supports_nonlinearity_correction=True,
            supports_trigger_delay=self._trigger_delay_max_us > 0,
            trigger_delay_max_us=self._trigger_delay_max_us,
        )

    @property
    def integration_time_us(self) -> int:
        return self._integration_time_us

    @property
    def current_trigger_mode(self) -> int:
        return self._trigger_mode

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def serial_number(self) -> str:
        return self._serial_number

    @property
    def trigger_delay_us(self) -> float:
        return self._trigger_delay_us

    def set_trigger_delay(self, microseconds: float) -> None:
        # Refusing here is deliberate. A profile without an on-board delay must
        # not accept one silently, or a delay sweep records delays that were
        # never applied and the resulting curve is meaningless.
        if self._trigger_delay_max_us <= 0:
            raise SpectrometerError(
                f"{self.model_name} does not support a programmable trigger delay."
            )
        self._trigger_delay_us = max(0.0, min(float(microseconds), self._trigger_delay_max_us))
        self._trigger_delay_configured = True

    def connect(self, device_index: int = 0) -> str:
        self._is_connected = True
        return f"Connected to {self.model_name} (Simulated)"

    def connect_simulated(self, profile_name: str = "Generic") -> str:
        profile = SIMULATION_PROFILES.get(profile_name, SIMULATION_PROFILES["Generic"])
        self.__init__(profile=profile)
        return f"Connected to {self.model_name} (Simulated)"

    def disconnect(self) -> None:
        self._is_connected = False

    def set_integration_time(self, microseconds: int) -> None:
        self._integration_time_us = max(self._int_min_us, min(int(microseconds), self._int_max_us))

    def set_trigger_mode(self, mode: int) -> None:
        self._trigger_mode = mode

    def get_wavelengths(self) -> np.ndarray:
        return self._wavelengths.copy()

    def get_intensities(self, correct_dark_counts: bool = False, correct_nonlinearity: bool = False) -> np.ndarray:
        if not self._is_connected:
            raise SpectrometerError("Spectrometer is disconnected.")

        span = self._wl_max - self._wl_min
        base_continuum = 0.05 * np.exp(-(self._wavelengths - self._wl_min) / (span * 0.4))
        y = base_continuum.copy()

        for center, rel_amp, width in self._emission_lines:
            if self._wl_min <= center <= self._wl_max:
                y += rel_amp * np.exp(-0.5 * ((self._wavelengths - center) / width) ** 2)

        # Generate against a fixed ADC scale so the absolute terms — dark
        # counts, shot noise, the readout floor — stay in counts. Devices that
        # report a different range (the Thorlabs CCS normalises to 0.0–1.0) are
        # rescaled once, at the end.
        if self._trigger_delay_configured:
            # Pulsed plasma: the emission is a transient, so what reaches the
            # detector is set by where the exposure window sits, not by a
            # linear integration-time scale meant for a continuous source.
            y *= SIMULATION_ADC_FULL_SCALE * 0.7
            y = apply_simulated_trigger_delay_response(
                self._wavelengths,
                y,
                self._trigger_delay_us,
                integration_us=self._integration_time_us,
                max_intensity=SIMULATION_ADC_FULL_SCALE,
                rng=self._rng,
            )
        else:
            y *= self._integration_time_us / SIMULATION_REFERENCE_INTEGRATION_TIME_US
            y *= SIMULATION_ADC_FULL_SCALE * 0.7
            # Shot noise follows the accumulated signal; the readout floor does
            # not. Same two terms the delayed branch applies, so a spectrum does
            # not visibly change character the moment a delay is configured.
            y += np.sqrt(np.maximum(y, 0.0)) * self._rng.standard_normal(y.size) * 0.5
            y += self._rng.standard_normal(y.size) * (SIMULATION_ADC_FULL_SCALE * 0.00012)

        if self._max_intensity != SIMULATION_ADC_FULL_SCALE:
            y = y / SIMULATION_ADC_FULL_SCALE * float(self._max_intensity)
        return np.clip(y, 0.0, self._max_intensity)


class Spectrometer(SimulatedSpectrometer):
    """Primary entry class for public edition (defaults to SimulatedSpectrometer)."""

    def __init__(self, profile_name: str = "Generic"):
        super().__init__(profile=SIMULATION_PROFILES.get(profile_name, SIMULATION_PROFILES["Generic"]))


# ═══════════════════════════════════════════════════════════════════════════
#  Acquisition-facing device modules
# ═══════════════════════════════════════════════════════════════════════════
#
# Acquisition Mode (app.py, worker.py, diagnostic_dialog.py) talks to a
# *module* rather than to ``SpectrometerBase`` directly, and reads a richer
# capabilities record: brand, serial number, explicit trigger-mode numbers, and
# trigger-delay limits. ``ModuleCapabilities`` is that record.
#
# ``SimulatedBackedModule`` below is the half of a module that never needs
# hardware. The vendor backends live in their own modules and subclass it, so
# each one only has to add its ``connect()`` path and its diagnostics:
#
#   ocean_optics.py   SpectrometerModule   — python-seabreeze over USB
#   thorlabs_ccs.py   ThorlabsCCSModule    — TLCCS_64.dll over NI-VISA
#
# Both vendor SDKs are third-party runtimes installed on the operator's
# machine, not something this repository can bundle. When one is missing,
# ``connect()`` raises ``NoDeviceError`` naming what to install, and the
# simulated path inherited from here keeps working unchanged.


VENDOR_RUNTIME_MISSING_HINT = (
    "Choose Simulation Mode to run the full acquisition pipeline without hardware."
)


@dataclass
class ModuleCapabilities:
    """Capability record consumed by Acquisition Mode.

    Field names are a contract with ``acquisition/app.py``,
    ``acquisition/worker.py`` and ``acquisition/graph.py``.
    """

    brand: str = "simulated"
    model: str = "Simulated"
    serial_number: str = "SIM00001"
    pixel_count: int = 2048
    wavelength_min: float = 200.0
    wavelength_max: float = 1000.0
    max_intensity: float = 65535.0
    integration_time_min_us: int = 1_000
    integration_time_max_us: int = 60_000_000
    normal_trigger_mode: int | None = 0
    external_trigger_mode: int | None = 1
    trigger_modes: dict[str, int] = field(default_factory=lambda: {"normal": 0, "external": 1})
    supports_dark_correction: bool = True
    supports_nonlinearity_correction: bool = True
    supports_trigger_delay: bool = True
    trigger_delay_min_us: float = 0.0
    trigger_delay_max_us: float = 1_000_000.0

    @property
    def has_external_trigger(self) -> bool:
        return self.external_trigger_mode is not None


def trigger_mode_fields(trigger_modes: dict[str, int] | None) -> dict:
    """Expand a trigger map into the ``ModuleCapabilities`` fields that mirror it.

    ``trigger_modes`` is the authority; ``normal_trigger_mode`` and
    ``external_trigger_mode`` are conveniences the acquisition layer reads
    directly. Building all three from one call keeps a backend from reporting a
    trigger map and a mode number that disagree.
    """
    modes = dict(trigger_modes or {"normal": 0})
    return {
        "trigger_modes": modes,
        "normal_trigger_mode": modes.get("normal", 0),
        "external_trigger_mode": modes.get("external"),
    }


class SimulatedBackedModule:
    """Acquisition module backed by :class:`SimulatedSpectrometer`.

    Concrete subclasses name the brand and describe how the real device would be
    opened. ``connect()`` is deliberately left to subclasses so that a module
    representing absent hardware cannot accidentally inherit a working connect.
    """

    brand = "simulated"
    default_profile = "Generic"
    #: Reason ``connect()`` cannot reach hardware, when the subclass has no
    #: hardware path of its own.
    unavailable_reason: str = VENDOR_RUNTIME_MISSING_HINT

    def __init__(self) -> None:
        self._backend: SimulatedSpectrometer | None = None
        self._simulated = False

    # ── Connection ───────────────────────────────────────────────────────

    def connect(self, device_index: int = 0) -> str:
        """Open the real device. Not possible in the public edition."""
        raise NoDeviceError(self.unavailable_reason)

    def connect_simulated(self, profile_name: str | None = None) -> str:
        """Attach the simulated backend; fully functional."""
        profile_key = profile_name or self.default_profile
        profile = SIMULATION_PROFILES.get(profile_key, SIMULATION_PROFILES["Generic"])
        self._backend = SimulatedSpectrometer(profile=profile)
        self._simulated = True
        return f"Connected to {self._backend.model} (Simulated)"

    def disconnect(self) -> None:
        if self._backend is not None:
            self._backend.disconnect()
        self._backend = None
        self._simulated = False

    @property
    def is_connected(self) -> bool:
        return self._backend is not None and self._backend.is_connected

    def _require_backend(self) -> SimulatedSpectrometer:
        if self._backend is None:
            raise SpectrometerError(f"{type(self).__name__} is not connected.")
        return self._backend

    # ── Identity and capabilities ────────────────────────────────────────

    @property
    def model(self) -> str:
        return self._backend.model if self._backend is not None else f"{self.brand} (disconnected)"

    @property
    def serial_number(self) -> str:
        return self._backend.serial_number if self._backend is not None else "N/A"

    @property
    def capabilities(self) -> ModuleCapabilities:
        if self._backend is None:
            return ModuleCapabilities(brand=self.brand, model=self.model, serial_number="N/A")
        base = self._backend.capabilities
        return ModuleCapabilities(
            brand="simulated" if self._simulated else self.brand,
            model=base.model,
            serial_number=self._backend.serial_number,
            pixel_count=base.pixels,
            wavelength_min=base.wl_min_nm,
            wavelength_max=base.wl_max_nm,
            max_intensity=base.max_intensity,
            integration_time_min_us=base.int_min_us,
            integration_time_max_us=base.int_max_us,
            supports_dark_correction=base.supports_dark_correction,
            supports_nonlinearity_correction=base.supports_nonlinearity_correction,
            supports_trigger_delay=base.supports_trigger_delay,
            trigger_delay_max_us=base.trigger_delay_max_us,
            **trigger_mode_fields(base.trigger_modes),
        )

    # ── Acquisition ──────────────────────────────────────────────────────

    @property
    def integration_time_us(self) -> int:
        return self._require_backend().integration_time_us

    def set_integration_time(self, microseconds: int) -> None:
        self._require_backend().set_integration_time(microseconds)

    @property
    def current_trigger_mode(self) -> int:
        return self._require_backend().current_trigger_mode

    def set_trigger_mode(self, mode: int) -> None:
        self._require_backend().set_trigger_mode(mode)

    @property
    def trigger_delay_us(self) -> float:
        return self._require_backend().trigger_delay_us

    def set_trigger_delay(self, microseconds: float) -> None:
        self._require_backend().set_trigger_delay(microseconds)

    def get_wavelengths(self) -> np.ndarray:
        return self._require_backend().get_wavelengths()

    def get_intensities(self, correct_dark_counts: bool = False, correct_nonlinearity: bool = False) -> np.ndarray:
        return self._require_backend().get_intensities(
            correct_dark_counts=correct_dark_counts,
            correct_nonlinearity=correct_nonlinearity,
        )

    def get_spectrum(self) -> tuple[np.ndarray, np.ndarray]:
        return self.get_wavelengths(), self.get_intensities()

    def drain_buffered_frames(self, max_frames: int = 8, time_budget_s: float = 2.5) -> int:
        """Discard stale frames. The simulated backend never buffers any."""
        del max_frames, time_budget_s
        return 0
