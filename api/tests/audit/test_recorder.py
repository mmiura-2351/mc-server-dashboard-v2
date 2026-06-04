"""The must-not-raise recorder (FR-AUD-2).

A failed audit write is logged and swallowed -- it never propagates into the
(already committed) operation. A successful write is delegated to the writer.
"""

from __future__ import annotations

import logging

import pytest

from mc_server_dashboard_api.audit.adapters.recorder import LoggingAuditRecorder
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from tests.audit.fakes import RecordingAuditWriter

_EVENT = AuditEvent(operation="server:create", outcome=Outcome.SUCCESS)


async def test_record_delegates_to_writer_on_success() -> None:
    writer = RecordingAuditWriter()
    recorder = LoggingAuditRecorder(writer)

    await recorder.record(_EVENT)

    assert writer.events == [_EVENT]


async def test_record_swallows_writer_failure() -> None:
    recorder = LoggingAuditRecorder(RecordingAuditWriter(fail=True))

    # Must not raise: the operation already committed (FR-AUD-2).
    await recorder.record(_EVENT)


async def test_record_logs_writer_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = LoggingAuditRecorder(RecordingAuditWriter(fail=True))

    with caplog.at_level(logging.ERROR):
        await recorder.record(_EVENT)

    assert any("audit write failed" in r.message for r in caplog.records)
