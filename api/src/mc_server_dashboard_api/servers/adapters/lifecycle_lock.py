"""PostgreSQL advisory-lock implementation of the ``LifecycleLock`` Port.

Holds a *session-level* ``pg_advisory_lock`` on a dedicated connection for the
whole gated operation (issue #827). A session-level advisory lock — unlike
``pg_advisory_xact_lock`` or ``SELECT ... FOR UPDATE`` — is held until explicitly
released, so it spans the MULTIPLE transactions an at-rest-gated use case runs
(the at-rest check, the seconds-to-minutes Storage mutation, and the final
commit). The dedicated connection is what makes that possible: the use case's own
``UnitOfWork`` opens a fresh session per ``async with`` and would drop a
session-lock when that session closes between transactions, so the lock lives on
its own connection that stays open for the operation's duration.

The lock is keyed on the server UUID, so it serializes operations on one server
across every API worker process (a DB-level lock, not an in-process asyncio one),
while leaving different servers fully concurrent. ``StartServer`` takes the same
lock for its desired-state flip, so a held lock blocks a start for the gated
operation's duration.

Acquisition is *bounded* (issue #876): a waiter polls ``pg_try_advisory_lock`` for
a short budget rather than parking on the blocking ``pg_advisory_lock`` (which
would pin this dedicated pool connection until the holder finishes), and raises
:class:`~...domain.errors.ServerBusyError` (a 409 the caller retries) once the
budget is spent. The acquiring connection runs in AUTOCOMMIT so it never sits
idle-in-transaction; a session-level advisory lock is connection-scoped, not
transaction-scoped, so committing does not drop it.

Advisory locks are a PostgreSQL feature. This adapter is wired only behind the
real Postgres engine; the unit suite uses the in-memory ``LifecycleLock`` fake
and the no-op :class:`~...domain.lifecycle_lock.NullLifecycleLock`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mc_server_dashboard_api.servers.domain.errors import ServerBusyError
from mc_server_dashboard_api.servers.domain.lifecycle_lock import LifecycleLock
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

# A constant namespace mixed into the lock key so the server-UUID-derived key
# cannot collide with an advisory lock any other subsystem takes on a bare id.
_LOCK_NAMESPACE = "server-lifecycle"

# Bounded-acquire budget (issue #876). A waiter polls ``pg_try_advisory_lock`` (a
# non-blocking attempt) on this cadence up to this total budget rather than calling
# the blocking ``pg_advisory_lock``. ``pg_advisory_lock`` would park the backend —
# and pin this dedicated pool connection — for the holder's whole operation
# (seconds to minutes), so a few contending waiters can exhaust the pool and starve
# every other request. ``lock_timeout`` does not bound advisory-lock functions (it
# governs table/row/object locks; the advisory functions are documented separately
# and are not covered), so the bound is enforced here with an explicit poll loop.
# The budget is short on purpose: contention means another lifecycle op is in
# flight, which is a transient 409 the caller retries, not something to wait minutes
# for behind a pinned pool slot.
_ACQUIRE_BUDGET_SECONDS = 5.0
_ACQUIRE_POLL_SECONDS = 0.1


def _lock_key(server_id: ServerId) -> int:
    """Derive a signed 64-bit advisory-lock key from the server UUID.

    ``pg_advisory_lock`` keys on a ``bigint``; the UUID is 128 bits, so we fold it
    to 64 with a stable hash over a namespaced byte string and map it into the
    signed range Postgres expects. A 64-bit fold of a 128-bit id has a negligible
    collision chance across one deployment's server set, and a collision would
    only over-serialize two unrelated servers (a rare, harmless slowdown), never
    skip the lock.
    """

    digest = hashlib.blake2b(
        f"{_LOCK_NAMESPACE}:{server_id.value}".encode(), digest_size=8
    ).digest()
    unsigned = int.from_bytes(digest, "big")
    # Map [0, 2**64) into the signed bigint range Postgres advisory locks use.
    return unsigned - (1 << 63)


class PgLifecycleLock(LifecycleLock):
    """:class:`LifecycleLock` over a PostgreSQL session-level advisory lock."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[None]:
        key = _lock_key(server_id)
        # A dedicated connection held open for the whole operation: the
        # session-level advisory lock lives on this connection and is released
        # explicitly on exit (and implicitly if the connection drops), spanning the
        # gated operation's multiple transactions on the UoW's own connections.
        #
        # AUTOCOMMIT: without it SQLAlchemy autobegins a transaction on the first
        # execute and never commits it, so this dedicated connection sits
        # idle-in-transaction for the whole (seconds-to-minutes) gated op. A
        # deployment with idle_in_transaction_session_timeout then kills the backend
        # mid-op and the advisory lock silently drops (issue #876). AUTOCOMMIT is
        # safe here precisely because a SESSION-level advisory lock is tied to the
        # connection, not the transaction: per the PG docs, "an advisory lock is held
        # until explicitly released or the session ends" and "session-level advisory
        # lock requests do not honor transaction semantics", so committing the
        # acquiring statement does NOT release the lock — it lives until our explicit
        # pg_advisory_unlock or the connection closing.
        async with self._engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await self._acquire(conn, key, server_id)
            try:
                yield
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )

    @staticmethod
    async def _acquire(conn: AsyncConnection, key: int, server_id: ServerId) -> None:
        # Bounded acquire (issue #876): poll the non-blocking pg_try_advisory_lock
        # instead of parking on the blocking pg_advisory_lock, so a waiter never pins
        # this pool connection longer than the budget. ``pg_try_advisory_lock``
        # returns true if it took the lock, false if it is held elsewhere.
        deadline = asyncio.get_running_loop().time() + _ACQUIRE_BUDGET_SECONDS
        while True:
            got = await conn.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            )
            if got:
                return
            if asyncio.get_running_loop().time() >= deadline:
                # Another lifecycle op holds the lock for this server; surface a
                # transient 409 rather than block (and pin a pool slot) for minutes.
                raise ServerBusyError(str(server_id.value))
            await asyncio.sleep(_ACQUIRE_POLL_SECONDS)
