"""Bind the servers ``ServerNotifier`` Port to the fleet real-time bus (#1838).

The servers application layer publishes operator notifications through the
``ServerNotifier`` Port; this adapter fulfils it by translating each notice into
the canonical ``NOTIFICATION`` payload (``notification_event``, issue #1836) and
publishing it on the fleet ``RealTimeEvents`` bus — the same in-process bus the
gRPC session relays Worker events onto. The cross-context import lives here at
the edge (adapters may reach into another context; the domain/application may
not), mirroring ``FleetControlPlaneAdapter``.
"""

from __future__ import annotations

from mc_server_dashboard_api.fleet.domain.real_time_events import (
    RealTimeEvents,
    notification_event,
)
from mc_server_dashboard_api.servers.domain.notifier import ServerNotifier
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class RealTimeEventsNotifier(ServerNotifier):
    """:class:`ServerNotifier` adapter over the fleet ``RealTimeEvents`` bus."""

    def __init__(self, real_time_events: RealTimeEvents) -> None:
        self._events = real_time_events

    def notify(
        self, *, server_id: ServerId, kind: str, title: str, detail: str = ""
    ) -> None:
        # ``publish`` is non-blocking and drops the event when no client is
        # subscribed to the server, so this never back-pressures the caller.
        self._events.publish(
            server_id=str(server_id.value),
            event=notification_event(kind=kind, title=title, detail=detail),
        )
