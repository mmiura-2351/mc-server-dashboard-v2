"""Fidelity of :class:`FakeServerRepository`'s writers against the adapter (#2505).

Every writer on the real ``SqlAlchemyServerRepository`` turns an entity into an
INSERT/UPDATE: the values leave the caller's object at execute time and no later
in-memory mutation of that object can reach the row. A fake that keeps the
caller's object as its "row" breaks that one-way street, and the direction it
breaks in is the dangerous one — a use case's post-transaction write-back onto
its own local entity (the entity-honesty assignments the lifecycle use cases do,
e.g. ``server.observed_at = current.observed_at``) retroactively rewrites what
the test believes was persisted, so a persisted-state assert can pass while the
adapter would have stored something else.

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


def _server(*, desired: DesiredState = DesiredState.RUNNING) -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("srv"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={"properties": {"motd": "hi"}},
        desired_state=desired,
        observed_state=ObservedState.RUNNING,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
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
    server.config["properties"]["motd"] = "rewritten"
    assert stored.observed_at == _NOW
    assert stored.observed_state is ObservedState.RUNNING
    assert stored.config == {"properties": {"motd": "hi"}}


async def test_add_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeServerRepository()
    server = _server()

    await repo.add(server)

    stored = repo.by_id[server.id]
    assert stored is not server
    server.observed_state = ObservedState.CRASHED
    server.config["properties"]["motd"] = "rewritten"
    assert stored.observed_state is ObservedState.RUNNING
    assert stored.config == {"properties": {"motd": "hi"}}


async def test_update_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeServerRepository()
    server = _server()
    repo.seed(server)

    await repo.update(server)

    stored = repo.by_id[server.id]
    assert stored is not server
    server.name = ServerName("renamed")
    server.config["properties"]["motd"] = "rewritten"
    assert stored.name == ServerName("srv")
    assert stored.config == {"properties": {"motd": "hi"}}


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
    server.config["properties"]["motd"] = "rewritten"
    assert stored.observed_state is ObservedState.RUNNING
    assert stored.config == {"properties": {"motd": "hi"}}
