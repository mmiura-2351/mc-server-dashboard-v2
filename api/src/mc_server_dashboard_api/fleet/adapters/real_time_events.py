"""In-process pub/sub adapter for the :class:`RealTimeEvents` Port (FR-MON-1..4).

A single-process fan-out: :meth:`InProcessRealTimeEvents.publish` copies an event
into every current subscriber's bounded buffer; :meth:`subscribe` returns an
async iterator that drains one subscriber's buffer in order. Topics are keyed by
server id and exist only while a server has at least one subscriber.

Best-effort delivery (FR-MON-4): ``publish`` is synchronous and never blocks, so
a slow subscriber cannot back-pressure the caller (the gRPC session path). When a
subscriber's buffer is full the oldest event is dropped and a single
:data:`EventStream.GAP` marker is queued ahead of the retained window so the
subscriber learns it fell behind. The marker is coalesced: while a gap is already
pending, further drops do not queue more markers.
"""

from __future__ import annotations

import asyncio
from collections import deque

from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    EventSubscription,
    RealTimeEvent,
    RealTimeEvents,
)

# Default per-subscriber buffer depth. Bounds memory per slow WebSocket client;
# overflow drops oldest with a gap marker rather than growing without limit.
_DEFAULT_MAX_QUEUE = 256

_GAP_EVENT = RealTimeEvent(stream=EventStream.GAP)


class _Subscription(EventSubscription):
    """One subscriber's bounded buffer; an async iterator over its events.

    Registered with its topic by :meth:`InProcessRealTimeEvents.subscribe` and
    deregistered by :meth:`aclose` (client disconnect / GC) — deterministically,
    not via generator finalisation, so a never-iterated subscription that is
    closed still releases its topic slot (no leak).
    """

    def __init__(
        self,
        owner: "InProcessRealTimeEvents",
        server_id: str,
        streams: frozenset[EventStream],
        max_queue: int,
    ) -> None:
        self._owner = owner
        self._server_id = server_id
        self._streams = streams
        self._buffer: deque[RealTimeEvent] = deque(maxlen=max_queue)
        self._gap_pending = False
        self._closed = False
        # Set whenever there is something to consume (an event or a pending gap)
        # or the subscription is closed; the consumer awaits it.
        self._ready = asyncio.Event()

    def offer(self, event: RealTimeEvent) -> None:
        """Buffer ``event`` if its stream is selected; drop-oldest on overflow."""

        if event.stream not in self._streams:
            return
        if len(self._buffer) == self._buffer.maxlen:
            # Buffer full: the leftmost (oldest) event is evicted by the bounded
            # deque on append. Signal the loss once with a coalesced gap marker.
            self._gap_pending = True
        self._buffer.append(event)
        self._ready.set()

    def __aiter__(self) -> "_Subscription":
        return self

    async def __anext__(self) -> RealTimeEvent:
        while True:
            if self._gap_pending:
                self._gap_pending = False
                return _GAP_EVENT
            if self._buffer:
                event = self._buffer.popleft()
                if not self._buffer and not self._gap_pending:
                    self._ready.clear()
                return event
            if self._closed:
                raise StopAsyncIteration
            self._ready.clear()
            await self._ready.wait()

    async def aclose(self) -> None:
        self._closed = True
        self._ready.set()
        self._owner._remove(self._server_id, self)


class InProcessRealTimeEvents(RealTimeEvents):
    """Single-process per-server pub/sub fan-out (no external transport)."""

    def __init__(self, *, max_queue: int = _DEFAULT_MAX_QUEUE) -> None:
        self._max_queue = max_queue
        self._topics: dict[str, set[_Subscription]] = {}

    def publish(self, *, server_id: str, event: RealTimeEvent) -> None:
        for sub in self._topics.get(server_id, ()):
            sub.offer(event)

    def subscribe(
        self, *, server_id: str, streams: frozenset[EventStream]
    ) -> EventSubscription:
        sub = _Subscription(self, server_id, streams, self._max_queue)
        self._topics.setdefault(server_id, set()).add(sub)
        return sub

    def subscriber_count(self, server_id: str) -> int:
        """Return how many live subscribers ``server_id`` has (test/observability)."""

        return len(self._topics.get(server_id, ()))

    def _remove(self, server_id: str, sub: _Subscription) -> None:
        topic = self._topics.get(server_id)
        if topic is None:
            return
        topic.discard(sub)
        if not topic:
            del self._topics[server_id]
