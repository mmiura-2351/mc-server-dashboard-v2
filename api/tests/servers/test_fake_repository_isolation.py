"""Entity isolation across every servers-context repository fake (#2516).

:mod:`tests.servers.test_server_repository_fake` establishes the rule for
``FakeServerRepository``; this module applies the same rule to the rest of
``tests.servers.fakes``. Restated: a repository fake is a stand-in for a real
adapter, and a real adapter is a *one-way* seam in both directions. A writer
serializes the entity into an INSERT/UPDATE, so no later in-memory mutation can
reach the row; a reader materializes a fresh entity per SELECT, so mutating a
loaded one reaches the database only through a writer.

A fake that stores, or hands back, the caller's object is therefore **more
forgiving than production** — and only in that direction. That is what makes it
dangerous rather than merely inaccurate: a mutant that should redden a
persisted-state assertion can be absorbed by the aliasing, silently, with
nothing visible in the test source (#2505, PR #2512).

So each test here *demonstrates* the detachment rather than asserting the copy
exists: it mutates the entity on the caller's side of the boundary after the
call and shows the other side did not move. Where an entity carries a mutable
collection (``ServerPlugin``'s jsonb list columns, ``PlayerGroup.players``) the
mutation goes one level in, so a shallow copy that claims a detachment it does
not have reddens too.

Detachment is one half of a writer's fidelity; whether the row EXISTS at all is
the other (#2557, following PR #2556). An ``UPDATE ... WHERE id = :id`` matches
nothing on an absent id, so nothing is written and no row appears. A fake that
keys the entity in regardless conjures a row production cannot produce, and a
test that updates a deleted entity and then reads it back is asserting a state
production can never reach. The ``_on_a_missing_row_is_a_no_op`` tests pin that
per writer, against the adapter each one stands in for.

``FakeGameSessionRepository`` is absent on purpose: ``GameSession`` is
``frozen=True``, so no mutation can cross its boundary in either direction and
there is nothing for a copy to protect.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.errors import GroupNotFoundError
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackAssignment,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.schedule import (
    Cadence,
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
    ScheduleRunId,
    ScheduleRunOutcome,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)
from tests.servers.fakes import (
    FakeBackupRepository,
    FakeGroupRepository,
    FakePluginRepository,
    FakeResourcePackRepository,
    FakeScheduleRepository,
    FakeScheduleRunRepository,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.UTC)
_SERVER = ServerId(uuid.uuid4())


# -- FakeBackupRepository --


def _backup() -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=_SERVER,
        storage_ref="ref/1",
        size_bytes=10,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )


def test_backup_seed_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeBackupRepository()
    backup = _backup()

    repo.seed(backup)

    stored = repo.by_id[backup.id]
    backup.storage_ref = "rewritten"
    backup.health = BackupHealth.QUARANTINED
    assert stored.storage_ref == "ref/1"
    assert stored.health is BackupHealth.HEALTHY


async def test_backup_add_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeBackupRepository()
    backup = _backup()

    await repo.add(backup)

    stored = repo.by_id[backup.id]
    backup.size_bytes = 999
    assert stored.size_bytes == 10


async def test_backup_readers_hand_out_copies() -> None:
    repo = FakeBackupRepository()
    backup = _backup()
    repo.seed(backup)

    loaded = await repo.get_by_id(backup.id)
    (listed,) = await repo.list_for_server(_SERVER)

    assert loaded is not None
    loaded.storage_ref = "rewritten-by-get"
    listed.storage_ref = "rewritten-by-list"
    assert repo.by_id[backup.id].storage_ref == "ref/1"


# -- FakePluginRepository --


def _plugin(*, rel_path: str = "mods/a.jar") -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=_SERVER,
        rel_path=rel_path,
        filename="a.jar",
        display_name="A",
        description=None,
        loader_type=LoaderType.MOD,
        source=PluginSource.MODRINTH,
        source_project_id="proj",
        source_version_id="ver",
        version_number="1.0",
        checksum_sha512="sha512",
        sha256="sha256",
        size_bytes=1,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        provides=["alias"],
        dependencies=[{"mod_identifier": "dep", "required": True}],
        mc_versions=["1.21.1"],
        catalog_dependencies=[{"project_id": "cdep", "required": True}],
    )


def _rewrite_plugin(plugin: ServerPlugin) -> None:
    """Edit a scalar and every jsonb list column, one level in.

    The four list columns are serialized whole by the adapter, so neither a
    replacement nor an in-place edit of an existing element can reach a row that
    has already been written -- a one-level copy would let the second through.
    """

    plugin.display_name = "rewritten"
    plugin.provides.append("smuggled")
    plugin.mc_versions.append("1.99")
    plugin.dependencies[0]["mod_identifier"] = "smuggled"
    plugin.catalog_dependencies[0]["project_id"] = "smuggled"


def _assert_plugin_unchanged(plugin: ServerPlugin) -> None:
    assert plugin.display_name == "A"
    assert plugin.provides == ["alias"]
    assert plugin.mc_versions == ["1.21.1"]
    assert plugin.dependencies == [{"mod_identifier": "dep", "required": True}]
    assert plugin.catalog_dependencies == [{"project_id": "cdep", "required": True}]


def test_plugin_seed_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakePluginRepository()
    plugin = _plugin()

    repo.seed(plugin)

    stored = repo.by_id[plugin.id]
    _rewrite_plugin(plugin)
    _assert_plugin_unchanged(stored)


async def test_plugin_add_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakePluginRepository()
    plugin = _plugin()

    await repo.add(plugin)

    stored = repo.by_id[plugin.id]
    _rewrite_plugin(plugin)
    _assert_plugin_unchanged(stored)


async def test_plugin_update_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakePluginRepository()
    plugin = _plugin()
    repo.seed(plugin)

    await repo.update(plugin)

    stored = repo.by_id[plugin.id]
    _rewrite_plugin(plugin)
    _assert_plugin_unchanged(stored)


async def test_plugin_update_on_a_missing_row_is_a_no_op() -> None:
    # ``SqlAlchemyPluginRepository.update`` issues
    # ``UPDATE server_plugin SET ... WHERE id = :id``
    # (servers/adapters/plugin_repository.py:162-190), which matches no row on
    # an absent id: nothing is written, nothing is raised, and the result of
    # ``session.execute`` is discarded under a ``-> None`` signature, so no
    # caller can read a rows-affected signal either.
    repo = FakePluginRepository()
    plugin = _plugin()

    await repo.update(plugin)

    assert repo.by_id == {}


async def test_plugin_readers_hand_out_copies() -> None:
    repo = FakePluginRepository()
    plugin = _plugin()
    repo.seed(plugin)

    loaded = [
        await repo.get_by_id(_SERVER, plugin.id),
        await repo.get_by_rel_path(_SERVER, plugin.rel_path),
        await repo.get_by_source_project_id(_SERVER, "proj"),
        (await repo.list_for_server(_SERVER))[0],
        (await repo.list_catalog_plugins(_SERVER))[0],
    ]

    for handed_out in loaded:
        assert handed_out is not None
        _rewrite_plugin(handed_out)
    _assert_plugin_unchanged(repo.by_id[plugin.id])


# -- FakeGroupRepository --


def _group() -> PlayerGroup:
    return PlayerGroup(
        id=GroupId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=GroupName("ops"),
        kind=GroupKind.OP,
        players=[Player(uuid.uuid4(), "steve")],
    )


def test_group_seed_stores_a_copy_the_caller_cannot_rewrite() -> None:
    # ``add`` / ``save`` already copy; ``seed`` -- the arrange half of the same
    # boundary -- did not, so a test keeping its seeded aggregate was asserting
    # on the row only by aliasing.
    repo = FakeGroupRepository()
    group = _group()

    repo.seed(group)

    stored = repo.by_id[group.id]
    group.name = GroupName("rewritten")
    group.upsert_player(Player(uuid.uuid4(), "smuggled"))
    assert stored.name == GroupName("ops")
    assert [p.username for p in stored.players] == ["steve"]


async def test_group_save_on_a_missing_row_with_players_reports_not_found() -> None:
    # ``save`` reads as an upsert but is not one: it never constructs a
    # ``PlayerGroupModel``. It loads the row and renames it only ``if row is not
    # None``, then replaces the child ``group_player`` set. No ``save`` can
    # therefore make a group appear -- ``add`` is the only insert path -- and all
    # three call sites (application/groups.py) ``_load_group`` first, which
    # raises ``GroupNotFoundError`` on an absent id.
    #
    # The absent-row branch is reachable anyway, by a concurrent delete landing
    # between that pre-read and the write. With players to write, the adapter
    # stages ``group_player`` INSERTs whose FK to ``player_group.id`` has no
    # parent and raises the same ``GroupNotFoundError`` at its own flush (#2583,
    # measured against PostgreSQL 18). Previously that was recorded here as an
    # unmodelled divergence, because a raw ``IntegrityError`` is a flush boundary
    # a fake has not got; a typed domain error is not, so the fake now models it
    # (#2557).
    repo = FakeGroupRepository()
    group = _group()

    with pytest.raises(GroupNotFoundError):
        await repo.save(group)

    assert repo.by_id == {}


async def test_group_save_on_a_missing_row_without_players_is_a_no_op() -> None:
    # The other half of the same branch: an empty player set stages no INSERT,
    # so nothing can violate the FK. The DELETE is the only *write* the adapter
    # emits, and it matches zero rows -- with the row gone, ``save``'s ``get``
    # re-queries and returns ``None`` (the identity map is weak and retains
    # nothing, #2583), so the ``if row is not None`` guard skips the rename and
    # no UPDATE is emitted at all. Measured: SELECT then DELETE, no UPDATE and no
    # INSERT. The save passes silently, leaving no row behind.
    #
    # Like the raising branch above, this one is what the *adapter* does, so the
    # claim is pinned against a live FK rather than against the fake alone --
    # ``tests/integration/test_group_repositories.py::
    # test_save_after_concurrent_group_delete_without_players_is_a_no_op``.
    # A fake asserting its own no-op would be true by construction.
    repo = FakeGroupRepository()
    group = _group()
    group.players = []

    await repo.save(group)

    assert repo.by_id == {}


# -- FakeScheduleRepository / FakeScheduleRunRepository --


def _schedule() -> Schedule:
    return Schedule(
        id=ScheduleId.new(),
        server_id=_SERVER,
        name="nightly",
        action=ScheduleAction.BACKUP,
        cadence=Cadence.from_cron("0 4 * * *"),
        enabled=True,
        created_at=_NOW,
        updated_at=_NOW,
        next_run_at=_NOW + dt.timedelta(hours=1),
    )


def _run(schedule_id: ScheduleId) -> ScheduleRun:
    return ScheduleRun(
        id=ScheduleRunId.new(),
        schedule_id=schedule_id,
        started_at=_NOW,
        finished_at=_NOW + dt.timedelta(seconds=1),
        outcome=ScheduleRunOutcome.SUCCESS,
        detail=None,
    )


def test_schedule_seed_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeScheduleRepository()
    schedule = _schedule()

    repo.seed(schedule)

    stored = repo.by_id[schedule.id]
    schedule.name = "rewritten"
    schedule.enabled = False
    assert stored.name == "nightly"
    assert stored.enabled is True


async def test_schedule_run_writers_store_copies_the_caller_cannot_rewrite() -> None:
    repo = FakeScheduleRunRepository()
    schedule_id = ScheduleId.new()
    seeded = _run(schedule_id)
    added = _run(schedule_id)

    repo.seed(seeded)
    await repo.add(added)

    stored = {row.id: row for row in repo.rows}
    seeded.detail = "rewritten"
    added.outcome = ScheduleRunOutcome.FAILURE
    assert stored[seeded.id].detail is None
    assert stored[added.id].outcome is ScheduleRunOutcome.SUCCESS


async def test_schedule_run_list_hands_out_copies() -> None:
    repo = FakeScheduleRunRepository()
    schedule_id = ScheduleId.new()
    run = _run(schedule_id)
    repo.seed(run)

    (listed,) = await repo.list_for_schedule(schedule_id)

    listed.detail = "rewritten"
    assert repo.rows[0].detail is None


# -- FakeResourcePackRepository --


def _pack() -> ResourcePack:
    return ResourcePack(
        id=ResourcePackId.new(),
        filename="pack.zip",
        display_name="Pack",
        description=None,
        sha1_hash="sha1",
        sha256_hash="sha256",
        size_bytes=1,
        uploaded_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _assignment(pack_id: ResourcePackId) -> ResourcePackAssignment:
    return ResourcePackAssignment(
        server_id=_SERVER,
        resource_pack_id=pack_id,
        require_resource_pack=True,
        resource_pack_prompt=None,
        assigned_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )


async def test_resource_pack_add_and_readers_are_detached() -> None:
    repo = FakeResourcePackRepository()
    pack = _pack()

    await repo.add(pack)

    pack.display_name = "rewritten-after-add"
    loaded = await repo.get_by_id(pack.id)
    (listed,) = await repo.list_all()
    assert loaded is not None
    loaded.display_name = "rewritten-by-get"
    listed.display_name = "rewritten-by-list"
    assert repo.packs[pack.id].display_name == "Pack"


async def test_resource_pack_assignment_add_and_readers_are_detached() -> None:
    repo = FakeResourcePackRepository()
    pack_id = ResourcePackId.new()
    assignment = _assignment(pack_id)

    await repo.add_assignment(assignment)

    assignment.require_resource_pack = False
    loaded = await repo.get_assignment_by_server(_SERVER)
    (listed,) = await repo.list_assignments_for_pack(pack_id)
    assert loaded is not None
    loaded.require_resource_pack = False
    listed.resource_pack_prompt = "rewritten-by-list"
    stored = repo.assignments[_SERVER]
    assert stored.require_resource_pack is True
    assert stored.resource_pack_prompt is None
