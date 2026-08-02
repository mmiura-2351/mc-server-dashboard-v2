"""Integrity-constraint -> domain-error translation for the community adapters.

Unique violations from PostgreSQL are translated to the same typed domain error a
use-case pre-check raises, so a caller that reaches the constraint gets the
promised 409 instead of a raw ``IntegrityError`` (500). The four backstops are
``uq_community_name``, ``uq_role_community_name``,
``uq_membership_user_community`` and ``uq_resource_grant_user_resource`` (all
migration 0004).

Extracted from :mod:`mc_server_dashboard_api.community.adapters.unit_of_work`,
which is where the map lived while ``flush`` and ``commit`` were its only call
sites; a second kind of call site cannot reach it there, because the unit of work
imports the repositories and so the repositories cannot import it back. The
module mirrors ``servers/adapters/integrity.py``, which the servers context
extracted for the same reason.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from mc_server_dashboard_api.community.domain.errors import (
    CommunityAlreadyExistsError,
    MembershipAlreadyExistsError,
    ResourceGrantAlreadyExistsError,
    RoleAlreadyExistsError,
)

_COMMUNITY_NAME_CONSTRAINTS = frozenset({"uq_community_name"})
_ROLE_NAME_CONSTRAINTS = frozenset({"uq_role_community_name"})
_MEMBERSHIP_CONSTRAINTS = frozenset({"uq_membership_user_community"})
_RESOURCE_GRANT_CONSTRAINTS = frozenset({"uq_resource_grant_user_resource"})


def translate_integrity_error(exc: IntegrityError) -> None:
    """Raise the matching domain error for a known unique violation, else return.

    An unrecognised violation is left to the caller to re-raise as-is.
    """

    constraint = _constraint_name(exc)
    if constraint in _COMMUNITY_NAME_CONSTRAINTS:
        raise CommunityAlreadyExistsError(str(constraint)) from exc
    if constraint in _ROLE_NAME_CONSTRAINTS:
        raise RoleAlreadyExistsError(str(constraint)) from exc
    if constraint in _MEMBERSHIP_CONSTRAINTS:
        raise MembershipAlreadyExistsError(str(constraint)) from exc
    if constraint in _RESOURCE_GRANT_CONSTRAINTS:
        raise ResourceGrantAlreadyExistsError(str(constraint)) from exc


def _constraint_name(exc: IntegrityError) -> str | None:
    """Extract the violated constraint name from the wrapped driver error.

    The constraint name lives on the asyncpg ``UniqueViolationError`` underneath
    the SQLAlchemy wrapper (``exc.orig`` is the DBAPI shim; its ``__cause__`` is
    the asyncpg error).
    """

    for candidate in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None
