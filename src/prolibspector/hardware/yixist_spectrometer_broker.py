"""Out-of-process YiXist SDK broker client - not in the public edition.

PUBLIC-EDITION STUB
===================
The private ProLIBSpector edition isolates the YiXist/YXSP vendor SDK in a
separate broker process and talks to it over a local pipe. That design exists
because the vendor DLL can wedge or crash on some firmware revisions; keeping it
out-of-process lets the GUI survive, reap an orphaned broker, and cancel a
pending read without losing the Tk main loop. The broker, its wire protocol and
the crash-recovery logic all live in the private edition.

This module exists so ``acquisition/controllers.py`` imports cleanly. The client
below never spawns a broker and never claims a connection: ``connect()`` raises
``NoDeviceError`` with the reason, which the acquisition view surfaces through
its normal connection-failure path (including the diagnostics dialog and its
Simulation fallback).
"""

from __future__ import annotations

import logging

from prolibspector.hardware.spectrometer import NoDeviceError
from prolibspector.hardware.yixist_spectrometer import YIXIST_UNAVAILABLE_REASON

logger = logging.getLogger(__name__)

BROKER_UNAVAILABLE_REASON = (
    "The YiXist SDK broker process is not included in the public edition. " + YIXIST_UNAVAILABLE_REASON
)


class YixistSpectrometerBrokerClient:
    """Unavailable-device shim standing in for the out-of-process SDK client."""

    brand = "yixist"
    model = "YiXist / YXSP broker (unavailable)"
    serial_number = "N/A"
    dll_path = None
    dll_version = None
    device_info: dict = {}

    def __init__(self, device_index: int = 0) -> None:
        self.device_index = int(device_index)

    @property
    def is_connected(self) -> bool:
        return False

    def connect(self, device_index: int | None = None) -> str:
        """Always raise: there is no broker process to connect to."""
        raise NoDeviceError(BROKER_UNAVAILABLE_REASON)

    def connect_simulated(self, profile_name: str = "Generic") -> str:
        raise NoDeviceError(
            f"{BROKER_UNAVAILABLE_REASON} Choose Simulation Mode for a simulated spectrometer."
        )

    def disconnect(self) -> None:
        """No-op: no broker was started."""
        return None

    def cancel_pending_read(self) -> None:
        """No-op: no read can be pending without a broker."""
        return None

    def release_orphaned_process(self) -> None:
        """No-op: no broker process is ever spawned here."""
        return None


__all__ = ["BROKER_UNAVAILABLE_REASON", "YixistSpectrometerBrokerClient"]
