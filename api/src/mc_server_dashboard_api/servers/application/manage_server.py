"""CRUD use cases for servers (Section 6.5).

These run *after* the route's two-layer authorization dependency has admitted the
caller (non-member -> 404, member-without-permission -> 403; Section 6.4), so they
assume an authorized member and only do the data work.

- :class:`CreateServer` validates the server type and execution backend against
  the known enums and stages a stopped server (desired=stopped, observed=stopped
  per DATABASE.md Section 7) with a fresh id.
- :class:`ReadServer` / :class:`ListServers` are community-scoped reads; a server
  whose ``community_id`` does not match the path community is reported as
  not-found (no cross-community existence signal, FR-COMM-3).
- :class:`UpdateServer` edits name/config only while the server is at rest
  (Section 6.9 spirit); changing the backend is rejected as immutable (FR-EXE-3).
- :class:`DeleteServer` deletes a stopped server and sweeps its resource grants in
  the same transaction (Section 10).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mc_server_dashboard_api.servers.domain.backup_schedule import (
    BACKUP_INTERVAL_CONFIG_KEY,
    schedule_from_config,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    ExecutionBackendImmutableError,
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
    ServerNotStoppedError,
    UnknownExecutionBackendError,
    UnknownServerTypeError,
    UnsupportedEditionError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileStore
from mc_server_dashboard_api.servers.domain.ports import (
    PortRange,
    pick_lowest_free_port,
    validate_explicit_port,
)
from mc_server_dashboard_api.servers.domain.snapshot_cadence import (
    SNAPSHOT_INTERVAL_CONFIG_KEY,
    override_from_config,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from mc_server_dashboard_api.servers.domain.version_validator import VersionValidator

# The resource type a server grant is keyed by (DATABASE.md Sections 6, 10). The
# delete sweep removes grants for ``(resource_type='server', resource_id=<id>)``.
_SERVER_RESOURCE_TYPE = "server"

# The only MC edition the version catalog can serve at M1 (Java-only; FR-VER-1).
_SUPPORTED_EDITION = "java"

# The EULA-acceptance file Mojang's server reads on start. Seeding it with
# ``eula=true`` at create time (when the operator accepts) records consent in the
# server's initial working set, so the first start does not crash on the default
# ``eula=false`` (issue #198). The trailing newline matches the file Mojang writes.
_EULA_REL_PATH = "eula.txt"
_EULA_ACCEPTED_CONTENT = b"eula=true\n"

# The server.properties key Mojang's server reads for its listen port. Create
# seeds it with the assigned game port (issue #243) into the initial working set,
# so the server boots on its tracked port without a manual edit. The trailing
# newline matches the line format Mojang writes.
_PROPERTIES_REL_PATH = "server.properties"

_logger = logging.getLogger(__name__)

# Config keys that are *operationally safe* to edit in any server state (issue
# #115). The criterion is narrow: a key qualifies only if it is read solely by an
# API-side scheduler that re-reads ``server.config`` each tick, and is never
# shipped into the running server's working set. Editing such a key changes only
# the cadence the next tick observes, so it is race-honest under a plain config
# UPDATE and needs no at-rest gate. Working-set-affecting keys (anything the
# Worker materialises into the live server) are NOT safe and keep the at-rest
# requirement. Keep this set minimal; add a key only when it provably meets the
# criterion.
_SAFE_CONFIG_KEYS = frozenset(
    {SNAPSHOT_INTERVAL_CONFIG_KEY, BACKUP_INTERVAL_CONFIG_KEY}
)


def _changed_config_keys(current: dict[str, Any], incoming: dict[str, Any]) -> set[str]:
    """Return the keys added, removed, or whose value changed between configs."""

    return {
        key
        for key in current.keys() | incoming.keys()
        if (key in current) != (key in incoming)
        or current.get(key) != incoming.get(key)
    }


def _parse_server_type(value: str) -> ServerType:
    try:
        return ServerType(value)
    except ValueError as exc:
        raise UnknownServerTypeError(value) from exc


def _parse_execution_backend(value: str) -> ExecutionBackend:
    try:
        return ExecutionBackend(value)
    except ValueError as exc:
        raise UnknownExecutionBackendError(value) from exc


@dataclass(frozen=True)
class CreateServer:
    """Create a server within a community (server:create, FR-SRV-1).

    Create validates the requested ``(server_type, mc_version)`` against the global
    version catalog (cheap, no download — the JAR is fetched on first start, the
    ensure-on-start ruling). The check rejects an unsupported edition (the catalog
    is Java-only at M1), an unsupported type (forge at M1), and an unoffered version
    before the row is staged (FR-VER-1).

    Create assigns the server's **game port** (issue #243): the lowest free
    in-range port from ``port_range``, or an operator-supplied ``game_port``
    validated against the range (422 out of range) and the taken set (409 taken).
    The deployment-wide ``UNIQUE(game_port)`` constraint is the ultimate guard; the
    pre-read is the friendly check that turns the common case into a typed error
    rather than an IntegrityError. The assigned port is persisted on the row.

    After the row commits, an **initial working-set seeding** step writes any seed
    files into the server's first published version through the Storage write path
    (the #208 initialize-first-version behavior). It seeds ``server.properties``
    with ``server-port=<port>`` so the server boots on its tracked port, and
    ``eula.txt`` when ``accept_eula`` is true (issue #198); both compose when both
    apply (sequential write_file calls on a fresh server publish correctly). A
    storage failure during seeding is caught, WARN-logged, and surfaced as a typed
    :class:`WorkingSetSeedFailedError` (mapped to 503 at the edge): the committed
    row stays in a degraded-but-repairable state (the missing files can be written
    via the files API), rather than leaking an unmapped 500.
    """

    uow: UnitOfWork
    clock: Clock
    version_validator: VersionValidator
    file_store: FileStore
    port_range: PortRange

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        name: str,
        mc_edition: str,
        mc_version: str,
        server_type: str,
        execution_backend: str,
        config: dict[str, Any],
        accept_eula: bool = False,
        game_port: int | None = None,
    ) -> Server:
        if mc_edition != _SUPPORTED_EDITION:
            # The catalog is Java-only at M1 (FR-VER-1); reject other editions
            # before staging the row so an unprovisionable server is never created.
            raise UnsupportedEditionError(mc_edition)
        parsed_type = _parse_server_type(server_type)
        parsed_backend = _parse_execution_backend(execution_backend)
        await self.version_validator.validate(
            server_type=server_type, version=mc_version
        )
        now = self.clock.now()
        async with self.uow:
            # Resolve the game port inside the transaction that adds the row, so the
            # taken-set read and the insert are consistent; the UNIQUE constraint
            # backstops a concurrent racer (raises on commit). An explicit port is
            # validated (range -> 422, taken -> 409); otherwise pick the lowest free.
            taken = await self.uow.servers.list_game_ports()
            if game_port is None:
                assigned_port = pick_lowest_free_port(self.port_range, taken=taken)
            else:
                assigned_port = validate_explicit_port(
                    game_port, self.port_range, taken=taken
                )
            server = Server(
                id=ServerId.new(),
                community_id=community_id,
                name=ServerName(name),
                mc_edition=mc_edition,
                mc_version=mc_version,
                server_type=parsed_type,
                execution_backend=parsed_backend,
                config=config,
                game_port=assigned_port,
                # A new server is at rest: the operator has not asked it to run, and
                # no Worker has reported on it (DATABASE.md Section 7).
                desired_state=DesiredState.STOPPED,
                observed_state=ObservedState.STOPPED,
                observed_at=None,
                assigned_worker_id=None,
                created_at=now,
                updated_at=now,
            )
            await self.uow.servers.add(server)
            await self.uow.commit()
        await self._seed_initial_working_set(
            community_id=community_id,
            server_id=server.id,
            game_port=assigned_port,
            accept_eula=accept_eula,
        )
        return server

    async def _seed_initial_working_set(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        game_port: int,
        accept_eula: bool,
    ) -> None:
        """Write the create-time seed files into the server's first version.

        Generic over a list of ``(rel_path, content)`` seeds. Each is written
        through :meth:`FileStore.write_file`, which initializes the first published
        version on a never-snapshotted server (the #208 behavior); sequential
        writes on a fresh server compose correctly (the #252 review finding), so
        ``server.properties`` and ``eula.txt`` may both be seeded in one create.
        ``server.properties`` is always seeded with the assigned game port (#243);
        ``eula.txt`` only when the operator accepted at create (issue #198).

        A storage failure is caught and re-raised as
        :class:`WorkingSetSeedFailedError` (mapped to 503): the row is already
        committed and is repairable via the files API, so this never rolls back the
        server — it only signals the degraded state.
        """

        seeds: list[tuple[str, bytes]] = [
            (_PROPERTIES_REL_PATH, f"server-port={game_port}\n".encode()),
        ]
        if accept_eula:
            seeds.append((_EULA_REL_PATH, _EULA_ACCEPTED_CONTENT))
        try:
            for rel_path, content in seeds:
                await self.file_store.write_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=rel_path,
                    content=content,
                )
        except Exception as exc:
            # The committed row stays (degraded but repairable via the files API);
            # surface a mapped 503 instead of an unmapped 500.
            _logger.warning(
                "initial working-set seeding failed; server row committed but "
                "working set is unseeded (repairable via files API)",
                extra={"server_id": str(server_id.value)},
            )
            raise WorkingSetSeedFailedError(str(server_id.value)) from exc


@dataclass(frozen=True)
class ReadServer:
    """Return a server by id, scoped to its community (server:read)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> Server:
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
        if server is None or server.community_id != community_id:
            raise ServerNotFoundError(str(server_id.value))
        return server


@dataclass(frozen=True)
class ListServers:
    """List the servers in a community (server:read)."""

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId) -> list[Server]:
        async with self.uow:
            return await self.uow.servers.list_for_community(community_id)


@dataclass(frozen=True)
class UpdateServer:
    """Edit a server's name/config (server:update).

    The at-rest gate is split by config semantics (issue #115). A config update
    that touches **only** operationally-safe keys (``_SAFE_CONFIG_KEYS`` — the
    cadence knobs, read by API-side schedulers and never shipped into the working
    set) is allowed in any state: the change is a plain config UPDATE the next
    scheduler tick picks up, so it is race-honest without stopping the server. Any
    other config change — touching a working-set key, or adding/removing/modifying
    an unsafe key — keeps the at-rest requirement, as does a name change.

    A per-server snapshot-interval override carried on ``config`` is validated
    against ``min_interval_seconds`` (the thrash floor, CONFIGURATION.md Section
    5.4): a below-floor or non-integer value is rejected (FR-DATA-7), surfaced as
    422 at the edge. Validation runs **before** the state gate, so a below-floor
    override on a running server is a 422, not a 409 (the precedence ruling).
    """

    uow: UnitOfWork
    clock: Clock
    min_interval_seconds: int = 0

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        execution_backend: str | None = None,
    ) -> Server:
        new_name = None if name is None else ServerName(name)
        if config is not None:
            # Validate the overrides carried on config before any write; each
            # raises on a bad value. The snapshot interval (FR-DATA-7) and the
            # backup schedule (FR-BAK-3) are validated the same way.
            override_from_config(config, floor=self.min_interval_seconds)
            schedule_from_config(config)
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
            if server is None or server.community_id != community_id:
                raise ServerNotFoundError(str(server_id.value))
            if execution_backend is not None and (
                _parse_execution_backend(execution_backend)
                is not server.execution_backend
            ):
                # The backend is immutable for the server's lifetime (FR-EXE-3).
                raise ExecutionBackendImmutableError(execution_backend)
            # The at-rest gate applies unless this is a safe-keys-only config edit
            # (issue #115): a config update whose changed keys are all in
            # ``_SAFE_CONFIG_KEYS``, with no name change, may run in any state.
            changed_keys = (
                set() if config is None else _changed_config_keys(server.config, config)
            )
            safe_only = (
                new_name is None
                and config is not None
                and changed_keys <= _SAFE_CONFIG_KEYS
            )
            if not safe_only and not server.is_at_rest():
                raise ServerNotStoppedError(str(server_id.value))
            if new_name is not None and new_name != server.name:
                clash = await self.uow.servers.get_by_community_and_name(
                    community_id, new_name
                )
                if clash is not None and clash.id != server_id:
                    raise ServerNameAlreadyExistsError(new_name.value)
                server.name = new_name
            if config is not None:
                server.config = config
            server.updated_at = self.clock.now()
            await self.uow.servers.update(server)
            await self.uow.commit()
        return server


@dataclass(frozen=True)
class DeleteServer:
    """Delete a stopped server and sweep its resource grants (server:delete)."""

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId, server_id: ServerId) -> None:
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
            if server is None or server.community_id != community_id:
                raise ServerNotFoundError(str(server_id.value))
            if not server.is_at_rest():
                raise ServerNotStoppedError(str(server_id.value))
            await self.uow.servers.delete(server_id)
            # No FK on resource_grant.resource_id, so the server delete does not
            # cascade; sweep the grants in the same transaction (Section 10).
            await self.uow.resource_grants.delete_for_resource(
                _SERVER_RESOURCE_TYPE, server_id.value
            )
            await self.uow.commit()
