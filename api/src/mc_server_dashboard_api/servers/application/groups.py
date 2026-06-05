"""Player-group use cases: CRUD, player edits, attachment, and file sync (#276).

These run after the route's authorization dependency admitted the caller, so they
assume an authorized member and only do the group work. Groups are
community-scoped; a group of kind ``op`` feeds a server's ``ops.json`` and kind
``whitelist`` feeds ``whitelist.json``.

**Sync posture (issue #276, option a).** Any change that affects an attached
server's authoritative player file — attach, detach, a player add/remove on an
attached group — regenerates that server's ``ops.json`` / ``whitelist.json``
through the :class:`FileStore` at-rest write seam (versioned). Only **at-rest**
servers are written; a running or otherwise unsettled server is left pending and
picks up the updated authoritative copy on its next natural hydrate (which always
ships the authoritative working set). The file is the union-merge of every
attached group of that kind, deterministically ordered by uuid, so it is byte-
stable diff-to-diff. The merge is total (no group of that kind attached → an
empty list, which clears the file).

**Partial-failure posture (PM ruling).** When a single group change touches
*several* attached servers (delete a group, add/remove a player), the file
fan-out runs after the DB commit and is **best-effort**: a per-server write
failure is WARN-logged (server id + group id + error) and the loop continues, so
one failing server does not strand the rest. The failed at-rest server is left
stale; a *write* failure is **not** healed by the next hydrate (hydrate only
covers servers that were not at-rest at sync time). The operator repair is to
re-trigger the sync — re-attach the group to that server, or edit the group again
— which reruns the fan-out.

Cross-community safety mirrors the servers use cases: a group or server whose
``community_id`` differs from the path community is reported not-found
(:class:`GroupNotFoundError` / :class:`ServerNotFoundError`), leaking no
cross-community existence signal (FR-COMM-3).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.errors import (
    GroupAttachmentNotFoundError,
    GroupNameAlreadyExistsError,
    GroupNotFoundError,
    InvalidGroupKindError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileStore
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
    merge_players,
    render_ops_json,
    render_whitelist_json,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)

_logger = logging.getLogger(__name__)


def _parse_kind(kind: str) -> GroupKind:
    try:
        return GroupKind(kind)
    except ValueError as exc:
        raise InvalidGroupKindError(kind) from exc


async def _load_group(
    uow: UnitOfWork, community_id: CommunityId, group_id: GroupId
) -> PlayerGroup:
    group = await uow.groups.get_by_id(group_id)
    if group is None or group.community_id != community_id:
        raise GroupNotFoundError(str(group_id.value))
    return group


async def _require_server(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> None:
    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))


def _render(kind: GroupKind, players: list[Player]) -> bytes:
    entries = (
        render_ops_json(players)
        if kind is GroupKind.OP
        else render_whitelist_json(players)
    )
    # Stable JSON: indented + trailing newline, the conventional MC file shape.
    return (json.dumps(entries, indent=2) + "\n").encode("utf-8")


@dataclass(frozen=True)
class CreateGroup:
    """Create a community-scoped player group (group:manage)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, name: str, kind: str
    ) -> PlayerGroup:
        group_kind = _parse_kind(kind)
        group_name = GroupName(name)
        async with self.uow:
            existing = await self.uow.groups.get_by_community_kind_name(
                community_id, group_kind, group_name
            )
            if existing is not None:
                raise GroupNameAlreadyExistsError(group_name.value)
            group = PlayerGroup(
                id=GroupId.new(),
                community_id=community_id,
                name=group_name,
                kind=group_kind,
                players=[],
            )
            await self.uow.groups.add(group)
            await self.uow.commit()
        return group


@dataclass(frozen=True)
class ListGroups:
    """List every group in a community (group:read)."""

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId) -> list[PlayerGroup]:
        async with self.uow:
            return await self.uow.groups.list_for_community(community_id)


@dataclass(frozen=True)
class ReadGroup:
    """Read one group (group:read)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, group_id: GroupId
    ) -> PlayerGroup:
        async with self.uow:
            return await _load_group(self.uow, community_id, group_id)


@dataclass(frozen=True)
class RenameGroup:
    """Rename a group (group:manage)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, group_id: GroupId, name: str
    ) -> PlayerGroup:
        new_name = GroupName(name)
        async with self.uow:
            group = await _load_group(self.uow, community_id, group_id)
            clash = await self.uow.groups.get_by_community_kind_name(
                community_id, group.kind, new_name
            )
            if clash is not None and clash.id != group.id:
                raise GroupNameAlreadyExistsError(new_name.value)
            group.name = new_name
            await self.uow.groups.save(group)
            await self.uow.commit()
        return group


@dataclass(frozen=True)
class DeleteGroup:
    """Delete a group and resync the servers it was attached to (group:manage).

    The attachments cascade away with the group row; before deleting, the use case
    captures the attached at-rest servers and regenerates their files *without*
    this group's players, so removing a group cleans up its contribution to
    ops.json / whitelist.json on the at-rest servers it touched.
    """

    uow: UnitOfWork
    file_store: FileStore

    async def __call__(self, *, community_id: CommunityId, group_id: GroupId) -> None:
        async with self.uow:
            group = await _load_group(self.uow, community_id, group_id)
            server_ids = await self.uow.groups.list_server_ids_for_group(group_id)
            await self.uow.groups.delete(group_id)
            await self.uow.commit()
        # Resync each previously-attached server (the group is now gone, so the
        # merge excludes it). Done after commit so the file reflects the persisted
        # attachment set; best-effort across servers (see helper docstring).
        await _sync_servers_best_effort(
            self.uow, self.file_store, community_id, server_ids, group_id, group.kind
        )


@dataclass(frozen=True)
class AddPlayer:
    """Add/update a player in a group, then resync attached servers (group:manage)."""

    uow: UnitOfWork
    file_store: FileStore

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        group_id: GroupId,
        player_uuid: uuid.UUID,
        username: str,
    ) -> PlayerGroup:
        player = Player(player_uuid, username)
        async with self.uow:
            group = await _load_group(self.uow, community_id, group_id)
            group.upsert_player(player)
            await self.uow.groups.save(group)
            server_ids = await self.uow.groups.list_server_ids_for_group(group_id)
            await self.uow.commit()
        await _sync_servers_best_effort(
            self.uow, self.file_store, community_id, server_ids, group_id, group.kind
        )
        return group


@dataclass(frozen=True)
class RemovePlayer:
    """Remove a player from a group, then resync attached servers (group:manage)."""

    uow: UnitOfWork
    file_store: FileStore

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        group_id: GroupId,
        player_uuid: uuid.UUID,
    ) -> PlayerGroup:
        async with self.uow:
            group = await _load_group(self.uow, community_id, group_id)
            group.remove_player(player_uuid)
            await self.uow.groups.save(group)
            server_ids = await self.uow.groups.list_server_ids_for_group(group_id)
            await self.uow.commit()
        await _sync_servers_best_effort(
            self.uow, self.file_store, community_id, server_ids, group_id, group.kind
        )
        return group


@dataclass(frozen=True)
class AttachGroup:
    """Attach a group to a server and sync that server's file (group:manage)."""

    uow: UnitOfWork
    file_store: FileStore

    async def __call__(
        self, *, community_id: CommunityId, group_id: GroupId, server_id: ServerId
    ) -> None:
        async with self.uow:
            group = await _load_group(self.uow, community_id, group_id)
            await _require_server(self.uow, community_id, server_id)
            await self.uow.groups.attach(group_id, server_id)
            await self.uow.commit()
        await _sync_server_file(
            self.uow, self.file_store, community_id, server_id, group.kind
        )


@dataclass(frozen=True)
class DetachGroup:
    """Detach a group from a server and sync that server's file (group:manage)."""

    uow: UnitOfWork
    file_store: FileStore

    async def __call__(
        self, *, community_id: CommunityId, group_id: GroupId, server_id: ServerId
    ) -> None:
        async with self.uow:
            group = await _load_group(self.uow, community_id, group_id)
            await _require_server(self.uow, community_id, server_id)
            removed = await self.uow.groups.detach(group_id, server_id)
            if not removed:
                raise GroupAttachmentNotFoundError(str(group_id.value))
            await self.uow.commit()
        await _sync_server_file(
            self.uow, self.file_store, community_id, server_id, group.kind
        )


@dataclass(frozen=True)
class ListServerGroups:
    """List the groups attached to a server (group:read)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[PlayerGroup]:
        async with self.uow:
            await _require_server(self.uow, community_id, server_id)
            return await self.uow.groups.list_groups_for_server(server_id)


@dataclass(frozen=True)
class ListGroupServers:
    """List the ids of servers a group is attached to (group:read)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, group_id: GroupId
    ) -> list[ServerId]:
        async with self.uow:
            await _load_group(self.uow, community_id, group_id)
            return await self.uow.groups.list_server_ids_for_group(group_id)


async def _sync_servers_best_effort(
    uow: UnitOfWork,
    file_store: FileStore,
    community_id: CommunityId,
    server_ids: list[ServerId],
    group_id: GroupId,
    kind: GroupKind,
) -> None:
    """Resync several attached servers, continuing past any single write failure.

    **Partial-failure posture (issue #276, PM ruling).** The DB change is already
    committed; this fan-out is best-effort. A per-server ``write_file`` failure is
    WARN-logged (server id + group id + error) and the loop continues, so one bad
    server does not strand the others. The failed at-rest server is left stale —
    a *write* failure is **not** healed by the next hydrate (hydrate only covers
    servers that were not at-rest at sync time). The operator repair is to
    re-trigger the sync: re-attach the group to that server, or edit the group
    again, which runs this fan-out afresh.
    """

    for server_id in server_ids:
        try:
            await _sync_server_file(uow, file_store, community_id, server_id, kind)
        except Exception:
            _logger.warning(
                "group file sync failed for one attached server; other servers "
                "still synced, this one is left stale until the sync is "
                "re-triggered (re-attach or edit the group)",
                extra={
                    "server_id": str(server_id.value),
                    "group_id": str(group_id.value),
                },
                exc_info=True,
            )


async def _sync_server_file(
    uow: UnitOfWork,
    file_store: FileStore,
    community_id: CommunityId,
    server_id: ServerId,
    kind: GroupKind,
) -> None:
    """Regenerate one server's ops.json / whitelist.json from its attached groups.

    Only at-rest servers are written (issue #276 posture a): a running/unsettled
    server is skipped and ships the authoritative copy on its next hydrate. The
    file is the union-merge of every attached group of ``kind``, ordered by uuid.
    """

    async with uow:
        server = await uow.servers.get_by_id(server_id)
        if server is None or server.community_id != community_id:
            return
        at_rest = server.is_at_rest()
        groups = await uow.groups.list_groups_for_server_kind(server_id, kind)
    if not at_rest:
        return
    players = merge_players(groups)
    await file_store.write_file(
        community_id=community_id,
        server_id=server_id,
        rel_path=kind.target_file,
        content=_render(kind, players),
    )
