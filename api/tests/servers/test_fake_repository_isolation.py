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
import io
import uuid
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
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
    InvalidFilePathError,
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
from mc_server_dashboard_api.storage.domain.value_objects import RelPath
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
    # between that pre-read and the write. The adapter's own existence re-read now
    # sees the missing row and raises ``GroupNotFoundError`` before anything is
    # staged (#2613).
    #
    # Before #2613, with players to write, the adapter staged ``group_player``
    # INSERTs whose FK to ``player_group.id`` had no parent and raised the same
    # error at its own flush (#2583, measured against PostgreSQL 18). Before #2583
    # moved that violation inside ``save``, it was recorded here as an unmodelled
    # divergence, and the load-bearing half of that reasoning was the *moment*:
    # the violation surfaced at whichever later flush the caller happened to
    # trigger, and a fake has no such flush to surface at. A typed domain error
    # raised at the call does land somewhere a fake can, so the fake now models it
    # (#2557). The argument stops there and does not reach the exception type: an
    # adapter that flushes inside its own call gives a fake the same moment for a
    # raw ``IntegrityError``, which is what
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
    # Like the branch above, the corresponding adapter path is pinned against a
    # real database rather than against the fake alone. ``FakeGroupRepository``
    # raises from a dict of its own, with no second connection to see the racer's
    # delete, so this assertion alone establishes nothing about the adapter; the
    # integration test makes its re-read observe the committed delete:
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
    # ``GroupNameAlreadyExistsError`` (#2000). Pinned against the live UNIQUE in
    # ``tests/integration/test_group_repositories.py::
    # test_add_after_concurrent_name_take_reports_name_exists``, as its rename
    # counterpart below is; until #2970 only the translation unit test stood
    # behind this site, and a fake session cannot show that the constraint is
    # reached at all. Keying on ``group.id`` alone was the forgiving direction:
    # two groups sharing the triple coexisted here, a state production cannot
    # hold (#2923).
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


@pytest.mark.parametrize("root", ["", ".", "./"])
async def test_file_store_list_dir_on_the_root_is_empty_not_a_miss(root: str) -> None:
    # The other half of the same contract: the ROOT always lists, empty included
    # -- an empty working set is empty, not missing -- so the refusal above must
    # not swallow it. EVERY spelling ``RelPath`` normalises to the same empty
    # ``parts`` (storage.domain.value_objects) -- ``"./"`` included, which the raw
    # key missed (issue #3067) -- and the empty one is reachable rather than
    # theoretical: ``?path=`` reaches ``ListDir`` verbatim (``path:
    # Annotated[str, Query()] = "."`` in servers/api/files.py), which hands it to
    # this seam unmodified at rest.
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


async def test_file_store_delete_dir_removes_the_subtree_later_calls_see() -> None:
    # A ``delete_dir`` that records nothing leaves the directory and every member
    # observable for the rest of the test, so create-then-delete-then-list passes
    # here while production reports the directory gone (#2972) -- the forgiving
    # direction again. Both backends take the whole subtree: fs ``shutil.rmtree``s
    # the resolved directory (``FsStorage._delete_dir``), and the object backend
    # deletes every key under the ``<dir>/`` prefix (``ObjectStorage.delete_dir``).
    # Pinned against the live backends in
    # ``tests/storage/test_port_contract.py::test_delete_dir_removes_subtree``.
    store = FakeFileStore()
    store.files["world/level.dat"] = b"a"
    store.files["world/region/r.dat"] = b"b"
    store.files["server.properties"] = b"keep"
    # A created EMPTY directory inside the subtree. No contract test states this
    # one: ``test_delete_dir_removes_subtree`` seeds files only. It follows from
    # the two implementations -- ``rmtree`` unlinks a nested empty directory like
    # any other member, and the object backend's prefix listing includes the
    # nested ``.dir`` marker #1125 anchors it with -- and it is the half of the
    # delete that the ``dirs`` bookkeeping is what models.
    await store.make_dir(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path="world/plugins"
    )

    await store.delete_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path="world")

    root_entries = await store.list_dir(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path="."
    )
    assert {(e.name, e.is_dir) for e in root_entries} == {("server.properties", False)}
    with pytest.raises(ServerFileNotFoundError):
        await store.list_dir(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="world"
        )
    for gone in ("world", "world/plugins", "world/region/r.dat"):
        assert (
            await store.path_exists(
                community_id=_COMMUNITY, server_id=_SERVER, rel_path=gone
            )
            is False
        )
    # A sibling outside the deleted subtree survives.
    assert (
        await store.read_file(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="server.properties"
        )
        == b"keep"
    )


async def test_file_store_delete_dir_removes_a_created_empty_directory() -> None:
    # The subtree the test above deletes holds FILES, so its gate is met by the
    # seeded-file half of ``_existing_subtree`` alone, and the ``make_dir`` half
    # was exercised only through ``export_dir`` (issue #3069): a regression that
    # stopped the gate consulting the records would make this exact call a 404
    # here while production deletes the directory. Measured against both live
    # backends through ``StorageFileStoreAdapter`` while implementing #3069 -- fs
    # ``rmtree``s the empty directory, the object backend's prefix listing finds
    # the ``.dir`` marker #1125 anchors it with -- and afterwards the name is free
    # and a listing misses, as for a populated directory.
    store = FakeFileStore()
    store.files["server.properties"] = b"keep"
    await store.make_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path="empty")

    await store.delete_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path="empty")

    with pytest.raises(ServerFileNotFoundError):
        await store.list_dir(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="empty"
        )
    assert (
        await store.path_exists(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="empty"
        )
        is False
    )


async def test_file_store_delete_dir_on_a_missing_directory_reports_not_found() -> None:
    # Both backends refuse rather than succeed silently: fs's ``_existing_dir``
    # gate raises ``NotFoundError`` and the object backend raises it on an empty
    # prefix listing, which ``StorageFileStoreAdapter.delete_dir`` surfaces as
    # ``ServerFileNotFoundError``. Pinned against the live backends in
    # ``tests/storage/test_port_contract.py::test_delete_missing_dir_is_not_found``.
    # A plain FILE at the name misses for the same reason it is not a listable
    # directory (#2885): ``_existing_dir`` answers False on it, and nothing sits
    # under ``server.properties/`` for the object backend to delete.
    store = FakeFileStore()
    store.files["server.properties"] = b"keep"

    with pytest.raises(ServerFileNotFoundError):
        await store.delete_dir(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="nope"
        )
    with pytest.raises(ServerFileNotFoundError):
        await store.delete_dir(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="server.properties"
        )

    assert (
        await store.read_file(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="server.properties"
        )
        == b"keep"
    )


async def test_file_store_rename_dir_moves_the_subtree_later_calls_see() -> None:
    # A ``rename_dir`` that records nothing leaves BOTH paths in whatever state
    # they were, so the move is invisible to every later call (#2972). Both
    # backends move the whole subtree: fs ``os.rename``s the directory
    # (``FsStorage._rename_dir``) and the object backend copies every key under
    # the ``<from>/`` prefix to ``<to>/`` before deleting the originals
    # (``ObjectStorage.rename_dir``). Pinned against the live backends in
    # ``tests/storage/test_port_contract.py::test_rename_dir_moves_subtree``,
    # which -- like the delete's -- seeds files only, so the created EMPTY
    # directory below is again derived rather than contract-pinned: ``os.rename``
    # carries it as part of the tree, and the object backend copies the nested
    # ``.dir`` marker like any other key.
    store = FakeFileStore()
    store.files["world/level.dat"] = b"a"
    store.files["world/region/r.dat"] = b"b"
    store.files["server.properties"] = b"keep"
    await store.make_dir(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path="world/plugins"
    )

    await store.rename_dir(
        community_id=_COMMUNITY,
        server_id=_SERVER,
        from_path="world",
        to_path="new_world",
    )

    entries = await store.list_dir(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path="new_world"
    )
    assert {(e.name, e.is_dir) for e in entries} == {
        ("level.dat", False),
        ("region", True),
        ("plugins", True),
    }
    assert (
        await store.read_file(
            community_id=_COMMUNITY,
            server_id=_SERVER,
            rel_path="new_world/region/r.dat",
        )
        == b"b"
    )
    assert (
        await store.list_dir(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="new_world/plugins"
        )
        == []
    )
    # The old directory is gone, subtree and all.
    with pytest.raises(ServerFileNotFoundError):
        await store.list_dir(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="world"
        )
    for gone in ("world", "world/plugins", "world/region/r.dat"):
        assert (
            await store.path_exists(
                community_id=_COMMUNITY, server_id=_SERVER, rel_path=gone
            )
            is False
        )
    # A sibling outside the renamed subtree survives.
    assert (
        await store.read_file(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="server.properties"
        )
        == b"keep"
    )


async def test_file_store_rename_dir_moves_a_created_empty_directory() -> None:
    # The rename sibling of the delete pin above, for the same reason: the subtree
    # renamed above holds files, so nothing pinned a source that exists only
    # through ``make_dir`` (issue #3069). Measured against both live backends
    # through ``StorageFileStoreAdapter`` while implementing #3069 -- fs
    # ``os.rename``s the empty directory, the object backend copies its ``.dir``
    # marker -- and afterwards the destination lists empty and the source's name
    # is free.
    store = FakeFileStore()
    await store.make_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path="empty")

    await store.rename_dir(
        community_id=_COMMUNITY,
        server_id=_SERVER,
        from_path="empty",
        to_path="moved",
    )

    assert (
        await store.list_dir(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="moved"
        )
        == []
    )
    assert (
        await store.path_exists(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="empty"
        )
        is False
    )


async def test_file_store_rename_dir_on_a_missing_source_reports_not_found() -> None:
    # The same refusal the delete makes, from the same two gates: fs's
    # ``_existing_dir`` on the SOURCE and the object backend's empty prefix
    # listing, surfaced by ``StorageFileStoreAdapter.rename_dir`` as
    # ``ServerFileNotFoundError`` -- the one error the ``FileStore.rename_dir``
    # docstring names. Pinned against the live backends in
    # ``tests/storage/test_port_contract.py::test_rename_missing_dir_is_not_found``.
    # ``rename_file`` in this fake already refuses its missing source; a
    # ``rename_dir`` that returns instead conjures a successful move of nothing.
    store = FakeFileStore()
    store.files["server.properties"] = b"keep"

    with pytest.raises(ServerFileNotFoundError):
        await store.rename_dir(
            community_id=_COMMUNITY,
            server_id=_SERVER,
            from_path="nope",
            to_path="dest",
        )
    # A plain FILE is not a directory source either.
    with pytest.raises(ServerFileNotFoundError):
        await store.rename_dir(
            community_id=_COMMUNITY,
            server_id=_SERVER,
            from_path="server.properties",
            to_path="dest",
        )

    assert (
        await store.path_exists(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="dest"
        )
        is False
    )


async def test_file_store_delete_file_on_a_missing_path_reports_not_found() -> None:
    # ``self.files.pop(rel_path, None)`` made a delete of a path the fake does not
    # hold a silent SUCCESS -- the file-side sibling of the directory ops #2972
    # closed, and the same forgiving direction: a use case that deletes a path it
    # never created passes here and 404s in production. Both backends refuse. fs
    # gates on ``_existing_file`` (``FsStorage._delete_file``) and the object
    # backend on ``head_object(key) is None`` (``ObjectStorage.delete_file``) --
    # the object delete is NOT idempotent at the SDK level, it heads the key
    # first -- and each raises ``NotFoundError``, which
    # ``StorageFileStoreAdapter.delete_file`` surfaces as
    # ``ServerFileNotFoundError``. That is the error ``FileStore.delete_file``
    # names, and the one ``Storage.delete_file`` demands in as many words: raise
    # "for a missing path so a no-op delete is not silently reported as a
    # success". Pinned against the live backends in
    # ``tests/storage/test_port_contract.py::test_delete_missing_file_is_not_found``.
    store = FakeFileStore()
    store.files["world/level.dat"] = b"a"
    store.files["server.properties"] = b"keep"

    with pytest.raises(ServerFileNotFoundError):
        await store.delete_file(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="nope"
        )
    # A DIRECTORY at the name misses too -- the mirror of the "a plain FILE is not
    # a directory source" half of #2972. Derived from the two implementations
    # rather than contract-pinned (no contract test deletes a directory through
    # ``delete_file``): ``_existing_file`` is an ``is_file`` check, so fs answers
    # False on a directory, and the object backend heads the key ``world``
    # itself, which the nested member does not write.
    with pytest.raises(ServerFileNotFoundError):
        await store.delete_file(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path="world"
        )

    # The refused deletes removed nothing.
    assert store.files == {"world/level.dat": b"a", "server.properties": b"keep"}


def _download_dir(store: FakeFileStore, rel_path: str) -> AsyncIterator[bytes]:
    return store.download_dir(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path=rel_path
    )


def _export_dir(store: FakeFileStore, rel_path: str) -> AsyncIterator[bytes]:
    return store.export_dir(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path=rel_path, extra=[]
    )


# Every gate case below is pinned on BOTH streams because production builds them
# from ONE body: ``StorageFileStoreAdapter.download_dir`` and ``.export_dir`` both
# return ``_download_dir_gen``, which differs only in the ``extra`` in-memory
# entries appended after the subtree. A rule that held for one and not the other
# would be a divergence this fake invented (issue #3034).
_DirZip = Callable[[FakeFileStore, str], AsyncIterator[bytes]]


async def _drain(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


@pytest.mark.parametrize("open_zip", [_download_dir, _export_dir])
async def test_file_store_dir_zip_on_an_unknown_directory_reports_not_found(
    open_zip: _DirZip,
) -> None:
    # Both dir-zip streams decided NOTHING about the path they were handed:
    # ``download_dir`` returned an empty generator for any ``rel_path`` and
    # ``export_dir`` zipped the whole tree for any ``rel_path``, so a use case that
    # downloads or exports a directory that does not exist passed here and 404s in
    # production -- the forgiving direction this module's docstring names, and the
    # same class as #2867, #2886 and #2887 (issue #3034).
    #
    # The real gate IS a listing: ``_download_dir_gen`` opens the pinned
    # working-set view and calls ``view.list_dir(_rel_path(rel_path))`` before the
    # zip starts, translating ``NotFoundError`` into ``ServerFileNotFoundError``.
    # Both backends miss there for a non-root path that lists nothing -- gone
    # (fs's ``iterdir`` ENOENT via ``_NOT_A_LISTABLE_DIR``; the object view's
    # ``if not objs and sub: raise NotFoundError``) -- or a plain file, which fs
    # reports as ENOTDIR through the same set and the object view sees as the key
    # prefix ``server.properties/`` listing nothing. Pinned against the live
    # backends in ``tests/storage/test_port_contract.py``.
    store = FakeFileStore()
    store.files["world/level.dat"] = b"x"
    store.files["server.properties"] = b"k=v"

    with pytest.raises(ServerFileNotFoundError):
        await _drain(open_zip(store, "nope"))

    # A seeded file is not a directory to zip either, for the same reason.
    with pytest.raises(ServerFileNotFoundError):
        await _drain(open_zip(store, "server.properties"))


@pytest.mark.parametrize("root", ["", ".", "./"])
@pytest.mark.parametrize("open_zip", [_download_dir, _export_dir])
async def test_file_store_dir_zip_on_the_root_streams_rather_than_missing(
    open_zip: _DirZip, root: str
) -> None:
    # The other half of the contract, and load-bearing rather than theoretical:
    # ``ExportServer`` always passes ``rel_path="."``
    # (servers/application/export_import.py), so a refusal that swallowed the root
    # would make every export a 404. Both views answer the root without a miss --
    # each guards its unpinned miss with ``if not rel_path.parts`` and the object
    # backend its pinned miss with ``and sub``. EVERY spelling, as the sibling
    # ``list_dir`` root pin above takes them: ``RelPath`` normalises ``""``,
    # ``"."`` and ``"./"`` to the same empty ``parts``.
    #
    # Draining without ``ServerFileNotFoundError`` IS the assertion here, not a
    # missing one. What a zip CONTAINS is pinned separately:
    # ``test_file_store_dir_zip_streams_the_subtree_as_production_walks_it`` below
    # pins a SUBTREE on both streams, and the ``ExportServer`` suite pins the ROOT,
    # reading the archive back.
    store = FakeFileStore()
    store.files["server.properties"] = b"k=v"

    await _drain(open_zip(store, root))


@pytest.mark.parametrize("alias", ["world", "world/", "./world", "world//"])
@pytest.mark.parametrize("open_zip", [_download_dir, _export_dir])
async def test_file_store_dir_zip_answers_every_spelling_of_a_directory(
    open_zip: _DirZip, alias: str
) -> None:
    # A directory that LISTS must not be a miss for the zip, under any spelling
    # production resolves to it: the gate is the same membership ``list_dir``,
    # ``delete_dir`` and ``rename_dir`` decide on, so a path they resolve and the
    # zip refuses would be a rule this fake invented (issue #3034). That membership
    # is decided on the ``RelPath``-canonical path, as ``StorageFileStoreAdapter``
    # decides it, so ``"./world"`` streams too (issue #3067). What each spelling's
    # archive CONTAINS is pinned with the rest of the per-method alias table below.
    store = FakeFileStore()
    store.files["world/level.dat"] = b"x"

    await _drain(open_zip(store, alias))


async def test_file_store_export_dir_zips_the_named_subtree() -> None:
    # ``export_dir`` ignored ``rel_path`` and zipped EVERY seeded file under the
    # arcname it was seeded with -- a content-fidelity divergence rather than a
    # refusal one (issue #3034). It is benign for ``ExportServer``, which only ever
    # passes ``"."``, but a subtree export would silently pass here against a fake
    # that zipped the whole tree.
    #
    # Production zips the subtree with arcnames relative to ``rel_path``:
    # ``_walk_files`` states it -- "the zip contains the subtree itself, not the
    # path leading to it" -- and ``test_file_store_adapter.py``'s
    # ``test_download_dir_streams_zip_of_subtree`` pins it against the live fs
    # backend. ``extra`` is appended after the subtree
    # (``test_export_dir_appends_extra_entries``).
    store = FakeFileStore()
    store.files["world/level.dat"] = b"level"
    store.files["world/region/r.dat"] = b"region"
    store.files["server.properties"] = b"k=v"

    blob = await store.export_dir(
        community_id=_COMMUNITY,
        server_id=_SERVER,
        rel_path="world",
        extra=[("export_metadata.json", b'{"format": 1}')],
    ).__anext__()

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        contents = {name: zf.read(name) for name in zf.namelist()}
    # The sibling outside the subtree is absent, and the members carry arcnames
    # relative to ``world`` rather than the paths they were seeded under.
    assert contents == {
        "level.dat": b"level",
        "region/r.dat": b"region",
        "export_metadata.json": b'{"format": 1}',
    }


async def test_file_store_export_dir_zips_a_created_empty_directory() -> None:
    # An EMPTY directory is a real one, not an unknown one: both backends list a
    # created directory as ``[]`` rather than missing
    # (``test_make_dir_creates_an_observable_empty_directory``, #1125), so the gate
    # must consult the ``make_dir`` records as well as the seeded files -- the same
    # pairing ``list_dir`` and ``_existing_subtree`` already make. The archive
    # carries only the ``extra`` entries, because production zips files and an
    # empty directory contributes none.
    store = FakeFileStore()
    await store.make_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path="backups")

    blob = await store.export_dir(
        community_id=_COMMUNITY,
        server_id=_SERVER,
        rel_path="backups",
        extra=[("export_metadata.json", b'{"format": 1}')],
    ).__anext__()

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert zf.namelist() == ["export_metadata.json"]


@pytest.mark.parametrize("open_zip", [_download_dir, _export_dir])
async def test_file_store_dir_zip_streams_the_subtree_as_production_walks_it(
    open_zip: _DirZip,
) -> None:
    # ``download_dir`` streamed NO bytes for a directory that exists, so a use case
    # that downloads one and asserts on the archive passed here after merely
    # draining it -- the forgiving direction again (issue #3069). Production builds
    # both streams from ``_download_dir_gen``, so the archive is pinned on both,
    # member for member, as ``StorageFileStoreAdapter`` writes it:
    #
    # - arcnames relative to ``rel_path``, and only what sits under it;
    # - in ``_walk_files``' order: each level is listed name-sorted (fs's
    #   ``_list_children``, the object backend's ``_entries_at_level``), its files
    #   are yielded as met and its subdirectories pushed onto a stack, so they are
    #   descended after the files and in REVERSE name order. Seeded here in an
    #   order that is neither that nor sorted-by-path, so neither the insertion
    #   order nor a plain sort reproduces it;
    # - files only: the walk skips ``is_dir`` entries, so a created empty directory
    #   contributes no member;
    # - deflated (``compression=zipfile.ZIP_DEFLATED``).
    #
    # Measured against BOTH live backends through ``StorageFileStoreAdapter`` while
    # implementing #3069 -- the committed adapter tests compare member SETS, so
    # none of them pins the order.
    store = FakeFileStore()
    store.files["a/y.txt"] = b"y"
    store.files["a/sub/w.txt"] = b"w"
    store.files["a/b.txt"] = b"b"
    store.files["a/sub2/v.txt"] = b"v"
    store.files["z.txt"] = b"outside"
    await store.make_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path="a/empty")

    blob = await _drain(open_zip(store, "a"))

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        members = [(info.filename, zf.read(info)) for info in zf.infolist()]
        compression = {info.compress_type for info in zf.infolist()}
    assert members == [
        ("b.txt", b"b"),
        ("y.txt", b"y"),
        ("sub2/v.txt", b"v"),
        ("sub/w.txt", b"w"),
    ]
    assert compression == {zipfile.ZIP_DEFLATED}


async def test_file_store_download_dir_zips_a_created_empty_directory() -> None:
    # The download sibling of the export pin on a created empty directory: an
    # empty directory is a real one, so what streams is a valid archive with no
    # members -- not the zero bytes this fake used to answer every directory with
    # (issue #3069). Measured against both live backends: a ``make_dir``-only
    # directory downloads as an empty zip.
    store = FakeFileStore()
    await store.make_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path="backups")

    blob = await _drain(_download_dir(store, "backups"))

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert zf.namelist() == []


# -- FakeFileStore: production's path rule, on every method (issue #3067) --
#
# ``StorageFileStoreAdapter`` builds a ``RelPath`` from every path it is handed
# (``_rel_path``) before Storage sees it. ``RelPath`` normalises away ``.``
# components and redundant separators, so each spelling below reaches both
# backends as the canonical path it names, and it refuses traversal, which the
# adapter surfaces as ``InvalidFilePathError`` before Storage is reached at all.
# A fake that decides on the raw key instead refuses the aliases -- a false red
# rather than a false green, but the infidelity that makes a correct use case look
# broken -- and reports traversal as a miss. The sibling fake in ``test_files.py``
# was brought onto the rule by #2887 / #2975; these pin it here, one row per
# method, because a method left on the raw key is a second path rule.

_SPELLINGS = [
    pytest.param(lambda path: "./" + path, id="dot-prefix"),
    pytest.param(lambda path: path + "/", id="trailing-slash"),
    pytest.param(lambda path: path.replace("/", "//", 1), id="doubled-separator"),
]


def _world(spell: Callable[[str], str] = lambda path: path) -> FakeFileStore:
    """A small working set, every seed typed through ``spell``."""

    store = FakeFileStore()
    store.files[spell("world/level.dat")] = b"level"
    store.files[spell("world/region/r.dat")] = b"region"
    store.files[spell("server.properties")] = b"k=v"
    store.dirs.add(spell("world/plugins"))
    return store


def _held(store: FakeFileStore) -> tuple[list[tuple[str, bytes]], list[str]]:
    """What the store holds, under the canonical path each entry names.

    A sorted LIST rather than a dict or set, so a second spelling of one path
    shows up as a second entry instead of collapsing into the first.
    """

    return (
        sorted((RelPath(path).value, data) for path, data in store.files.items()),
        sorted(RelPath(path).value for path in store.dirs),
    )


_PathCall = Callable[[FakeFileStore, str], Awaitable[object]]


async def _read_file(store: FakeFileStore, path: str) -> object:
    return await store.read_file(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path=path
    )


async def _open_file_stream(store: FakeFileStore, path: str) -> object:
    return await _drain(
        store.open_file_stream(
            community_id=_COMMUNITY, server_id=_SERVER, rel_path=path
        )
    )


async def _path_exists(store: FakeFileStore, path: str) -> object:
    return await store.path_exists(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path=path
    )


async def _write_file(store: FakeFileStore, path: str) -> object:
    await store.write_file(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path=path, content=b"new"
    )
    return None


async def _delete_file(store: FakeFileStore, path: str) -> object:
    await store.delete_file(community_id=_COMMUNITY, server_id=_SERVER, rel_path=path)
    return None


async def _rename_file_from(store: FakeFileStore, path: str) -> object:
    await store.rename_file(
        community_id=_COMMUNITY,
        server_id=_SERVER,
        from_path=path,
        to_path="moved.dat",
    )
    return None


async def _rename_file_to(store: FakeFileStore, path: str) -> object:
    await store.rename_file(
        community_id=_COMMUNITY,
        server_id=_SERVER,
        from_path="server.properties",
        to_path=path,
    )
    return None


async def _rename_file_from_nothing_to(store: FakeFileStore, path: str) -> object:
    await store.rename_file(
        community_id=_COMMUNITY, server_id=_SERVER, from_path="ghost", to_path=path
    )
    return None


async def _list_dir(store: FakeFileStore, path: str) -> object:
    return await store.list_dir(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path=path
    )


async def _delete_dir(store: FakeFileStore, path: str) -> object:
    await store.delete_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path=path)
    return None


async def _rename_dir_from(store: FakeFileStore, path: str) -> object:
    await store.rename_dir(
        community_id=_COMMUNITY, server_id=_SERVER, from_path=path, to_path="moved"
    )
    return None


async def _rename_dir_to(store: FakeFileStore, path: str) -> object:
    await store.rename_dir(
        community_id=_COMMUNITY,
        server_id=_SERVER,
        from_path="world/region",
        to_path=path,
    )
    return None


async def _rename_dir_from_nothing_to(store: FakeFileStore, path: str) -> object:
    await store.rename_dir(
        community_id=_COMMUNITY, server_id=_SERVER, from_path="ghost", to_path=path
    )
    return None


async def _make_dir(store: FakeFileStore, path: str) -> object:
    await store.make_dir(community_id=_COMMUNITY, server_id=_SERVER, rel_path=path)
    return None


async def _zip_members(stream: AsyncIterator[bytes]) -> list[tuple[str, bytes]]:
    # Members rather than bytes: each entry carries its write time, so two archives
    # of one subtree differ byte-for-byte across a clock second.
    with zipfile.ZipFile(io.BytesIO(await _drain(stream))) as zf:
        return [(info.filename, zf.read(info)) for info in zf.infolist()]


async def _download_dir_members(store: FakeFileStore, path: str) -> object:
    return await _zip_members(_download_dir(store, path))


async def _export_dir_members(store: FakeFileStore, path: str) -> object:
    return await _zip_members(_export_dir(store, path))


async def _validate_rel_path(store: FakeFileStore, path: str) -> object:
    store.validate_rel_path(path)
    return None


async def _retain_if_changed(store: FakeFileStore, path: str) -> object:
    await store.retain_if_changed(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path=path
    )
    return None


async def _list_versions(store: FakeFileStore, path: str) -> object:
    return await store.list_versions(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path=path
    )


async def _read_version(store: FakeFileStore, path: str) -> object:
    return await store.read_version(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path=path, version_id="v1"
    )


async def _rollback(store: FakeFileStore, path: str) -> object:
    await store.rollback(
        community_id=_COMMUNITY, server_id=_SERVER, rel_path=path, version_id="v1"
    )
    return None


# Every method that decides something about a path, each against a path that
# EXISTS in ``_world()`` (or, for a destination, a free one), so the canonical
# spelling is a hit and an alias that misses is a failure rather than a match. Each
# path has a ``/`` in it, so every spelling in ``_SPELLINGS`` differs from it.
_ANSWERING_CALLS = [
    pytest.param(_read_file, "world/level.dat", id="read_file"),
    pytest.param(_open_file_stream, "world/level.dat", id="open_file_stream"),
    pytest.param(_path_exists, "world/level.dat", id="path_exists-file"),
    pytest.param(_path_exists, "world/region", id="path_exists-dir"),
    pytest.param(_path_exists, "world/plugins", id="path_exists-created-dir"),
    pytest.param(_write_file, "world/level.dat", id="write_file-overwrite"),
    pytest.param(_write_file, "world/new.txt", id="write_file-new"),
    pytest.param(_delete_file, "world/level.dat", id="delete_file"),
    pytest.param(_rename_file_from, "world/level.dat", id="rename_file-source"),
    pytest.param(_rename_file_to, "world/moved.dat", id="rename_file-destination"),
    pytest.param(_list_dir, "world/region", id="list_dir"),
    pytest.param(_list_dir, "world/plugins", id="list_dir-created-dir"),
    pytest.param(_delete_dir, "world/region", id="delete_dir"),
    pytest.param(_delete_dir, "world/plugins", id="delete_dir-created-dir"),
    pytest.param(_rename_dir_from, "world/region", id="rename_dir-source"),
    pytest.param(_rename_dir_from, "world/plugins", id="rename_dir-created-source"),
    pytest.param(_rename_dir_to, "archive/region", id="rename_dir-destination"),
    pytest.param(_make_dir, "world/new", id="make_dir"),
    pytest.param(_make_dir, "world/plugins", id="make_dir-existing"),
    pytest.param(_download_dir_members, "world/region", id="download_dir"),
    pytest.param(_export_dir_members, "world/region", id="export_dir"),
]


@pytest.mark.parametrize("spell", _SPELLINGS)
@pytest.mark.parametrize(("call", "path"), _ANSWERING_CALLS)
async def test_file_store_answers_every_spelling_of_a_path(
    call: _PathCall, path: str, spell: Callable[[str], str]
) -> None:
    # An alias answers exactly what its canonical path answers, and leaves the
    # store exactly as the canonical path does. The comparison is on the RAW
    # containers: a write, rename or ``make_dir`` through an alias lands under the
    # canonical key -- or on the entry already there -- never under the caller's
    # spelling, because production holds one entry per canonical path and a test
    # that reads ``store.files[...]`` back reads it by that name.
    canonical, aliased = _world(), _world()

    expected = await call(canonical, path)

    assert await call(aliased, spell(path)) == expected
    assert (aliased.files, aliased.dirs) == (canonical.files, canonical.dirs)


@pytest.mark.parametrize("spell", _SPELLINGS)
@pytest.mark.parametrize(("call", "path"), _ANSWERING_CALLS)
async def test_file_store_answers_a_path_seeded_under_an_alias(
    call: _PathCall, path: str, spell: Callable[[str], str]
) -> None:
    # The stored-key half of the same rule. ``files`` and ``dirs`` are typed by
    # hand, so a seed carries the same aliases a caller does; canonicalising only
    # the lookup would leave an alias-seeded entry unreachable under the name
    # production resolves it to. A mutation acts on the entry that EXISTS, under
    # whatever spelling it was seeded, so the store never holds two spellings of one
    # path -- ``_held`` keeps a second one visible rather than collapsing it.
    canonical, aliased = _world(), _world(spell)

    expected = await call(canonical, path)

    assert await call(aliased, path) == expected
    assert _held(aliased) == _held(canonical)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(_validate_rel_path, id="validate_rel_path"),
        pytest.param(_read_file, id="read_file"),
        pytest.param(_open_file_stream, id="open_file_stream"),
        pytest.param(_path_exists, id="path_exists"),
        pytest.param(_write_file, id="write_file"),
        pytest.param(_retain_if_changed, id="retain_if_changed"),
        pytest.param(_delete_file, id="delete_file"),
        pytest.param(_rename_file_from, id="rename_file-source"),
        pytest.param(_rename_file_to, id="rename_file-destination"),
        pytest.param(_rename_file_from_nothing_to, id="rename_file-before-a-miss"),
        pytest.param(_list_dir, id="list_dir"),
        pytest.param(_delete_dir, id="delete_dir"),
        pytest.param(_rename_dir_from, id="rename_dir-source"),
        pytest.param(_rename_dir_to, id="rename_dir-destination"),
        pytest.param(_rename_dir_from_nothing_to, id="rename_dir-before-a-miss"),
        pytest.param(_make_dir, id="make_dir"),
        pytest.param(_download_dir_members, id="download_dir"),
        pytest.param(_export_dir_members, id="export_dir"),
        pytest.param(_list_versions, id="list_versions"),
        pytest.param(_read_version, id="read_version"),
        pytest.param(_rollback, id="rollback"),
    ],
)
async def test_file_store_refuses_traversal_as_production_does(
    call: _PathCall,
) -> None:
    # ``RelPath`` refuses a ``..`` component and ``_rel_path`` surfaces it as
    # ``InvalidFilePathError`` on EVERY adapter method, before Storage is reached,
    # so nothing is looked up and nothing is changed. The raw key reported a miss,
    # or wrote the traversal in as a key. The ``-before-a-miss`` rows pin the
    # ORDER for a destination: the adapter builds both paths as the arguments of
    # one Storage call, so an unusable destination is refused even when the source
    # would have missed. The stream rows are drained, since the adapter builds the
    # ``RelPath`` inside the generator body.
    store = _world()
    files, dirs = dict(store.files), set(store.dirs)

    with pytest.raises(InvalidFilePathError):
        await call(store, "../escape")

    assert (store.files, store.dirs) == (files, dirs)


@pytest.mark.parametrize("root", ["", ".", "./"])
async def test_file_store_path_exists_answers_the_root_under_every_spelling(
    root: str,
) -> None:
    # The root is always occupied, and every spelling ``RelPath`` normalises to
    # empty ``parts`` is the root. The listing and both dir-zip streams take the
    # same three spellings in their root pins above.
    #
    # The file methods get no root row here: a READ, a delete or a rename source
    # at the root misses whether the key is raw or canonical, so a pin would redden
    # for nothing. ``delete_dir`` and ``rename_dir`` get none either -- the
    # backends disagree at the root (fs ``rmtree``s or renames the snapshot
    # directory itself, the object backend loops over every key), so there is no
    # single production behaviour to pin (#2923's rule).
    store = FakeFileStore()

    assert await _path_exists(store, root) is True


@pytest.mark.parametrize("root", ["", ".", "./"])
async def test_file_store_make_dir_at_the_root_records_nothing(root: str) -> None:
    # ``make_dir`` of the root is a no-op in both backends (the object backend
    # returns before writing the ``//.dir`` marker #1944 names, fs's
    # ``exist_ok=True`` mkdir of the snapshot directory changes nothing), under
    # every spelling of it. A record for ``"./"`` would list as a nameless ``.``
    # directory at the root.
    store = FakeFileStore()

    await _make_dir(store, root)

    assert store.dirs == set()
    assert await _list_dir(store, ".") == []


@pytest.mark.parametrize("root", ["", ".", "./"])
async def test_file_store_write_file_refuses_the_root(root: str) -> None:
    # The root names a directory, not a file, and both backends refuse to write it
    # with ``PathTraversalError("rel_path must name a file, not the root")``
    # (``FsStorage._write_file``, ``ObjectStorage.write_file``, issue #542), which
    # ``StorageFileStoreAdapter.write_file`` surfaces as ``InvalidFilePathError``.
    # Once every spelling of the root is one canonical path, a fake that stored it
    # would hold a FILE named ``.`` -- the forgiving direction.
    store = FakeFileStore()

    with pytest.raises(InvalidFilePathError):
        await _write_file(store, root)

    assert store.files == {}
    assert store.writes == []


@pytest.mark.parametrize(
    ("seed", "call", "path"),
    [
        pytest.param(
            lambda store: store.files.update(
                {"world/level.dat": b"a", "./world/level.dat": b"b"}
            ),
            _read_file,
            "world/level.dat",
            id="files",
        ),
        pytest.param(
            lambda store: store.dirs.update({"world/plugins", "./world/plugins/"}),
            _list_dir,
            "world/plugins",
            id="dirs",
        ),
    ],
)
async def test_file_store_refuses_one_path_seeded_under_two_spellings(
    seed: Callable[[FakeFileStore], None], call: _PathCall, path: str
) -> None:
    # Canonicalising the stored keys can make two seeds name one path. That is a
    # SEEDING MISTAKE, not a state to model -- production holds ONE entry at a
    # canonical path -- so it is refused loudly rather than letting one seed win by
    # insertion or hash order, which would read as a flake (the guard #3032 gave
    # the sibling fake in ``test_files.py``).
    store = FakeFileStore()
    seed(store)

    with pytest.raises(AssertionError, match="two spellings"):
        await call(store, path)
