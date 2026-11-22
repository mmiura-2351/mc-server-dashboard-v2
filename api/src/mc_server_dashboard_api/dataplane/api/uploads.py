"""The snapshot uploads in flight, by server and publishing Worker (issue #3950).

A Worker's snapshot command can end before the API's commit of its upload does.
The Worker's transfer deadline or a Worker restart ends the command while the API
holds the whole body, and the API goes on committing it. A start that hydrated
that Worker meanwhile would run it on the older generation, and the commit would
then publish a world the new run does not contain.

So each upload that names its Worker counts as in flight here from the moment its
request enters the data plane, before its transfer-slot wait and before the first
read it decides on, until it has committed or given up. A start that hydrates a
Worker first waits for that Worker's uploads of the server to leave
(:meth:`SnapshotUploads.settled`), and the hydrate then serves what they publish.

Everything here runs on the event loop and none of it awaits while it updates the
count, so it needs no lock. A commit is published before its upload leaves, so a
start that sees none left reads a generation that includes it. Like the per-server
storage lock, the count is per process, and so are the uploads it counts. No entry
outlives the uploads it counts, so a deleted server leaves nothing behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

_Key = tuple[uuid.UUID, str]


class SnapshotUploads:
    """The snapshot uploads being received or committed, by server and Worker."""

    def __init__(self) -> None:
        self._in_flight: dict[_Key, int] = {}
        self._left: dict[_Key, asyncio.Event] = {}

    @contextlib.asynccontextmanager
    async def track(
        self, server_id: uuid.UUID, worker_id: str | None
    ) -> AsyncIterator[None]:
        """Count an upload of ``server_id`` from ``worker_id`` as in flight.

        It counts for the whole block, however the block ends. An upload that
        names no Worker cannot be told apart from any Worker's, so it is not
        counted.
        """

        if worker_id is None:
            yield
            return
        key = (server_id, worker_id)
        self._in_flight[key] = self._in_flight.get(key, 0) + 1
        try:
            yield
        finally:
            remaining = self._in_flight[key] - 1
            if remaining:
                self._in_flight[key] = remaining
            else:
                del self._in_flight[key]
                left = self._left.pop(key, None)
                if left is not None:
                    left.set()

    async def settled(
        self, server_id: uuid.UUID, worker_id: str, *, timeout: float
    ) -> bool:
        """Wait until no upload of ``server_id`` from ``worker_id`` is in flight.

        Returns False if one still is after ``timeout`` seconds. The wait holds
        nothing an upload needs.
        """

        key = (server_id, worker_id)
        try:
            async with asyncio.timeout(timeout):
                while key in self._in_flight:
                    await self._left.setdefault(key, asyncio.Event()).wait()
        except TimeoutError:
            return False
        return True
