"""Fidelity of :class:`FakeServerRepository` against the adapter (#2505, #2516,
#2520).

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
matches their detachment timing exactly. ``add`` is the one writer where the fake
snapshots *earlier* than the adapter, which stages the model and serializes at
flush; that is the harmless direction (more isolating, so an extra red at worst)
and is asserted as such below.

Detachment is only half of the fidelity, though: an UPDATE also names a COLUMN
SET, and everything outside it keeps whatever the row already holds (#2520).
A fake that stores the entity wholesale lets any caller's stale view of a column
it never touched — ``observed_state`` / ``observed_at`` above all, which the
lifecycle entities always carry a transaction out of date — overwrite a fresher
row, so the #216 freshest-wins guard cannot be modelled at all. The tests below
pin each writer's column set from both sides: what it writes, and what it must
leave alone.

Readers are the same street travelled outward (#2516): a SELECT materializes a
fresh entity per call, so mutating a loaded one can never reach the row until a
writer is called. A reader that hands back the stored object lets a use case's
in-place edit rewrite the row it has not written yet — again the forgiving
direction, and the one the reconciler path (``list_all`` /
``list_desired_running_assigned`` / ``list_reconcilable``) travels most.

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
    WorkerId,
)
from tests.servers.fakes import FakeServerRepository

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.UTC)
_LATER = _NOW + dt.timedelta(minutes=5)
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


def _server(
    *,
    desired: DesiredState = DesiredState.RUNNING,
    observed: ObservedState = ObservedState.RUNNING,
    assigned_worker_id: WorkerId | None = None,
) -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("srv"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config=deepcopy(_CONFIG),
        desired_state=desired,
        observed_state=observed,
        observed_at=_NOW,
        assigned_worker_id=assigned_worker_id,
        created_at=_NOW,
        updated_at=_NOW,
        slug="srv",
        # ``backup_retention`` is held RAW on the entity, like ``config``, "so a
        # malformed persisted value cannot fail every entity load" -- the copy may
        # therefore not assume it flat, hence the nested key.
        backup_retention=deepcopy(_RETENTION),
    )


def _assert_reader_detached(handed_out: Server, *, repo: FakeServerRepository) -> None:
    """A reader's return value must share nothing with the row it came from.

    The real adapter materializes a fresh entity per SELECT, so an edit to a
    loaded server reaches the database only through a writer. A reader that
    returns the stored object -- or one sharing its jsonb blobs -- lets the edit
    land with no write at all, which is the forgiving direction #2505 is about.
    """

    stored = repo.by_id[handed_out.id]
    assert handed_out is not stored
    handed_out.name = ServerName("rewritten")
    _rewrite_jsonb(handed_out)
    assert stored.name == ServerName("srv")
    assert stored.config == _CONFIG
    assert stored.backup_retention == _RETENTION


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


async def test_update_writes_the_columns_the_adapter_writes() -> None:
    repo = FakeServerRepository()
    server = _server()
    repo.seed(server)
    server.name = ServerName("renamed")
    server.config = {"properties": {"motd": "edited"}}
    server.game_port = 25566
    server.bedrock_port = 19133
    server.slug = "renamed"
    server.updated_at = _LATER

    await repo.update(server)

    stored = repo.by_id[server.id]
    assert stored.name == ServerName("renamed")
    assert stored.config == {"properties": {"motd": "edited"}}
    assert stored.game_port == 25566
    assert stored.bedrock_port == 19133
    assert stored.slug == "renamed"
    assert stored.updated_at == _LATER


async def test_update_leaves_the_columns_the_adapter_does_not_write() -> None:
    # The column-scope half of the same street (#2520): the adapter's UPDATE
    # names six columns, so a caller carrying a STALE observed_state/observed_at
    # -- every lifecycle entity does, it was loaded before the edit -- cannot
    # clobber a fresher row. Storing the entity wholesale lets it, which is
    # exactly the rule the #216 freshest-wins guard exists to enforce.
    repo = FakeServerRepository()
    worker = WorkerId(uuid.uuid4())
    server = _server(observed=ObservedState.RUNNING, assigned_worker_id=worker)
    repo.seed(server)
    # A fresher Worker report lands on the row after the caller loaded its entity.
    assert await repo.record_observed_state(server.id, ObservedState.CRASHED, _LATER)

    server.assigned_worker_id = None
    server.observed_state = ObservedState.RUNNING
    server.observed_at = _NOW
    server.desired_state = DesiredState.STOPPED
    server.backup_retention = {"keep_last": 99}
    server.name = ServerName("renamed")

    await repo.update(server)

    stored = repo.by_id[server.id]
    assert stored.observed_state is ObservedState.CRASHED
    assert stored.observed_at == _LATER
    assert stored.desired_state is DesiredState.RUNNING
    assert stored.assigned_worker_id == worker
    assert stored.backup_retention == _RETENTION
    # The edit the caller actually made still lands.
    assert stored.name == ServerName("renamed")


async def test_update_lifecycle_writes_the_columns_the_adapter_writes() -> None:
    repo = FakeServerRepository()
    server = _server()
    repo.seed(server)
    worker = WorkerId(uuid.uuid4())
    server.desired_state = DesiredState.STOPPED
    server.assigned_worker_id = worker
    server.config = {"properties": {"motd": "edited"}}
    server.updated_at = _LATER

    assert await repo.update_lifecycle(server, expected_from=DesiredState.RUNNING)

    stored = repo.by_id[server.id]
    assert stored.desired_state is DesiredState.STOPPED
    assert stored.assigned_worker_id == worker
    assert stored.config == {"properties": {"motd": "edited"}}
    assert stored.updated_at == _LATER


async def test_update_lifecycle_leaves_the_columns_the_adapter_does_not_write() -> None:
    # Same column-scope rule on the lifecycle writer (#2520), where it bites
    # hardest: this is the writer the convergence paths call while holding an
    # entity whose observed_* they loaded a transaction ago.
    repo = FakeServerRepository()
    server = _server(observed=ObservedState.RUNNING)
    repo.seed(server)
    # A fresher Worker report lands on the row after the caller loaded its entity.
    assert await repo.record_observed_state(server.id, ObservedState.CRASHED, _LATER)

    server.observed_state = ObservedState.RUNNING
    server.observed_at = _NOW
    server.backup_retention = {"keep_last": 99}
    server.name = ServerName("renamed")
    server.game_port = 25566
    server.bedrock_port = 19133
    server.slug = "renamed"
    server.desired_state = DesiredState.STOPPED

    assert await repo.update_lifecycle(server, expected_from=DesiredState.RUNNING)

    stored = repo.by_id[server.id]
    assert stored.observed_state is ObservedState.CRASHED
    assert stored.observed_at == _LATER
    assert stored.backup_retention == _RETENTION
    assert stored.name == ServerName("srv")
    assert stored.game_port is None
    assert stored.bedrock_port is None
    assert stored.slug == "srv"
    # The transition the caller actually made still lands.
    assert stored.desired_state is DesiredState.STOPPED


async def test_update_on_a_missing_row_is_a_no_op() -> None:
    # ``UPDATE ... WHERE id = :id`` matches no row and writes nothing; the fake
    # must not conjure one, which the wholesale ``by_id[id] = entity`` did.
    repo = FakeServerRepository()
    server = _server()

    await repo.update(server)

    assert repo.by_id == {}


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


async def test_update_backup_retention_stores_a_copy_of_the_policy() -> None:
    # The narrow single-column writer (issue #1841) takes a raw dict rather than
    # an entity, so it does not run through ``_copy`` -- but the adapter
    # serializes the jsonb value whole and immediately, so the caller's dict must
    # not stay reachable from the row either.
    repo = FakeServerRepository()
    server = _server()
    repo.seed(server)
    # Deliberately NOT ``_RETENTION``: a distinct value means the assertion
    # below fails if the narrow write were skipped, not only if it aliased.
    policy: dict[str, Any] = {"keep_last": 5, "nested": {"k": 7}}

    await repo.update_backup_retention(server.id, policy)

    stored = repo.by_id[server.id]
    policy["keep_last"] = 99
    policy["nested"]["k"] = 99
    assert stored.backup_retention == {"keep_last": 5, "nested": {"k": 7}}


async def test_get_by_id_hands_out_a_copy_the_caller_cannot_write_through() -> None:
    repo = FakeServerRepository()
    server = _server()
    repo.seed(server)

    loaded = await repo.get_by_id(server.id)

    assert loaded is not None
    _assert_reader_detached(loaded, repo=repo)


async def test_get_by_community_and_name_hands_out_a_copy() -> None:
    repo = FakeServerRepository()
    server = _server()
    repo.seed(server)

    loaded = await repo.get_by_community_and_name(server.community_id, server.name)

    assert loaded is not None
    _assert_reader_detached(loaded, repo=repo)


async def test_get_by_slug_hands_out_a_copy() -> None:
    repo = FakeServerRepository()
    server = _server()
    repo.seed(server)

    loaded = await repo.get_by_slug(server.slug)

    assert loaded is not None
    _assert_reader_detached(loaded, repo=repo)


async def test_list_for_community_hands_out_copies() -> None:
    repo = FakeServerRepository()
    server = _server()
    repo.seed(server)

    (loaded,) = await repo.list_for_community(server.community_id)

    _assert_reader_detached(loaded, repo=repo)


async def test_list_all_hands_out_copies() -> None:
    repo = FakeServerRepository()
    server = _server()
    repo.seed(server)

    (loaded,) = await repo.list_all()

    _assert_reader_detached(loaded, repo=repo)


async def test_list_desired_running_assigned_hands_out_copies() -> None:
    repo = FakeServerRepository()
    server = _server(assigned_worker_id=WorkerId(uuid.uuid4()))
    repo.seed(server)

    (loaded,) = await repo.list_desired_running_assigned()

    _assert_reader_detached(loaded, repo=repo)


async def test_list_reconcilable_hands_out_copies() -> None:
    # The reconciler path: desired=running with no assignment is an orphan, so
    # this row is reconcilable. These three readers feed the loop that decides
    # what to place and dispatch, which is the least directly covered code path
    # in the repository -- a leak here is the hardest to see.
    repo = FakeServerRepository()
    server = _server()
    repo.seed(server)

    (loaded,) = await repo.list_reconcilable()

    _assert_reader_detached(loaded, repo=repo)
