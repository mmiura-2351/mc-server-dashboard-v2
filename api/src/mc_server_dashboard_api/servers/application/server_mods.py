"""Application use cases for server↔mod assignment & deployment (issue #1262).

A mod differs from a resource pack: a resource pack is a URL written into
``server.properties``; a **mod is a physical jar placed into the server's working
set** -- ``mods/`` for fabric/forge/neoforge/quilt, ``plugins/`` for paper. The
assignment use cases manage the ``server_mods`` rows AND that physical placement.

Every mutation is **at-rest gated**: it requires the server stopped (409
``server_unsettled`` while running) and holds the per-server ``LifecycleLock``
around its whole check-mutate-commit, reusing the exact pattern of
``AssignResourcePack``. On start, the existing hydrate carries the working set to
the worker -- no new worker protocol.

Deployment rules:

* Deploy = side ∈ {``server``, ``both``} **and** ``enabled``: the jar is copied
  to ``<dir>/<filename>``. A client-only mod (``side == "client"``) is never
  placed server-side.
* Disable = the deployed file is renamed to ``<filename>.disabled`` (kept, not
  deleted); re-enable redeploys it.
* Unassign = the deployed file (and any ``.disabled`` variant) is removed.

``ListServerMods`` lists the mod set and attaches a phase-B validation checklist
(issue #1263) computed by the pure :func:`validate_mod_set`.

``ListClientMods`` / ``DownloadClientModpack`` (issue #1265) serve the *client*
side of the set: the assigned, enabled mods whose side ∈ {``client``, ``both``}.
These are read-only (no working-set mutation, no at-rest gate); the download
streams the jars into a single zip with bounded memory.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.client_modpack_zip import (
    stream_client_modpack,
)
from mc_server_dashboard_api.servers.application.mod_validation import (
    ModValidation,
    validate_mod_set,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.errors import (
    ModAssignmentNotFoundError,
    ModNotFoundError,
    ServerFileNotFoundError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileStore
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    LifecycleLock,
    NullLifecycleLock,
)
from mc_server_dashboard_api.servers.domain.mod import Mod, ModId, ModLoader
from mc_server_dashboard_api.servers.domain.mod_store import ModStore
from mc_server_dashboard_api.servers.domain.server_mod import (
    ServerModAssignment,
    ServerModId,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)

# Loaders whose jars live in ``plugins/``; everything else uses ``mods/``.
_PLUGIN_LOADERS: frozenset[ModLoader] = frozenset({"paper"})


def _target_dir(loader_type: ModLoader) -> str:
    """Return the working-set directory a mod's jar deploys into."""

    return "plugins" if loader_type in _PLUGIN_LOADERS else "mods"


def _deployed_path(mod: Mod) -> str:
    """The deterministic, collision-free working-set path of a mod's jar.

    Library filenames are not unique (only ``sha256_hash`` is deduped), so two
    distinct mods can share a ``filename``. The path is namespaced by the mod id
    so two distinct mods never resolve to the same on-disk file (issue #1279);
    mod loaders (``mods/``) and Bukkit/Paper (``plugins/``) accept any jar name,
    so the on-disk name is functionally irrelevant. This is the single source of
    truth for the deployed path across deploy/disable/enable/unassign.
    """

    return f"{_target_dir(mod.loader_type)}/{mod.id.value.hex}-{mod.filename}"


def _disabled_path(mod: Mod) -> str:
    return f"{_deployed_path(mod)}.disabled"


def _deploys_server_side(mod: Mod) -> bool:
    """A mod is placed server-side only when its side includes the server."""

    return mod.side in ("server", "both")


async def _load_server_at_rest(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> None:
    """Validate the server exists, belongs to community, and is at rest."""

    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))
    if not server.is_at_rest():
        raise ServerFilesUnsettledError(str(server_id.value))


async def _read_jar(store: ModStore, mod: Mod) -> bytes:
    """Pull a mod's jar bytes from the store."""

    chunks = [chunk async for chunk in store.open(mod.id, mod.filename)]
    return b"".join(chunks)


class _Deployer:
    """Place / remove a mod's jar in a server's working set via the FileStore."""

    def __init__(
        self,
        file_store: FileStore,
        store: ModStore,
        community_id: CommunityId,
        server_id: ServerId,
    ) -> None:
        self._files = file_store
        self._store = store
        self._community_id = community_id
        self._server_id = server_id

    async def _write(self, rel_path: str, content: bytes) -> None:
        await self._files.write_file(
            community_id=self._community_id,
            server_id=self._server_id,
            rel_path=rel_path,
            content=content,
        )

    async def _delete(self, rel_path: str) -> None:
        try:
            await self._files.delete_file(
                community_id=self._community_id,
                server_id=self._server_id,
                rel_path=rel_path,
            )
        except ServerFileNotFoundError:
            pass

    async def deploy(self, mod: Mod, *, enabled: bool) -> None:
        """Bring the working set in line with the mod's side / enabled state.

        Client-only mods are never placed server-side. A server-side mod is
        written to ``<filename>`` when enabled, or ``<filename>.disabled`` when
        disabled (kept on disk, not deleted). The other variant is cleared so a
        toggle never leaves both files behind.
        """

        if not _deploys_server_side(mod):
            await self.remove(mod)
            return

        content = await _read_jar(self._store, mod)
        if enabled:
            await self._delete(_disabled_path(mod))
            await self._write(_deployed_path(mod), content)
        else:
            await self._delete(_deployed_path(mod))
            await self._write(_disabled_path(mod), content)

    async def remove(self, mod: Mod) -> None:
        """Remove the deployed jar and any ``.disabled`` variant."""

        await self._delete(_deployed_path(mod))
        await self._delete(_disabled_path(mod))


@dataclass(frozen=True)
class AssignMods:
    """Assign one or more library mods to a server and deploy their jars.

    Multi-select: ``mod_ids`` is a list. Each id must resolve to a library mod
    (else 404). An already-assigned mod is left as-is (idempotent re-assign).
    Server-side mods (side ∈ {server, both}) are deployed enabled; client-only
    mods are recorded but not placed server-side.
    """

    uow: UnitOfWork
    file_store: FileStore
    store: ModStore
    clock: Clock
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        mod_ids: list[ModId],
        assigned_by: uuid.UUID,
    ) -> list[ServerModAssignment]:
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                await _load_server_at_rest(self.uow, community_id, server_id)

                mods: list[Mod] = []
                for mod_id in mod_ids:
                    mod = await self.uow.mods.get_by_id(mod_id)
                    if mod is None:
                        raise ModNotFoundError(str(mod_id.value))
                    mods.append(mod)

                existing = {
                    a.mod_id: a
                    for a in await self.uow.mods.list_assignments_for_server(server_id)
                }

            deployer = _Deployer(self.file_store, self.store, community_id, server_id)
            now = self.clock.now()
            new_assignments: list[ServerModAssignment] = []
            result: list[ServerModAssignment] = []

            for mod in mods:
                if mod.id in existing:
                    result.append(existing[mod.id])
                    continue
                assignment = ServerModAssignment(
                    id=ServerModId.new(),
                    server_id=server_id,
                    mod_id=mod.id,
                    enabled=True,
                    assigned_by=assigned_by,
                    created_at=now,
                    updated_at=now,
                )
                await deployer.deploy(mod, enabled=True)
                new_assignments.append(assignment)
                result.append(assignment)

            async with self.uow:
                for assignment in new_assignments:
                    await self.uow.mods.add_assignment(assignment)
                await self.uow.commit()

        return result


@dataclass(frozen=True)
class UnassignMod:
    """Remove a mod assignment from a server and delete its deployed jar."""

    uow: UnitOfWork
    file_store: FileStore
    store: ModStore
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        mod_id: ModId,
    ) -> None:
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                await _load_server_at_rest(self.uow, community_id, server_id)

                assignment = await self.uow.mods.get_assignment(server_id, mod_id)
                if assignment is None:
                    raise ModAssignmentNotFoundError(str(mod_id.value))
                mod = await self.uow.mods.get_by_id(mod_id)
                if mod is None:
                    raise ModNotFoundError(str(mod_id.value))

            deployer = _Deployer(self.file_store, self.store, community_id, server_id)
            await deployer.remove(mod)

            async with self.uow:
                await self.uow.mods.delete_assignment(server_id, mod_id)
                await self.uow.commit()


@dataclass(frozen=True)
class SetModEnabled:
    """Enable or disable an assigned mod, adjusting its deployed jar.

    Disable renames the deployed jar to ``<filename>.disabled`` (kept on disk);
    enable redeploys it. A client-only mod is never placed server-side, so the
    toggle updates only the row.
    """

    uow: UnitOfWork
    file_store: FileStore
    store: ModStore
    clock: Clock
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        mod_id: ModId,
        enabled: bool,
    ) -> ServerModAssignment:
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                await _load_server_at_rest(self.uow, community_id, server_id)

                assignment = await self.uow.mods.get_assignment(server_id, mod_id)
                if assignment is None:
                    raise ModAssignmentNotFoundError(str(mod_id.value))
                mod = await self.uow.mods.get_by_id(mod_id)
                if mod is None:
                    raise ModNotFoundError(str(mod_id.value))

            deployer = _Deployer(self.file_store, self.store, community_id, server_id)
            await deployer.deploy(mod, enabled=enabled)

            assignment.enabled = enabled
            assignment.updated_at = self.clock.now()

            async with self.uow:
                await self.uow.mods.set_assignment_enabled(assignment)
                await self.uow.commit()

        return assignment


@dataclass(frozen=True)
class ServerModSet:
    """A server's mod set plus its phase-B validation checklist (issue #1263)."""

    entries: list[tuple[ServerModAssignment, Mod]]
    validation: ModValidation


@dataclass(frozen=True)
class ListServerMods:
    """Return a server's mod set with its validation checklist.

    Each assignment is paired with its library mod, and the whole set is run
    through the pure :func:`validate_mod_set` against the server's loader and MC
    version. Validation is display-only (#1263): it never mutates the set.
    """

    uow: UnitOfWork

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
    ) -> ServerModSet:
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
            if server is None or server.community_id != community_id:
                raise ServerNotFoundError(str(server_id.value))

            assignments = await self.uow.mods.list_assignments_for_server(server_id)
            entries: list[tuple[ServerModAssignment, Mod]] = []
            for assignment in assignments:
                mod = await self.uow.mods.get_by_id(assignment.mod_id)
                if mod is not None:
                    entries.append((assignment, mod))

        validation = validate_mod_set(
            server_type=server.server_type.value,
            mc_version=server.mc_version,
            mods=[mod for _, mod in entries],
        )
        return ServerModSet(entries=entries, validation=validation)


def _needs_client(mod: Mod) -> bool:
    """A mod the client needs: its side reaches the client (client or both)."""

    return mod.side in ("client", "both")


async def _select_client_mods(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> tuple[str, list[Mod]]:
    """Return the server's name and its assigned, enabled, client-needed mods.

    Selection (epic #1258): assigned to the server, ``enabled``, side ∈
    {``client``, ``both``}. Server-only mods are excluded; a disabled assignment
    is excluded even when its side reaches the client. Ordered by the assignment
    order (``list_assignments_for_server`` orders by ``created_at``). The server
    name is returned alongside so the download path need not re-load the server.
    """

    async with uow:
        server = await uow.servers.get_by_id(server_id)
        if server is None or server.community_id != community_id:
            raise ServerNotFoundError(str(server_id.value))

        mods: list[Mod] = []
        for assignment in await uow.mods.list_assignments_for_server(server_id):
            if not assignment.enabled:
                continue
            mod = await uow.mods.get_by_id(assignment.mod_id)
            if mod is not None and _needs_client(mod):
                mods.append(mod)
    return server.name.value, mods


@dataclass(frozen=True)
class ListClientMods:
    """List the mods a player's client needs for a server (issue #1265).

    Read-only: the assigned, enabled mods with side ∈ {client, both}. Does not
    touch the working set, so no at-rest gate.
    """

    uow: UnitOfWork

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
    ) -> list[Mod]:
        _, mods = await _select_client_mods(self.uow, community_id, server_id)
        return mods


@dataclass(frozen=True)
class DownloadClientModpack:
    """Stream the client-needed jars of a server as a single zip (issue #1265).

    Selects the same mods as :class:`ListClientMods`, then streams their jars
    from the :class:`ModStore` into one zip with bounded memory. Returns the byte
    stream paired with the server name so the HTTP layer can name the download.
    """

    uow: UnitOfWork
    store: ModStore

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
    ) -> tuple[AsyncIterator[bytes], str]:
        server_name, mods = await _select_client_mods(self.uow, community_id, server_id)
        return stream_client_modpack(self.store, mods), server_name
