"""Translation of PostgreSQL unique-constraint violations to identity errors.

Shared by the UnitOfWork's commit path (staged inserts flushed at commit) and the
repository's eager Core UPDATE, so a duplicate username/email surfaces as the same
domain conflict error regardless of which write hits the constraint first.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from mc_server_dashboard_api.identity.domain.errors import (
    EmailAlreadyExistsError,
    UsernameAlreadyExistsError,
)

# Unique constraints/indexes on ``user`` (migration 0002) mapped to the domain
# error to raise when an insert/update violates them, so the duplicate race
# surfaces as the same error as the use case's pre-check.
_USERNAME_CONSTRAINTS = frozenset({"uq_user_username", "uq_user_username_lower"})
_EMAIL_CONSTRAINTS = frozenset({"uq_user_email"})


def translate_integrity_error(exc: IntegrityError) -> None:
    """Raise the matching domain error for a known unique violation, else return.

    The constraint name lives on the asyncpg ``UniqueViolationError`` underneath
    the SQLAlchemy wrapper (``exc.orig`` is the DBAPI shim; its ``__cause__`` is
    the asyncpg error). An unrecognised violation is left to the caller to
    re-raise as-is.
    """

    constraint = _constraint_name(exc)
    if constraint in _USERNAME_CONSTRAINTS:
        raise UsernameAlreadyExistsError(str(constraint)) from exc
    if constraint in _EMAIL_CONSTRAINTS:
        raise EmailAlreadyExistsError(str(constraint)) from exc


def _constraint_name(exc: IntegrityError) -> str | None:
    """Extract the violated constraint name from the wrapped driver error."""

    for candidate in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None
