"""Fidelity of :class:`FakeServerRepository`'s writers against the adapter (#2505).

Every writer on the real ``SqlAlchemyServerRepository`` turns an entity into an
INSERT/UPDATE, and once that statement has been serialized no in-memory mutation
of the caller's object can reach the row. A fake that keeps the caller's object
as its "row" breaks that one-way street, and the direction it breaks in is the
dangerous one — a use case's post-transaction write-back onto its own local
entity (the entity-honesty assignments the lifecycle use cases do, e.g.
``server.observed_at = current.observed_at``) retroactively rewrites what the
test believes was persisted, so a persisted-state assert can pass while the
adapter would have stored something else.

``update`` / ``update_lifecycle`` execute their UPDATE immediately, so the fake
matches them exactly. ``add`` is the one writer where the fake snapshots
*earlier* than the adapter, which stages the model and serializes at flush; that
is the harmless direction (more isolating, so an extra red at worst) and is
asserted as such below.

That is not hypothetical: re-recording ``crashed`` with a fresh stamp inside
``StopServer``'s crash-preserve arm was absorbed by the aliasing and left
``test_stop_server_not_found_on_crashed_snapshots_and_keeps_crashed`` green,
while the identical mutation in ``redispatch_stop`` — which does not run
``update_lifecycle``, so its stored row stayed a distinct object — reddened.
These assertions pin the copy so mutation evidence stays sound.
"""

from __future__ import annotations

import datetime as dt
import uuid
from copy import deepcopy
from typing import Any

from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import FakeServerRepository

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.UTC)
_CONFIG: dict[str, Any] = {"properties": {"motd": "hi"}}
_RETENTION: dict[str, Any] = {"keep_last": 2, "nested": {"k": 1}}


def _rewrite_jsonb(server: Server) -> None:
    """Edit both jsonb blobs NESTED-deep, as a caller would after the write.

    A one-level copy lets both edits through, so these pin the copy's depth as
    well as its existence -- deleting either ``deepcopy`` in ``_copy`` reddens.
    ``config["properties"][k] = v`` is the shape use cases really write;
    ``backup_retention`` is nested here because the column is held raw, so the
    copy may not assume the shape today's writers happen to emit.
    """

    server.config["properties"]["motd"] = "rewritten"
    assert server.backup_retention is not None
    server.backup_retention["nested"]["k"] = 99


def _server(*, desired: DesiredState = DesiredState.RUNNING) -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("srv"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config=deepcopy(_CONFIG),
        desired_state=desired,
        observed_state=ObservedState.RUNNING,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
        # ``backup_retention`` is held RAW on the entity, like ``config``, "so a
        # malformed persisted value cannot fail every entity load" -- the copy may
        # therefore not assume it flat, hence the nested key.
        backup_retention=deepcopy(_RETENTION),
    )


async def test_update_lifecycle_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeServerRepository()
    server = _server()
    repo.seed(server)
    server.desired_state = DesiredState.STOPPED

    assert await repo.update_lifecycle(server, expected_from=DesiredState.RUNNING)

    stored = repo.by_id[server.id]
    assert stored is not server
    # The write-back a use case performs on its own entity after the transaction
    # must not reach the row -- the adapter's UPDATE has already executed.
    server.observed_at = _NOW - dt.timedelta(minutes=5)
    server.observed_state = ObservedState.CRASHED
    _rewrite_jsonb(server)
    assert stored.observed_at == _NOW
    assert stored.observed_state is ObservedState.RUNNING
    assert stored.config == _CONFIG
    assert stored.backup_retention == _RETENTION


async def test_add_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeServerRepository()
    server = _server()

    await repo.add(server)

    stored = repo.by_id[server.id]
    # The one writer where the fake is deliberately STRICTER than the adapter: the
    # real ``add`` stages a model holding this ``config`` by reference and only
    # serializes at flush, so a mutation before the commit would still land. The
    # sole caller commits on the next line, and erring toward isolation can only
    # cost an extra red -- never absorb a mutant, which is what #2505 is about.
    assert stored is not server
    server.observed_state = ObservedState.CRASHED
    _rewrite_jsonb(server)
    assert stored.observed_state is ObservedState.RUNNING
    assert stored.config == _CONFIG
    assert stored.backup_retention == _RETENTION


async def test_update_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeServerRepository()
    server = _server()
    repo.seed(server)

    await repo.update(server)

    stored = repo.by_id[server.id]
    assert stored is not server
    server.name = ServerName("renamed")
    _rewrite_jsonb(server)
    assert stored.name == ServerName("srv")
    assert stored.config == _CONFIG
    assert stored.backup_retention == _RETENTION


async def test_seed_stores_a_copy_the_caller_cannot_rewrite() -> None:
    # ``seed`` is the arrange half of the same street: a test that keeps its
    # seeded object and asserts on it later is asserting on the row only by
    # aliasing, which the adapter never grants.
    repo = FakeServerRepository()
    server = _server()

    repo.seed(server)

    stored = repo.by_id[server.id]
    assert stored is not server
    server.observed_state = ObservedState.CRASHED
    _rewrite_jsonb(server)
    assert stored.observed_state is ObservedState.RUNNING
    assert stored.config == _CONFIG
    assert stored.backup_retention == _RETENTION
