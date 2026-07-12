"""The ``ServerNotifier`` Port: emit operator-facing server notifications.

The scheduler runner (issue #1838) is the first producer of API-originated
notifications (``EventStream.NOTIFICATION``, issue #1836): a scheduled action
that fails is surfaced as a live, best-effort notice. The canonical payload and
the WebSocket transport live in the fleet context (``RealTimeEvents``,
``notification_event``), which the servers application layer may not import
(ARCHITECTURE.md Section 2.2, the import-linter contract); so this Port is the
servers-side seam and a servers adapter binds it to the fleet bus at the edge,
mirroring the :class:`ControlPlane` / ``FleetControlPlaneAdapter`` precedent.

Publishing is synchronous, non-blocking, and best-effort (FR-MON-4): a slow
subscriber never back-pressures the caller and a notice with no subscriber is
dropped. The durable record of what happened is the schedule-run history and the
audit log, not this live stream.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class ServerNotifier(abc.ABC):
    """Port: publish an operator-facing notification about a server."""

    @abc.abstractmethod
    def notify(
        self, *, server_id: ServerId, kind: str, title: str, detail: str = ""
    ) -> None:
        """Fan a notification about ``server_id`` out to subscribed clients.

        ``kind`` is a stable machine-readable discriminator a client routes on
        (e.g. ``schedule_failed``); ``title`` / ``detail`` are the human-readable
        one-liner and longer text a UI renders. Non-blocking and best-effort:
        never raises for a delivery failure and never awaits a subscriber.
        """
