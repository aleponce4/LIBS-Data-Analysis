"""Placeholder for the brokered YiXist client.

The real client runs the vendor DLL in a separate process and talks to it over a
pipe, because that DLL can wedge hard enough to take the GUI down with it -- an
out-of-process broker turns a hung driver into a killable subprocess instead of a
frozen application. That isolation machinery is generic and does ship here, in
brokered_spectrometer.py; only the YiXist-specific broker target is absent.

See BrokeredSpectrometerClient for the part worth reading.
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

    def __init__(self, device_index: int = 0) -> None:
        self.device_index = int(device_index)
        # Per-instance so it can never be shared across clients.
        self.device_info: dict = {}

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

