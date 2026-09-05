"""Integration tests for the player-group repository on PostgreSQL (issue #276).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The schema is created and torn down per
test via the real 0001-0012 migrations so the adapter runs against the documented
shape. A community and a server are seeded; the repository's CRUD, player upsert,
attach/detach, and the cross-direction listings are exercised end to end, plus the
``ON DELETE CASCADE`` from server and group deletion.

The concurrent-delete tests (issue #2583) live here rather than beside the other
group unit tests because they need a **real** flush: the bug is a live FK refusing
the staged ``group_player`` INSERTs, and an in-memory fake repository has no
constraints to violate, so it reports success where PostgreSQL raises
(issues #2557, #2549).

The issue #2924 pair is here for the first reason: the community FK it covers
is live only against PostgreSQL, and the use-case half also shows that nothing
in ``CreateGroup`` short-circuits the flush that raises it.

The issue #2613 tests are here for the same reason and one more: the writes they
cover are the ones with *nothing to insert*, so what has to be pinned is that
``save`` still refuses them, and that the interleaved player edit really does hit
``uq_group_player_group_uuid`` on a live index. Both racers are deterministic and
sleepless -- a repository that deletes the group as it hands it back, and a
session that lets a racer commit between ``save``'s DELETE and its flush.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.group_repository import (
    SqlAlchemyGroupRepository,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.groups import (
    AddPlayer,
    AttachGroup,
    CreateGroup,
    RemovePlayer,
    RenameGroup,
)
from mc_server_dashboard_api.servers.domain.errors import (
    CommunityNotFoundError,
    GroupNameAlreadyExistsError,
    GroupNotFoundError,
    GroupPlayerEditConflictError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import FakeFileStore

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 5, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _seed_community(engine: AsyncEngine) -> uuid.UUID:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with CommunityUnitOfWork(create_session_factory(engine)) as uow:
        await uow.communities.add(community)
        await uow.commit()
    return community.id.value


async def _seed_server(engine: AsyncEngine, community_id: uuid.UUID) -> uuid.UUID:
    server_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO server (id, community_id, name, mc_edition, mc_version, "
                "server_type, config, slug, desired_state, "
                "observed_state, created_at, updated_at) VALUES "
                "(:id, :cid, 'srv', 'java', '1.21.1', 'vanilla', "
                "'{}', :slug, 'stopped', 'stopped', :at, :at)"
            ),
            {
                "id": server_id,
                "cid": community_id,
                "slug": f"srv-{str(server_id)[:8]}-00",
                "at": _NOW,
            },
        )
    return server_id


def _group(
    community_id: uuid.UUID, players: list[Player], *, name: str = "admins"
) -> PlayerGroup:
    return PlayerGroup(
        id=GroupId.new(),
        community_id=CommunityId(community_id),
        name=GroupName(name),
        kind=GroupKind.OP,
        players=players,
    )


async def test_add_get_and_player_save(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    pid = uuid.uuid4()
    group = _group(community_id, [Player(pid, "alice")])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.groups.get_by_id(group.id)
    assert loaded is not None
    assert [(p.uuid, p.username) for p in loaded.players] == [(pid, "alice")]

    # Upsert the username and persist (delete-then-insert player set).
    loaded.upsert_player(Player(pid, "alice2"))
    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.save(loaded)
        await uow.commit()
    async with ServersUnitOfWork(factory) as uow:
        again = await uow.groups.get_by_id(group.id)
    assert again is not None
    assert again.players[0].username == "alice2"


async def test_add_group_with_players_round_trips(engine: AsyncEngine) -> None:
    # Regression: ``add`` must flush the parent player_group row before the
    # group_player children, or the child INSERT violates the FK. Persisting a
    # group that already carries players must round-trip the whole set.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    group = _group(community_id, [Player(p1, "alice"), Player(p2, "bob")])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.groups.get_by_id(group.id)
    assert loaded is not None
    assert {(p.uuid, p.username) for p in loaded.players} == {
        (p1, "alice"),
        (p2, "bob"),
    }


async def test_attach_detach_and_listings(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    server_id = await _seed_server(engine, community_id)
    factory = create_session_factory(engine)
    group = _group(community_id, [])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.groups.attach(group.id, ServerId(server_id))
        # Re-attach is idempotent.
        await uow.groups.attach(group.id, ServerId(server_id))
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        assert await uow.groups.is_attached(group.id, ServerId(server_id)) is True
        servers = await uow.groups.list_server_ids_for_group(group.id)
        groups = await uow.groups.list_groups_for_server(ServerId(server_id))
        op_groups = await uow.groups.list_groups_for_server_kind(
            ServerId(server_id), GroupKind.OP
        )
    assert [s.value for s in servers] == [server_id]
    assert [g.id for g in groups] == [group.id]
    assert [g.id for g in op_groups] == [group.id]

    async with ServersUnitOfWork(factory) as uow:
        assert await uow.groups.detach(group.id, ServerId(server_id)) is True
        await uow.commit()
    async with ServersUnitOfWork(factory) as uow:
        assert await uow.groups.is_attached(group.id, ServerId(server_id)) is False


async def test_delete_group_cascades_players_and_attachments(
    engine: AsyncEngine,
) -> None:
    community_id = await _seed_community(engine)
    server_id = await _seed_server(engine, community_id)
    factory = create_session_factory(engine)
    group = _group(community_id, [Player(uuid.uuid4(), "alice")])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.groups.attach(group.id, ServerId(server_id))
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.delete(group.id)
        await uow.commit()

    async with engine.connect() as conn:
        players = (
            await conn.execute(text("SELECT count(*) FROM group_player"))
        ).scalar_one()
        attachments = (
            await conn.execute(text("SELECT count(*) FROM server_group"))
        ).scalar_one()
    assert players == 0
    assert attachments == 0


async def test_deleting_server_cascades_attachment(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    server_id = await _seed_server(engine, community_id)
    factory = create_session_factory(engine)
    group = _group(community_id, [])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.groups.attach(group.id, ServerId(server_id))
        await uow.commit()

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM server WHERE id = :id"), {"id": server_id})

    async with engine.connect() as conn:
        attachments = (
            await conn.execute(text("SELECT count(*) FROM server_group"))
        ).scalar_one()
    assert attachments == 0
    # The group itself survives the server delete.
    async with ServersUnitOfWork(factory) as uow:
        assert await uow.groups.get_by_id(group.id) is not None


# --- concurrent group delete during a player edit (issue #2583) ---------------


class _DeleteOnLoadGroupRepository(SqlAlchemyGroupRepository):
    """A group repository that deletes the group right after handing it back.

    Reproduces the production interleave deterministically, with no sleeps: the
    use case's ``_load_group`` read succeeds, another request's ``DeleteGroup``
    commits on its own connection, and only then does ``save`` stage the
    replacement ``group_player`` rows against a parent that is gone.
    """

    def __init__(self, session: AsyncSession, engine: AsyncEngine) -> None:
        super().__init__(session)
        self._engine = engine

    async def get_by_id(self, group_id: GroupId) -> PlayerGroup | None:
        group = await super().get_by_id(group_id)
        async with self._engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM player_group WHERE id = :id"), {"id": group_id.value}
            )
        return group


class _RacingUnitOfWork(ServersUnitOfWork):
    """A servers UnitOfWork wired with :class:`_DeleteOnLoadGroupRepository`."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine
    ) -> None:
        super().__init__(session_factory)
        self._engine = engine

    async def __aenter__(self) -> _RacingUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self.groups = _DeleteOnLoadGroupRepository(self._session, self._engine)
        return self


async def test_save_after_concurrent_group_delete_reports_not_found(
    engine: AsyncEngine,
) -> None:
    # Since #2613 this stops at ``save``'s own re-read: the delete lands before the
    # save, so the row is already gone when the re-read looks and the not-found is
    # raised before a single group_player row is staged -- the FK is never reached.
    # What it pins is that a player-carrying edit of a deleted group is refused at
    # all. The FK at the flush needs a racer that commits *after* the re-read, and
    # is pinned by ``test_save_flush_after_a_racing_delete_reports_not_found``
    # (issue #2938).
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    group = _group(community_id, [Player(uuid.uuid4(), "alice")])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.groups.get_by_id(group.id)
        assert loaded is not None
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM player_group WHERE id = :id"), {"id": group.id.value}
            )
        loaded.upsert_player(Player(uuid.uuid4(), "bob"))
        with pytest.raises(GroupNotFoundError):
            await uow.groups.save(loaded)


async def test_save_after_concurrent_group_delete_without_players_reports_not_found(
    engine: AsyncEngine,
) -> None:
    # The other half of the branch above, pinned against a real flush for the same
    # reason: a fake asserting its own no-op establishes nothing about the
    # adapter, so both branches modelled by ``FakeGroupRepository.save``
    # (tests/servers/test_fake_repository_isolation.py) get one here.
    #
    # With the player set emptied there is no INSERT, so nothing violates the FK
    # that carries the not-found for the player-carrying branch (#2583) -- save
    # used to write nothing and pass silently, which told the caller the edit
    # succeeded (#2613). The load now re-asserts the row, so both branches report
    # the same not-found regardless of whether the group happened to have players.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    only_player = uuid.uuid4()
    group = _group(community_id, [Player(only_player, "alice")])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.groups.get_by_id(group.id)
        assert loaded is not None
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM player_group WHERE id = :id"), {"id": group.id.value}
            )
        loaded.remove_player(only_player)
        with pytest.raises(GroupNotFoundError):
            await uow.groups.save(loaded)


async def test_save_after_concurrent_name_take_reports_name_exists(
    engine: AsyncEngine,
) -> None:
    # The rename half of the same flush: save's delete-then-insert autoflushes
    # the pending name UPDATE, so uq_player_group_community_kind_name surfaces
    # inside save too and must stay translated (issue #2000).
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    group = _group(community_id, [], name="admins")

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.groups.get_by_id(group.id)
        assert loaded is not None
        # A racer takes the name the rename is heading for.
        async with ServersUnitOfWork(factory) as racer:
            await racer.groups.add(_group(community_id, [], name="moderators"))
            await racer.commit()
        loaded.name = GroupName("moderators")
        with pytest.raises(GroupNameAlreadyExistsError):
            await uow.groups.save(loaded)


# --- the create path's other parent: the community (issue #2924) -------------


async def test_add_after_concurrent_community_delete_reports_community_not_found(
    engine: AsyncEngine,
) -> None:
    # add's flush carries fk_player_group_community_id_community as well as the
    # name uniqueness: the community deleted since the caller's pre-read leaves
    # the player_group INSERT with no parent. Live FK, so this pins the
    # translation rather than a fake's opinion of it.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)

    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM community WHERE id = :id"), {"id": community_id}
        )

    async with ServersUnitOfWork(factory) as uow:
        with pytest.raises(CommunityNotFoundError):
            await uow.groups.add(_group(community_id, []))


async def test_create_group_reports_a_concurrent_community_delete_as_not_found(
    engine: AsyncEngine,
) -> None:
    # The reachable path. CreateGroup's only pre-read is the group name lookup,
    # which answers None whether or not the community is there, so nothing
    # short-circuits the INSERT: the use case really does reach the flush and
    # depends on the translation for its typed error. The community pre-read that
    # would have caught this is the authorization gate, one layer up.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)

    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM community WHERE id = :id"), {"id": community_id}
        )

    use_case = CreateGroup(uow=ServersUnitOfWork(factory))
    with pytest.raises(CommunityNotFoundError):
        await use_case(community_id=CommunityId(community_id), name="admins", kind="op")


async def test_add_player_reports_a_concurrent_group_delete_as_not_found(
    engine: AsyncEngine,
) -> None:
    # The reachable path: AddPlayer follows save with list_server_ids_for_group,
    # whose autoflush used to be where the untranslated IntegrityError (500) fired.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    group = _group(community_id, [Player(uuid.uuid4(), "alice")])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    use_case = AddPlayer(
        uow=_RacingUnitOfWork(factory, engine), file_store=FakeFileStore()
    )
    with pytest.raises(GroupNotFoundError):
        await use_case(
            community_id=CommunityId(community_id),
            group_id=group.id,
            player_uuid=uuid.uuid4(),
            username="bob",
        )


async def test_remove_player_reports_a_concurrent_group_delete_as_not_found(
    engine: AsyncEngine,
) -> None:
    # Same race on the removal side. The group keeps a second player so save
    # still stages an INSERT for the surviving one -- the emptied-set shape,
    # where there is nothing left to violate the FK, is the test below.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    doomed = uuid.uuid4()
    group = _group(community_id, [Player(doomed, "alice"), Player(uuid.uuid4(), "bob")])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    use_case = RemovePlayer(
        uow=_RacingUnitOfWork(factory, engine), file_store=FakeFileStore()
    )
    with pytest.raises(GroupNotFoundError):
        await use_case(
            community_id=CommunityId(community_id),
            group_id=group.id,
            player_uuid=doomed,
        )


# --- writes on a group a racer deleted, with nothing to insert (issue #2613) --


async def test_rename_group_reports_a_concurrent_group_delete_as_not_found(
    engine: AsyncEngine,
) -> None:
    # A group with no players: save stages no INSERT, so the FK that carries the
    # not-found for the player-carrying rename (#2583) never fires. The rename
    # used to report 200 having written nothing -- the behaviour differing purely
    # on whether the group happened to have players, which no caller can see.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    group = _group(community_id, [])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    use_case = RenameGroup(uow=_RacingUnitOfWork(factory, engine))
    with pytest.raises(GroupNotFoundError):
        await use_case(
            community_id=CommunityId(community_id),
            group_id=group.id,
            name="moderators",
        )


async def test_remove_last_player_reports_a_concurrent_group_delete_as_not_found(
    engine: AsyncEngine,
) -> None:
    # The same silent success reached the other way: removing the *last* player
    # empties the set, so save stages no INSERT and the FK never fires. The
    # request used to return a 0-player group for a group that no longer exists.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    only_player = uuid.uuid4()
    group = _group(community_id, [Player(only_player, "alice")])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    use_case = RemovePlayer(
        uow=_RacingUnitOfWork(factory, engine), file_store=FakeFileStore()
    )
    with pytest.raises(GroupNotFoundError):
        await use_case(
            community_id=CommunityId(community_id),
            group_id=group.id,
            player_uuid=only_player,
        )


async def test_remove_last_player_of_a_live_group_deletes_its_row(
    engine: AsyncEngine,
) -> None:
    # The other side of the branch above, and a gap the #2607 review found: with
    # no racer, emptying the player set must actually delete the group_player row.
    # Nothing pinned that -- ``save`` mutated to ``if not group.players: return``
    # survived the suite, because every other emptied-set test asserts a raised
    # error or a group that is gone anyway.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    only_player = uuid.uuid4()
    group = _group(community_id, [Player(only_player, "alice")])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    use_case = RemovePlayer(uow=ServersUnitOfWork(factory), file_store=FakeFileStore())
    result = await use_case(
        community_id=CommunityId(community_id),
        group_id=group.id,
        player_uuid=only_player,
    )

    assert result.players == []
    async with engine.connect() as conn:
        players = (
            await conn.execute(text("SELECT count(*) FROM group_player"))
        ).scalar_one()
    assert players == 0


class _RacingSession(AsyncSession):
    """A session that lets a racer commit between ``save``'s DELETE and its flush.

    ``save`` replaces the player set wholesale (delete-then-insert) and owns the
    flush that writes the replacement rows. Under READ COMMITTED the DELETE's
    snapshot is taken when *that statement* runs, so rows a racer commits after it
    are invisible to the DELETE and still there for the INSERT -- the interleave
    that violates ``uq_group_player_group_uuid``. Firing the racer from the
    explicit ``flush`` puts it exactly there with no sleeps: autoflush runs on the
    sync Session underneath, so ``save``'s own ``await flush()`` is the only one
    that reaches this override.
    """

    def __init__(
        self, *args: object, racer: Callable[[], Awaitable[None]], **kwargs: object
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._racer = racer
        self._raced = False

    async def flush(self, objects: Sequence[object] | None = None) -> None:
        if not self._raced:
            self._raced = True
            await self._racer()
        await super().flush(objects)


async def test_interleaved_player_edits_report_an_edit_conflict(
    engine: AsyncEngine,
) -> None:
    # Two player edits on one group that genuinely interleave: the loser's
    # wholesale DELETE runs first and matches nothing, the winner adds the player
    # and commits, and the loser then re-inserts the same
    # ``(group_id, player_uuid)`` pair onto the winner's committed row. Neither
    # caller did anything wrong, so the loser gets the typed conflict its route
    # maps to 409 rather than the untranslated IntegrityError (500) it used to.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    group = _group(community_id, [])
    contested = uuid.uuid4()

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    async def winner() -> None:
        async with ServersUnitOfWork(factory) as other:
            loaded = await other.groups.get_by_id(group.id)
            assert loaded is not None
            loaded.upsert_player(Player(contested, "alice"))
            await other.groups.save(loaded)
            await other.commit()

    racing_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=_RacingSession, racer=winner
    )
    async with ServersUnitOfWork(racing_factory) as loser:  # type: ignore[arg-type]
        loaded = await loser.groups.get_by_id(group.id)
        assert loaded is not None
        loaded.upsert_player(Player(contested, "alice"))
        with pytest.raises(GroupPlayerEditConflictError):
            await loser.groups.save(loaded)

    # The winner's edit stands; the loser's transaction wrote nothing.
    async with engine.connect() as conn:
        players = (
            await conn.execute(text("SELECT count(*) FROM group_player"))
        ).scalar_one()
    assert players == 1


# --- the FK at save's own flush, reached past the re-read (issue #2938) -------


async def test_save_flush_after_a_racing_delete_reports_not_found(
    engine: AsyncEngine,
) -> None:
    # ``save``'s flush of the replacement group_player rows is where
    # fk_group_player_group_id_player_group becomes the not-found (#2583), and no
    # live test reached it: #2613's re-read answers every racer that deletes before
    # the save, so dropping the constraint from ``_GROUP_MISSING_CONSTRAINTS``
    # reddened nothing. Firing the racer from ``_RacingSession.flush`` puts its
    # DELETE after the re-read has already found the row.
    #
    # The group starts empty so the loser's wholesale DELETE matches nothing and
    # takes no locks the racer's cascade would wait on; the one player added here
    # is what gives the flush an INSERT to carry into the FK check.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    group = _group(community_id, [])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    async def deleter() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM player_group WHERE id = :id"), {"id": group.id.value}
            )

    racing_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=_RacingSession, racer=deleter
    )
    async with ServersUnitOfWork(racing_factory) as loser:  # type: ignore[arg-type]
        loaded = await loser.groups.get_by_id(group.id)
        assert loaded is not None
        loaded.upsert_player(Player(uuid.uuid4(), "alice"))
        with pytest.raises(GroupNotFoundError) as raised:
            await loser.groups.save(loaded)

    # Which statement raised, not merely that something did: the re-read's own
    # not-found carries no cause, so a translated IntegrityError naming the FK is
    # what distinguishes the flush from it.
    cause = raised.value.__cause__
    assert isinstance(cause, IntegrityError)
    assert "fk_group_player_group_id_player_group" in str(cause)


# --- concurrent racers on the attach write (issue #2612) ----------------------


async def test_attach_of_an_already_attached_pair_is_a_no_op(
    engine: AsyncEngine,
) -> None:
    # attach used to read is_attached and then stage the row, so two concurrent
    # attaches of the same pair both passed the read and the loser violated
    # pk_server_group at an untranslated commit (500). PostgreSQL is now asked
    # for the no-op directly, which is the same answer the pre-check gave and the
    # only one that survives the race.
    #
    # That losing interleave sits *between* the old SELECT and its INSERT, inside
    # one method, so no test could enter it from outside -- which is why two
    # sequential attaches always looked safe. What this pins is the INSERT
    # itself: with ``on_conflict_do_nothing`` dropped it fails on the live
    # pk_server_group.
    community_id = await _seed_community(engine)
    server_id = await _seed_server(engine, community_id)
    factory = create_session_factory(engine)
    group = _group(community_id, [])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    # A racer holds the attachment before this session writes it.
    async with ServersUnitOfWork(factory) as racer:
        await racer.groups.attach(group.id, ServerId(server_id))
        await racer.commit()

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.attach(group.id, ServerId(server_id))
        await uow.commit()

    async with engine.connect() as conn:
        attachments = (
            await conn.execute(text("SELECT count(*) FROM server_group"))
        ).scalar_one()
    assert attachments == 1


async def test_attach_after_a_concurrent_group_delete_reports_not_found(
    engine: AsyncEngine,
) -> None:
    # fk_server_group_group_id_player_group names the parent row that vanished,
    # so the racer gets the not-found the use case's own pre-read would have
    # raised had the delete landed a moment earlier.
    community_id = await _seed_community(engine)
    server_id = await _seed_server(engine, community_id)
    factory = create_session_factory(engine)
    group = _group(community_id, [])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM player_group WHERE id = :id"), {"id": group.id.value}
            )
        with pytest.raises(GroupNotFoundError):
            await uow.groups.attach(group.id, ServerId(server_id))


async def test_attach_after_a_concurrent_server_delete_reports_not_found(
    engine: AsyncEngine,
) -> None:
    # The other end of the same row: fk_server_group_server_id_server names the
    # server that vanished, which _require_server reports as ServerNotFoundError.
    community_id = await _seed_community(engine)
    server_id = await _seed_server(engine, community_id)
    factory = create_session_factory(engine)
    group = _group(community_id, [])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM server WHERE id = :id"), {"id": server_id}
            )
        with pytest.raises(ServerNotFoundError):
            await uow.groups.attach(group.id, ServerId(server_id))


async def test_attach_group_reports_a_concurrent_group_delete_as_not_found(
    engine: AsyncEngine,
) -> None:
    # The reachable path: AttachGroup's _load_group succeeds, the group is
    # deleted, and the INSERT lands on the live FK. Both errors the route
    # already maps to 404, so no new status is introduced.
    community_id = await _seed_community(engine)
    server_id = await _seed_server(engine, community_id)
    factory = create_session_factory(engine)
    group = _group(community_id, [])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    use_case = AttachGroup(
        uow=_RacingUnitOfWork(factory, engine), file_store=FakeFileStore()
    )
    with pytest.raises(GroupNotFoundError):
        await use_case(
            community_id=CommunityId(community_id),
            group_id=group.id,
            server_id=ServerId(server_id),
        )
