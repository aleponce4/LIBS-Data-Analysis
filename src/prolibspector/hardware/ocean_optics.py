"""Ocean Optics / Ocean Insight backend, driven through python-seabreeze.

Model-agnostic: pixel count, wavelength range, integration limits, trigger-mode
numbers and correction support are all read from the device at connect time and
published through :class:`~prolibspector.hardware.spectrometer.ModuleCapabilities`,
so the acquisition UI configures itself from the instrument rather than from a
per-model branch here.

Two things make this longer than a straight SDK wrapper, and both are earned:

* **Backend selection.** python-seabreeze ships two backends. ``cseabreeze``
  wraps the vendor C library and needs the WinUSB driver bound to the device;
  ``pyseabreeze`` is pure Python over libusb and accepts more drivers but is
  slower to enumerate. Which one works depends on the machine, and seabreeze
  cannot reliably switch backends after device discovery has run once in a
  process. So each backend is probed in a fresh subprocess, and only the
  winner is activated in this one.
* **libusb staging on Windows.** ``pyseabreeze`` fails at import with an
  unhelpful "No pyusb backend found" unless ``libusb-1.0.dll`` is reachable.
  The DLL usually ships inside the ``libusb_package`` wheel rather than on
  PATH, so the candidate directories are registered before the backend is
  activated.

seabreeze itself is imported lazily inside the connection flow, so Analysis
Mode starts with no spectrometer library present.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import logging
import os
import platform
import subprocess
import sys
from pathlib import Path

import numpy as np

from prolibspector.hardware.spectrometer import (
    DEFAULT_INTEGRATION_TIME_US,
    VENDOR_RUNTIME_MISSING_HINT,
    ModuleCapabilities,
    NoDeviceError,
    SimulatedBackedModule,
    SpectrometerError,
    trigger_mode_fields,
)
from prolibspector.hardware.usb_scan import (
    driver_ok_for_backend,
    linux_usb_runtime_status,
    scan_usb_spectrometers,
)

logger = logging.getLogger(__name__)

OCEAN_OPTICS_DEFAULT_INTEGRATION_TIME_US = DEFAULT_INTEGRATION_TIME_US

SEABREEZE_MISSING_REASON = (
    "python-seabreeze is not installed, so no Ocean Optics device can be opened. "
    "Install it with:  pip install seabreeze\n" + VENDOR_RUNTIME_MISSING_HINT
)

_LIBUSB_DLL_NAME = "libusb-1.0.dll"
_DLL_DIRECTORY_HANDLES: list = []

_SEABREEZE_BACKEND_ORDER = ("cseabreeze", "pyseabreeze")
_SEABREEZE_PROBE_TIMEOUT_SEC = 15
_SEABREEZE_BACKEND_SELECTION_CACHE: dict | None = None
_SEABREEZE_ACTIVE_BACKEND: str | None = None


# ═══════════════════════════════════════════════════════════════════════
#  Trigger mode mapping
# ═══════════════════════════════════════════════════════════════════════

# Semantic names seabreeze reports for the modes we care about.
_NORMAL_MODE_NAMES = {"NORMAL", "OBP_NORMAL"}
_EXTERNAL_TRIGGER_MODE_NAMES = {
    "HARDWARE", "EDGE", "OBP_EXTERNAL", "OBP_EDGE",
    "SYNCHRONIZATION", "LEVEL", "EXTERNAL",
}

# The cseabreeze C wrapper (2.4+) exposes neither ``_trigger_modes`` nor
# ``_feature_classes``, so runtime introspection finds nothing and the Arm
# Trigger button would be permanently disabled on an otherwise working device.
# These are the documented OOI protocol numbers:
#   0 normal (free running) · 1 software · 2 external level · 3 external edge
# Edge (3) is the one a Q-switch sync pulse drives.
_OOI_EDGE_TRIGGER_MODELS = {
    "USB2000", "USB2000+", "USB4000", "HR2000", "HR2000+",
    "HR4000", "QE65000", "QE65Pro", "QEPro", "FLAME-S",
    "FLAME-T", "Maya2000", "Maya2000Pro", "NIRQuest256",
    "NIRQuest512",
}

_MODEL_TRIGGER_FALLBACKS: dict[str, dict[str, int]] = {
    model: {"normal": 0, "external": 3} for model in _OOI_EDGE_TRIGGER_MODELS
}

# OBP-protocol devices number the external trigger differently.
for _obp_model in ("HDX", "Ocean-ST", "Ocean-SR2", "Ocean-SR4", "Ocean-SR6"):
    _MODEL_TRIGGER_FALLBACKS[_obp_model] = {"normal": 0, "external": 1}


def build_trigger_map(spec) -> dict[str, int]:
    """Derive a semantic trigger map from an open seabreeze spectrometer.

    Introspects the device first and falls back to the model table above when
    the backend hides its mode enum. Always returns a map containing at least
    ``normal``; ``external`` is absent when neither route could establish it,
    which the UI reads as "this device cannot be armed".
    """
    trigger_map: dict[str, int] = {}
    raw_modes = None

    # The pyseabreeze device object carries the enum on its spectrometer feature.
    try:
        spec_feature = spec._dev.features.get("spectrometer", [None])[0]
        if spec_feature is not None and hasattr(spec_feature, "_trigger_modes"):
            raw_modes = spec_feature._trigger_modes
    except Exception:
        pass

    # Some builds only carry it on the device class.
    if raw_modes is None:
        try:
            feature_classes = getattr(type(spec._dev), "_feature_classes", {})
            for feature_class in feature_classes.get("spectrometer", []):
                if hasattr(feature_class, "_trigger_modes"):
                    raw_modes = feature_class._trigger_modes
                    break
        except Exception:
            pass

    for mode_value in raw_modes or ():
        try:
            mode_name = mode_value.name if hasattr(mode_value, "name") else str(mode_value)
            mode_int = int(mode_value)
        except Exception:
            continue
        upper = mode_name.upper()
        if upper in _NORMAL_MODE_NAMES:
            trigger_map["normal"] = mode_int
        elif upper in _EXTERNAL_TRIGGER_MODE_NAMES:
            trigger_map["external"] = mode_int
        # Keep the vendor's own spelling too, for anything driving this directly.
        trigger_map[mode_name.lower()] = mode_int

    if "external" not in trigger_map:
        model = getattr(spec, "model", "")
        fallback = _MODEL_TRIGGER_FALLBACKS.get(model)
        if fallback is not None:
            logger.info(
                "No trigger-mode introspection for %s; using the documented "
                "protocol numbers %s.", model, fallback,
            )
            trigger_map.update(fallback)
        else:
            logger.info(
                "No trigger-mode introspection and no known numbers for model %r; "
                "only normal mode will be offered.", model,
            )

    trigger_map.setdefault("normal", 0)
    return trigger_map


# ═══════════════════════════════════════════════════════════════════════
#  Backend probing and libusb staging
# ═══════════════════════════════════════════════════════════════════════

def _activate_seabreeze_backend(seabreeze_module, backend: str) -> None:
    """Activate a backend once per interpreter; seabreeze cannot switch later."""
    global _SEABREEZE_ACTIVE_BACKEND
    if _SEABREEZE_ACTIVE_BACKEND == backend:
        return
    seabreeze_module.use(backend)
    _SEABREEZE_ACTIVE_BACKEND = backend


def _iter_libusb_candidate_dirs() -> list[Path]:
    """Return the directories that plausibly hold ``libusb-1.0.dll``."""
    candidates: list[Path] = []

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass))
        candidates.append(Path(sys.executable).resolve().parent)

    spec = importlib.util.find_spec("libusb_package")
    if spec and spec.origin:
        candidates.append(Path(spec.origin).resolve().parent)

    candidates.append(Path(sys.prefix) / "Lib" / "site-packages" / "libusb_package")
    candidates.append(Path(sys.base_prefix) / "Lib" / "site-packages" / "libusb_package")

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(resolved)
    return deduped


def _prepare_libusb_runtime(backend: str) -> list[str]:
    """Make libusb loadable before pyseabreeze is activated.

    Returns the directories confirmed to contain the DLL, so a caller can tell
    "staged nothing because there was nothing to stage" from "staged it".
    """
    if backend != "pyseabreeze" or platform.system() != "Windows":
        return []

    confirmed_dirs: list[str] = []
    path_entries = {
        entry.strip().lower()
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry.strip()
    }

    for candidate_dir in _iter_libusb_candidate_dirs():
        if not (candidate_dir / _LIBUSB_DLL_NAME).is_file():
            continue

        candidate_str = str(candidate_dir)
        confirmed_dirs.append(candidate_str)

        if hasattr(os, "add_dll_directory"):
            try:
                # Held for the process lifetime; releasing the handle would
                # un-register the directory under the caller's feet.
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(candidate_str))
            except (FileNotFoundError, OSError):
                pass

        if candidate_str.lower() not in path_entries:
            current_path = os.environ.get("PATH", "")
            os.environ["PATH"] = (
                candidate_str if not current_path else candidate_str + os.pathsep + current_path
            )
            path_entries.add(candidate_str.lower())

    return confirmed_dirs


def collect_seabreeze_probe(backend: str) -> dict:
    """Load one backend and enumerate devices, in whatever process calls this.

    Kept importable because the subprocess probe below re-enters it by name.
    """
    result = {
        "backend": backend,
        "use_ok": False,
        "list_ok": False,
        "device_count": 0,
        "devices": [],
        "failure": None,
    }

    try:
        runtime_dirs = _prepare_libusb_runtime(backend)
        if backend == "pyseabreeze" and not runtime_dirs:
            logger.debug("No libusb runtime directory found before probing pyseabreeze")

        import seabreeze

        seabreeze.use(backend)
        result["use_ok"] = True

        from seabreeze.spectrometers import list_devices

        devices = list_devices()
        result["list_ok"] = True
        result["device_count"] = len(devices)

        for index, device in enumerate(devices):
            try:
                model = device.model
            except Exception:
                model = "Unknown"
            try:
                serial = device.serial_number
            except Exception:
                serial = f"device_{index}"
            result["devices"].append({"index": index, "model": model, "serial": serial})

        if result["device_count"] == 0:
            result["failure"] = "loaded OK but found 0 devices"

    except Exception as exc:
        result["failure"] = str(exc)

    return result


def _probe_seabreeze_backend(backend: str) -> dict:
    """Probe one backend in a clean subprocess and return its structured result."""
    probe = {
        "backend": backend,
        "use_ok": False,
        "list_ok": False,
        "device_count": 0,
        "devices": [],
        "failure": None,
    }

    if getattr(sys, "frozen", False):
        command = [sys.executable, "--seabreeze-probe", backend]
    else:
        probe_script = (
            "import json, sys\n"
            "from prolibspector.hardware.ocean_optics import collect_seabreeze_probe\n"
            "print(json.dumps(collect_seabreeze_probe(sys.argv[1])))\n"
        )
        command = [sys.executable, "-c", probe_script, backend]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_SEABREEZE_PROBE_TIMEOUT_SEC,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except subprocess.TimeoutExpired:
        probe["failure"] = "probe timed out"
        return probe
    except Exception as exc:
        probe["failure"] = f"probe error: {exc}"
        return probe

    # A backend may log to stdout before the JSON line, so scan from the end.
    for line in reversed([line.strip() for line in completed.stdout.splitlines() if line.strip()]):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            probe.update(parsed)
            return probe

    stderr = completed.stderr.strip()
    if stderr:
        probe["failure"] = stderr
    elif completed.returncode != 0:
        probe["failure"] = f"probe exited with code {completed.returncode}"
    else:
        probe["failure"] = "probe returned no JSON result"
    return probe


def describe_seabreeze_probe(probe: dict) -> str:
    """Render one probe result as a line for the diagnostics dialog."""
    if probe.get("use_ok") and probe.get("list_ok"):
        count = int(probe.get("device_count", 0))
        return f"found {count} device(s)" if count else "loaded OK but found 0 devices"

    failure = str(probe.get("failure") or "").strip()
    if failure:
        if "No pyusb backend found" in failure:
            if platform.system() == "Linux":
                return (
                    "No pyusb/libusb backend found. Install system libusb and "
                    "confirm this user has USB permissions."
                )
            return (
                "No pyusb backend found. Install `libusb-package`, or put "
                "`libusb-1.0.dll` on PATH."
            )
        return failure
    if probe.get("use_ok"):
        return "loaded OK but list_devices() failed"
    return "failed to load"


def _probe_found_devices(probe: dict | None) -> bool:
    probe = probe or {}
    return bool(
        probe.get("use_ok")
        and probe.get("list_ok")
        and int(probe.get("device_count", 0)) > 0
    )


def select_seabreeze_backend(*, force_refresh: bool = False) -> dict:
    """Pick the backend to use, preferring one that actually finds a device.

    A backend that loads but enumerates nothing is not good enough — that is
    exactly the WinUSB-vs-libusb mismatch case — so probing continues to the
    next candidate and only falls back to "loaded but empty" if no backend
    does better. A successful selection is cached; a failed one is not, so
    plugging the device in and retrying re-probes.
    """
    global _SEABREEZE_BACKEND_SELECTION_CACHE

    if force_refresh:
        _SEABREEZE_BACKEND_SELECTION_CACHE = None
    elif _SEABREEZE_BACKEND_SELECTION_CACHE is not None:
        return copy.deepcopy(_SEABREEZE_BACKEND_SELECTION_CACHE)

    probes: list[dict] = []
    selected_probe = None
    first_loadable_probe = None

    for backend in _SEABREEZE_BACKEND_ORDER:
        probe = _probe_seabreeze_backend(backend)
        probes.append(probe)

        if probe.get("use_ok") and first_loadable_probe is None:
            first_loadable_probe = probe

        if _probe_found_devices(probe):
            selected_probe = probe
            break

    if selected_probe is None:
        selected_probe = first_loadable_probe

    selected_backend = selected_probe["backend"] if selected_probe else None

    def _summarize(candidates: list[dict]) -> str:
        return "; ".join(
            f"{probe['backend']}: {describe_seabreeze_probe(probe)}" for probe in candidates
        )

    if selected_probe is None:
        failure_reason = _summarize(probes)
    elif _probe_found_devices(selected_probe):
        rejected = [probe for probe in probes if probe["backend"] != selected_backend]
        failure_reason = (
            f"{_summarize(rejected)}; fell back to {selected_backend}" if rejected else None
        )
    else:
        failure_reason = _summarize(probes)

    selection = {
        "selected_backend": selected_backend,
        "selected_probe": selected_probe,
        "probes": probes,
        "failure_reason": failure_reason,
    }
    if _probe_found_devices(selection.get("selected_probe")):
        _SEABREEZE_BACKEND_SELECTION_CACHE = copy.deepcopy(selection)
    return selection


# ═══════════════════════════════════════════════════════════════════════
#  SpectrometerModule
# ═══════════════════════════════════════════════════════════════════════

class SpectrometerModule(SimulatedBackedModule):
    """Acquisition module for Ocean Optics hardware.

    Hardware state lives in ``_spec`` (an open seabreeze ``Spectrometer``).
    While it is ``None`` every operation falls through to
    :class:`SimulatedBackedModule`, so ``connect_simulated()`` needs nothing
    from this class and the simulated pipeline is identical with or without
    seabreeze installed.
    """

    brand = "ocean_optics"
    unavailable_reason = SEABREEZE_MISSING_REASON

    def __init__(self) -> None:
        super().__init__()
        self._spec = None
        self._seabreeze = None
        self._wavelengths: np.ndarray | None = None
        self._hardware_capabilities = ModuleCapabilities(brand=self.brand)
        self._integration_time_us = OCEAN_OPTICS_DEFAULT_INTEGRATION_TIME_US
        self._current_trigger_mode = 0
        self._seabreeze_backend: str | None = None
        self._seabreeze_backend_fail_reason: str | None = None

    # ── Connection ───────────────────────────────────────────────────────

    def list_available_devices(self) -> list:
        """Return ``(model, serial, device)`` for every device seabreeze sees."""
        self._import_seabreeze()
        from seabreeze.spectrometers import list_devices

        try:
            devices = list_devices()
        except Exception as exc:
            raise SpectrometerError(f"Error scanning for spectrometers: {exc}")

        result = []
        for device in devices:
            try:
                result.append((device.model, device.serial_number, device))
            except Exception:
                result.append(("Unknown", "?", device))
        return result

    def connect(self, device_index: int = 0) -> str:
        """Open the device at *device_index* and publish its capabilities."""
        self._import_seabreeze()
        from seabreeze.spectrometers import Spectrometer, list_devices

        try:
            devices = list_devices()
        except Exception as exc:
            raise SpectrometerError(f"Error scanning for spectrometers: {exc}")

        if not devices:
            driver_hint = (
                "  2. Linux udev/libusb permissions let this user reach VID 0x2457\n"
                if platform.system() == "Linux"
                else "  2. The USB driver is bound (run seabreeze_os_setup)\n"
            )
            raise NoDeviceError(
                "No Ocean Optics spectrometer found.\n\n"
                "Check that:\n"
                "  1. The spectrometer is plugged in over USB\n"
                f"{driver_hint}"
                "  3. No other software (OceanView) already holds the device"
            )

        if device_index >= len(devices):
            raise SpectrometerError(
                f"Device index {device_index} out of range — "
                f"only {len(devices)} device(s) found."
            )

        logger.info("Found %d Ocean Optics device(s); opening index %d.", len(devices), device_index)
        try:
            self._spec = Spectrometer(devices[device_index])
        except Exception as exc:
            raise SpectrometerError(f"Failed to open spectrometer: {exc}")

        try:
            capabilities = self._read_device_capabilities()
        except Exception:
            self._close_device()
            raise

        self._hardware_capabilities = capabilities
        self.set_integration_time(self._integration_time_us)
        self.set_trigger_mode(capabilities.normal_trigger_mode)
        self._verify_first_read(capabilities)

        status = "\n".join([
            f"Connected: {capabilities.model} (S/N: {capabilities.serial_number})",
            (f"Pixels: {capabilities.pixel_count} | "
             f"Range: {capabilities.wavelength_min}–{capabilities.wavelength_max} nm"),
            (f"Integration: {capabilities.integration_time_min_us / 1000.0}–"
             f"{capabilities.integration_time_max_us / 1000.0} ms"),
            f"Max intensity: {capabilities.max_intensity:.0f}",
            "Trigger modes: " + ", ".join(
                f"{name}={value}" for name, value in capabilities.trigger_modes.items()
            ),
        ])
        logger.info(status)
        return status

    def _read_device_capabilities(self) -> ModuleCapabilities:
        """Query the open device. Anything optional degrades to a safe default."""
        self._wavelengths = self._spec.wavelengths()

        integration_min_us = 1_000
        integration_max_us = 60_000_000
        try:
            queried_min, queried_max = self._spec.integration_time_micros_limits
            integration_min_us = int(queried_min)
            integration_max_us = int(queried_max)
        except Exception:
            logger.debug("Device did not report integration limits; using defaults.")

        max_intensity = 65535.0
        try:
            max_intensity = float(self._spec.max_intensity)
        except Exception:
            logger.debug("Device did not report max intensity; assuming 16-bit.")

        supports_nonlinearity = False
        supports_dark = False
        try:
            supports_nonlinearity = bool(self._spec.features.get("nonlinearity_coefficients"))
            supports_dark = True
        except Exception:
            logger.debug("Device did not report correction features.")

        trigger_modes = build_trigger_map(self._spec)

        return ModuleCapabilities(
            brand=self.brand,
            model=self._spec.model,
            serial_number=self._spec.serial_number,
            pixel_count=self._spec.pixels,
            wavelength_min=round(float(self._wavelengths[0]), 1),
            wavelength_max=round(float(self._wavelengths[-1]), 1),
            max_intensity=max_intensity,
            integration_time_min_us=integration_min_us,
            integration_time_max_us=integration_max_us,
            supports_dark_correction=supports_dark,
            supports_nonlinearity_correction=supports_nonlinearity,
            # No Ocean Optics device in this family exposes a programmable
            # trigger-to-exposure delay; the gate is external to the detector.
            supports_trigger_delay=False,
            trigger_delay_max_us=0.0,
            **trigger_mode_fields(trigger_modes),
        )

    def _verify_first_read(self, capabilities: ModuleCapabilities) -> None:
        """Read one spectrum before reporting success.

        A device can open cleanly and still fail every read — usually after a
        cancelled transfer left the USB interface wedged. Failing here, with
        the device closed, is much easier to act on than failing later mid-run.
        """
        try:
            spectrum = self._spec.intensities()
        except Exception as exc:
            self._close_device()
            raise SpectrometerError(
                f"Spectrometer opened but failed its verification read:\n{exc}\n\n"
                "The device may be wedged. Unplug it, wait a few seconds, and reconnect."
            )

        if len(spectrum) != capabilities.pixel_count:
            logger.warning(
                "Verification read returned %d pixels, expected %d.",
                len(spectrum), capabilities.pixel_count,
            )
        else:
            logger.info("Verification read OK: %d pixels.", capabilities.pixel_count)

    def disconnect(self) -> None:
        if self._spec is None:
            super().disconnect()
            return
        try:
            normal = self._hardware_capabilities.normal_trigger_mode
            if self._current_trigger_mode != normal:
                # Leave the device free-running; an armed device left behind
                # blocks the next process that opens it.
                try:
                    self._spec.trigger_mode(normal)
                except Exception as exc:
                    logger.debug("Could not restore normal trigger mode: %s", exc)
            self._close_device()
            logger.info("Ocean Optics spectrometer disconnected.")
        except Exception as exc:
            logger.warning("Error during disconnect: %s", exc)
            self._close_device()

    def _close_device(self) -> None:
        spec, self._spec = self._spec, None
        if spec is not None:
            try:
                spec.close()
            except Exception as exc:
                logger.debug("Error closing seabreeze device: %s", exc)
        self._wavelengths = None
        self._current_trigger_mode = 0
        self._hardware_capabilities = ModuleCapabilities(brand=self.brand)
        self._seabreeze_backend = None
        self._seabreeze_backend_fail_reason = None

    # ── Identity and capabilities ────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        if self._spec is None:
            return super().is_connected
        is_open = getattr(self._spec, "is_open", None)
        if is_open is not None:
            return bool(is_open)
        # Some builds only expose the flag on the underlying device object. If
        # neither is readable, report disconnected rather than guessing open —
        # a wrong "yes" here turns into a hang on the next read.
        try:
            return bool(self._spec._dev.is_open)
        except Exception:
            return False

    @property
    def capabilities(self) -> ModuleCapabilities:
        if self._spec is None:
            return super().capabilities
        return self._hardware_capabilities

    @property
    def model(self) -> str:
        if self._spec is None:
            return super().model
        return self._spec.model if self.is_connected else "N/A"

    @property
    def serial_number(self) -> str:
        if self._spec is None:
            return super().serial_number
        return self._spec.serial_number if self.is_connected else "N/A"

    @property
    def seabreeze_backend(self) -> str | None:
        """Which backend this module activated, once it has connected."""
        return self._seabreeze_backend

    @property
    def seabreeze_backend_fail_reason(self) -> str | None:
        """Why the other backend was passed over, when one was."""
        return self._seabreeze_backend_fail_reason

    # ── Acquisition ──────────────────────────────────────────────────────

    @property
    def integration_time_us(self) -> int:
        if self._spec is None:
            return super().integration_time_us
        return self._integration_time_us

    def set_integration_time(self, microseconds: int) -> None:
        if self._spec is None:
            super().set_integration_time(microseconds)
            return
        if not self.is_connected:
            raise SpectrometerError("Spectrometer not connected.")

        capabilities = self._hardware_capabilities
        microseconds = max(
            capabilities.integration_time_min_us,
            min(int(microseconds), capabilities.integration_time_max_us),
        )
        try:
            self._spec.integration_time_micros(microseconds)
        except Exception as exc:
            raise SpectrometerError(f"Failed to set integration time: {exc}")
        self._integration_time_us = microseconds
        logger.info("Integration time set to %d µs", microseconds)

    @property
    def current_trigger_mode(self) -> int:
        if self._spec is None:
            return super().current_trigger_mode
        return self._current_trigger_mode

    def set_trigger_mode(self, mode: int) -> None:
        if self._spec is None:
            super().set_trigger_mode(mode)
            return
        if not self.is_connected:
            raise SpectrometerError("Spectrometer not connected.")

        valid_modes = set(self._hardware_capabilities.trigger_modes.values())
        if mode not in valid_modes:
            raise SpectrometerError(
                f"Invalid trigger mode {mode} for {self.model}. "
                f"Supported: {self._hardware_capabilities.trigger_modes}"
            )

        try:
            self._spec.trigger_mode(mode)
        except Exception as exc:
            raise SpectrometerError(
                f"Failed to set trigger mode {mode}: {exc}\n"
                "If the device appears frozen, disconnect and reconnect it."
            )
        self._current_trigger_mode = mode
        logger.info("Trigger mode set to %d", mode)

    @property
    def trigger_delay_us(self) -> float:
        if self._spec is None:
            return super().trigger_delay_us
        return 0.0

    def set_trigger_delay(self, microseconds: float) -> None:
        if self._spec is None:
            super().set_trigger_delay(microseconds)
            return
        raise SpectrometerError(
            f"{self.model} has no programmable trigger delay. On this instrument "
            "the delay is set on the external gate generator, not in the detector."
        )

    def get_wavelengths(self) -> np.ndarray:
        if self._spec is None:
            return super().get_wavelengths()
        if not self.is_connected:
            raise SpectrometerError("Spectrometer not connected.")
        return self._wavelengths.copy()

    def get_intensities(
        self,
        correct_dark_counts: bool = False,
        correct_nonlinearity: bool = False,
    ) -> np.ndarray:
        """Acquire one spectrum.

        In normal mode this returns after the integration time. In external
        trigger mode it blocks in the SDK until a trigger edge arrives — the
        caller is responsible for not doing that on a UI thread.
        """
        if self._spec is None:
            return super().get_intensities(
                correct_dark_counts=correct_dark_counts,
                correct_nonlinearity=correct_nonlinearity,
            )
        if not self.is_connected:
            raise SpectrometerError("Spectrometer not connected.")
        try:
            return self._spec.intensities(
                correct_dark_counts=correct_dark_counts,
                correct_nonlinearity=correct_nonlinearity,
            )
        except Exception as exc:
            raise SpectrometerError(f"Acquisition error: {exc}")

    # ── Diagnostics ──────────────────────────────────────────────────────

    def _import_seabreeze(self) -> None:
        """Import seabreeze and activate the winning backend, once."""
        if self._seabreeze is not None:
            return
        try:
            import seabreeze
        except ImportError:
            # Nothing to connect to rather than a fault: the UI answers this
            # by offering the simulated device, not by reporting a bug.
            raise NoDeviceError(SEABREEZE_MISSING_REASON)

        selection = select_seabreeze_backend()
        backend = selection["selected_backend"]
        if backend is None:
            reason = selection["failure_reason"] or "no backend could be loaded"
            raise SpectrometerError(f"Neither seabreeze backend could be loaded.\n{reason}")

        _prepare_libusb_runtime(backend)
        _activate_seabreeze_backend(seabreeze, backend)
        self._seabreeze_backend = backend
        self._seabreeze_backend_fail_reason = selection["failure_reason"]
        self._seabreeze = seabreeze

        if selection["failure_reason"]:
            logger.info("Using %s backend (%s)", backend, selection["failure_reason"])
        else:
            logger.info("Using %s backend", backend)

    @classmethod
    def diagnose(cls) -> dict:
        """Build the Ocean Optics tab of the diagnostics dialog.

        Deliberately reports the USB bus and seabreeze separately: the useful
        signal is the *disagreement* between them, because a device the OS can
        see but seabreeze cannot is a driver problem with a known fix.
        """
        report: dict = {
            "backend": "SpectrometerModule",
            "platform": platform.system(),
            "seabreeze_installed": False,
            "seabreeze_backend": None,
            "seabreeze_backend_fail_reason": None,
            "usb_devices": [],
            "seabreeze_devices": [],
            "per_device_errors": {},
            "driver_warnings": [],
            "notes": [],
        }

        if platform.system() == "Linux":
            runtime = linux_usb_runtime_status()
            report.update(runtime)
            if not runtime["pyusb_installed"]:
                report["notes"].append("PyUSB is not installed. Install the `pyusb` package.")
            if not runtime["libusb_found"]:
                report["notes"].append(
                    "System libusb was not found. Install libusb-1.0 with your package manager."
                )

        try:
            report["usb_devices"] = [
                device for device in scan_usb_spectrometers()
                if device["brand"] == "ocean_optics"
            ]
        except Exception as exc:
            report["notes"].append(f"USB bus scan error: {exc}")

        try:
            import seabreeze
            report["seabreeze_installed"] = True
        except ImportError:
            report["notes"].append(
                "python-seabreeze is NOT installed.\nInstall it with:  pip install seabreeze"
            )
            return report

        selection = select_seabreeze_backend(force_refresh=True)
        backend = selection["selected_backend"]
        report["seabreeze_backend"] = backend
        report["seabreeze_backend_fail_reason"] = selection["failure_reason"]

        probed_devices = (selection["selected_probe"] or {}).get("devices") or []
        report["seabreeze_devices"] = [
            {
                "index": device.get("index", index),
                "model": device.get("model", "Unknown"),
                "serial": device.get("serial", f"device_{index}"),
            }
            for index, device in enumerate(probed_devices)
        ]

        if backend is None:
            report["notes"].append("Neither seabreeze backend could be loaded.")
            return report

        try:
            _prepare_libusb_runtime(backend)
            _activate_seabreeze_backend(seabreeze, backend)
        except Exception as exc:
            report["notes"].append(
                f"Selected seabreeze backend '{backend}' could not be activated: {exc}"
            )
            return report

        cls._probe_each_device(report)
        cls._add_cross_reference_notes(report, backend)
        return report

    @staticmethod
    def _probe_each_device(report: dict) -> None:
        """Open and close every enumerated device to surface per-device errors."""
        from seabreeze.spectrometers import Spectrometer, list_devices

        try:
            devices = list_devices()
        except Exception as exc:
            report["notes"].append(f"list_devices() error: {exc}")
            return

        if not devices:
            return

        report["seabreeze_devices"] = []
        for index, device in enumerate(devices):
            try:
                model, serial = device.model, device.serial_number
            except Exception:
                model, serial = "Unknown", f"device_{index}"
            report["seabreeze_devices"].append({"index": index, "model": model, "serial": serial})

            try:
                Spectrometer(device).close()
            except Exception as exc:
                report["per_device_errors"][serial] = str(exc)

    @staticmethod
    def _add_cross_reference_notes(report: dict, backend: str) -> None:
        """Explain any gap between what the bus shows and what seabreeze sees."""
        if platform.system() == "Windows" and report["usb_devices"]:
            for usb_device in report["usb_devices"]:
                ok, advice = driver_ok_for_backend(usb_device["driver"], backend)
                if not ok:
                    report["driver_warnings"].append({
                        "device": usb_device["description"],
                        "instance_id": usb_device["instance_id"],
                        "current_driver": usb_device["driver"],
                        "needed_backend": backend,
                        "advice": advice,
                    })

        usb_count = len(report["usb_devices"])
        seabreeze_count = len(report["seabreeze_devices"])

        if usb_count > 0 and seabreeze_count < usb_count:
            if platform.system() == "Linux":
                report["notes"].append(
                    f"Linux sees {usb_count} Ocean Optics USB device(s) but seabreeze "
                    f"recognises {seabreeze_count}.\n"
                    "Check udev rules and user permissions for VID 0x2457, then reconnect."
                )
            else:
                failure_reason = str(report.get("seabreeze_backend_fail_reason") or "").lower()
                driver_looks_fine = any(
                    driver_ok_for_backend(device.get("driver", ""), "pyseabreeze")[0]
                    or driver_ok_for_backend(device.get("driver", ""), "cseabreeze")[0]
                    for device in report["usb_devices"]
                )
                claim_failed = any(
                    phrase in failure_reason for phrase in ("access denied", "timed out")
                )
                if claim_failed and driver_looks_fine:
                    report["notes"].append(
                        f"Windows sees {usb_count} Ocean Optics USB device(s) but seabreeze "
                        f"recognises {seabreeze_count}.\n"
                        "The driver looks correct, so seabreeze could not *claim* the device. "
                        "That usually means another process still holds it, or Windows has not "
                        "released the interface after a cancelled read. Close OceanView and any "
                        "other instance, wait a few seconds, then unplug and replug the device."
                    )
                else:
                    report["notes"].append(
                        f"Windows sees {usb_count} Ocean Optics USB device(s) but seabreeze "
                        f"recognises {seabreeze_count}.\n"
                        "The missing device(s) most likely have the wrong USB driver bound."
                    )

        if platform.system() == "Linux" and not report["usb_devices"]:
            report["notes"].append(
                "No Ocean Optics device is visible on the Linux USB bus. If one is attached, "
                "check the cable, then `lsusb` and udev permissions."
            )


__all__ = [
    "OCEAN_OPTICS_DEFAULT_INTEGRATION_TIME_US",
    "SEABREEZE_MISSING_REASON",
    "SpectrometerModule",
    "build_trigger_map",
    "collect_seabreeze_probe",
    "describe_seabreeze_probe",
    "select_seabreeze_backend",
]
