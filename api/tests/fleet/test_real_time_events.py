"""Unit tests for the in-process ``RealTimeEvents`` pub/sub adapter.

Drive the Port's contract (TESTING.md Section 4) without any transport: per-server
topics, bounded per-subscriber buffers with drop-oldest + a gap marker, isolation
between subscribers, and cleanup on unsubscribe so no buffer leaks.
"""

from __future__ import annotations

import asyncio

import pytest

from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    RealTimeEvent,
)

_ALL = frozenset({EventStream.STATUS, EventStream.LOG, EventStream.METRICS})


def _status(state: str) -> RealTimeEvent:
    return RealTimeEvent(stream=EventStream.STATUS, payload={"state": state})


async def test_subscriber_receives_published_event() -> None:
    bus = InProcessRealTimeEvents()
    sub = bus.subscribe(server_id="s1", streams=_ALL)

    bus.publish(server_id="s1", event=_status("running"))

    event = await asyncio.wait_for(sub.__anext__(), timeout=1)
    assert event == _status("running")
    await sub.aclose()


async def test_topics_are_per_server() -> None:
    bus = InProcessRealTimeEvents()
    sub = bus.subscribe(server_id="s1", streams=_ALL)

    bus.publish(server_id="s2", event=_status("running"))
    bus.publish(server_id="s1", event=_status("stopped"))

    event = await asyncio.wait_for(sub.__anext__(), timeout=1)
    assert event.payload == {"state": "stopped"}
    await sub.aclose()


async def test_stream_filtering_excludes_unselected_streams() -> None:
    bus = InProcessRealTimeEvents()
    sub = bus.subscribe(server_id="s1", streams=frozenset({EventStream.STATUS}))

    bus.publish(
        server_id="s1",
        event=RealTimeEvent(stream=EventStream.LOG, payload={"line": "x"}),
    )
    bus.publish(server_id="s1", event=_status("running"))

    event = await asyncio.wait_for(sub.__anext__(), timeout=1)
    # The LOG event was filtered out; the first delivered event is the STATUS one.
    assert event.stream is EventStream.STATUS
    await sub.aclose()


async def test_multiple_subscribers_each_get_their_own_copy() -> None:
    bus = InProcessRealTimeEvents()
    a = bus.subscribe(server_id="s1", streams=_ALL)
    b = bus.subscribe(server_id="s1", streams=_ALL)

    bus.publish(server_id="s1", event=_status("running"))

    ea = await asyncio.wait_for(a.__anext__(), timeout=1)
    eb = await asyncio.wait_for(b.__anext__(), timeout=1)
    assert ea == eb == _status("running")
    await a.aclose()
    await b.aclose()


async def test_slow_subscriber_drops_oldest_and_gets_gap_marker() -> None:
    # A buffer of 2 overflows on the 3rd publish: the oldest (e1) is dropped and
    # a GAP marker is enqueued so the subscriber learns it fell behind, ahead of
    # the events it can still see (the newest window e2, e3).
    bus = InProcessRealTimeEvents(max_queue=2)
    sub = bus.subscribe(server_id="s1", streams=_ALL)

    bus.publish(server_id="s1", event=_status("e1"))
    bus.publish(server_id="s1", event=_status("e2"))
    bus.publish(server_id="s1", event=_status("e3"))

    drained = [await asyncio.wait_for(sub.__anext__(), timeout=1) for _ in range(3)]
    assert drained[0].stream is EventStream.GAP
    assert [e.payload for e in drained[1:]] == [{"state": "e2"}, {"state": "e3"}]
    await sub.aclose()


async def test_gap_marker_is_not_duplicated_while_still_behind() -> None:
    # Once a gap is pending, further drops do not enqueue more GAP markers: the
    # subscriber is already told it is behind (a single, coalesced signal). With a
    # buffer of 1, e1 then e2 then e3 drops twice but yields exactly one GAP.
    bus = InProcessRealTimeEvents(max_queue=1)
    sub = bus.subscribe(server_id="s1", streams=_ALL)

    bus.publish(server_id="s1", event=_status("e1"))
    bus.publish(server_id="s1", event=_status("e2"))
    bus.publish(server_id="s1", event=_status("e3"))

    first = await asyncio.wait_for(sub.__anext__(), timeout=1)
    second = await asyncio.wait_for(sub.__anext__(), timeout=1)
    assert first.stream is EventStream.GAP
    assert second.payload == {"state": "e3"}
    await sub.aclose()


async def test_unsubscribe_cleans_up_buffer_no_leak() -> None:
    bus = InProcessRealTimeEvents()
    sub = bus.subscribe(server_id="s1", streams=_ALL)
    assert bus.subscriber_count("s1") == 1

    await sub.aclose()

    assert bus.subscriber_count("s1") == 0
    # A publish after the last subscriber leaves drops the topic entirely.
    bus.publish(server_id="s1", event=_status("running"))
    assert bus.subscriber_count("s1") == 0


async def test_publish_with_no_subscribers_is_a_noop() -> None:
    bus = InProcessRealTimeEvents()
    # Must not raise and must not create a lingering topic.
    bus.publish(server_id="s1", event=_status("running"))
    assert bus.subscriber_count("s1") == 0


async def test_iterator_stops_after_close() -> None:
    bus = InProcessRealTimeEvents()
    sub = bus.subscribe(server_id="s1", streams=_ALL)
    await sub.aclose()
    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()
