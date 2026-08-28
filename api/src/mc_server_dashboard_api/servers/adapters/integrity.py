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
the resource-pack-in-use FK backstop (issue #1962) -- the DELETE direction only,
see below;
``fk_group_player_group_id_player_group`` (migration 0012) is the
group-deleted-mid-edit backstop (issue #2583);
``uq_server_plugin_server_rel`` (migration 0019) is the per-server plugin path
backstop; ``fk_server_group_group_id_player_group`` /
``fk_server_group_server_id_server`` (migration 0012) are the
attach-target-vanished backstops (issue #2612); and
``uq_group_player_group_uuid`` (migration 0012) is the interleaved-player-edit
backstop (issue #2613).

A *duplicate* racer conflicts (409); a *deleted* racer is gone, so the FK naming
the vanished parent row translates to that context's not-found error (404) --
the very error the use case's own pre-read would have raised had the delete
landed a moment earlier.

``uq_group_player_group_uuid`` is a third shape: neither caller duplicated
anything a pre-read could have caught, and neither is gone. Two player edits on
one group interleave, and because ``save`` replaces the player set wholesale the
loser's DELETE cannot see rows the winner committed after it ran, so the loser
re-inserts a pair that now exists. The delete-then-insert stays (the owner's
#2613 ruling: a 409 for a genuinely simultaneous edit of one group is acceptable
at this scale), so the constraint is translated to a conflict the loser can
retry -- its transaction rolls back whole, the winner's edit stands, and
re-reading the group and reapplying the edit succeeds.

The map below is **deliberately partial**. The issue #2583 audit walked every
named UNIQUE and FOREIGN KEY constraint in ``api/migrations/`` against it and
found further reachable-but-untranslated ones; each needed its own typed error
and its own decision, tracked as issues #2611, #2612 and #2613 rather than
guessed at here. A constraint's absence below is therefore not evidence that
violating it is unreachable.

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
try/translate. ``attach`` executes its INSERT for the same reason.

A DELETE is the third shape, and the one a map entry alone does not cover
(issue #2612): ``fk_srv_rp_assignments_resource_pack_id_resource_packs`` is the
schema's only non-``ON DELETE CASCADE`` FK and is not ``DEFERRABLE``, so
PostgreSQL refuses ``SqlAlchemyResourcePackRepository.delete`` at *statement*
end -- before the translating ``commit`` ever runs. Being in the map below is
therefore not evidence that a constraint is handled; the wrap has to sit on the
statement that raises.

That one constraint also fires in the opposite direction, where it means the
opposite thing (issue #2784): the *assignment INSERT* is refused because the
``resource_packs`` row it names is gone -- the not-found (404) every other
missing-parent FK above translates to, not "in use" (409). One name, two
conditions, and only the statement site tells them apart, so
``SqlAlchemyResourcePackRepository.add_assignment`` flushes its own staged row
and translates it through :func:`translate_assignment_insert_error`. The map
entry below therefore carries the DELETE direction's meaning, for ``delete``'s
wrap to read.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from mc_server_dashboard_api.servers.domain.errors import (
    GroupNameAlreadyExistsError,
    GroupNotFoundError,
    GroupPlayerEditConflictError,
    PluginAlreadyExistsError,
    PortAlreadyTakenError,
    ResourcePackInUseError,
    ResourcePackNotFoundError,
    ScheduleNameAlreadyExistsError,
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
    SlugAlreadyTakenError,
)

_SERVER_NAME_CONSTRAINTS = frozenset({"uq_server_community_name"})
_PORT_CONSTRAINTS = frozenset({"uq_server_game_port", "uq_server_bedrock_port"})
_SLUG_CONSTRAINTS = frozenset({"uq_server_slug"})
_SCHEDULE_NAME_CONSTRAINTS = frozenset({"uq_schedule_server_id_name"})
_GROUP_NAME_CONSTRAINTS = frozenset({"uq_player_group_community_kind_name"})
_GROUP_PLAYER_EDIT_CONSTRAINTS = frozenset({"uq_group_player_group_uuid"})
_GROUP_MISSING_CONSTRAINTS = frozenset(
    {
        "fk_group_player_group_id_player_group",
        "fk_server_group_group_id_player_group",
    }
)
_SERVER_MISSING_CONSTRAINTS = frozenset({"fk_server_group_server_id_server"})
_PLUGIN_PATH_CONSTRAINTS = frozenset({"uq_server_plugin_server_rel"})
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
    if constraint in _GROUP_PLAYER_EDIT_CONSTRAINTS:
        raise GroupPlayerEditConflictError(str(constraint)) from exc
    if constraint in _GROUP_MISSING_CONSTRAINTS:
        raise GroupNotFoundError(str(constraint)) from exc
    if constraint in _SERVER_MISSING_CONSTRAINTS:
        raise ServerNotFoundError(str(constraint)) from exc
    if constraint in _PLUGIN_PATH_CONSTRAINTS:
        raise PluginAlreadyExistsError(str(constraint)) from exc
    if constraint in _RESOURCE_PACK_FK_CONSTRAINTS:
        raise ResourcePackInUseError(str(constraint)) from exc


def translate_assignment_insert_error(exc: IntegrityError) -> None:
    """Translate a violation raised by the resource-pack *assignment* INSERT.

    Same constraint as the map's, opposite direction: here the pack the row names
    is gone, so it is not-found (404) rather than in use (409) (issue #2784).
    Anything else falls through to the shared translation.
    """

    constraint = _constraint_name(exc)
    if constraint in _RESOURCE_PACK_FK_CONSTRAINTS:
        raise ResourcePackNotFoundError(str(constraint)) from exc
    translate_integrity_error(exc)


def _constraint_name(exc: IntegrityError) -> str | None:
    """Extract the violated constraint name from the wrapped driver error."""

    for candidate in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None
