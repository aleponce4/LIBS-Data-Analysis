"""Thorlabs CCS-series backend, driven through ``TLCCS_64.dll`` over NI-VISA.

Every CCS model — CCS100, CCS125, CCS150, CCS175, CCS200 — carries the same
3648-pixel Toshiba TCD1304 linear CCD behind the same DLL, so one class covers
the family and the product ID in the VISA resource string is what names the
model.

Four differences from the Ocean Optics path shape this module:

* Intensities come back **normalised 0.0–1.0**, not ADC counts, so saturation
  is a value of 1.0 rather than 65535.
* Integration time is passed to the DLL in **seconds**; the app works in
  microseconds everywhere else.
* There is **no external hardware trigger** in the DLL API at all, which makes
  this backend unsuitable for gated LIBS acquisition and useful mainly for
  continuous-source work and alignment.
* A scan is **polled, not blocking**: start it, then watch the device status
  word for the transfer bit before reading the data out.

The DLL is a third-party runtime (it arrives with ThorSpectra) and is loaded
lazily through ctypes, so this module imports cleanly with nothing installed.
"""

from __future__ import annotations

import logging
import os
import platform
import time

import numpy as np

from prolibspector.core.runtime import supports_thorlabs_ccs
from prolibspector.hardware.spectrometer import (
    DEFAULT_INTEGRATION_TIME_US,
    VENDOR_RUNTIME_MISSING_HINT,
    ModuleCapabilities,
    NoDeviceError,
    SimulatedBackedModule,
    SpectrometerError,
    trigger_mode_fields,
)
from prolibspector.hardware.usb_scan import scan_usb_spectrometers

logger = logging.getLogger(__name__)

#: Product ID → model, used both to name an opened device and to build the
#: VISA search patterns that discover one.
CCS_PRODUCT_IDS: dict[int, str] = {
    0x8081: "CCS100",
    0x8083: "CCS125",
    0x8085: "CCS150",
    0x8087: "CCS175",
    0x8089: "CCS200",
}

#: Shared across the whole CCS family.
CCS_PIXEL_COUNT = 3648

#: Status bit meaning "scan data is ready to transfer" (from TLCCS.h).
_STATUS_SCAN_TRANSFER = 0x0010

_INTEGRATION_TIME_MIN_US = 10
_INTEGRATION_TIME_MAX_US = 60_000_000

_DLL_SEARCH_PATHS = (
    r"C:\Program Files\IVI Foundation\VISA\Win64\Bin\TLCCS_64.dll",
    r"C:\Program Files (x86)\IVI Foundation\VISA\Win64\Bin\TLCCS_64.dll",
)

TLCCS_MISSING_REASON = (
    "The Thorlabs driver TLCCS_64.dll was not found, so no CCS device can be opened.\n"
    "Install ThorSpectra from thorlabs.com; it also supplies the NI-VISA runtime.\n"
    "Expected at:  " + _DLL_SEARCH_PATHS[0] + "\n" + VENDOR_RUNTIME_MISSING_HINT
)


def _find_tlccs_dll() -> str | None:
    """Return the path to TLCCS_64.dll in its usual install locations."""
    for path in _DLL_SEARCH_PATHS:
        if os.path.isfile(path):
            return path
    return None


def _load_visa():
    """Load the NI-VISA runtime, trying the 64-bit library first."""
    import ctypes

    for library_name in ("visa64.dll", "visa32.dll"):
        try:
            return ctypes.cdll.LoadLibrary(library_name)
        except OSError:
            continue
    return None


def _serial_from_resource(resource: str) -> str:
    """Pull the serial out of a VISA resource string like ``…::M00123456::RAW``."""
    for part in resource.replace("'", "").split("::"):
        if part.startswith("M") and len(part) > 1:
            return part
    return "N/A"


def _find_visa_resources(visa) -> list[tuple[str, str, str]]:
    """Return ``(model, serial, resource)`` for every CCS the VISA layer sees."""
    from ctypes import byref, c_ulong, create_string_buffer

    resource_manager = c_ulong(0)
    if visa.viOpenDefaultRM(byref(resource_manager)) != 0:
        return []

    found: list[tuple[str, str, str]] = []
    try:
        for product_id, model in CCS_PRODUCT_IDS.items():
            pattern = f"USB0::0x1313::0x{product_id:04X}::?*::RAW".encode()
            find_list = c_ulong(0)
            count = c_ulong(0)
            buffer = create_string_buffer(512)

            if visa.viFindRsrc(resource_manager, pattern, byref(find_list), byref(count), buffer) != 0:
                continue
            if count.value == 0:
                continue

            resource = buffer.value.decode()
            found.append((model, _serial_from_resource(resource), resource))

            # viFindRsrc yields the first match; the rest come from viFindNext.
            for _ in range(count.value - 1):
                if visa.viFindNext(find_list, buffer) != 0:
                    break
                resource = buffer.value.decode()
                found.append((model, _serial_from_resource(resource), resource))

            visa.viClose(find_list)
    finally:
        visa.viClose(resource_manager)

    return found


class ThorlabsCCSModule(SimulatedBackedModule):
    """Acquisition module for Thorlabs CCS hardware.

    Hardware state is the open DLL session in ``_handle``. While it is ``None``
    every operation falls through to :class:`SimulatedBackedModule`, so the
    simulated CCS175 profile works identically whether or not ThorSpectra is
    installed.
    """

    brand = "thorlabs"
    default_profile = "CCS175"
    unavailable_reason = TLCCS_MISSING_REASON

    def __init__(self) -> None:
        super().__init__()
        self._lib = None
        self._handle: int | None = None
        self._wavelengths: np.ndarray | None = None
        self._hardware_capabilities = ModuleCapabilities(brand=self.brand)
        self._integration_time_us = DEFAULT_INTEGRATION_TIME_US
        self._model_name = "CCS"
        self._serial = "N/A"

    # ── Connection ───────────────────────────────────────────────────────

    def connect(self, device_index: int = 0) -> str:
        """Discover CCS devices over VISA and open the one at *device_index*."""
        self._require_platform()
        try:
            self._load_dll()
        except SpectrometerError as exc:
            # A missing DLL is "nothing to connect to" as far as the UI is
            # concerned, so raise the flavour that offers simulation.
            raise NoDeviceError(str(exc))

        devices = self.list_available_devices()
        if not devices:
            raise NoDeviceError(
                "No Thorlabs CCS spectrometer found.\n\n"
                "Check that:\n"
                "  1. The spectrometer is plugged in over USB\n"
                "  2. ThorSpectra (and with it NI-VISA) is installed\n"
                "  3. The READY LED on the device is green\n"
                "  4. No other software already holds the device"
            )

        if device_index >= len(devices):
            raise SpectrometerError(
                f"Device index {device_index} out of range — "
                f"only {len(devices)} device(s) found."
            )

        return self._open_resource(devices[device_index][2])

    def connect_with_resource(self, resource: str) -> str:
        """Open a specific VISA resource, e.g. ``USB0::0x1313::0x8087::M00123456::RAW``."""
        self._require_platform()
        self._load_dll()
        return self._open_resource(resource)

    def _require_platform(self) -> None:
        if not supports_thorlabs_ccs():
            raise NoDeviceError(
                "Thorlabs CCS support is Windows only: TLCCS_64.dll and the NI-VISA "
                "runtime it needs have no Linux equivalent here. Use Ocean Optics or "
                "Simulation Mode instead."
            )

    def _open_resource(self, resource: str) -> str:
        from ctypes import byref, c_double, c_ulong

        handle = c_ulong(0)
        error = self._lib.tlccs_init(
            resource.encode() if isinstance(resource, str) else resource,
            1,  # id_query
            1,  # reset
            byref(handle),
        )
        if error != 0:
            raise SpectrometerError(
                "Failed to initialise the CCS spectrometer.\n"
                f"Resource: {resource}\n"
                f"TLCCS error code: {error}\n\n"
                "Check that the serial number and product ID in the resource string are correct."
            )
        self._handle = handle.value

        wavelength_buffer = (c_double * CCS_PIXEL_COUNT)()
        range_min = c_double(0)
        range_max = c_double(0)
        self._lib.tlccs_getWavelengthData(
            self._handle, 0, wavelength_buffer, byref(range_min), byref(range_max)
        )
        self._wavelengths = np.array(wavelength_buffer[:], dtype=np.float64)

        self._model_name = self._model_from_resource(resource)
        self._serial = _serial_from_resource(resource)

        self._lib.tlccs_setIntegrationTime(
            self._handle, c_double(self._integration_time_us * 1e-6)
        )

        capabilities = ModuleCapabilities(
            brand=self.brand,
            model=self._model_name,
            serial_number=self._serial,
            pixel_count=CCS_PIXEL_COUNT,
            wavelength_min=round(float(self._wavelengths[0]), 1),
            wavelength_max=round(float(self._wavelengths[-1]), 1),
            max_intensity=1.0,
            integration_time_min_us=_INTEGRATION_TIME_MIN_US,
            integration_time_max_us=_INTEGRATION_TIME_MAX_US,
            supports_dark_correction=False,
            supports_nonlinearity_correction=False,
            supports_trigger_delay=False,
            trigger_delay_max_us=0.0,
            **trigger_mode_fields({"normal": 0}),
        )
        self._hardware_capabilities = capabilities
        self._verify_first_read()

        status = "\n".join([
            f"Connected: {capabilities.model} (S/N: {capabilities.serial_number})",
            (f"Pixels: {capabilities.pixel_count} | "
             f"Range: {capabilities.wavelength_min}–{capabilities.wavelength_max} nm"),
            (f"Integration: {capabilities.integration_time_min_us / 1000.0}–"
             f"{capabilities.integration_time_max_us / 1000.0} ms"),
            "Intensity: normalised 0.0–1.0",
        ])
        logger.info(status)
        return status

    @staticmethod
    def _model_from_resource(resource: str) -> str:
        upper = resource.upper()
        for product_id, model in CCS_PRODUCT_IDS.items():
            if f"0x{product_id:04X}" in upper:
                return model
        return "CCS"

    def _verify_first_read(self) -> None:
        try:
            spectrum = self.get_intensities()
        except Exception as exc:
            self._close_handle()
            raise SpectrometerError(f"CCS opened but its verification read failed:\n{exc}")

        if len(spectrum) != CCS_PIXEL_COUNT:
            logger.warning(
                "CCS verification read returned %d pixels, expected %d.",
                len(spectrum), CCS_PIXEL_COUNT,
            )
        else:
            logger.info("CCS verification read OK: %d pixels.", CCS_PIXEL_COUNT)

    def disconnect(self) -> None:
        if self._handle is None:
            super().disconnect()
            return
        self._close_handle()
        logger.info("CCS spectrometer disconnected.")

    def _close_handle(self) -> None:
        handle, self._handle = self._handle, None
        if self._lib is not None and handle is not None:
            try:
                self._lib.tlccs_close(handle)
            except Exception as exc:
                logger.warning("Error closing the CCS handle: %s", exc)
        self._wavelengths = None
        self._hardware_capabilities = ModuleCapabilities(brand=self.brand)
        self._model_name = "CCS"
        self._serial = "N/A"

    # ── Identity and capabilities ────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        if self._handle is None:
            return super().is_connected
        return True

    @property
    def capabilities(self) -> ModuleCapabilities:
        if self._handle is None:
            return super().capabilities
        return self._hardware_capabilities

    @property
    def model(self) -> str:
        if self._handle is None:
            return super().model
        return self._model_name

    @property
    def serial_number(self) -> str:
        if self._handle is None:
            return super().serial_number
        return self._serial

    # ── Acquisition ──────────────────────────────────────────────────────

    @property
    def integration_time_us(self) -> int:
        if self._handle is None:
            return super().integration_time_us
        return self._integration_time_us

    def set_integration_time(self, microseconds: int) -> None:
        if self._handle is None:
            super().set_integration_time(microseconds)
            return

        from ctypes import c_double

        microseconds = max(
            _INTEGRATION_TIME_MIN_US, min(int(microseconds), _INTEGRATION_TIME_MAX_US)
        )
        # The DLL takes seconds; everything above this line is microseconds.
        self._lib.tlccs_setIntegrationTime(self._handle, c_double(microseconds * 1e-6))
        self._integration_time_us = microseconds
        logger.info("CCS integration time set to %d µs", microseconds)

    @property
    def current_trigger_mode(self) -> int:
        if self._handle is None:
            return super().current_trigger_mode
        return 0

    def set_trigger_mode(self, mode: int) -> None:
        if self._handle is None:
            super().set_trigger_mode(mode)
            return
        if mode != 0:
            raise SpectrometerError(
                f"Thorlabs CCS does not support trigger mode {mode}. The DLL exposes "
                "no external hardware trigger, so only normal mode (0) exists."
            )

    def set_trigger_delay(self, microseconds: float) -> None:
        if self._handle is None:
            super().set_trigger_delay(microseconds)
            return
        raise SpectrometerError(
            f"{self._model_name} has no programmable trigger delay; the DLL exposes "
            "no triggering at all."
        )

    def get_wavelengths(self) -> np.ndarray:
        if self._handle is None:
            return super().get_wavelengths()
        return self._wavelengths.copy()

    def get_intensities(
        self,
        correct_dark_counts: bool = False,
        correct_nonlinearity: bool = False,
    ) -> np.ndarray:
        """Acquire one spectrum as 3648 doubles in 0.0–1.0.

        The correction flags are accepted for interface parity and ignored: the
        CCS DLL applies no dark or nonlinearity correction of its own.
        """
        if self._handle is None:
            return super().get_intensities(
                correct_dark_counts=correct_dark_counts,
                correct_nonlinearity=correct_nonlinearity,
            )

        from ctypes import byref, c_double, c_int

        error = self._lib.tlccs_startScan(self._handle)
        if error != 0:
            raise SpectrometerError(f"CCS startScan failed (error {error})")

        # Poll the status word rather than blocking; the budget is the exposure
        # plus five seconds of slack, expressed in 1 ms polls.
        status = c_int(0)
        max_polls = int(self._integration_time_us / 1000) + 5000
        for _ in range(max_polls):
            self._lib.tlccs_getDeviceStatus(self._handle, byref(status))
            if status.value & _STATUS_SCAN_TRANSFER:
                break
            time.sleep(0.001)
        else:
            raise SpectrometerError(
                "CCS scan timed out with no data. Check the integration time and "
                "that the device is still connected."
            )

        data = (c_double * CCS_PIXEL_COUNT)()
        error = self._lib.tlccs_getScanData(self._handle, data)
        if error != 0:
            raise SpectrometerError(f"CCS getScanData failed (error {error})")

        return np.array(data[:], dtype=np.float64)

    # ── Discovery and diagnostics ────────────────────────────────────────

    def list_available_devices(self) -> list:
        """Return ``(model, serial, resource)`` for every CCS on the VISA bus."""
        if not supports_thorlabs_ccs():
            return []
        self._load_dll()

        try:
            visa = _load_visa()
            if visa is None:
                logger.warning("NI-VISA not found — cannot discover CCS devices.")
                return []
            return _find_visa_resources(visa)
        except Exception as exc:
            logger.debug("CCS device discovery failed: %s", exc)
            return []

    def _load_dll(self) -> None:
        """Load TLCCS_64.dll from its install location, or from PATH."""
        self._require_platform()
        if self._lib is not None:
            return

        import ctypes

        path = _find_tlccs_dll()
        if path is not None:
            try:
                self._lib = ctypes.cdll.LoadLibrary(path)
                logger.info("Loaded TLCCS DLL from %s", path)
                return
            except OSError as exc:
                logger.debug("Failed to load %s: %s", path, exc)

        try:
            self._lib = ctypes.cdll.LoadLibrary("TLCCS_64.dll")
            logger.info("Loaded TLCCS_64.dll from PATH")
        except OSError:
            raise SpectrometerError(TLCCS_MISSING_REASON)

    @classmethod
    def diagnose(cls) -> dict:
        """Build the Thorlabs tab of the diagnostics dialog.

        Reports the USB bus, the DLL and the VISA layer separately, because a
        device on the bus that VISA cannot enumerate points at a missing or
        broken ThorSpectra install rather than at the device.
        """
        report: dict = {
            "backend": "ThorlabsCCSModule",
            "platform": platform.system(),
            "supported": supports_thorlabs_ccs(),
            "dll_found": False,
            "dll_path": None,
            "visa_installed": False,
            "visa_resources": [],
            "usb_devices": [],
            "notes": [],
        }

        if not supports_thorlabs_ccs():
            report["notes"].append(
                "The Thorlabs CCS backend is Windows only: it needs TLCCS_64.dll and "
                "the NI-VISA runtime. Use Ocean Optics or Simulation Mode on Linux."
            )
            return report

        try:
            report["usb_devices"] = [
                device for device in scan_usb_spectrometers()
                if device["brand"] == "thorlabs"
            ]
        except Exception as exc:
            report["notes"].append(f"USB bus scan error: {exc}")

        dll_path = _find_tlccs_dll()
        if dll_path is None:
            import shutil

            dll_path = shutil.which("TLCCS_64.dll")
        if dll_path:
            report["dll_found"] = True
            report["dll_path"] = dll_path
        else:
            report["notes"].append(
                "TLCCS_64.dll not found.\nInstall ThorSpectra from thorlabs.com to get it."
            )

        cls._add_visa_report(report)

        if report["usb_devices"] and not report["visa_resources"]:
            report["notes"].append(
                f"Windows sees {len(report['usb_devices'])} Thorlabs USB device(s) on the "
                "bus, but VISA cannot find them.\nThat usually means the VISA/ThorSpectra "
                "driver is missing, or the device needs a power cycle."
            )

        return report

    @staticmethod
    def _add_visa_report(report: dict) -> None:
        try:
            visa = _load_visa()
            if visa is None:
                report["notes"].append(
                    "The NI-VISA runtime was not found (neither visa64.dll nor visa32.dll).\n"
                    "It is required to talk to a Thorlabs CCS and normally arrives with "
                    "ThorSpectra."
                )
                return

            report["visa_installed"] = True
            report["visa_resources"] = [
                {"model": model, "resource": resource}
                for model, _serial, resource in _find_visa_resources(visa)
            ]
            if not report["visa_resources"]:
                report["notes"].append(
                    "NI-VISA is installed but found no CCS resources.\n"
                    "Check that the device is plugged in and that ThorSpectra can see it."
                )
        except Exception as exc:
            report["notes"].append(f"VISA diagnostic error: {exc}")


__all__ = [
    "CCS_PIXEL_COUNT",
    "CCS_PRODUCT_IDS",
    "TLCCS_MISSING_REASON",
    "ThorlabsCCSModule",
]
