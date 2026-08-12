"""YiXist / YXSP spectrometer backend - not included in the public edition.

PUBLIC-EDITION STUB
===================
The private ProLIBSpector edition drives YiXist/YXSP spectrometers through the
vendor's ``spectrometer.dll`` via ctypes: SDK COM-port enumeration, per-device
metadata probing, hardware external-trigger arming, programmable trigger delay
(SDK >= 1.2.7), and DLL identity hashing for the reproducibility manifest. That
backend depends on a redistributable vendor SDK and on hardware-specific
timing work, and is not part of the public edition.

This module exists so the public edition imports and runs. It reports the
device family as unavailable and says why. It never claims to connect:

* ``yixist_supported()`` returns ``False``, which disables the YiXist button in
  the connection dialog.
* ``YixistSpectrometerModule.diagnose()`` returns a well-formed report whose
  notes state plainly that the driver is not bundled, so the diagnostics dialog
  renders normally instead of showing an empty tab.
* ``YixistSpectrometerModule.connect()`` raises ``NoDeviceError``.

Use Simulation Mode, or the private edition, for YiXist hardware.
"""

from __future__ import annotations

import logging
import platform
import sys

from prolibspector.hardware.spectrometer import NoDeviceError

logger = logging.getLogger(__name__)

#: Single source of truth for the user-facing reason, so UI copy stays honest.
YIXIST_UNAVAILABLE_REASON = (
    "The YiXist / YXSP driver is not included in the public edition. It requires "
    "the vendor spectrometer SDK and ships with the private ProLIBSpector edition. "
    "Use Simulation Mode to explore acquisition without hardware."
)


def yixist_supported() -> bool:
    """Return whether a usable YiXist backend is present.

    Always ``False`` here: the public edition bundles no YiXist driver. The
    platform is reported in :func:`YixistSpectrometerModule.diagnose` for
    context, but it is not the reason support is missing.
    """
    return False


def yixist_dll_identity(dll_path: str | None = None) -> dict[str, str | None]:
    """Return SDK DLL identity fields for the reproducibility manifest.

    There is no bundled SDK in the public edition, so every field is ``None``
    and the reason is recorded rather than guessed.
    """
    return {
        "path": str(dll_path) if dll_path else None,
        "sha256": None,
        "product_version": None,
        "file_version": None,
        "unavailable_reason": YIXIST_UNAVAILABLE_REASON,
    }


class YixistSpectrometerModule:
    """Unavailable-device shim for the YiXist / YXSP backend."""

    brand = "yixist"
    model = "YiXist / YXSP (unavailable)"
    serial_number = "N/A"
    dll_path = None
    dll_version = None

    def __init__(self, device_index: int = 0) -> None:
        self.device_index = int(device_index)

    @property
    def is_connected(self) -> bool:
        return False

    def connect(self, device_index: int | None = None) -> str:
        """Always raise: there is no YiXist driver to connect through."""
        raise NoDeviceError(YIXIST_UNAVAILABLE_REASON)

    def connect_simulated(self, profile_name: str = "Generic") -> str:
        """Refuse to masquerade as a simulated YiXist device.

        The generic simulated backend (``SpectrometerModule``) is the supported
        hardware-free path; pretending a YiXist device is present would put a
        wrong instrument identity into saved manifests.
        """
        raise NoDeviceError(
            f"{YIXIST_UNAVAILABLE_REASON} Choose Simulation Mode for a simulated spectrometer."
        )

    def disconnect(self) -> None:
        """No-op: nothing was ever opened."""
        return None

    @classmethod
    def diagnose(cls) -> dict:
        """Return the diagnostics-tab report for the YiXist backend."""
        return {
            "backend": "YixistSpectrometerModule",
            "supported": False,
            "edition": "public",
            "platform": platform.system(),
            "architecture": platform.machine(),
            "python": sys.version.split()[0],
            "dll_found": False,
            "dll_path": None,
            "dll_version": None,
            "dll_identity": {},
            "supports_trigger_delay": False,
            "ports": [],
            "devices": [],
            "per_device_errors": {},
            "notes": [
                YIXIST_UNAVAILABLE_REASON,
                "No COM ports were scanned, because there is no SDK to scan them with.",
            ],
        }


__all__ = [
    "YIXIST_UNAVAILABLE_REASON",
    "YixistSpectrometerModule",
    "yixist_dll_identity",
    "yixist_supported",
]
