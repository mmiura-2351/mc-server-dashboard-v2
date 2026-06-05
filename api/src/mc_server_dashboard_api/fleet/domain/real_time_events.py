"""The ``RealTimeEvents`` Port: relay Worker events to subscribed clients.

ARCHITECTURE.md Section 5.1 places ``RealTimeEvents`` on the API side: the
control-plane gRPC servicer publishes the status / log / metrics events a Worker
reports (FR-MON-1..3), and a WebSocket subscription drains them to a client. The
interface lives in the fleet domain (the worker-facing context that owns the
session stream), mirroring :class:`ServerStateSink`; the in-process pub/sub
adapter fulfils it at the edge.

Delivery is best-effort (FR-MON-4): a subscriber that cannot keep up loses its
oldest buffered events and is told so by a :data:`EventStream.GAP` marker, but
the publisher (the gRPC session) never blocks on a slow subscriber. The event
types here are the domain's own framework-free shapes, not the generated wire
types — the fleet domain never imports the ``mcsd`` stubs.
"""

from __future__ import annotations

import abc
import datetime as dt
import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


class EventStream(enum.Enum):
    """The kind of a real-time event (FR-MON-1..3), plus the gap marker.

    ``STATUS`` / ``LOG`` / ``METRICS`` are the three Worker-reported streams a
    client may subscribe to. ``GAP`` is an adapter-synthesised marker telling a
    subscriber that buffered events were dropped because it fell behind
    (best-effort delivery, FR-MON-4); it is never a subscribable stream.
    """

    STATUS = "status"
    LOG = "log"
    METRICS = "metrics"
    GAP = "gap"


@dataclass(frozen=True)
class RealTimeEvent:
    """One real-time event for a server: its ``stream`` and JSON-able ``payload``.

    The payload mirrors the corresponding wire event's fields (e.g. a status
    change carries ``state``/``detail``; a log line carries ``line``/``stream``);
    the adapter maps the proto to this shape at the transport edge. A ``GAP``
    event carries an empty payload.

    ``emitted_at`` is the Worker's authoritative clock reading for the event
    (``WorkerMessage.emitted_at``), so a queued subscriber sees the true event
    time rather than the relay's send time. It is ``None`` when the Worker left
    the proto field unset/zero; the transport then falls back to receive time.

    ``server_id`` is the id of the server the event is about. It is ``None`` on a
    per-server subscription (where the id is the topic key the subscriber already
    knows) and is set by the relay on the firehose (``subscribe_all``), where one
    subscriber sees events from many servers and must tell them apart.
    """

    stream: EventStream
    payload: dict[str, object] = field(default_factory=dict)
    emitted_at: dt.datetime | None = None
    server_id: str | None = None


class EventSubscription(AsyncIterator[RealTimeEvent], abc.ABC):
    """An async iterator over one subscriber's events, closeable for cleanup.

    Iterating drains the subscriber's buffer in order; :meth:`aclose` releases
    the subscription (its topic slot and buffer) so a disconnect leaves no leak.
    """

    @abc.abstractmethod
    async def aclose(self) -> None:
        """Release this subscription; subsequent iteration raises StopAsyncIteration."""


class RealTimeEvents(abc.ABC):
    """Port: publish server events and subscribe to a server's selected streams."""

    @abc.abstractmethod
    def publish(self, *, server_id: str, event: RealTimeEvent) -> None:
        """Fan ``event`` out to every current subscriber of ``server_id``.

        Non-blocking: a subscriber whose buffer is full loses its oldest event
        and will receive a :data:`EventStream.GAP` marker, so a slow subscriber
        can never back-pressure the caller (the gRPC session path). A server with
        no subscribers drops the event.
        """

    @abc.abstractmethod
    def subscribe(
        self, *, server_id: str, streams: frozenset[EventStream]
    ) -> EventSubscription:
        """Return an :class:`EventSubscription` of ``server_id`` events in ``streams``.

        Each subscriber gets its own bounded buffer; iterating drains it in
        order. Events of a stream not in ``streams`` are not delivered (the GAP
        marker is always delivered). The subscription is cleaned up when it is
        closed (client disconnect / GC), leaving no leaked buffer.
        """

    @abc.abstractmethod
    def subscribe_all(self, *, streams: frozenset[EventStream]) -> EventSubscription:
        """Return a firehose :class:`EventSubscription` of *all* servers' events.

        Like :meth:`subscribe` but not keyed to one server: every published event
        whose stream is in ``streams`` is delivered, each tagged with its
        :attr:`RealTimeEvent.server_id` so a many-server consumer (the
        community-scoped stream) can route or filter by server. Same bounded
        buffer, drop-oldest + GAP, and deterministic cleanup on close.
        """
