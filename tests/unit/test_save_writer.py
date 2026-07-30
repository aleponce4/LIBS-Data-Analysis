"""Comprehensive unit tests for background save writer reliability and contract enforcement."""

import time
import pytest
from prolibspector.acquisition.mapping_save_writer import (
    MappingSaveWriter,
    MappingSaveWriterError,
)


def test_mapping_save_writer_async_export():
    """Verify normal queue submission, execution in submit order, drain, and close."""
    executed = []

    def dummy_job():
        executed.append(len(executed) + 1)

    writer = MappingSaveWriter(name="TestSaveWriter")
    for _ in range(5):
        writer.submit(dummy_job)

    drained = writer.drain(timeout_s=5.0)
    assert drained is True
    assert executed == [1, 2, 3, 4, 5]

    err = writer.close(timeout_s=2.0)
    assert err is None


def test_save_job_failure_propagated_and_latched():
    """Verify that a job failure is latched and reported by the writer."""
    def failing_job():
        raise ValueError("Disk I/O simulation error")

    writer = MappingSaveWriter(name="FailingWriter")
    writer.submit(failing_job)

    # Allow worker thread to execute job and latch failure
    drained = writer.drain(timeout_s=2.0)
    assert drained is True
    assert isinstance(writer.failure, ValueError)
    assert "Disk I/O simulation error" in str(writer.failure)

    # Submitting new jobs must raise MappingSaveWriterError
    with pytest.raises(MappingSaveWriterError) as exc_info:
        writer.submit(lambda: None)
    assert "already failed" in str(exc_info.value)

    # Closing must return the latched exception
    close_err = writer.close(timeout_s=2.0)
    assert close_err is writer.failure


def test_later_jobs_skipped_after_failure():
    """Verify that after a failure, queued jobs are discarded without execution."""
    executed = []

    def job_ok_1():
        executed.append("ok1")

    def job_fail():
        raise RuntimeError("Write failed")

    def job_ok_2():
        executed.append("ok2")

    writer = MappingSaveWriter(name="SkipLaterJobsWriter", queue_maxsize=10)
    writer.submit(job_ok_1)
    writer.submit(job_fail)
    
    # Try to enqueue job_ok_2 (if worker hasn't processed job_fail yet, it puts in queue)
    try:
        writer.submit(job_ok_2)
    except MappingSaveWriterError:
        pass

    writer.drain(timeout_s=2.0)
    
    # job_ok_1 ran, job_fail failed, job_ok_2 was skipped/never executed
    assert "ok1" in executed
    assert "ok2" not in executed
    assert isinstance(writer.failure, RuntimeError)

    writer.close(timeout_s=2.0)


def test_drain_timeout_returns_false():
    """Verify that drain() returns False when timeout expires before queue is cleared."""
    release_event = pytest.importorskip("threading").Event()

    def slow_job():
        release_event.wait(timeout=2.0)

    writer = MappingSaveWriter(name="SlowWriter")
    writer.submit(slow_job)

    # drain with tiny timeout should return False
    drained = writer.drain(timeout_s=0.05)
    assert drained is False

    # Release slow job and clean up
    release_event.set()
    writer.drain(timeout_s=2.0)
    writer.close(timeout_s=2.0)


def test_close_reports_undrained_writer():
    """Verify close() returns TimeoutError when writer cannot drain before timeout."""
    release_event = pytest.importorskip("threading").Event()

    def blocking_job():
        release_event.wait(timeout=5.0)

    writer = MappingSaveWriter(name="BlockingWriter")
    writer.submit(blocking_job)

    # Close with tiny timeout should report TimeoutError
    err = writer.close(timeout_s=0.05)
    assert isinstance(err, TimeoutError)

    # Unblock job and clean up
    release_event.set()
    time.sleep(0.1)
