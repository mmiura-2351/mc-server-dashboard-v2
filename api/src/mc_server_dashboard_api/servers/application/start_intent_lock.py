"""Per-server lock over the commit of a start intent and its revert (issue #3741).

A failed start reverts its intent only while the intent is still its own: the
stop-intent count it read is unchanged, and the row is still desired=running on
its Worker (``stop_intents.py``). A newer start must not commit an intent between
that check and the revert's write, or the revert would undo the newer intent. So
the two writes that create a start intent (the compare-and-set and commit in
``StartServer.__call__`` and ``place_and_start``, from the count read before
them through the load confirmation after the commit) and the two reverts
(``_compensate`` and ``_unassign``: read, check, compare-and-set, commit, bump
and load decrement) hold this lock for the server. Stops and drains do not take
it. They only withdraw an intent, and a revert's compare-and-set already loses to
one that lands in between.

The lock also orders the load. The registry keys a committed entry by Worker and
server, so a revert's decrement after a newer start's confirmation on the same
Worker would drop the newer start's entry. Under the lock, the revert decrements
before a newer start can confirm.

This is not the lifecycle lock. Operations that can never create a start intent
hold that lock across Storage I/O: a backup upload or delete, a prune, the
integrity sweep. Its wait is bounded, so a revert queued behind one of them would
give up and leave the row running with no start sent. This lock is held only
across the short writes above, and its wait is not bounded.

Lock order: a start takes this lock inside its lifecycle lock, and a revert takes
only this one. Nothing takes the lifecycle lock while holding this one.

In-process, like ``StopIntentCounter``, and for the same reason. The API is a
single instance (REQUIREMENTS.md NFR-SCALE-1), and both the intent commits and
the reverts that this lock orders run in this process.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.domain.value_objects import ServerId


@dataclass
class StartIntentLock:
    """One ``asyncio.Lock`` per server, made on first use."""

    _locks: dict[ServerId, asyncio.Lock] = field(default_factory=dict)

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[None]:
        lock = self._locks.get(server_id)
        if lock is None:
            lock = self._locks[server_id] = asyncio.Lock()
        async with lock:
            yield

    def is_held(self, server_id: ServerId) -> bool:
        """Whether someone holds the lock for ``server_id`` now."""

        lock = self._locks.get(server_id)
        return lock is not None and lock.locked()
