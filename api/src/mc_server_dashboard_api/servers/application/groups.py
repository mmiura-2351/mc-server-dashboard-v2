"""Player-group use cases: CRUD, player edits, attachment, and file sync (#276).

These run after the route's authorization dependency admitted the caller, so they
assume an authorized member and only do the group work. Groups are
community-scoped; a group of kind ``op`` feeds a server's ``ops.json`` and kind
``whitelist`` feeds ``whitelist.json``.

**Sync posture (issue #276, option a).** Any change that affects an attached
server's authoritative player file — attach, detach, a player add/remove on an
attached group — regenerates that server's ``ops.json`` / ``whitelist.json``
through the :class:`FileStore` at-rest write seam (versioned). Only **at-rest**
servers are written there. The file is the union-merge of every attached group
of that kind, deterministically ordered by uuid, so it is byte-stable
diff-to-diff. The merge is total (no group of that kind attached → an empty
list, which clears the file).

**Deferred sync (issue #3223).** A running or otherwise unsettled server cannot
be written: the Worker's live working set, and the final snapshot taken from it
at stop, would overwrite the authoritative copy — and a hydrate only ships
whatever that copy holds, so nothing would ever apply the change. Every change
therefore records, in its own transaction, that each affected server's file is
owed a regeneration (:meth:`GroupRepository.mark_sync_pending`). A change to a
group itself holds off attaches of that group
(:meth:`GroupRepository.lock_against_attach`), so the servers it lists are all
the servers it affects. It takes that lock BEFORE it writes anything of the
group: a player edit that locked the group row only after writing its player
rows would deadlock with a delete, which holds the group row and waits on those
rows to cascade.

``StartServer`` then regenerates the owed files from the *current* union before
it decides whether to hydrate (:func:`regenerate_pending_group_files`). That
write advances the working-set generation like any at-rest edit, so a Worker
still holding the pre-edit scratch hydrates instead of booting it. The launch
paths:

- a start (operator or schedule) applies the owed files;
- the reconciler's placement of an unassigned server (``place_and_start``)
  always hydrates from the store, so it applies them first;
- an in-place restart (``RestartServer``) and the reconciler's re-dispatch onto
  the Worker that still holds the server (``redispatch_start``, typically
  after a crash) do **not**: both relaunch the Worker's own working set, and
  applying the change there means forcing a hydrate from the last published
  snapshot over a scratch that may be newer. The change stays owed until the
  next clean stop and start (decision tracked in issue #3271).

The mark is what tells a file a group change made stale from one no group ever
managed. A server with no mark keeps its ``ops.json`` / ``whitelist.json``
exactly as the game and the operator left them.

**Partial-failure posture (PM ruling).** When a single group change touches
*several* attached servers (delete a group, add/remove a player), the file
fan-out runs after the DB commit and is **best-effort**: a per-server write
failure is WARN-logged (server id + group id + error) and the loop continues, so
one failing server does not strand the rest. The failed at-rest server is left
stale until its next start, which regenerates the file because the mark is still
standing; re-attaching the group or editing it again reruns the fan-out sooner.

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
    ServerFileNotFoundError,
    ServerNotFoundError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileStore
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    PendingGroupSync,
    Player,
    PlayerGroup,
    merge_players,
    render_ops_json,
    render_whitelist_json,
)
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    LifecycleLock,
    NullLifecycleLock,
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
    captures the attached servers and regenerates their files *without* this
    group's players, so removing a group cleans up its contribution to ops.json /
    whitelist.json — at once on the at-rest servers, and at its next start on a
    running one (the owed regeneration is recorded with the delete).
    """

    uow: UnitOfWork
    file_store: FileStore
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(self, *, community_id: CommunityId, group_id: GroupId) -> None:
        async with self.uow:
            group = await _load_group(self.uow, community_id, group_id)
            await self.uow.groups.lock_against_attach(group_id)
            server_ids = await self.uow.groups.list_server_ids_for_group(group_id)
            await self.uow.groups.delete(group_id)
            await self.uow.groups.mark_sync_pending(server_ids, group.kind)
            await self.uow.commit()
        # Resync each previously-attached server (the group is now gone, so the
        # merge excludes it). Done after commit so the file reflects the persisted
        # attachment set; best-effort across servers (see helper docstring).
        await _sync_servers_best_effort(
            self.uow,
            self.file_store,
            self.lifecycle_lock,
            community_id,
            server_ids,
            group_id,
            group.kind,
        )


@dataclass(frozen=True)
class AddPlayer:
    """Add/update a player in a group, then resync attached servers (group:manage)."""

    uow: UnitOfWork
    file_store: FileStore
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

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
            await self.uow.groups.lock_against_attach(group_id)
            group.upsert_player(player)
            await self.uow.groups.save(group)
            server_ids = await self.uow.groups.list_server_ids_for_group(group_id)
            await self.uow.groups.mark_sync_pending(server_ids, group.kind)
            await self.uow.commit()
        await _sync_servers_best_effort(
            self.uow,
            self.file_store,
            self.lifecycle_lock,
            community_id,
            server_ids,
            group_id,
            group.kind,
        )
        return group


@dataclass(frozen=True)
class RemovePlayer:
    """Remove a player from a group, then resync attached servers (group:manage)."""

    uow: UnitOfWork
    file_store: FileStore
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        group_id: GroupId,
        player_uuid: uuid.UUID,
    ) -> PlayerGroup:
        async with self.uow:
            group = await _load_group(self.uow, community_id, group_id)
            await self.uow.groups.lock_against_attach(group_id)
            group.remove_player(player_uuid)
            await self.uow.groups.save(group)
            server_ids = await self.uow.groups.list_server_ids_for_group(group_id)
            await self.uow.groups.mark_sync_pending(server_ids, group.kind)
            await self.uow.commit()
        await _sync_servers_best_effort(
            self.uow,
            self.file_store,
            self.lifecycle_lock,
            community_id,
            server_ids,
            group_id,
            group.kind,
        )
        return group


@dataclass(frozen=True)
class AttachGroup:
    """Attach a group to a server and sync that server's file (group:manage)."""

    uow: UnitOfWork
    file_store: FileStore
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self, *, community_id: CommunityId, group_id: GroupId, server_id: ServerId
    ) -> None:
        async with self.uow:
            group = await _load_group(self.uow, community_id, group_id)
            await _require_server(self.uow, community_id, server_id)
            await self.uow.groups.attach(group_id, server_id)
            await self.uow.groups.mark_sync_pending([server_id], group.kind)
            await self.uow.commit()
        await _sync_server_file(
            self.uow,
            self.file_store,
            self.lifecycle_lock,
            community_id,
            server_id,
            group.kind,
        )


@dataclass(frozen=True)
class DetachGroup:
    """Detach a group from a server and sync that server's file (group:manage)."""

    uow: UnitOfWork
    file_store: FileStore
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self, *, community_id: CommunityId, group_id: GroupId, server_id: ServerId
    ) -> None:
        async with self.uow:
            group = await _load_group(self.uow, community_id, group_id)
            await _require_server(self.uow, community_id, server_id)
            removed = await self.uow.groups.detach(group_id, server_id)
            if not removed:
                raise GroupAttachmentNotFoundError(str(group_id.value))
            await self.uow.groups.mark_sync_pending([server_id], group.kind)
            await self.uow.commit()
        await _sync_server_file(
            self.uow,
            self.file_store,
            self.lifecycle_lock,
            community_id,
            server_id,
            group.kind,
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
    lifecycle_lock: LifecycleLock,
    community_id: CommunityId,
    server_ids: list[ServerId],
    group_id: GroupId,
    kind: GroupKind,
) -> None:
    """Resync several attached servers, continuing past any single write failure.

    **Partial-failure posture (issue #276, PM ruling).** The DB change is already
    committed; this fan-out is best-effort. A per-server ``write_file`` failure is
    WARN-logged (server id + group id + error) and the loop continues, so one bad
    server does not strand the others. The failed at-rest server is left stale
    until its next start, which regenerates the file because the regeneration is
    still recorded as owed (issue #3223). Re-attaching the group to that server,
    or editing the group again, runs this fan-out afresh and repairs it sooner.
    """

    for server_id in server_ids:
        try:
            await _sync_server_file(
                uow, file_store, lifecycle_lock, community_id, server_id, kind
            )
        except Exception:
            _logger.warning(
                "group file sync failed for one attached server; other servers "
                "still synced, this one is left stale until its next start or "
                "until the sync is re-triggered (re-attach or edit the group)",
                extra={
                    "server_id": str(server_id.value),
                    "group_id": str(group_id.value),
                },
                exc_info=True,
            )


async def _sync_server_file(
    uow: UnitOfWork,
    file_store: FileStore,
    lifecycle_lock: LifecycleLock,
    community_id: CommunityId,
    server_id: ServerId,
    kind: GroupKind,
) -> None:
    """Regenerate one server's ops.json / whitelist.json from its attached groups.

    Only at-rest servers are written (issue #276 posture a): a running/unsettled
    server is skipped here, and the regeneration its change recorded as owed is
    made by the server's next start (:func:`regenerate_pending_group_files`,
    issue #3223). The file is the union-merge of every attached group of
    ``kind``, ordered by uuid.

    A successful write leaves the mark standing: the start is the one place that
    clears it, and it writes nothing when it finds the file already current.

    The per-server lifecycle lock is held across the at-rest check and the Storage
    write (issue #1222), matching every other at-rest write path, so a concurrent
    ``StartServer`` cannot flip desired=running between the check and the write.
    """

    async with lifecycle_lock.hold(server_id):
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


async def regenerate_pending_group_files(
    uow: UnitOfWork,
    file_store: FileStore,
    *,
    community_id: CommunityId,
    server_id: ServerId,
) -> list[PendingGroupSync]:
    """Regenerate the files ``server_id`` is owed; return the marks applied (#3223).

    The start-time half of the deferred sync. ``StartServer`` calls it inside its
    own transaction, under the lifecycle lock, for a stopped and unassigned
    server: the final snapshot of the previous run has settled by then, so the
    write lands on the working set the launch will hydrate and nothing publishes
    over it afterwards. The caller clears the returned marks in the transaction
    that commits the start.

    The marks are read BEFORE the groups. A change that commits after that read
    re-records its mark with a new token, so the caller's clear leaves it standing
    whether or not this pass happened to see the change; reading the groups first
    would let a change slip in between and be cleared unapplied.

    A file whose bytes are already the current union is not rewritten: an edit
    made at rest was written then, and a second identical write would advance the
    generation and retain a duplicate version for nothing.

    A storage failure raises :class:`WorkingSetSeedFailedError`, so the start
    fails rather than launching with a file known to be stale.
    """

    pending = await uow.groups.list_sync_pending(server_id)
    for owed in pending:
        groups = await uow.groups.list_groups_for_server_kind(server_id, owed.kind)
        content = _render(owed.kind, merge_players(groups))
        try:
            try:
                current: bytes | None = await file_store.read_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=owed.kind.target_file,
                )
            except ServerFileNotFoundError:
                current = None
            if current != content:
                await file_store.write_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=owed.kind.target_file,
                    content=content,
                )
        except Exception as exc:
            _logger.warning(
                "regenerating a group-derived player file before start failed; "
                "the start is refused rather than launched with a stale file",
                extra={
                    "server_id": str(server_id.value),
                    "file": owed.kind.target_file,
                },
                exc_info=True,
            )
            raise WorkingSetSeedFailedError(str(server_id.value)) from exc
    return pending
