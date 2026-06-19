"""Unit tests for the servers UnitOfWork integrity-error translation (no DB).

A duplicate server name, game port, or slug that races past the use-case
pre-check surfaces as an IntegrityError at commit time; the adapter must
translate the unique-violation to the matching domain error
(``uq_server_community_name`` -> :class:`ServerNameAlreadyExistsError`,
``uq_server_game_port`` -> :class:`PortAlreadyTakenError`, ``uq_server_slug``
-> :class:`SlugAlreadyTakenError`) so the API returns 409, not 500. An
unrelated violation is re-raised untranslated.

The asyncpg/SQLAlchemy stack is faked: a session whose ``commit`` raises a
prebuilt IntegrityError carrying the violated constraint name, mirroring the
shape ``_constraint_name`` reads at runtime.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from mc_server_dashboard_api.servers.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.servers.domain.errors import (
    ModAlreadyExistsError,
    PortAlreadyTakenError,
    ServerNameAlreadyExistsError,
    SlugAlreadyTakenError,
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


async def test_commit_translates_mod_sha256_violation() -> None:
    uow, session = _uow_with_commit_error("uq_mods_sha256_hash")
    with pytest.raises(ModAlreadyExistsError):
        await uow.commit()
    assert session.rolled_back is True


async def test_commit_reraises_unknown_violation_untranslated() -> None:
    uow, _ = _uow_with_commit_error("uq_some_other_constraint")
    with pytest.raises(IntegrityError):
        await uow.commit()
