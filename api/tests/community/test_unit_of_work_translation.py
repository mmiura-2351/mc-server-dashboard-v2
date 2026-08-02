"""Unit tests for the community integrity-error translation (no database).

ProvisionCommunity flushes mid-transaction (so the membership_role FK targets
exist before the join row is staged). A duplicate that races past the
get_by_name pre-check surfaces as an IntegrityError at *flush* time, not commit,
so the adapter must translate flush-time violations to the same domain duplicate
errors it raises for commit-time ones — otherwise the API returns 500 instead of
the promised 409 (Section 6.2).

A rename UPDATE has the other timing: it violates at its own ``execute``, one
statement before any commit, so the repositories translate there (issue #2611).
Both call-site kinds are covered here; the pins that need a live constraint are
in ``tests/integration/test_community_{repositories,roles_grants}.py``.

The asyncpg/SQLAlchemy stack is faked: a session whose ``flush`` (or ``execute``)
raises a prebuilt IntegrityError carrying the violated constraint name, mirroring
the shape ``_constraint_name`` reads at runtime.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

from mc_server_dashboard_api.community.adapters.repositories import (
    SqlAlchemyCommunityRepository,
    SqlAlchemyRoleRepository,
)
from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community, Role
from mc_server_dashboard_api.community.domain.errors import (
    CommunityAlreadyExistsError,
    MembershipAlreadyExistsError,
    RoleAlreadyExistsError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
    Permission,
    RoleId,
    RoleName,
)

_NOW = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)


class _FakeOrig(Exception):
    def __init__(self, constraint_name: str) -> None:
        super().__init__(constraint_name)
        self.constraint_name = constraint_name


def _integrity_error(constraint: str) -> IntegrityError:
    return IntegrityError("stmt", {}, _FakeOrig(constraint))


class _FakeSession:
    """Async session double: ``flush`` raises, ``rollback`` is recorded."""

    def __init__(self, error: IntegrityError) -> None:
        self._error = error
        self.rolled_back = False

    async def flush(self) -> None:
        raise self._error

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


def _uow_with_flush_error(constraint: str) -> tuple[SqlAlchemyUnitOfWork, _FakeSession]:
    session = _FakeSession(_integrity_error(constraint))
    uow = SqlAlchemyUnitOfWork(_FakeFactory(session))  # type: ignore[arg-type]
    uow._session = session  # type: ignore[assignment]
    return uow, session


async def test_flush_translates_community_name_violation() -> None:
    uow, session = _uow_with_flush_error("uq_community_name")
    with pytest.raises(CommunityAlreadyExistsError):
        await uow.flush()
    assert session.rolled_back is True


async def test_flush_translates_role_name_violation() -> None:
    uow, _ = _uow_with_flush_error("uq_role_community_name")
    with pytest.raises(RoleAlreadyExistsError):
        await uow.flush()


async def test_flush_translates_membership_violation() -> None:
    uow, _ = _uow_with_flush_error("uq_membership_user_community")
    with pytest.raises(MembershipAlreadyExistsError):
        await uow.flush()


async def test_flush_reraises_unknown_violation_untranslated() -> None:
    uow, _ = _uow_with_flush_error("uq_some_other_constraint")
    with pytest.raises(IntegrityError):
        await uow.flush()


# --- the rename UPDATE, which violates at its own execute (issue #2611) ------


class _FakeExecuteSession:
    """Async session double whose ``execute`` raises the prebuilt violation.

    No ``rollback``: the repositories deliberately do not roll back at the execute
    site — the enclosing UnitOfWork's ``__aexit__`` does it on the way out. A
    session double that offered one would hide a repository that called it.
    """

    def __init__(self, error: IntegrityError) -> None:
        self._error = error

    async def execute(self, _stmt: object) -> None:
        raise self._error


def _role(name: str = "Editor") -> Role:
    return Role(
        id=RoleId.new(),
        community_id=CommunityId.new(),
        name=RoleName(name),
        permissions={Permission("server:read")},
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=False,
    )


def _community(name: str = "guild") -> Community:
    return Community(
        id=CommunityId.new(),
        name=CommunityName(name),
        created_at=_NOW,
        updated_at=_NOW,
    )


async def test_role_update_translates_role_name_violation() -> None:
    session = _FakeExecuteSession(_integrity_error("uq_role_community_name"))
    repo = SqlAlchemyRoleRepository(session)  # type: ignore[arg-type]
    with pytest.raises(RoleAlreadyExistsError):
        await repo.update(_role())


async def test_role_update_reraises_unknown_violation_untranslated() -> None:
    session = _FakeExecuteSession(_integrity_error("uq_some_other_constraint"))
    repo = SqlAlchemyRoleRepository(session)  # type: ignore[arg-type]
    with pytest.raises(IntegrityError):
        await repo.update(_role())


async def test_community_update_translates_community_name_violation() -> None:
    session = _FakeExecuteSession(_integrity_error("uq_community_name"))
    repo = SqlAlchemyCommunityRepository(session)  # type: ignore[arg-type]
    with pytest.raises(CommunityAlreadyExistsError):
        await repo.update(_community())


async def test_community_update_reraises_unknown_violation_untranslated() -> None:
    session = _FakeExecuteSession(_integrity_error("uq_some_other_constraint"))
    repo = SqlAlchemyCommunityRepository(session)  # type: ignore[arg-type]
    with pytest.raises(IntegrityError):
        await repo.update(_community())
