"""Shared helpers for the fleet endpoint tests.

The real-time WebSocket tests drive the app through a ``TestClient``, which runs
it on its portal's event loop in another thread. The in-process bus is bound to
that loop (each subscription wakes its relay through an ``asyncio.Event``), so a
publish from the test thread can land between the relay's buffer-empty check and
its ``_ready.clear()`` and lose the wake-up (#3506). The helpers below publish on
the app's loop, as the real publishers do.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi.testclient import TestClient

from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.fleet.domain.real_time_events import RealTimeEvent


def publish_burst_on_app_loop(
    client: TestClient,
    bus: InProcessRealTimeEvents,
    *,
    server_id: str,
    events: Iterable[RealTimeEvent],
) -> None:
    """Publish ``events`` in order within one callback on the app's loop.

    The relay cannot run between them, so a burst overflows a small buffer
    exactly as a burst from a real publisher does.
    """

    def _publish_all() -> None:
        for event in events:
            bus.publish(server_id=server_id, event=event)

    assert client.portal is not None
    client.portal.call(_publish_all)


def publish_on_app_loop(
    client: TestClient,
    bus: InProcessRealTimeEvents,
    *,
    server_id: str,
    event: RealTimeEvent,
) -> None:
    """Publish ``event`` on the app's loop."""

    publish_burst_on_app_loop(client, bus, server_id=server_id, events=[event])
