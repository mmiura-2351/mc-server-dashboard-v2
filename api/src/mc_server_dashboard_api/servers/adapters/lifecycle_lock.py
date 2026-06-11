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

Advisory locks are a PostgreSQL feature. This adapter is wired only behind the
real Postgres engine; the unit suite uses the in-memory ``LifecycleLock`` fake
and the no-op :class:`~...domain.lifecycle_lock.NullLifecycleLock`.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mc_server_dashboard_api.servers.domain.lifecycle_lock import LifecycleLock
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

# A constant namespace mixed into the lock key so the server-UUID-derived key
# cannot collide with an advisory lock any other subsystem takes on a bare id.
_LOCK_NAMESPACE = "server-lifecycle"


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
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            try:
                yield
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )
