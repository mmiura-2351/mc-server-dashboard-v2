"""Unique-constraint -> domain-error translation for the servers adapters.

Unique violations from PostgreSQL are translated to the same typed domain error
the use-case pre-checks raise, so a concurrent racer that slips past a pre-read
gets the same HTTP mapping (409) instead of a raw ``IntegrityError`` (500).
``uq_server_community_name`` (migration 0005) is the name backstop;
``uq_server_game_port`` (migration 0009) and ``uq_server_bedrock_port``
(migration 0027) are the port backstops; ``uq_server_slug`` (migration 0016) is
the relay slug backstop.

Shared by two call sites, because *when* a violation surfaces depends on the
statement shape: an INSERT staged via ``session.add`` (create) flushes at
commit, so :class:`SqlAlchemyUnitOfWork` translates in ``commit``; an UPDATE
(re-port #311, slug rename #955, Bedrock allocation #1541) executes -- and
violates -- immediately inside the transaction, so the server repository
translates at the ``update`` execute site.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from mc_server_dashboard_api.servers.domain.errors import (
    PortAlreadyTakenError,
    ServerNameAlreadyExistsError,
    SlugAlreadyTakenError,
)

_SERVER_NAME_CONSTRAINTS = frozenset({"uq_server_community_name"})
_PORT_CONSTRAINTS = frozenset({"uq_server_game_port", "uq_server_bedrock_port"})
_SLUG_CONSTRAINTS = frozenset({"uq_server_slug"})


def translate_integrity_error(exc: IntegrityError) -> None:
    """Raise the matching domain error for a known unique violation, else return."""

    constraint = _constraint_name(exc)
    if constraint in _SERVER_NAME_CONSTRAINTS:
        raise ServerNameAlreadyExistsError(str(constraint)) from exc
    if constraint in _PORT_CONSTRAINTS:
        raise PortAlreadyTakenError(str(constraint)) from exc
    if constraint in _SLUG_CONSTRAINTS:
        raise SlugAlreadyTakenError(str(constraint)) from exc


def _constraint_name(exc: IntegrityError) -> str | None:
    """Extract the violated constraint name from the wrapped driver error."""

    for candidate in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None
