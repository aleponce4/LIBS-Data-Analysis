"""Tests for epoch-guarded serialization of device connect/disconnect work.

The coordinator exists because hardware connect and disconnect calls block on
USB and serial I/O and so cannot run on the Tk thread — but moving them off it
creates a race the UI has no other defence against: an operation the user has
already superseded can still be in flight, and its completion callback would
happily install a device nobody asked for any more.

Every operation therefore carries an epoch token, and starting a newer
operation in the same scope invalidates the older ones. These tests drive that
directly, with a fake view standing in for Tk so nothing here needs a display.
"""

import threading
import time

import pytest

from prolibspector.acquisition.connection_coordinator import (
    ConnectionCoordinator,
    ConnectionPhase,
    DeviceLink,
)


class _FakeRoot:
    """Records ``after`` callbacks instead of scheduling them on a Tk loop."""

    def __init__(self):
        self._callbacks = {}
        self._next_id = 0
        self._lock = threading.Lock()

    def after(self, _delay_ms, callback):
        with self._lock:
            self._next_id += 1
            self._callbacks[self._next_id] = callback
            return self._next_id

    def after_cancel(self, after_id):
        with self._lock:
            self._callbacks.pop(after_id, None)

    def run_pending(self):
        """Run everything currently scheduled, standing in for the Tk loop."""
        with self._lock:
            due, self._callbacks = list(self._callbacks.values()), {}
        for callback in due:
            callback()


class _FakeView:
    def __init__(self):
        self.root = _FakeRoot()


@pytest.fixture
def coordinator():
    view = _FakeView()
    coordinator = ConnectionCoordinator(view)
    try:
        yield coordinator, view
    finally:
        coordinator.shutdown()


def _pump_until(view, predicate, timeout_s=5.0):
    """Drive the fake Tk loop until *predicate* holds, or give up."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        view.root.run_pending()
        if predicate():
            return True
        time.sleep(0.005)
    return False


# ── The happy path ───────────────────────────────────────────────────────

def test_a_current_completion_is_delivered_on_the_tk_thread(coordinator):
    coordinator, view = coordinator
    delivered = []

    token = coordinator.begin("spectrometer")
    coordinator.submit(
        lambda: "connected",
        token=token,
        on_done=delivered.append,
        label="connect",
    )

    assert _pump_until(view, lambda: delivered), "completion was never delivered"
    assert delivered == ["connected"]


def test_a_failure_reaches_on_error_not_on_done(coordinator):
    coordinator, view = coordinator
    done, errors = [], []

    def _fail():
        raise OSError("could not open COM3")

    token = coordinator.begin("laser")
    coordinator.submit(_fail, token=token, on_done=done.append, on_error=errors.append, label="connect")

    assert _pump_until(view, lambda: errors), "error was never delivered"
    assert not done
    assert isinstance(errors[0], OSError)


def test_work_runs_off_the_calling_thread(coordinator):
    """The whole point: a blocking device call must not sit on the Tk thread."""
    coordinator, view = coordinator
    thread_names = []

    token = coordinator.begin("spectrometer")
    coordinator.submit(
        lambda: threading.current_thread().name,
        token=token,
        on_done=thread_names.append,
        label="connect",
    )

    assert _pump_until(view, lambda: thread_names)
    assert thread_names[0] != threading.current_thread().name


# ── Stale completions ────────────────────────────────────────────────────

def test_a_late_connect_cannot_resurrect_a_disconnected_device(coordinator):
    """The race this class exists for.

    The user starts a connect, gets bored, and disconnects. The connect then
    succeeds. Its completion must be dropped — otherwise the app reports a
    device the user has already dismissed — and the port it opened must be
    handed to the disposer rather than leaked.
    """
    coordinator, view = coordinator
    connected, disposed = [], []
    release_connect = threading.Event()

    def _slow_connect():
        release_connect.wait(timeout=5)
        return "serial-port-handle"

    stale_token = coordinator.begin("spectrometer")
    coordinator.submit(
        _slow_connect,
        token=stale_token,
        on_done=connected.append,
        on_stale=disposed.append,
        label="spectrometer connect",
    )

    # The user disconnects while the connect is still blocked.
    coordinator.begin("spectrometer")
    assert not coordinator.is_current(stale_token)

    release_connect.set()

    assert _pump_until(view, lambda: disposed), "stale payload was never disposed"
    assert connected == [], "a superseded connect was delivered anyway"
    assert disposed == ["serial-port-handle"]


def test_invalidate_all_drops_every_scope(coordinator):
    coordinator, _view = coordinator
    spectrometer = coordinator.begin("spectrometer")
    laser = coordinator.begin("laser")

    coordinator.invalidate_all()

    assert not coordinator.is_current(spectrometer)
    assert not coordinator.is_current(laser)


def test_scopes_do_not_cancel_each_other(coordinator):
    """Connecting the laser must not invalidate a spectrometer connect in flight."""
    coordinator, _view = coordinator
    spectrometer = coordinator.begin("spectrometer")

    coordinator.begin("laser")

    assert coordinator.is_current(spectrometer)


def test_shutdown_stops_accepting_work(coordinator):
    coordinator, view = coordinator
    delivered = []

    coordinator.shutdown()
    token = coordinator.begin("spectrometer")
    coordinator.submit(lambda: "connected", token=token, on_done=delivered.append, label="connect")

    view.root.run_pending()
    time.sleep(0.05)
    view.root.run_pending()
    assert delivered == []


# ── The link record the UI reads ─────────────────────────────────────────

def test_device_link_reports_transitional_phases():
    link = DeviceLink()
    assert not link.transitioning

    for phase in (ConnectionPhase.CONNECTING, ConnectionPhase.DISCONNECTING):
        link.phase = phase
        assert link.transitioning, phase

    for phase in (
        ConnectionPhase.DISCONNECTED,
        ConnectionPhase.CONNECTED,
        ConnectionPhase.RUN_ACTIVE,
        ConnectionPhase.ERROR,
    ):
        link.phase = phase
        assert not link.transitioning, phase
