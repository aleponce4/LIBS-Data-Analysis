"""Tests for the subprocess-backed spectrometer client.

The client exists for one scenario: a vendor SDK read that never returns and
cannot be unwound in-process. These drive that scenario for real — a broker
subprocess is started, told to block forever inside a read, and then recovered
by each of the three documented routes.

The broker targets are module-level functions because ``spawn`` pickles the
target by name and re-imports this module in the child.
"""

import multiprocessing
import threading

import numpy as np
import pytest

from prolibspector.hardware.brokered_spectrometer import (
    BrokeredSpectrometerClient,
    capabilities_from_mapping,
    capabilities_to_mapping,
)
from prolibspector.hardware.spectrometer import (
    ModuleCapabilities,
    NoDeviceError,
    SpectrometerError,
)

_FAKE_CAPABILITIES = {
    "model": "Fake USB4000",
    "serial_number": "FAKE123",
    "brand": "ocean_optics",
    "pixel_count": 2,
    "wavelength_min": 200.0,
    "wavelength_max": 201.0,
    "max_intensity": 65535.0,
    "integration_time_min_us": 1,
    "integration_time_max_us": 1_000_000,
    "trigger_modes": {"normal": 0, "external": 3},
    "supports_dark_correction": True,
    "supports_nonlinearity_correction": True,
}


def blocking_broker_main(request_queue, response_queue, read_started_event):
    """A broker that answers normally until asked to read, then hangs forever.

    This is the failure being modelled: an armed read waiting on a trigger edge
    that never arrives, with the SDK offering no way to cancel it.
    """
    import time

    integration_time_us = 2_000
    current_trigger_mode = 0

    while True:
        request = request_queue.get()
        request_id = request["id"]
        command = request["command"]
        payload = request.get("payload") or {}

        if command == "connect":
            response_queue.put({
                "id": request_id,
                "status": "capabilities",
                "payload": {
                    "status": "Connected fake broker",
                    "capabilities": dict(_FAKE_CAPABILITIES),
                    "integration_time_us": integration_time_us,
                    "current_trigger_mode": current_trigger_mode,
                    "wavelengths": np.array([200.0, 201.0]),
                },
            })
        elif command in ("disconnect", "shutdown"):
            response_queue.put({"id": request_id, "status": "ok", "payload": {}})
            if command == "shutdown":
                return
        elif command == "set_integration_time":
            integration_time_us = int(payload["microseconds"])
            response_queue.put({
                "id": request_id,
                "status": "ok",
                "payload": {"integration_time_us": integration_time_us},
            })
        elif command == "set_trigger_mode":
            current_trigger_mode = int(payload["mode"])
            response_queue.put({
                "id": request_id,
                "status": "ok",
                "payload": {"current_trigger_mode": current_trigger_mode},
            })
        elif command == "get_wavelengths":
            response_queue.put({
                "id": request_id,
                "status": "spectrum",
                "payload": {"wavelengths": np.array([200.0, 201.0])},
            })
        elif command == "get_intensities":
            read_started_event.set()
            time.sleep(3600)
        else:
            response_queue.put({
                "id": request_id,
                "status": "error",
                "error_type": "SpectrometerError",
                "message": f"Unexpected command: {command}",
            })


def no_device_broker_main(request_queue, response_queue):
    """A broker whose device is absent; answers the first request and exits."""
    request = request_queue.get()
    response_queue.put({
        "id": request["id"],
        "status": "error",
        "error_type": "NoDeviceError",
        "message": "No fake broker device found.",
    })


class _AliveProcess:
    """Stand-in for a running broker, for the paths that never touch a queue."""

    pid = -1

    def is_alive(self):
        return True


@pytest.fixture
def blocking_client():
    """A client wired to a broker that will hang on its first read."""
    read_started = multiprocessing.get_context("spawn").Event()
    client = BrokeredSpectrometerClient(
        broker_target=blocking_broker_main,
        broker_args=(read_started,),
        command_timeout_s=2.0,
        connect_timeout_s=20.0,
        cancel_reconnect_delay_s=0.01,
    )
    try:
        yield client, read_started
    finally:
        client.disconnect()


def _start_blocked_read(client, read_started):
    """Arm the device and leave a read hanging on the broker. Returns the thread."""
    assert "Connected fake broker" in client.connect(device_index=0)
    client.set_integration_time(250_000)
    client.set_trigger_mode(client.capabilities.external_trigger_mode)

    errors: list[str] = []

    def _read():
        try:
            client.get_intensities()
        except Exception as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    assert read_started.wait(timeout=20), "broker never reached the read"
    return thread, errors


# ── Error typing across the process boundary ─────────────────────────────

def test_connect_preserves_the_no_device_error_type():
    """The UI offers simulation on NoDeviceError, so the type must survive.

    Exceptions do not cross a process boundary as objects; the broker sends a
    type *name* and the client re-raises the matching class.
    """
    client = BrokeredSpectrometerClient(
        broker_target=no_device_broker_main,
        command_timeout_s=2.0,
        connect_timeout_s=20.0,
    )
    with pytest.raises(NoDeviceError):
        client.connect(device_index=0)


def test_invalid_trigger_mode_is_rejected_before_it_reaches_the_broker():
    client = BrokeredSpectrometerClient(
        broker_target=no_device_broker_main,
        command_timeout_s=2.0,
        connect_timeout_s=20.0,
    )
    client._is_connected = True
    client._process = _AliveProcess()
    client._capabilities.trigger_modes = {"normal": 0}

    with pytest.raises(SpectrometerError):
        client.set_trigger_mode(99)


def test_connect_simulated_is_refused():
    """The broker isolates a real SDK; there is nothing to isolate in simulation."""
    client = BrokeredSpectrometerClient(broker_target=no_device_broker_main)
    with pytest.raises(SpectrometerError):
        client.connect_simulated()


# ── Capability round-tripping ────────────────────────────────────────────

def test_capabilities_survive_the_queue_round_trip():
    original = ModuleCapabilities(
        brand="ocean_optics",
        model="USB4000",
        serial_number="USB4F1234",
        pixel_count=3648,
        trigger_modes={"normal": 0, "external": 3},
        normal_trigger_mode=0,
        external_trigger_mode=3,
    )
    restored = capabilities_from_mapping(capabilities_to_mapping(original))

    assert restored.model == original.model
    assert restored.pixel_count == original.pixel_count
    assert restored.trigger_modes == original.trigger_modes
    assert restored.external_trigger_mode == 3
    assert restored.has_external_trigger


def test_rehydrated_mode_numbers_follow_the_trigger_map():
    """The map is the authority; the mode numbers are rebuilt from it."""
    restored = capabilities_from_mapping({"trigger_modes": {"normal": 0}})

    assert restored.normal_trigger_mode == 0
    assert restored.external_trigger_mode is None
    assert not restored.has_external_trigger


# ── Recovery from a wedged read ──────────────────────────────────────────

@pytest.mark.slow
def test_cooperative_cancel_leaves_the_blocked_broker_alive(blocking_client):
    """The default must not kill a process sitting inside vendor SDK code.

    Terminating mid-read can leave the USB device in a worse state than the
    wedge did, so the client disowns the broker and waits to be told.
    """
    client, read_started = blocking_client
    read_thread, _errors = _start_blocked_read(client, read_started)
    original_pid = client.broker_pid

    assert client.cancel_pending_read(timeout_s=0.1) is False
    assert client.broker_pid == original_pid
    assert not client.is_connected
    assert read_thread.is_alive()

    # Only the explicit reap ends it.
    assert client.release_orphaned_process() is True
    read_thread.join(timeout=20)
    assert not read_thread.is_alive()


@pytest.mark.slow
def test_forced_cancel_restarts_the_broker_and_restores_configuration(blocking_client):
    """Last-resort recovery must hand back a device configured as it was."""
    client, read_started = blocking_client
    read_thread, errors = _start_blocked_read(client, read_started)
    original_pid = client.broker_pid

    assert client.cancel_pending_read(timeout_s=0.1, force_terminate=True) is True
    read_thread.join(timeout=20)

    assert not read_thread.is_alive()
    assert errors, "the killed read should have raised in its own thread"
    assert client.broker_pid != original_pid
    assert client.is_connected
    assert client.integration_time_us == 250_000
    assert client.current_trigger_mode == client.capabilities.normal_trigger_mode
    np.testing.assert_allclose(client.get_wavelengths(), np.array([200.0, 201.0]))


def test_release_orphaned_process_refuses_while_connected():
    """Safe to call unconditionally on the recovery path: a live device is never reaped."""
    client = BrokeredSpectrometerClient(
        broker_target=no_device_broker_main,
        command_timeout_s=2.0,
        connect_timeout_s=20.0,
    )
    client._is_connected = True
    client._process = _AliveProcess()

    assert client.release_orphaned_process() is False
