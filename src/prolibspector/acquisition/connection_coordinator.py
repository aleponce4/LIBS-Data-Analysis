"""Serialized, epoch-guarded execution of device connect/disconnect work.

Hardware connect and disconnect calls block on USB/serial I/O, so they must
never run on the Tk thread. The coordinator runs them one at a time on a
dedicated daemon thread and hands completions back to the Tk thread through a
queue drained by a Tk ``after`` poll — the worker thread never calls into Tk,
which keeps delivery safe both under ``mainloop`` and under ``update()``-pumped
tests, and the daemon flag means a wedged serial read can never block
interpreter exit.

Every operation carries an epoch token; starting any newer operation in the
same scope invalidates in-flight completions so a late connect can never
resurrect a device the user has since disconnected or closed.
"""

import logging
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

_POLL_MS = 15


class ConnectionPhase(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RUN_ACTIVE = "run_active"
    DISCONNECTING = "disconnecting"
    ERROR = "error"


@dataclass
class DeviceLink:
    """Desired vs. actual connection state for one hardware device."""

    desired: bool = False
    phase: ConnectionPhase = ConnectionPhase.DISCONNECTED
    detail: str = ""

    @property
    def transitioning(self) -> bool:
        return self.phase in (ConnectionPhase.CONNECTING, ConnectionPhase.DISCONNECTING)


class ConnectionCoordinator:
    """Runs device operations one at a time off the Tk thread.

    ``begin(scope)`` returns a token for a new operation and invalidates every
    older one in that scope (scopes keep independent devices from cancelling
    each other). ``submit()`` runs the blocking callable on the worker thread
    and delivers ``on_done``/``on_error`` on the Tk thread only while the token
    is still current — stale completions are logged, dropped, and, when an
    ``on_stale`` disposer is given, their payload is cleaned up on the worker
    thread (e.g. closing a serial port a stale connect left open).
    """

    def __init__(self, view):
        self._view = view
        self._jobs = queue.Queue()
        self._worker = None
        self._epochs = {}
        self._completions = queue.Queue()
        self._pending = 0
        self._poll_id = None
        self._shutdown = False

    def begin(self, scope: str = "default") -> tuple:
        """Start a new operation for *scope*, invalidating older ones in it."""
        epoch = self._epochs.get(scope, 0) + 1
        self._epochs[scope] = epoch
        return (scope, epoch)

    def is_current(self, token) -> bool:
        scope, epoch = token
        return self._epochs.get(scope, 0) == epoch

    def invalidate_all(self):
        """Invalidate every in-flight operation (used when shutdown begins)."""
        for scope in list(self._epochs):
            self._epochs[scope] += 1

    def submit(self, fn, *, token, on_done=None, on_error=None, on_stale=None, label="device operation"):
        """Run *fn* on the worker thread; deliver its result on the Tk thread.

        Must be called from the Tk thread. Completion callbacks are dropped
        when the token is stale or the coordinator has been shut down; a
        successful-but-stale result is passed to *on_stale* on the worker
        thread so acquired resources can be released."""
        if self._shutdown:
            logger.info("Coordinator shut down; dropping %s.", label)
            return

        def _run():
            try:
                result = fn()
            except Exception as exc:
                logger.warning("%s failed: %s", label, exc, exc_info=True)
                self._completions.put((token, on_error, exc, label, None))
                return
            self._completions.put((token, on_done, result, label, on_stale))

        self._pending += 1
        self._run_on_worker(_run)
        self._ensure_poll()

    def _run_on_worker(self, job):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._job_loop, daemon=True, name="DeviceOps")
            self._worker.start()
        self._jobs.put(job)

    def _job_loop(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                job()
            except Exception:
                logger.exception("Device operation job raised.")

    def _dispose_stale(self, on_stale, payload, label):
        def _dispose():
            try:
                on_stale(payload)
            except Exception:
                logger.debug("Stale-payload disposal for %s failed.", label, exc_info=True)

        if self._shutdown:
            # Worker may already be draining to exit; dispose inline as a
            # best effort (the process is on its way out anyway).
            _dispose()
        else:
            self._run_on_worker(_dispose)

    def _ensure_poll(self):
        if self._poll_id is not None:
            return
        try:
            self._poll_id = self._view.root.after(_POLL_MS, self._drain)
        except (tk.TclError, RuntimeError, AttributeError):
            self._poll_id = None

    def _drain(self):
        self._poll_id = None
        while True:
            try:
                token, callback, payload, label, on_stale = self._completions.get_nowait()
            except queue.Empty:
                break
            self._pending = max(0, self._pending - 1)
            if not self.is_current(token):
                logger.info("Dropping stale completion for %s (token %s).", label, token)
                if on_stale is not None:
                    self._dispose_stale(on_stale, payload, label)
                continue
            if callback is None:
                continue
            try:
                callback(payload)
            except Exception:
                logger.exception("Completion callback for %s raised.", label)
        if self._pending > 0 and not self._shutdown:
            self._ensure_poll()

    def shutdown(self):
        """Stop accepting work. Does not wait for the in-flight operation."""
        self._shutdown = True
        if self._poll_id is not None:
            try:
                self._view.root.after_cancel(self._poll_id)
            except (tk.TclError, RuntimeError):
                pass
            self._poll_id = None
        self._jobs.put(None)
