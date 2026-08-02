"""Unit tests for the servers adapters' integrity-error translation (no DB).

A duplicate server name, game port, Bedrock port, slug, schedule name, or
group name that races past the use-case pre-check surfaces as an
IntegrityError; the adapters must translate the unique-violation to the
matching domain error
(``uq_server_community_name`` -> :class:`ServerNameAlreadyExistsError`,
``uq_server_game_port`` / ``uq_server_bedrock_port`` ->
:class:`PortAlreadyTakenError`, ``uq_server_slug`` ->
:class:`SlugAlreadyTakenError`, ``uq_schedule_server_id_name`` ->
:class:`ScheduleNameAlreadyExistsError`,
``uq_player_group_community_kind_name`` ->
:class:`GroupNameAlreadyExistsError`) so the API returns 409, not 500. An
unrelated violation is re-raised untranslated.

A concurrent *delete* is translated the same way: the FK a stale write violates
names the row that vanished, so it surfaces as the not-found error the use case's
own pre-read raises (``fk_group_player_group_id_player_group`` ->
:class:`GroupNotFoundError`, 404, issue #2583).

The call sites share the translation (adapters/integrity.py): the UnitOfWork's
``commit`` (an INSERT racer flushes at commit) and the server / schedule /
group repositories' ``update`` / ``add`` / ``save`` (an UPDATE racer violates at
execute time, inside the transaction — the re-port/slug-rename/Bedrock-allocation
write path, issue #1541, the schedule rename path, issue #1837, the group
create flush path, issue #2000, and the group player-edit flush path, issue
#2583).

The asyncpg/SQLAlchemy stack is faked: a session whose ``commit`` (or
``execute`` / ``flush``) raises a prebuilt IntegrityError carrying the violated
constraint name, mirroring the shape ``_constraint_name`` reads at runtime.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from mc_server_dashboard_api.servers.adapters.group_repository import (
    SqlAlchemyGroupRepository,
)
from mc_server_dashboard_api.servers.adapters.repositories import (
    SqlAlchemyServerRepository,
)
from mc_server_dashboard_api.servers.adapters.schedule_repository import (
    SqlAlchemyScheduleRepository,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    GroupNameAlreadyExistsError,
    GroupNotFoundError,
    PortAlreadyTakenError,
    ResourcePackInUseError,
    ScheduleNameAlreadyExistsError,
    ServerNameAlreadyExistsError,
    SlugAlreadyTakenError,
)
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.schedule import (
    Cadence,
    Schedule,
    ScheduleAction,
    ScheduleId,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)


class _FakeOrig(Exception):
    def __init__(self, constraint_name: str) -> None:
        super().__init__(constraint_name)
        self.constraint_name = constraint_name


def _integrity_error(constraint: str) -> IntegrityError:
    return IntegrityError("stmt", {}, _FakeOrig(constraint))


class _FakeSession:
    def __init__(self, error: IntegrityError) -> None:
        self._error = error
        self.rolled_back = False

    async def commit(self) -> None:
        raise self._error

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


def _uow_with_commit_error(
    constraint: str,
) -> tuple[SqlAlchemyUnitOfWork, _FakeSession]:
    session = _FakeSession(_integrity_error(constraint))
    uow = SqlAlchemyUnitOfWork(_FakeFactory(session))  # type: ignore[arg-type]
    uow._session = session  # type: ignore[assignment]
    return uow, session


async def test_commit_translates_server_name_violation() -> None:
    uow, session = _uow_with_commit_error("uq_server_community_name")
    with pytest.raises(ServerNameAlreadyExistsError):
        await uow.commit()
    assert session.rolled_back is True


async def test_commit_translates_game_port_violation() -> None:
    uow, session = _uow_with_commit_error("uq_server_game_port")
    with pytest.raises(PortAlreadyTakenError):
        await uow.commit()
    assert session.rolled_back is True


async def test_commit_translates_slug_violation() -> None:
    uow, session = _uow_with_commit_error("uq_server_slug")
    with pytest.raises(SlugAlreadyTakenError):
        await uow.commit()
    assert session.rolled_back is True


async def test_commit_reraises_unknown_violation_untranslated() -> None:
    uow, _ = _uow_with_commit_error("uq_some_other_constraint")
    with pytest.raises(IntegrityError):
        await uow.commit()


async def test_commit_translates_bedrock_port_violation() -> None:
    # issue #1541: the Bedrock UDP port shares the typed port error.
    uow, session = _uow_with_commit_error("uq_server_bedrock_port")
    with pytest.raises(PortAlreadyTakenError):
        await uow.commit()
    assert session.rolled_back is True


async def test_commit_translates_schedule_name_violation() -> None:
    # issue #1837: a schedule-create racer flushing at commit surfaces typed.
    uow, session = _uow_with_commit_error("uq_schedule_server_id_name")
    with pytest.raises(ScheduleNameAlreadyExistsError):
        await uow.commit()
    assert session.rolled_back is True


# --- repository UPDATE path (issue #1541) ------------------------------------
# An UPDATE violates its unique backstop at execute time, inside the
# transaction, so the repository translates there (commit is never reached).


class _FakeExecuteSession:
    def __init__(self, error: IntegrityError) -> None:
        self._error = error

    async def execute(self, stmt: object) -> None:
        raise self._error


def _repo_with_execute_error(constraint: str) -> SqlAlchemyServerRepository:
    session = _FakeExecuteSession(_integrity_error(constraint))
    return SqlAlchemyServerRepository(session)  # type: ignore[arg-type]


def _server_entity() -> Server:
    now = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.PAPER,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=now,
        updated_at=now,
        bedrock_port=19132,
        slug="amber-falcon-42",
    )


async def test_update_translates_bedrock_port_violation_at_execute() -> None:
    repo = _repo_with_execute_error("uq_server_bedrock_port")
    with pytest.raises(PortAlreadyTakenError):
        await repo.update(_server_entity())


async def test_update_translates_slug_violation_at_execute() -> None:
    repo = _repo_with_execute_error("uq_server_slug")
    with pytest.raises(SlugAlreadyTakenError):
        await repo.update(_server_entity())


async def test_update_reraises_unknown_violation_untranslated() -> None:
    repo = _repo_with_execute_error("uq_some_other_constraint")
    with pytest.raises(IntegrityError):
        await repo.update(_server_entity())


def _schedule_entity() -> Schedule:
    now = dt.datetime(2026, 7, 11, 12, 0, tzinfo=dt.timezone.utc)
    return Schedule(
        id=ScheduleId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
        name="nightly",
        action=ScheduleAction.START,
        cadence=Cadence.from_interval(3600),
        enabled=False,
        created_at=now,
        updated_at=now,
    )


async def test_schedule_update_translates_name_violation_at_execute() -> None:
    # issue #1837: a schedule-rename racer violates at execute time.
    session = _FakeExecuteSession(_integrity_error("uq_schedule_server_id_name"))
    repo = SqlAlchemyScheduleRepository(session)  # type: ignore[arg-type]
    with pytest.raises(ScheduleNameAlreadyExistsError):
        await repo.update(_schedule_entity())


async def test_schedule_update_reraises_unknown_violation_untranslated() -> None:
    session = _FakeExecuteSession(_integrity_error("uq_some_other_constraint"))
    repo = SqlAlchemyScheduleRepository(session)  # type: ignore[arg-type]
    with pytest.raises(IntegrityError):
        await repo.update(_schedule_entity())


# --- group commit path (issue #2000, rename) ----------------------------------
# A group rename (ORM attribute update) flushes at commit, so the UnitOfWork
# commit backstop must translate the constraint.


async def test_commit_translates_group_name_violation() -> None:
    # issue #2000: a group-rename racer flushing at commit surfaces typed.
    uow, session = _uow_with_commit_error("uq_player_group_community_kind_name")
    with pytest.raises(GroupNameAlreadyExistsError):
        await uow.commit()
    assert session.rolled_back is True


# --- group repository flush path (issue #2000, create) ------------------------
# SqlAlchemyGroupRepository.add flushes explicitly (the parent row must exist
# before child rows), so the violation surfaces at that flush, not at commit.


class _FakeFlushSession:
    """A session whose ``flush`` raises an IntegrityError; ``add`` is a no-op."""

    def __init__(self, error: IntegrityError) -> None:
        self._error = error

    def add(self, instance: object) -> None:
        pass

    async def flush(self) -> None:
        raise self._error


def _group_entity() -> PlayerGroup:
    return PlayerGroup(
        id=GroupId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=GroupName("vip"),
        kind=GroupKind.WHITELIST,
        players=[],
    )


async def test_group_add_translates_name_violation_at_flush() -> None:
    # issue #2000: a group-create racer violates at flush time.
    session = _FakeFlushSession(_integrity_error("uq_player_group_community_kind_name"))
    repo = SqlAlchemyGroupRepository(session)  # type: ignore[arg-type]
    with pytest.raises(GroupNameAlreadyExistsError):
        await repo.add(_group_entity())


async def test_group_add_reraises_unknown_violation_untranslated() -> None:
    session = _FakeFlushSession(_integrity_error("uq_some_other_constraint"))
    repo = SqlAlchemyGroupRepository(session)  # type: ignore[arg-type]
    with pytest.raises(IntegrityError):
        await repo.add(_group_entity())


# --- group repository save path (issue #2583, player edit) --------------------
# SqlAlchemyGroupRepository.save stages the replacement group_player rows and
# flushes them itself. A concurrent group delete makes those INSERTs violate
# fk_group_player_group_id_player_group, which means the group is gone: the same
# GroupNotFoundError (404) the use case's own pre-read raises.


class _FakeSaveSession:
    """A session shaped for ``save``: ``flush`` raises, the rest are no-ops.

    ``get`` returns ``None``, which is what production really does here, though
    not for the obvious reason. The natural guess is that ``_load_group`` leaves
    the row in the session's identity map, so a concurrent delete would leave
    ``get`` handing back a *stale* row -- but the identity map is **weak**, and
    ``get_by_id`` hydrates a framework-free ``PlayerGroup`` and drops the
    ``PlayerGroupModel``, so nothing holds it. Measured on PostgreSQL 18: after
    ``get_by_id`` the identity map is empty and ``get`` re-queries to ``None``;
    pin a strong reference to the row instead and the same call hands back the
    stale row. Either way the staged ``group_player`` INSERTs are what violate
    and the flush is where the translation must sit; the live-FK behaviour is
    pinned in ``tests/integration/test_group_repositories.py``.
    """

    def __init__(self, error: IntegrityError) -> None:
        self._error = error

    async def get(self, entity: object, ident: object) -> None:
        return None

    async def execute(self, stmt: object) -> None:
        return None

    def add(self, instance: object) -> None:
        pass

    async def flush(self) -> None:
        raise self._error


def _group_with_player() -> PlayerGroup:
    group = _group_entity()
    group.players = [Player(uuid.uuid4(), "alice")]
    return group


async def test_group_save_translates_player_fk_violation_at_flush() -> None:
    # issue #2583: the group was deleted mid-edit, so the staged player INSERTs
    # hit the FK; the caller gets not-found, not a raw IntegrityError (500).
    error = _integrity_error("fk_group_player_group_id_player_group")
    repo = SqlAlchemyGroupRepository(_FakeSaveSession(error))  # type: ignore[arg-type]
    with pytest.raises(GroupNotFoundError):
        await repo.save(_group_with_player())


async def test_group_save_translates_name_violation_at_flush() -> None:
    # issue #2583: save's own flush is also where a rename's UPDATE lands, so the
    # name backstop must survive the same wrapping.
    session = _FakeSaveSession(_integrity_error("uq_player_group_community_kind_name"))
    repo = SqlAlchemyGroupRepository(session)  # type: ignore[arg-type]
    with pytest.raises(GroupNameAlreadyExistsError):
        await repo.save(_group_with_player())


async def test_group_save_reraises_unknown_violation_untranslated() -> None:
    session = _FakeSaveSession(_integrity_error("uq_some_other_constraint"))
    repo = SqlAlchemyGroupRepository(session)  # type: ignore[arg-type]
    with pytest.raises(IntegrityError):
        await repo.save(_group_with_player())


async def test_commit_translates_group_player_fk_violation() -> None:
    # issue #2583: RenameGroup does not query after save, so its staged player
    # INSERTs reach commit instead — the same violation, the same typed error.
    uow, session = _uow_with_commit_error("fk_group_player_group_id_player_group")
    with pytest.raises(GroupNotFoundError):
        await uow.commit()
    assert session.rolled_back is True


# --- FK violation path (issue #1962) ------------------------------------------
# A concurrent assignment races past the delete pre-check; the FK violation on
# the resource_packs row delete surfaces as ResourcePackInUseError (409), not 500.


async def test_commit_translates_resource_pack_fk_violation() -> None:
    uow, session = _uow_with_commit_error(
        "fk_srv_rp_assignments_resource_pack_id_resource_packs"
    )
    with pytest.raises(ResourcePackInUseError):
        await uow.commit()
    assert session.rolled_back is True
