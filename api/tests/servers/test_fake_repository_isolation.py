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

Refusal is the third (#2612, #2784, #2858). A dict carries no constraints, so a
fake that writes a row PostgreSQL would reject is forgiving in the same
direction: a use case built on the refusal passes here and fails there. Where the
adapter translates the violation the fake raises that typed domain error; where
nothing in the integrity map names the constraint, it raises the untranslated
``IntegrityError`` the adapter re-raises, because a 500 is still the refusal the
caller meets, and modelling it as anything friendlier would invent a production
behaviour that does not exist.

A fake can only refuse what it can see, and where it cannot the omission is
stated rather than left to read as an oversight (#2923). Its own rows carry the
UNIQUEs and the foreign keys whose parent it holds, so those are modelled; a
foreign key onto a row another fake owns is not — ``FakeGroupRepository`` holds
neither the ``server`` its ``attach`` names nor the ``community`` its ``add``
names, so ``fk_server_group_server_id_server`` and
``fk_player_group_community_id_community`` stay forgiving there, both by the same
decision. In a fake-driven test those two parents are asserted one layer up, by
the use case's own pre-read (``AttachGroup``'s ``_require_server``, the route's
authorization gate for the community), which is the only place that can see them.

``FakeGameSessionRepository`` is absent on purpose: ``GameSession`` is
``frozen=True``, so no mutation can cross its boundary in either direction and
there is nothing for a copy to protect.

``FakeFileStore`` is not a repository, but it is a fake standing in for an
adapter and the forgiving direction is the same hazard (#2867): where the real
seam REFUSES, a fake that answers lets a use case that depends on the refusal
pass here and fail in production. Its pins therefore live here too. What it
CANNOT describe is forgiving the same way (#2886): a store with no notion of a
directory answers every listing entry ``is_dir=False`` and forgets a created
directory, so a caller that branches on the flag, enumerates subdirectories, or
acts on what it just created is exercised against a world production never
serves.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace

import pytest
from sqlalchemy.exc import IntegrityError

from mc_server_dashboard_api.servers.adapters.integrity import _constraint_name
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.errors import (
    GroupNameAlreadyExistsError,
    GroupNotFoundError,
    PluginAlreadyExistsError,
    ResourcePackInUseError,
    ResourcePackNotFoundError,
    ServerFileNotFoundError,
)
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
    FakeFileStore,
    FakeGroupRepository,
    FakePluginRepository,
    FakeResourcePackRepository,
    FakeScheduleRepository,
    FakeScheduleRunRepository,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.UTC)
_SERVER = ServerId(uuid.uuid4())
_COMMUNITY = CommunityId(uuid.uuid4())


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


async def test_plugin_update_onto_a_taken_rel_path_reports_already_exists() -> None:
    # ``uq_server_plugin_server_rel`` refuses the adapter's UPDATE when another
    # row on the same server already holds the target path, and the adapter now
    # translates that to ``PluginAlreadyExistsError`` (#2612). Pinned against the
    # live UNIQUE in
    # ``tests/integration/test_plugin_repositories.py``; modelled here so a
    # use-case test driving the fake is not more forgiving than production.
    repo = FakePluginRepository()
    moving = _plugin()
    repo.seed(moving)
    repo.seed(_plugin(rel_path="mods/taken.jar"))

    moving.rel_path = "mods/taken.jar"
    with pytest.raises(PluginAlreadyExistsError):
        await repo.update(moving)

    assert repo.by_id[moving.id].rel_path == "mods/a.jar"


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


def _group(
    *, community_id: CommunityId | None = None, name: str = "ops"
) -> PlayerGroup:
    return PlayerGroup(
        id=GroupId(uuid.uuid4()),
        community_id=community_id or CommunityId(uuid.uuid4()),
        name=GroupName(name),
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
    # ``PlayerGroupModel``. It re-reads the row, then replaces the child
    # ``group_player`` set. No ``save`` can therefore make a group appear --
    # ``add`` is the only insert path -- and all three call sites
    # (application/groups.py) ``_load_group`` first, which raises
    # ``GroupNotFoundError`` on an absent id.
    #
    # The absent-row branch is reachable anyway, by a concurrent delete landing
    # between that pre-read and the write. With players to write, the adapter
    # stages ``group_player`` INSERTs whose FK to ``player_group.id`` has no
    # parent and raises the same ``GroupNotFoundError`` at its own flush (#2583,
    # measured against PostgreSQL 18). Previously that was recorded here as an
    # unmodelled divergence, and the load-bearing half of that reasoning was the
    # *moment*: the violation surfaced at whichever later flush the caller
    # happened to trigger, and a fake has no such flush to surface at. A typed
    # domain error raised at the call does land somewhere a fake can, so the fake
    # now models it (#2557). The argument stops there and does not reach the
    # exception type: an adapter that flushes inside its own call gives a fake the
    # same moment for a raw ``IntegrityError``, which is what
    # ``test_resource_pack_second_assignment_for_one_server_is_refused`` models
    # (#2858).
    repo = FakeGroupRepository()
    group = _group()

    with pytest.raises(GroupNotFoundError):
        await repo.save(group)

    assert repo.by_id == {}


async def test_group_save_on_a_missing_row_without_players_reports_not_found() -> None:
    # The other half of the same branch, and it used to diverge: an empty player
    # set stages no INSERT, so nothing violated the FK that carries the not-found
    # above, and the save passed silently -- telling the caller a rename or a
    # last-player removal had succeeded on a group that was gone (#2613). The
    # adapter's re-read now asserts the row before any write, so the branch a
    # caller cannot see (whether the group happened to have players) no longer
    # changes the answer.
    #
    # Like the branch above, this is what the *adapter* does, so it is pinned
    # against a real flush rather than against the fake alone --
    # ``tests/integration/test_group_repositories.py::
    # test_save_after_concurrent_group_delete_without_players_reports_not_found``.
    repo = FakeGroupRepository()
    group = _group()
    group.players = []

    with pytest.raises(GroupNotFoundError):
        await repo.save(group)

    assert repo.by_id == {}


async def test_group_add_of_a_duplicate_name_reports_already_exists() -> None:
    # ``uq_player_group_community_kind_name`` refuses a second group holding one
    # community's ``(kind, name)``, and ``SqlAlchemyGroupRepository.add`` flushes
    # the ``player_group`` row itself, so the refusal lands inside the call as
    # ``GroupNameAlreadyExistsError`` (#2000; that translation is pinned in
    # ``tests/servers/test_unit_of_work_translation.py::
    # test_group_add_translates_name_violation_at_flush``). Keying on ``group.id``
    # alone was the forgiving direction: two groups sharing the triple coexisted
    # here, a state production cannot hold (#2923).
    repo = FakeGroupRepository()
    first = _group()
    await repo.add(first)

    with pytest.raises(GroupNameAlreadyExistsError):
        await repo.add(_group(community_id=first.community_id))

    assert list(repo.by_id) == [first.id]


async def test_group_add_of_a_stored_id_is_refused() -> None:
    # ``id`` alone is ``pk_player_group`` (migration 0012), so ``add`` is an
    # INSERT and never an upsert: a second row under a stored id duplicates the
    # key and PostgreSQL refuses it at the same explicit flush that carries the
    # name UNIQUE above. No map entry names the PK, so
    # ``SqlAlchemyGroupRepository.add`` re-raises the ``IntegrityError``
    # untranslated -- a 500 (that fall-through is pinned in
    # ``tests/servers/test_unit_of_work_translation.py::
    # test_group_add_reraises_unknown_violation_untranslated``). Keying the row in
    # regardless made the fake an upsert, the forgiving direction, and left a
    # locally checkable divergence unstated while its two neighbours were
    # modelled. ``CreateGroup`` mints ``GroupId.new()``, which is what keeps the
    # hole latent rather than live.
    #
    # Same reasoning, same shim and same untranslated error as
    # ``test_resource_pack_second_assignment_for_one_server_is_refused`` (#2858),
    # including the measurement recorded there: on PostgreSQL 18 the ORM raises
    # for every duplicate shape rather than short-circuiting with a
    # ``FlushError``, and this adapter stages its row the same way -- a fresh
    # model instance followed by a flush the method owns.
    repo = FakeGroupRepository()
    first = _group()
    await repo.add(first)

    # A different name, so the row is refused by its key rather than by the
    # UNIQUE the test above pins.
    with pytest.raises(IntegrityError) as raised:
        await repo.add(replace(first, name=GroupName("second"), players=[]))

    # Read the name back through the adapter's own accessor, as the pin for the
    # assignment PK does: it is the whole payload of the shim, and a caller that
    # translates reaches it this way.
    assert _constraint_name(raised.value) == "pk_player_group"
    # The stored row stands; the refused INSERT wrote nothing over it.
    assert repo.by_id[first.id].name == GroupName("ops")


async def test_group_save_onto_a_taken_name_reports_already_exists() -> None:
    # The same UNIQUE on the rename path: ``save``'s player-row DELETE autoflushes
    # the pending name UPDATE, so a racer that took the target triple between the
    # caller's pre-check and the write is refused inside ``save`` too (#2000).
    # Pinned against the live UNIQUE in
    # ``tests/integration/test_group_repositories.py::
    # test_save_after_concurrent_name_take_reports_name_exists``; modelled here so
    # a use-case test driving the fake sees the same refusal.
    repo = FakeGroupRepository()
    community = CommunityId(uuid.uuid4())
    moving = _group(community_id=community)
    repo.seed(moving)
    repo.seed(_group(community_id=community, name="taken"))

    moving.name = GroupName("taken")
    with pytest.raises(GroupNameAlreadyExistsError):
        await repo.save(moving)

    assert repo.by_id[moving.id].name == GroupName("ops")


async def test_group_attach_to_a_missing_group_reports_not_found() -> None:
    # ``attach`` executes its INSERT rather than staging it, so
    # ``fk_server_group_group_id_player_group`` is refused inside the call and
    # translated to ``GroupNotFoundError`` -- the very error the use case's
    # pre-read raises, for a group a racer deleted just after it (#2612). Pinned
    # against the live FK in ``tests/integration/test_group_repositories.py::
    # test_attach_after_a_concurrent_group_delete_reports_not_found``.
    #
    # The row's other FK, ``fk_server_group_server_id_server``, is the half this
    # fake cannot see (see the module docstring) and is deliberately left
    # forgiving, so this pin says nothing about it.
    repo = FakeGroupRepository()

    with pytest.raises(GroupNotFoundError):
        await repo.attach(GroupId(uuid.uuid4()), _SERVER)

    assert repo.attachments == set()


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
    pack = _pack()
    await repo.add(pack)
    assignment = _assignment(pack.id)

    await repo.add_assignment(assignment)

    assignment.require_resource_pack = False
    loaded = await repo.get_assignment_by_server(_SERVER)
    (listed,) = await repo.list_assignments_for_pack(pack.id)
    assert loaded is not None
    loaded.require_resource_pack = False
    listed.resource_pack_prompt = "rewritten-by-list"
    stored = repo.assignments[_SERVER]
    assert stored.require_resource_pack is True
    assert stored.resource_pack_prompt is None


async def test_resource_pack_delete_while_assigned_reports_in_use() -> None:
    # ``fk_srv_rp_assignments_resource_pack_id_resource_packs`` is not
    # DEFERRABLE, so the adapter's DELETE is refused at statement end while an
    # assignment still references the pack, and the adapter now translates that
    # to ``ResourcePackInUseError`` (#2612). Pinned against the live FK in
    # ``tests/integration/test_resource_pack_repositories.py``; modelled here so
    # a use-case test driving the fake sees the same refusal.
    repo = FakeResourcePackRepository()
    pack = _pack()
    await repo.add(pack)
    await repo.add_assignment(_assignment(pack.id))

    with pytest.raises(ResourcePackInUseError):
        await repo.delete(pack.id)

    assert pack.id in repo.packs


async def test_resource_pack_assignment_to_missing_pack_reports_not_found() -> None:
    # The same FK in the opposite direction, where it means the opposite thing:
    # the adapter's assignment INSERT is refused because the ``resource_packs``
    # row it names is gone, which is not-found (404), not in-use (409) (#2784).
    # Pinned against the live FK in
    # ``tests/integration/test_resource_pack_repositories.py``; modelled here so
    # a use-case test driving the fake sees the same refusal.
    repo = FakeResourcePackRepository()

    with pytest.raises(ResourcePackNotFoundError):
        await repo.add_assignment(_assignment(ResourcePackId.new()))

    assert repo.assignments == {}


async def test_resource_pack_second_assignment_for_one_server_is_refused() -> None:
    # ``server_id`` alone is ``pk_server_resource_pack_assignments`` (migration
    # 0018), so a second assignment for a server that already has one is a
    # duplicate INSERT, not an upsert: PostgreSQL refuses it. No map entry names
    # the PK, so ``SqlAlchemyResourcePackRepository.add_assignment``'s own flush
    # re-raises the ``IntegrityError`` untranslated -- a 500 (that fall-through is
    # pinned in ``tests/servers/test_unit_of_work_translation.py::
    # test_resource_pack_add_assignment_reraises_unknown_violation``). Keying the
    # row in regardless made the fake an upsert, the forgiving direction: a caller
    # that adds without deleting first passes here and 500s in production (#2858).
    # ``AssignResourcePack`` deletes the existing row first, which is what keeps
    # the hole latent rather than live.
    #
    # Unlike its two neighbours above, this refusal is taken from the migration's
    # ``PrimaryKeyConstraint`` declaration rather than pinned against a live
    # database, and the asymmetry is deliberate: those two turn on *when* the FK
    # fires -- statement end rather than the unit of work's commit -- which only a
    # real statement settles. A PK has no such question. Measured on PostgreSQL 18
    # while reviewing PR #2888, all three duplicate shapes raise here -- a row
    # another session committed, one this session already flushed, and one first
    # SELECTed into this session's identity map -- and the ORM does not
    # short-circuit any of them with a ``FlushError``.
    repo = FakeResourcePackRepository()
    pack = _pack()
    await repo.add(pack)
    first = _assignment(pack.id)
    await repo.add_assignment(first)

    with pytest.raises(IntegrityError) as raised:
        await repo.add_assignment(_assignment(pack.id))

    # Read back through the adapter's own accessor rather than off ``orig``: the
    # constraint name is the whole payload of the shim the fake raises, and a
    # caller that translates reaches it this way, so the pin reddens if the shim's
    # shape drifts out from under it.
    assert _constraint_name(raised.value) == "pk_server_resource_pack_assignments"
    # The first row stands; the refused INSERT wrote nothing over it.
    assert repo.assignments[_SERVER].assigned_by == first.assigned_by


# -- FakeFileStore --


async def test_file_store_list_dir_on_an_unknown_directory_reports_not_found() -> None:
    # ``StorageFileStoreAdapter.list_dir`` translates Storage's ``NotFoundError``
    # into ``ServerFileNotFoundError``, and both Storage backends raise it for a
    # non-root path that lists nothing -- gone, a plain file, or reached through
    # one (Port.list_dir, #2394). Answering ``[]`` there instead is the forgiving
    # direction: ``_path_is_dir`` never reaches its not-found fallback, so every
    # caller that branches file-vs-directory takes the directory branch whatever
    # the test intended (#2867). Pinned against the live backends in
    # ``tests/storage/test_port_contract.py``.
    store = FakeFileStore()
    store.files["world/level.dat"] = b"x"

    with pytest.raises(ServerFileNotFoundError):
        await store.list_dir(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="nope"
        )

    # A seeded file is not a directory either, for the same reason.
    with pytest.raises(ServerFileNotFoundError):
        await store.list_dir(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="world/level.dat"
        )


@pytest.mark.parametrize("root", ["", "."])
async def test_file_store_list_dir_on_the_root_is_empty_not_a_miss(root: str) -> None:
    # The other half of the same contract: the ROOT always lists, empty included
    # -- an empty working set is empty, not missing -- so the refusal above must
    # not swallow it. BOTH spellings, because ``RelPath`` normalises ``""`` and
    # ``"."`` to the same empty ``parts`` (storage.domain.value_objects), and the
    # empty one is reachable rather than theoretical: ``?path=`` reaches ``ListDir``
    # verbatim (``path: Annotated[str, Query()] = "."`` in servers/api/files.py),
    # which hands it to this seam unmodified at rest.
    store = FakeFileStore()

    assert (
        await store.list_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path=root)
        == []
    )


async def test_file_store_list_dir_surfaces_a_subdirectory() -> None:
    # ``tests/storage/test_port_contract.py::test_list_dir_lists_entries`` pins this
    # listing against BOTH live backends: the parent of a nested file is one entry
    # with ``is_dir=True`` and size 0 -- fs lstats the real directory
    # (``_list_entries``), the object backend collapses the shared key prefix
    # (``_entries_at_level``) -- listed alongside the direct files. A fake that
    # drops every nested path and hardcodes ``is_dir=False`` answers ``[]`` here
    # and never produces a directory at all (#2886), which is the same forgiving
    # direction #2885 closed: a caller that branches on ``is_dir``, or that
    # enumerates subdirectories, passes here for a reason production cannot
    # reproduce.
    store = FakeFileStore()
    store.files["world/level.dat"] = b"abc"
    store.files["server.properties"] = b"k=v"

    entries = await store.list_dir(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path="."
    )

    assert {(e.name, e.is_dir) for e in entries} == {
        ("world", True),
        ("server.properties", False),
    }
    world = next(e for e in entries if e.name == "world")
    assert world.size == 0


async def test_file_store_make_dir_creates_a_directory_later_calls_see() -> None:
    # A ``make_dir`` that records nothing leaves the directory non-existent for
    # every later call, and since #2885's refusal that is a hard
    # ``ServerFileNotFoundError`` where production succeeds (#2886). Both backends
    # make the new directory observable: fs materializes a real one
    # (``FsStorage._make_dir``), and the object backend anchors the prefix with a
    # zero-byte ``.dir`` marker that ``_entries_at_level`` hides again
    # (``tests/storage/test_object_specifics.py``
    # ``::test_make_dir_writes_marker_and_dir_is_visible``). So the parent lists
    # it, listing it is EMPTY rather than the miss above, and the name is
    # occupied.
    store = FakeFileStore()
    store.files["server.properties"] = b"k=v"

    await store.make_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path="plugins")

    root_entries = await store.list_dir(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path="."
    )
    assert ("plugins", True) in {(e.name, e.is_dir) for e in root_entries}
    assert (
        await store.list_dir(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="plugins"
        )
        == []
    )
    assert (
        await store.path_exists(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="plugins"
        )
        is True
    )

    # The ROOT is the one path no directory is created UNDER: the object backend
    # returns before writing a marker (#1944, whose ``//.dir`` key the worker's
    # safeJoin rejects) and fs's ``exist_ok=True`` mkdir of the snapshot dir
    # itself is equally a no-op. So the listing must not gain a nameless entry.
    await store.make_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path=".")

    assert (
        await store.list_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path=".")
        == root_entries
    )
