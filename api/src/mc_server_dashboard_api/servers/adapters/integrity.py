"""Integrity-constraint -> domain-error translation for the servers adapters.

Unique and foreign-key violations from PostgreSQL are translated to the same
typed domain error the use-case pre-checks raise, so a concurrent racer that
slips past a pre-read gets the same HTTP mapping instead of a raw
``IntegrityError`` (500).
``uq_server_community_name`` (migration 0005) is the name backstop;
``uq_server_game_port`` (migration 0009) and ``uq_server_bedrock_port``
(migration 0027) are the port backstops; ``uq_server_slug`` (migration 0016) is
the relay slug backstop; ``uq_schedule_server_id_name`` (migration 0029) is the
per-server schedule name backstop; ``uq_player_group_community_kind_name``
(migration 0012) is the per-community, per-kind group name backstop;
``fk_srv_rp_assignments_resource_pack_id_resource_packs`` (migration 0018) is
the resource-pack-in-use FK backstop (issue #1962);
``fk_group_player_group_id_player_group`` (migration 0012) is the
group-deleted-mid-edit backstop (issue #2583).

A *duplicate* racer conflicts (409); a *deleted* racer is gone, so the FK naming
the vanished parent row translates to that context's not-found error (404) --
the very error the use case's own pre-read would have raised had the delete
landed a moment earlier.

Shared by two kinds of call site, because *when* a violation surfaces depends on
the statement shape: an INSERT staged via ``session.add`` (create) flushes at
commit, so :class:`SqlAlchemyUnitOfWork` translates in ``commit``; an UPDATE
(re-port #311, slug rename #955, Bedrock allocation #1541, schedule rename
#1837) executes -- and violates -- immediately inside the transaction, so the
server and schedule repositories translate at their ``update`` execute sites.
The group write paths are a special case: ``SqlAlchemyGroupRepository.add``
flushes explicitly (the parent row must exist before child rows) and ``save``
flushes its replacement player rows rather than leave them for whichever
autoflush the caller happens to trigger next, so the violation surfaces at those
``flush()`` calls, not at commit -- the repository wraps both with the same
try/translate.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from mc_server_dashboard_api.servers.domain.errors import (
    GroupNameAlreadyExistsError,
    GroupNotFoundError,
    PortAlreadyTakenError,
    ResourcePackInUseError,
    ScheduleNameAlreadyExistsError,
    ServerNameAlreadyExistsError,
    SlugAlreadyTakenError,
)

_SERVER_NAME_CONSTRAINTS = frozenset({"uq_server_community_name"})
_PORT_CONSTRAINTS = frozenset({"uq_server_game_port", "uq_server_bedrock_port"})
_SLUG_CONSTRAINTS = frozenset({"uq_server_slug"})
_SCHEDULE_NAME_CONSTRAINTS = frozenset({"uq_schedule_server_id_name"})
_GROUP_NAME_CONSTRAINTS = frozenset({"uq_player_group_community_kind_name"})
_GROUP_MISSING_CONSTRAINTS = frozenset({"fk_group_player_group_id_player_group"})
_RESOURCE_PACK_FK_CONSTRAINTS = frozenset(
    {"fk_srv_rp_assignments_resource_pack_id_resource_packs"}
)


def translate_integrity_error(exc: IntegrityError) -> None:
    """Raise the matching domain error for a known constraint violation, else return."""

    constraint = _constraint_name(exc)
    if constraint in _SERVER_NAME_CONSTRAINTS:
        raise ServerNameAlreadyExistsError(str(constraint)) from exc
    if constraint in _PORT_CONSTRAINTS:
        raise PortAlreadyTakenError(str(constraint)) from exc
    if constraint in _SLUG_CONSTRAINTS:
        raise SlugAlreadyTakenError(str(constraint)) from exc
    if constraint in _SCHEDULE_NAME_CONSTRAINTS:
        raise ScheduleNameAlreadyExistsError(str(constraint)) from exc
    if constraint in _GROUP_NAME_CONSTRAINTS:
        raise GroupNameAlreadyExistsError(str(constraint)) from exc
    if constraint in _GROUP_MISSING_CONSTRAINTS:
        raise GroupNotFoundError(str(constraint)) from exc
    if constraint in _RESOURCE_PACK_FK_CONSTRAINTS:
        raise ResourcePackInUseError(str(constraint)) from exc


def _constraint_name(exc: IntegrityError) -> str | None:
    """Extract the violated constraint name from the wrapped driver error."""

    for candidate in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None
