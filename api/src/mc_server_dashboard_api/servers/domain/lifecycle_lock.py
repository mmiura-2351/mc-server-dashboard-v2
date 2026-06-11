"""The servers-context ``LifecycleLock`` Port: a per-server mutual-exclusion lock.

Every at-rest-gated use case (RestoreBackup, the file mutations, UpdateServer,
DeleteServer, DeleteBackup) checks ``server.is_at_rest()`` in one transaction,
mutates Storage over seconds-to-minutes, then commits a second transaction. A
start committed in that window operates on data being mutated underneath it
(issue #827). ``FOR UPDATE`` on the server row cannot cover that gap: the gated
operation spans *multiple* transactions, and a row lock lives only for the one
transaction that took it. A per-server advisory lock held *across* those
transactions makes "at rest" stable for the operation's whole duration.

The contract: ``hold(server_id)`` returns an async context manager. While one
caller holds the lock for a server, any other caller's ``hold`` for the same
server blocks until the first releases. The at-rest-gated use cases take the
lock around their full check-mutate-commit sequence; ``StartServer`` takes the
same lock around its desired-state flip, so a held lock blocks the start for the
gated operation's duration (it then re-evaluates the row and either 409s or runs
against the settled post-mutation state). The lock is keyed on the server UUID
and is process-independent (a DB advisory lock), so it serializes operations
across multiple API workers, not just within one process.
"""

from __future__ import annotations

import abc
import contextlib
from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class LifecycleLock(abc.ABC):
    """Port: a per-server advisory lock held across a gated operation."""

    @abc.abstractmethod
    def hold(self, server_id: ServerId) -> contextlib.AbstractAsyncContextManager[None]:
        """Acquire the per-server lock; release it when the context exits.

        Blocks until the lock is free for ``server_id``. The returned context
        manager holds the lock for its body and releases on exit (normal or
        error).
        """


class NullLifecycleLock(LifecycleLock):
    """No-op :class:`LifecycleLock`: acquires nothing, blocks no one.

    The default the use cases carry so their many construction sites (and the
    unit tests that do not exercise the lock) need not wire a real lock. The
    application factory injects the real DB-backed lock; tests that assert the
    serialization use a fake that records or coordinates acquisitions.
    """

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[None]:
        yield
