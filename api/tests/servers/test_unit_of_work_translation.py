"""Unit tests for the servers adapters' integrity-error translation (no DB).

A duplicate server name, game port, Bedrock port, or slug that races past the
use-case pre-check surfaces as an IntegrityError; the adapters must translate
the unique-violation to the matching domain error
(``uq_server_community_name`` -> :class:`ServerNameAlreadyExistsError`,
``uq_server_game_port`` / ``uq_server_bedrock_port`` ->
:class:`PortAlreadyTakenError`, ``uq_server_slug`` ->
:class:`SlugAlreadyTakenError`) so the API returns 409, not 500. An unrelated
violation is re-raised untranslated.

Two call sites share the translation (adapters/integrity.py): the UnitOfWork's
``commit`` (an INSERT racer flushes at commit) and the server repository's
``update`` (an UPDATE racer violates at execute time, inside the transaction —
the re-port/slug-rename/Bedrock-allocation write path, issue #1541).

The asyncpg/SQLAlchemy stack is faked: a session whose ``commit`` (or
``execute``) raises a prebuilt IntegrityError carrying the violated constraint
name, mirroring the shape ``_constraint_name`` reads at runtime.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from mc_server_dashboard_api.servers.adapters.repositories import (
    SqlAlchemyServerRepository,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    PortAlreadyTakenError,
    ServerNameAlreadyExistsError,
    SlugAlreadyTakenError,
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
