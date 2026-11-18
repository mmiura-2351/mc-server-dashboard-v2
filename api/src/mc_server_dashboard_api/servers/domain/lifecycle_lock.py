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

A held lock can be lost before its holder releases it (issue #3927): the real
lock lives on a database session, and when that session ends the database
releases the lock without telling the holder. So ``hold`` yields a
:class:`HeldLifecycleLock`, and a holder calls its ``ensure_held`` immediately
before each write that relies on the exclusion -- a storage flip or prune, an
in-place working-set write, an archive write or delete, a row commit that assumed
the server at rest. A lost lock raises
:class:`~mc_server_dashboard_api.servers.domain.errors.LifecycleLockLostError`
there, so the operation stops before that write. A loss between the check and the
write it guards goes unnoticed (``docs/app/DATABASE.md`` Section 7).
"""

from __future__ import annotations

import abc
import contextlib
from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class HeldLifecycleLock(abc.ABC):
    """A lifecycle lock its holder still has to confirm before each guarded write."""

    @abc.abstractmethod
    async def ensure_held(self) -> None:
        """Return if the lock is still held; raise if it was lost (issue #3927).

        Raises :class:`~...domain.errors.LifecycleLockLostError` when the lock
        is no longer held, or when that cannot be confirmed. Once lost, it stays
        lost: every later call raises too.
        """


class NullHeldLifecycleLock(HeldLifecycleLock):
    """A held lock that is never lost: what :class:`NullLifecycleLock` yields."""

    async def ensure_held(self) -> None:
        return None


class LifecycleLock(abc.ABC):
    """Port: a per-server advisory lock held across a gated operation."""

    @abc.abstractmethod
    def hold(
        self, server_id: ServerId
    ) -> contextlib.AbstractAsyncContextManager[HeldLifecycleLock]:
        """Acquire the per-server lock; release it when the context exits.

        Waits until the lock is free for ``server_id``, but the wait is *bounded*
        (issue #876): a real implementation that would otherwise pin a scarce
        resource (a DB pool connection) gives up after a short budget and raises
        :class:`~mc_server_dashboard_api.servers.domain.errors.ServerBusyError`
        (a transient 409 the caller retries) when another lifecycle operation holds
        the lock too long. The returned context manager holds the lock for its body
        and releases on exit (normal or error). It yields the
        :class:`HeldLifecycleLock` the body confirms the lock through.
        """


class NullLifecycleLock(LifecycleLock):
    """No-op :class:`LifecycleLock`: acquires nothing, blocks no one.

    The default the use cases carry so their many construction sites (and the
    unit tests that do not exercise the lock) need not wire a real lock. The
    application factory injects the real DB-backed lock; tests that assert the
    serialization use a fake that records or coordinates acquisitions.
    """

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[HeldLifecycleLock]:
        yield NullHeldLifecycleLock()
