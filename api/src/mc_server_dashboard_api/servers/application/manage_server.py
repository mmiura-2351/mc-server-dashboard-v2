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
import secrets
from collections.abc import Awaitable, Callable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any

from mc_server_dashboard_api.servers.domain.backup_schedule import (
    BACKUP_INTERVAL_CONFIG_KEY,
    schedule_from_config,
)
from mc_server_dashboard_api.servers.domain.backup_store import BackupArchiveStore
from mc_server_dashboard_api.servers.domain.bedrock_tunnel import (
    BedrockTunnelCredentials,
    NullBedrockTunnelCredentials,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.cpu_allocation import (
    CPU_ALLOCATION_CONFIG_KEY,
    cpu_allocation_from_config,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    PermissionDeniedError,
    PortAlreadyTakenError,
    PortOutOfRangeError,
    ServerFileNotFoundError,
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
    ServerNotStoppedError,
    SlugAlreadyTakenError,
    UnknownServerTypeError,
    UnsupportedEditionError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileStore
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    LifecycleLock,
    NullLifecycleLock,
)
from mc_server_dashboard_api.servers.domain.memory_limit import (
    MEMORY_LIMIT_CONFIG_KEY,
    memory_limit_from_config,
)
from mc_server_dashboard_api.servers.domain.plugin import has_enabled_geyser
from mc_server_dashboard_api.servers.domain.ports import (
    PortRange,
    pick_lowest_free_port,
    validate_explicit_port,
)
from mc_server_dashboard_api.servers.domain.server_properties import (
    apply_overrides,
    remove_keys,
    set_rcon_properties,
    set_server_port,
)
from mc_server_dashboard_api.servers.domain.slug import generate_slug, validate_slug
from mc_server_dashboard_api.servers.domain.snapshot_cadence import (
    SNAPSHOT_INTERVAL_CONFIG_KEY,
    override_from_config,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    JAR_KEY_CONFIG_FIELD,
    JAR_SOURCE_CONFIG_FIELD,
    CommunityId,
    DesiredState,
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

# The server.properties Mojang's server reads on start. Create seeds it with the
# assigned game port (issue #243) and the RCON keys (enable-rcon / rcon.port /
# rcon.password, issue #335) into the initial working set, so the server boots on
# its tracked port AND RCON is on out of the box (the console / graceful-stop path
# needs it). The trailing newline matches the line format Mojang writes.
_PROPERTIES_REL_PATH = "server.properties"

# The number of random bytes behind the per-server RCON password (issue #335). The
# password lives only in server.properties (the worker reads it there); it is never
# persisted in the DB. ``secrets.token_urlsafe`` returns ~1.3 chars per byte.
_RCON_PASSWORD_BYTES = 32


def _new_rcon_password() -> str:
    """Generate a fresh per-server RCON secret (the default token generator)."""

    return secrets.token_urlsafe(_RCON_PASSWORD_BYTES)


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

# Config keys whose edit is gated by ``backup:schedule`` rather than
# ``server:update`` (issue #458). Backup scheduling has no dedicated endpoint —
# it rides the ``backup_interval_hours`` key on the config blob — so the gate
# branches by the changed-key set: a config edit that touches only this key
# requires ``backup:schedule``; any other change requires ``server:update``; a
# mixed edit requires both. ``server:update`` no longer implies scheduling. Note
# the snapshot cadence key is NOT here: it has no dedicated permission code and
# stays under ``server:update``.
_SCHEDULING_CONFIG_KEYS = frozenset({BACKUP_INTERVAL_CONFIG_KEY})

# Config keys consumed by the platform (resource limits, scheduler cadences)
# that are NOT server.properties overrides (issue #1209). Everything else in the
# config dict is treated as a server.properties key-value pair and written into
# the file on create and update.
_RESERVED_CONFIG_KEYS = frozenset(
    {
        MEMORY_LIMIT_CONFIG_KEY,
        CPU_ALLOCATION_CONFIG_KEY,
        SNAPSHOT_INTERVAL_CONFIG_KEY,
        BACKUP_INTERVAL_CONFIG_KEY,
        # System-written JAR keys (issues #118, #1676). Written by the start
        # path; never operator-settable, hidden from the overrides editor.
        JAR_KEY_CONFIG_FIELD,
        JAR_SOURCE_CONFIG_FIELD,
        # Platform-managed server.properties keys (issue #1243). These are set
        # by _seed_initial_working_set (create) and _rewrite_server_port
        # (update); user config must not override them via the properties
        # override path.
        "server-port",
        "enable-rcon",
        "rcon.port",
        "rcon.password",
        # Resource-pack keys managed by set_resource_pack_properties /
        # clear_resource_pack_properties (issue #1253).
        "resource-pack",
        "resource-pack-sha1",
        "require-resource-pack",
        "resource-pack-prompt",
    }
)

# The permission codes the update gate evaluates (issue #458). Kept as bare
# strings so this application module stays free of community-context value
# objects; the edge maps a denial to the 403 ``permission`` member.
_SERVER_UPDATE_PERMISSION = "server:update"
_BACKUP_SCHEDULE_PERMISSION = "backup:schedule"


def _changed_config_keys(current: dict[str, Any], incoming: dict[str, Any]) -> set[str]:
    """Return the keys added, removed, or whose value changed between configs."""

    return {
        key
        for key in current.keys() | incoming.keys()
        if (key in current) != (key in incoming)
        or current.get(key) != incoming.get(key)
    }


def _properties_overrides(config: dict[str, Any]) -> dict[str, str]:
    """Extract the server.properties key-value pairs from a config dict (#1209).

    Everything in the config dict that is not a reserved platform key is treated
    as a server.properties override. Values are stringified because
    ``server.properties`` is a plain-text key=value file.
    """

    return {
        key: str(value)
        for key, value in config.items()
        if key not in _RESERVED_CONFIG_KEYS
    }


def _parse_server_type(value: str) -> ServerType:
    try:
        return ServerType(value)
    except ValueError as exc:
        raise UnknownServerTypeError(value) from exc


@dataclass(frozen=True)
class CreateServer:
    """Create a server within a community (server:create, FR-SRV-1).

    Create validates the requested ``(server_type, mc_version)`` against the global
    version catalog (cheap, no download — the JAR is fetched on first start, the
    ensure-on-start ruling). The check rejects an unsupported edition (the catalog
    is Java-only at M1), an uncatalogued type, and an unoffered version before
    the row is staged.

    Create assigns the server's **game port** (issue #243): the lowest free
    in-range port from ``port_range``, or an operator-supplied ``game_port``
    validated against the range (422 out of range) and the taken set (409 taken).
    The deployment-wide ``UNIQUE(game_port)`` constraint is the ultimate guard; the
    pre-read is the friendly check that turns the common case into a typed error
    rather than an IntegrityError. The assigned port is persisted on the row.

    After the row commits, an **initial working-set seeding** step writes any seed
    files into the server's first published version through the Storage write path
    (the #208 initialize-first-version behavior). It seeds ``server.properties``
    with ``server-port=<port>`` so the server boots on its tracked port (#243) plus
    the RCON keys (``enable-rcon=true`` / ``rcon.port`` / a fresh random
    ``rcon.password``) so the console / graceful-stop path works out of the box
    (issue #335), and ``eula.txt`` when ``accept_eula`` is true (issue #198); both
    files compose when both apply (sequential write_file calls on a fresh server
    publish correctly). A storage failure during seeding is caught, WARN-logged, and
    surfaced as a typed :class:`WorkingSetSeedFailedError` (mapped to 503 at the
    edge): the committed row stays in a degraded-but-repairable state (the missing
    files can be written via the files API), rather than leaking an unmapped 500.

    The RCON password is generated via the injected ``token_generator`` (default a
    ``secrets``-backed random token), kept injectable so tests are deterministic;
    the secret lives only in ``server.properties`` (the worker's canonical source),
    never in the DB.
    """

    uow: UnitOfWork
    clock: Clock
    version_validator: VersionValidator
    file_store: FileStore
    port_range: PortRange
    token_generator: Callable[[], str] = field(default=_new_rcon_password)
    # Operator-configurable memory-limit knobs (issue #1069). ``None`` preserves
    # the current behavior (no default / 1 TiB ceiling).
    default_memory_limit_mb: int | None = None
    max_memory_limit_mb: int | None = None

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        name: str,
        mc_edition: str,
        mc_version: str,
        server_type: str,
        config: dict[str, Any],
        accept_eula: bool = False,
        game_port: int | None = None,
        slug: str | None = None,
    ) -> Server:
        if mc_edition != _SUPPORTED_EDITION:
            # The catalog is Java-only at M1 (FR-VER-1); reject other editions
            # before staging the row so an unprovisionable server is never created.
            raise UnsupportedEditionError(mc_edition)
        parsed_type = _parse_server_type(server_type)
        # Apply the operator-configurable default when the request omits the key
        # (issue #1069). The default is injected into the config so it persists on
        # the row and is visible in responses. An explicit value takes precedence.
        if (
            self.default_memory_limit_mb is not None
            and MEMORY_LIMIT_CONFIG_KEY not in config
        ):
            config[MEMORY_LIMIT_CONFIG_KEY] = self.default_memory_limit_mb
        # A per-server memory limit carried on config (#705) is validated before
        # the row is staged: a bad shape/range 422s. The ceiling is overridden
        # by the operator-configurable max (issue #1069). Range only — host
        # capacity is the deferred placement sub-issue #710.
        memory_limit_from_config(config, ceiling_mb=self.max_memory_limit_mb)
        # The per-server CPU allocation carried on config (#722) is validated the
        # same way: a bad shape/range 422s before the row is staged. It is a soft
        # relative share, not a hard cap; range only — host capacity is the
        # deferred placement sub-issue #710.
        cpu_allocation_from_config(config)
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
            # Assign the relay slug (issue #955, #981): if the caller supplied an
            # explicit non-blank slug, validate it (422 invalid, 409 taken) and use
            # it; otherwise auto-generate a 6-char random slug. Both paths run
            # inside the same transaction that inserts the row so the taken-set read
            # and insert are consistent; the UNIQUE constraint backstops a racer.
            taken_slugs = await self.uow.servers.list_slugs()
            explicit_slug = slug.strip() if slug is not None and slug.strip() else None
            if explicit_slug is not None:
                validate_slug(explicit_slug)
                if explicit_slug in taken_slugs:
                    raise SlugAlreadyTakenError(explicit_slug)
                assigned_slug = explicit_slug
            else:
                assigned_slug = generate_slug(taken=taken_slugs)
            server = Server(
                id=ServerId.new(),
                community_id=community_id,
                name=ServerName(name),
                mc_edition=mc_edition,
                mc_version=mc_version,
                server_type=parsed_type,
                config=config,
                game_port=assigned_port,
                slug=assigned_slug,
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
            config=config,
        )
        return server

    async def _seed_initial_working_set(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        game_port: int,
        accept_eula: bool,
        config: dict[str, Any],
    ) -> None:
        """Write the create-time seed files into the server's first version.

        Generic over a list of ``(rel_path, content)`` seeds. Each is written
        through :meth:`FileStore.write_file`, which initializes the first published
        version on a never-snapshotted server (the #208 behavior); sequential
        writes on a fresh server compose correctly (the #252 review finding), so
        ``server.properties`` and ``eula.txt`` may both be seeded in one create.
        ``server.properties`` is always seeded with the assigned game port (#243)
        and the RCON keys (#335: ``enable-rcon=true``, ``rcon.port``, and a fresh
        random ``rcon.password``); user-supplied server.properties overrides from
        ``config`` are merged on top (#1209); ``eula.txt`` only when the operator
        accepted at create (issue #198).

        A storage failure is caught and re-raised as
        :class:`WorkingSetSeedFailedError` (mapped to 503): the row is already
        committed and is repairable via the files API, so this never rolls back the
        server — it only signals the degraded state.
        """

        properties = set_rcon_properties(
            set_server_port(b"", game_port), password=self.token_generator()
        )
        overrides = _properties_overrides(config)
        if overrides:
            properties = apply_overrides(properties, overrides)
        seeds: list[tuple[str, bytes]] = [
            (_PROPERTIES_REL_PATH, properties),
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
class LookupServerCommunity:
    """Return the community a server belongs to, or ``None`` if it is unknown.

    The community-scoped events stream (#288) reads a firehose of every server's
    status events and must decide, per event, whether the server is in the
    stream's community. This is that lookup: a single indexed read by server id,
    returning the owning community id (no cross-community existence concern — the
    caller compares it to the path community before forwarding anything).
    """

    uow: UnitOfWork

    async def __call__(self, *, server_id: ServerId) -> CommunityId | None:
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
        return server.community_id if server is not None else None


@dataclass(frozen=True)
class ListServers:
    """List the servers in a community (server:read)."""

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId) -> list[Server]:
        async with self.uow:
            return await self.uow.servers.list_for_community(community_id)


@dataclass(frozen=True)
class BedrockJoinability:
    """Whether a server's Bedrock response fields should be surfaced (issue #1555).

    ``ServerResponse.bedrock_address``/``bedrock_port`` are non-null only when the
    server ALSO carries at least one *enabled* Geyser copy -- a disabled Geyser is
    not listening on its RakNet port, so surfacing the address would advertise a
    join target nothing answers. This uses the same ``has_enabled_geyser``
    predicate as the Bedrock tunnel dispatch skip
    (``ServersServerStateSink._sync_bedrock_tunnel``, issue #1544), so the two
    never drift on what counts as "Bedrock enabled".

    Two entry points: :meth:`for_server` for the single-server response
    endpoints, :meth:`for_servers` batched for the servers list endpoint (so
    listing does not add one plugin query per server).
    """

    uow: UnitOfWork

    async def for_server(self, server_id: ServerId) -> bool:
        async with self.uow:
            plugins = await self.uow.plugins.list_for_server(server_id)
        return has_enabled_geyser(plugins)

    async def for_servers(
        self, server_ids: Sequence[ServerId]
    ) -> AbstractSet[ServerId]:
        async with self.uow:
            return await self.uow.plugins.enabled_geyser_server_ids(server_ids)


@dataclass(frozen=True)
class UpdateServer:
    """Edit a server's name/config/game port (server:update).

    The at-rest gate is split by config semantics (issue #115). A config update
    that touches **only** operationally-safe keys (``_SAFE_CONFIG_KEYS`` — the
    cadence knobs, read by API-side schedulers and never shipped into the working
    set) is allowed in any state: the change is a plain config UPDATE the next
    scheduler tick picks up, so it is race-honest without stopping the server. Any
    other config change — touching a working-set key, or adding/removing/modifying
    an unsafe key — keeps the at-rest requirement, as does a name change or a game
    port change.

    A per-server snapshot-interval override carried on ``config`` is validated
    against ``min_interval_seconds`` (the thrash floor, CONFIGURATION.md Section
    5.4): a below-floor or non-integer value is rejected (FR-DATA-7), surfaced as
    422 at the edge. Validation runs **before** the state gate, so a below-floor
    override on a running server is a 422, not a 409 (the precedence ruling).

    A **game port** change (issue #311) is at-rest only and validated like create:
    the new port must be in the configured range (422 out of range) and free
    deployment-wide (409 taken), the deployment-wide ``UNIQUE(game_port)`` the
    ultimate backstop (#261). It rewrites ``server-port=<port>`` in the at-rest
    ``server.properties`` through the file write seam so the DB ``game_port`` and
    the real bind port stay in sync; a legacy server with no properties file gets
    one created with just the port line. The range check runs before the state
    gate (the same 422-before-409 precedence); the file rewrite happens after the
    DB commit (#1705) so a commit failure (e.g. a unique-violation race) does not
    leave the file and row out of step. The inverse divergence — committed row,
    failed file write — is the self-healing direction (retryable).
    """

    uow: UnitOfWork
    clock: Clock
    file_store: FileStore
    port_range: PortRange
    min_interval_seconds: int = 0
    lifecycle_lock: LifecycleLock = NullLifecycleLock()
    # Operator-configurable ceiling for memory-limit validation (issue #1069).
    # ``None`` preserves the hardcoded 1 TiB ceiling.
    max_memory_limit_mb: int | None = None

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        game_port: int | None = None,
        slug: str | None = None,
        authorize: Callable[[str], Awaitable[bool]],
    ) -> Server:
        # Validate the slug format before any DB work (422 beats 409, same
        # precedence as the port range check).
        if slug is not None:
            validate_slug(slug)
        new_name = None if name is None else ServerName(name)
        if config is not None:
            # Validate the overrides carried on config before any write; each
            # raises on a bad value. The snapshot interval (FR-DATA-7) and the
            # backup schedule (FR-BAK-3) are validated the same way.
            override_from_config(config, floor=self.min_interval_seconds)
            schedule_from_config(config)
            # The per-server memory limit (#705) is validated the same way: a
            # bad shape/range 422s before any write. The ceiling is overridden
            # by the operator-configurable max (issue #1069). Range only — host
            # capacity is the deferred placement sub-issue #710.
            memory_limit_from_config(config, ceiling_mb=self.max_memory_limit_mb)
            # The per-server CPU allocation (#722) is validated the same way: a
            # bad shape/range 422s before any write. A soft relative share, not a
            # hard cap; range only — host capacity is the deferred placement
            # sub-issue #710.
            cpu_allocation_from_config(config)
        if game_port is not None and game_port not in self.port_range:
            # Range is a pure 422 that runs before the state gate (the precedence
            # ruling), so an out-of-range port on a running server is a 422.
            raise PortOutOfRangeError(str(game_port))
        # Hold the per-server lifecycle lock around the read-check-mutate-commit
        # transaction (issue #827): the at-rest gate and the same-transaction
        # server.properties rewrite must serialize against a concurrent start, so a
        # start cannot flip desired=running between the at-rest check and the commit.
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                server = await self.uow.servers.get_by_id(server_id)
                if server is None or server.community_id != community_id:
                    raise ServerNotFoundError(str(server_id.value))
                # The config diff drives two gates. The at-rest gate (issue #115)
                # applies unless this is a safe-keys-only config edit: a config update
                # whose changed keys are all in ``_SAFE_CONFIG_KEYS``, with no name
                # change and no port change, may run in any state. The permission gate
                # (issue #458) branches by the same changed-key set: scheduling-only
                # edits need ``backup:schedule``; any other change needs
                # ``server:update``; a mixed edit needs both. The permission gate runs
                # after existence (a missing server is 404, no existence signal) and
                # before the at-rest gate.
                changed_keys = (
                    set()
                    if config is None
                    else _changed_config_keys(server.config, config)
                )
                await self._authorize_update(
                    authorize=authorize,
                    new_name=new_name,
                    game_port=game_port,
                    slug=slug,
                    changed_keys=changed_keys,
                )
                # Slug rename is safe while running (routing is consulted only at
                # join time; RELAY.md Section 3). The at-rest gate fires only for
                # name/port changes and unsafe-key config edits.
                safe_only = (
                    new_name is None
                    and game_port is None
                    and (config is None or changed_keys <= _SAFE_CONFIG_KEYS)
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
                # Capture deferred file-write state (#1705): the actual writes
                # happen after the DB commit so a commit failure does not
                # leave the file and row out of step.
                pending_overrides: dict[str, str] | None = None
                pending_removed_keys: AbstractSet[str] = frozenset()
                pending_port: int | None = None
                if config is not None:
                    # Sync changed server.properties overrides to the file
                    # (#1209, #1242). Only non-reserved keys are properties
                    # overrides; compare old vs new to avoid a needless file
                    # rewrite. Keys in old but not new must be removed from
                    # the file so stale overrides do not survive (#1242).
                    old_overrides = _properties_overrides(server.config)
                    new_overrides = _properties_overrides(config)
                    if new_overrides != old_overrides:
                        pending_overrides = new_overrides
                        pending_removed_keys = (
                            old_overrides.keys() - new_overrides.keys()
                        )
                    server.config = config
                if game_port is not None and game_port != server.game_port:
                    # Uniqueness check excluding the server's own current port;
                    # the file rewrite is deferred to after commit (#1705).
                    taken = await self.uow.servers.list_game_ports()
                    if server.game_port is not None:
                        taken.discard(server.game_port)
                    if game_port in taken:
                        raise PortAlreadyTakenError(str(game_port))
                    pending_port = game_port
                    server.game_port = game_port
                if slug is not None and slug != server.slug:
                    # Uniqueness check excluding the server's own current slug
                    # (a server renaming to its own slug is a no-op, not a conflict).
                    # The deployment-wide UNIQUE(slug) constraint backstops a racer.
                    clash = await self.uow.servers.get_by_slug(slug)
                    if clash is not None and clash.id != server_id:
                        raise SlugAlreadyTakenError(slug)
                    server.slug = slug
                server.updated_at = self.clock.now()
                await self.uow.servers.update(server)
                await self.uow.commit()
            # Deferred file writes (#1705): run after the DB commit succeeds
            # so a commit failure never leaves the file and row out of step.
            # A post-commit file-write failure is the self-healing direction:
            # the committed row is correct and the file can be re-written on
            # a subsequent update.
            if pending_overrides is not None:
                await self._rewrite_properties_overrides(
                    community_id=community_id,
                    server_id=server_id,
                    overrides=pending_overrides,
                    removed_keys=pending_removed_keys,
                )
            if pending_port is not None:
                await self._rewrite_server_port(
                    community_id=community_id,
                    server_id=server_id,
                    port=pending_port,
                )
            return server

    async def _authorize_update(
        self,
        *,
        authorize: Callable[[str], Awaitable[bool]],
        new_name: ServerName | None,
        game_port: int | None,
        slug: str | None,
        changed_keys: set[str],
    ) -> None:
        """Enforce the per-update permission gate (issue #458).

        Computes the required permission codes from what the edit touches and
        denies on the first the caller is missing. ``backup:schedule`` is required
        when the edit changes a scheduling key; ``server:update`` when it changes
        anything else (a name, port, backend, or any non-scheduling config key). A
        mixed edit requires both. ``server:update`` is checked first so a wholly
        unauthorized caller is denied on the broad code. A no-op PATCH that touches
        nothing (empty body, or a config that round-trips identical) still requires
        ``server:update`` — the conservative pre-#458 default, so a PATCH cannot
        leak server detail or act as a state oracle without any permission.
        """

        scheduling_change = bool(changed_keys & _SCHEDULING_CONFIG_KEYS)
        other_change = (
            new_name is not None
            or game_port is not None
            or slug is not None
            or bool(changed_keys - _SCHEDULING_CONFIG_KEYS)
        )
        required: list[str] = []
        if other_change:
            required.append(_SERVER_UPDATE_PERMISSION)
        if scheduling_change:
            required.append(_BACKUP_SCHEDULE_PERMISSION)
        if not required:
            required.append(_SERVER_UPDATE_PERMISSION)
        for code in required:
            if not await authorize(code):
                raise PermissionDeniedError(code)

    async def _rewrite_server_port(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        port: int,
    ) -> None:
        """Set ``server-port=<port>`` in the at-rest ``server.properties`` (#311).

        Reads the current file, rewrites its port line (or appends one), and
        writes it back through the versioned file seam. A legacy server with no
        properties file is handled by treating the absent file as empty, so a file
        with just the port line is created. Called after the DB commit (#1705): a
        storage failure is surfaced as :class:`WorkingSetSeedFailedError` (mapped
        to 503); the row's ``game_port`` is already committed, but the divergence
        is the self-healing direction (the file can be re-written on retry).
        """

        try:
            current = await self.file_store.read_file(
                community_id=community_id,
                server_id=server_id,
                rel_path=_PROPERTIES_REL_PATH,
            )
        except ServerFileNotFoundError:
            # Legacy server with no seeded properties (#243): start from empty so
            # the rewrite produces a file with just the port line.
            current = b""
        try:
            await self.file_store.write_file(
                community_id=community_id,
                server_id=server_id,
                rel_path=_PROPERTIES_REL_PATH,
                content=set_server_port(current, port),
            )
        except Exception as exc:
            _logger.warning(
                "rewriting server.properties for a port change failed; "
                "aborting the port update",
                extra={"server_id": str(server_id.value)},
            )
            raise WorkingSetSeedFailedError(str(server_id.value)) from exc

    async def _rewrite_properties_overrides(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        overrides: dict[str, str],
        removed_keys: AbstractSet[str] = frozenset(),
    ) -> None:
        """Sync config-carried server.properties overrides into the file (#1209).

        Reads the current ``server.properties``, applies the overrides via
        :func:`apply_overrides`, removes lines for keys in *removed_keys*
        (#1242), and writes it back. A legacy server with no properties file is
        handled by treating the absent file as empty. Called after the DB commit
        (#1705): a storage failure is surfaced as
        :class:`WorkingSetSeedFailedError` (mapped to 503); the config row is
        already committed, but the divergence is the self-healing direction (the
        file can be re-written on retry).
        """

        try:
            current = await self.file_store.read_file(
                community_id=community_id,
                server_id=server_id,
                rel_path=_PROPERTIES_REL_PATH,
            )
        except ServerFileNotFoundError:
            current = b""
        try:
            updated = apply_overrides(current, overrides)
            if removed_keys:
                updated = remove_keys(updated, removed_keys)
            await self.file_store.write_file(
                community_id=community_id,
                server_id=server_id,
                rel_path=_PROPERTIES_REL_PATH,
                content=updated,
            )
        except Exception as exc:
            _logger.warning(
                "rewriting server.properties for config overrides failed; "
                "aborting the config update",
                extra={"server_id": str(server_id.value)},
            )
            raise WorkingSetSeedFailedError(str(server_id.value)) from exc


@dataclass(frozen=True)
class DeleteServer:
    """Delete a stopped server, prune its Storage, and sweep its grants (server:delete).

    Storage retention (issue #777): a deleted server keeps exactly two artifacts,
    neither with a DB row (rows cascade with the server delete):

    1. **The latest backup archive, if any** — the newest backup by ``created_at``
       is retained; all older archives are deleted (archive-first per the
       DeleteBackup convention, so an archive is never orphaned with a live row).
       Selection is the literal latest-by-``created_at`` regardless of ``health``
       (owner ruling on #777: "latest existing"): a QUARANTINED newest archive is
       still the one kept. The mandatory working-set tar.gz below is the safety net
       — it is strictly newer and always healthy enough to re-import.
    2. **The current working set, packed as a final tar.gz** — mandatory and
       fail-closed: the pack runs through the backup seam BEFORE the row delete, so
       a pack failure aborts the whole delete and the working set is never silently
       lost. A server with no published snapshot has nothing to pack (no-op).

    The pack and the archive prune happen before the row delete so a crash between
    the Storage work and the commit leaves the server row intact (the delete is
    simply retried), never a server whose Storage is pruned but row survives with a
    dangling backup index.

    Two-transaction shape (the DeleteBackup pattern): the at-rest check and the
    destructive prune live in separate transactions with a potentially minutes-long
    pack between them. The whole sequence runs under the per-server lifecycle lock
    (#827): ``StartServer`` takes the same lock for its desired-state flip, so a
    start cannot flip ``desired=running`` between the at-rest check and the commit —
    it blocks until this delete releases, then 409s on the now-deleted (or still
    at-rest) row. The final transaction still re-checks ``is_at_rest()`` as
    belt-and-suspenders — redundant while the real lock is wired, but it keeps the
    never-delete-a-running-server invariant if the lock is ever a no-op.

    The backups list is read in that SAME final transaction (not before the pack):
    a backup created during the pack window would otherwise be neither the head nor
    in the deletable tail and would survive as a third orphan archive. Reading it
    fresh keeps the "latest existing at delete time" head and prunes every other
    archive before the rows cascade.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore
    lifecycle_lock: LifecycleLock = NullLifecycleLock()
    # Forget the server's in-memory Bedrock tunnel credential on delete (issue
    # #1544), or a stale entry lingers and keeps validating for a gone server.
    # Defaults to a no-op so non-relay construction sites need not wire it.
    bedrock_tunnel: BedrockTunnelCredentials = NullBedrockTunnelCredentials()

    async def __call__(self, *, community_id: CommunityId, server_id: ServerId) -> None:
        # Hold the per-server lifecycle lock across the at-rest check, the
        # (possibly minutes-long) pack, and the final row-delete commit (issue
        # #827): a start cannot flip desired=running anywhere in this window, since
        # StartServer takes the same lock for its desired-state flip and blocks
        # until this delete releases. This closes the check-then-pack TOCTOU; the
        # second-transaction re-check below is now belt-and-suspenders (it only
        # fires if the lock is ever a no-op), kept rather than removed.
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                server = await self.uow.servers.get_by_id(server_id)
                if server is None or server.community_id != community_id:
                    raise ServerNotFoundError(str(server_id.value))
                if not server.is_at_rest():
                    raise ServerNotStoppedError(str(server_id.value))
            # Pack the working set into the retained final tar.gz first. This is
            # mandatory and fail-closed (#777): if it raises, the row is untouched
            # and the whole delete fails rather than dropping the latest state.
            await self.backup_store.prune_to_final_snapshot(
                community_id=community_id, server_id=server_id
            )
            async with self.uow:
                # Re-check at-rest in the final transaction as belt-and-suspenders:
                # the lifecycle lock already blocks a concurrent start for the whole
                # delete, so this cannot fire in normal operation, but it keeps the
                # never-delete-a-running-server invariant if the lock is ever
                # mis-wired (e.g. a NullLifecycleLock), independent of the lock.
                server = await self.uow.servers.get_by_id(server_id)
                if server is None or server.community_id != community_id:
                    raise ServerNotFoundError(str(server_id.value))
                if not server.is_at_rest():
                    raise ServerNotStoppedError(str(server_id.value))
                # Filesystem-driven archive prune (#1707): list every archive
                # ref that physically exists on storage, regardless of whether a
                # metadata row tracks it. This catches both row-tracked archives
                # and orphans (archives written before their row committed, or
                # whose row commit failed). The newest DB row's ref is the
                # retained head; every other physical archive is deleted.
                backups = await self.uow.backups.list_for_server(server_id)
                head_ref = backups[0].storage_ref if backups else None
                all_refs = await self.backup_store.list_archive_refs(
                    community_id=community_id, server_id=server_id
                )
                for ref in all_refs:
                    if ref != head_ref:
                        await self.backup_store.delete(
                            community_id=community_id,
                            server_id=server_id,
                            storage_ref=ref,
                        )
                await self.uow.servers.delete(server_id)
                # No FK on resource_grant.resource_id, so the server delete does not
                # cascade; sweep the grants in the same transaction (Section 10).
                await self.uow.resource_grants.delete_for_resource(
                    _SERVER_RESOURCE_TYPE, server_id.value
                )
                await self.uow.commit()
        # Forget the deleted server's Bedrock tunnel credential (issue #1544).
        # After the commit and outside the lock: the row is gone, so an at-rest
        # server that had a lingering entry (e.g. left at observed=unknown) can
        # never re-mint it, and evicting before a failed commit would drop a
        # still-live server's credential. Idempotent when none is held.
        self.bedrock_tunnel.close(server_id)
