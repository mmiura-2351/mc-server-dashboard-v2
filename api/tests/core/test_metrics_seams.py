"""The reconciler loop and audit recorder feed their metrics seams (issue #282)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from mc_server_dashboard_api.audit.adapters.recorder import LoggingAuditRecorder
from mc_server_dashboard_api.audit.domain.events import AuditEvent
from mc_server_dashboard_api.audit.domain.writer import AuditWriter
from mc_server_dashboard_api.core.adapters import metrics
from mc_server_dashboard_api.servers.adapters.reconciler_loop import run_reconciler_loop


@pytest.mark.asyncio
async def test_audit_write_failure_increments_counter() -> None:
    class _BoomWriter(AuditWriter):
        async def write(self, event: AuditEvent) -> None:
            raise RuntimeError("db down")

    before = metrics.audit_write_failures_total._value.get()
    recorder = LoggingAuditRecorder(_BoomWriter())
    # Must not raise (FR-AUD-2) and must count the swallowed failure.
    await recorder.record(_event())
    after = metrics.audit_write_failures_total._value.get()
    assert after == before + 1


@pytest.mark.asyncio
async def test_reconciler_loop_counts_ticks_and_stamps_success() -> None:
    ticks_before = metrics.reconciler_ticks_total._value.get()
    ts_before = metrics.reconciler_last_success_timestamp_seconds._value.get()

    reset = AsyncMock()
    reconciler = AsyncMock()
    # Stop the loop after the first tick so the test does not spin forever.
    reconciler.tick.side_effect = [None, asyncio.CancelledError()]

    with pytest.raises(asyncio.CancelledError):
        await run_reconciler_loop(
            reconciler,
            reset=reset,
            warn_missing_ports=AsyncMock(),
            tick_seconds=0,
        )

    assert metrics.reconciler_ticks_total._value.get() >= ticks_before + 1
    assert metrics.reconciler_last_success_timestamp_seconds._value.get() > ts_before


def _event() -> AuditEvent:
    import uuid

    from mc_server_dashboard_api.audit.domain.events import Outcome

    return AuditEvent(
        operation="test.op",
        outcome=Outcome.SUCCESS,
        actor_id=uuid.uuid4(),
    )
