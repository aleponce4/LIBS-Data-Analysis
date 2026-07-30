"""Background save writer for 2D mapping shots.

Moves the per-shot persistence work (binary store row, index/timing CSV rows,
completion records, batched state writes) off the acquisition thread. Modeled
on ``DeviceOperationCoordinator``'s queue+sentinel worker with three additions
that persistence needs: a bounded queue for backpressure, a latched first
failure, and a drain/join used before run finalization so state and summary
files never run ahead of the saved bytes.

The writer thread is the only mutator of the mapping spectrum store while a
run is active; resume correctness relies on jobs executing in submit order
(write bytes -> record completion -> batched state write).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

MAPPING_SAVE_QUEUE_MAXSIZE = 32
MAPPING_SAVE_CLOSE_TIMEOUT_S = 30.0


class MappingSaveWriterError(RuntimeError):
    """Raised when submitting to a writer that has already failed or closed."""


class MappingSaveWriter:
    """Single-thread FIFO executor for mapping shot save jobs."""

    def __init__(
        self,
        *,
        name: str = "MappingSaveWriter",
        queue_maxsize: int = MAPPING_SAVE_QUEUE_MAXSIZE,
    ) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(queue_maxsize)))
        self._failure: Exception | None = None
        self._pending = 0
        self._condition = threading.Condition()
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    @property
    def failure(self) -> Exception | None:
        return self._failure

    @property
    def pending(self) -> int:
        return self._pending

    def submit(self, job: Callable[[], None]) -> None:
        """Queue a job; blocks when the queue is full (backpressure).

        Raises MappingSaveWriterError if the writer already failed or closed,
        so the acquisition loop stops firing shots whose data could not be
        persisted.
        """
        if self._failure is not None:
            raise MappingSaveWriterError(f"Mapping save writer already failed: {self._failure}") from self._failure
        if self._closed:
            raise MappingSaveWriterError("Mapping save writer is closed.")
        with self._condition:
            self._pending += 1
        try:
            self._queue.put(job)
        except Exception:
            with self._condition:
                self._pending -= 1
                self._condition.notify_all()
            raise

    def drain(self, timeout_s: float) -> bool:
        """Wait until every submitted job has finished; False on timeout."""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._condition:
            while self._pending > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
        return True

    def close(self, *, timeout_s: float = MAPPING_SAVE_CLOSE_TIMEOUT_S) -> Exception | None:
        """Drain, stop the thread, and return the latched failure (if any).

        A drain/join timeout is reported as a TimeoutError so callers can
        refuse to persist a clean completion over unsaved shots.
        """
        already_closed = self._closed
        self._closed = True
        if not already_closed:
            try:
                self._queue.put(None, timeout=max(1.0, float(timeout_s)))
            except queue.Full:
                return self._failure or TimeoutError("Mapping save writer queue did not accept shutdown.")
        self._thread.join(timeout=max(0.0, float(timeout_s)))
        if self._thread.is_alive():
            return self._failure or TimeoutError("Mapping save writer did not drain before the timeout.")
        return self._failure

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                # After the first failure the remaining queue is discarded:
                # their targets stay un-recorded, so a resumed run re-shoots
                # them instead of trusting half-written state.
                if self._failure is None:
                    job()
            except Exception as exc:
                self._failure = exc
                logger.exception("Mapping save job failed; failing the run.")
            finally:
                with self._condition:
                    self._pending -= 1
                    self._condition.notify_all()
