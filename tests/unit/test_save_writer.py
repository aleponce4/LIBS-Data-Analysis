"""Tests for background spectrum and manifest export writer."""

import time
from prolibspector.acquisition.mapping_save_writer import MappingSaveWriter


def test_mapping_save_writer_async_export():
    executed = []

    def dummy_job():
        executed.append(1)

    writer = MappingSaveWriter(name="TestSaveWriter")
    for _ in range(5):
        writer.submit(dummy_job)

    drained = writer.drain(timeout_s=5.0)
    assert drained is True
    assert len(executed) == 5

    err = writer.close(timeout_s=2.0)
    assert err is None
