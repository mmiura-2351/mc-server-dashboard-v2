"""Composition root: the single place adapters are bound to Ports.

This is the edge wiring (ARCHITECTURE.md Section 2.1). It is the only module
allowed to import ``adapters`` alongside ``application``/``domain`` and to read
configuration. Routers depend on the Port-returning provider functions here via
FastAPI's ``Depends``; tests override the providers to inject fakes.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mc_server_dashboard_api.audit.adapters.clock import (
    SystemClock as AuditSystemClock,
)
from mc_server_dashboard_api.audit.adapters.query import SqlAlchemyAuditQuery
from mc_server_dashboard_api.audit.adapters.recorder import LoggingAuditRecorder
from mc_server_dashboard_api.audit.adapters.writer import SqlAlchemyAuditWriter
from mc_server_dashboard_api.audit.application.list_audit_log import ListAuditLog
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.community.adapters.clock import (
    SystemClock as CommunitySystemClock,
)
from mc_server_dashboard_api.community.adapters.permission_checker import (
    RepositoryMembershipVisibility,
    RoleGrantPermissionChecker,
)
from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.adapters.user_directory import (
    IdentityUserDirectory,
)
from mc_server_dashboard_api.community.application.list_my_communities import (
    ListMyCommunities,
)
from mc_server_dashboard_api.community.application.manage_community import (
    DeleteCommunity,
    ReadCommunity,
    RenameCommunity,
)
from mc_server_dashboard_api.community.application.manage_grant import (
    CreateGrant,
    ListGrants,
    RevokeGrant,
)
from mc_server_dashboard_api.community.application.manage_membership import (
    AddMember,
    AssignRole,
    ListMembers,
    RemoveMember,
    UnassignRole,
)
from mc_server_dashboard_api.community.application.manage_role import (
    CreateRole,
    DeleteRole,
    ListRoles,
    ReadRole,
    UpdateRole,
)
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.application.read_my_permissions import (
    ReadMyEffectivePermissions,
)
from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
    PermissionChecker,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    ResourceRef,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    UserId as CommunityUserId,
)
from mc_server_dashboard_api.config import (
    BruteForceSettings,
    PasswordSettings,
    RegistrationSettings,
    Settings,
    TokenSettings,
)
from mc_server_dashboard_api.core.adapters.database import (
    SqlAlchemyDatabasePing,
    create_session_factory,
)
from mc_server_dashboard_api.core.adapters.readiness import (
    FlagControlPlaneReadiness,
)
from mc_server_dashboard_api.core.domain.health import DatabasePing
from mc_server_dashboard_api.core.domain.readiness import ControlPlaneReadiness
from mc_server_dashboard_api.fleet.application.list_workers import ListWorkers
from mc_server_dashboard_api.fleet.application.set_worker_drain import SetWorkerDrain
from mc_server_dashboard_api.fleet.domain.control_plane import (
    ControlPlane as FleetControlPlane,
)
from mc_server_dashboard_api.fleet.domain.real_time_events import RealTimeEvents
from mc_server_dashboard_api.fleet.domain.registry import WorkerRegistry
from mc_server_dashboard_api.identity.adapters.client_ip import (
    forwarded_for_header,
    resolve_client_ip,
)
from mc_server_dashboard_api.identity.adapters.clock import SystemClock
from mc_server_dashboard_api.identity.adapters.common_passwords import (
    load_common_passwords,
)
from mc_server_dashboard_api.identity.adapters.community_ownership import (
    CommunityBackedOwnership,
)
from mc_server_dashboard_api.identity.adapters.login_attempt_store import (
    SqlAlchemyLoginAttemptStore,
)
from mc_server_dashboard_api.identity.adapters.login_failure_delay import (
    FixedLoginFailureDelay,
)
from mc_server_dashboard_api.identity.adapters.password_hasher import (
    Argon2PasswordHasher,
    BcryptPasswordHasher,
)
from mc_server_dashboard_api.identity.adapters.sleeper import AsyncioSleeper
from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.identity.application.admin_delete_user import (
    AdminDeleteUser,
)
from mc_server_dashboard_api.identity.application.authenticate_request import (
    AuthenticateRequest,
)
from mc_server_dashboard_api.identity.application.change_password import ChangePassword
from mc_server_dashboard_api.identity.application.delete_account import DeleteAccount
from mc_server_dashboard_api.identity.application.list_users import ListUsers
from mc_server_dashboard_api.identity.application.login import Login
from mc_server_dashboard_api.identity.application.logout import Logout
from mc_server_dashboard_api.identity.application.refresh_session import RefreshSession
from mc_server_dashboard_api.identity.application.register_user import RegisterUser
from mc_server_dashboard_api.identity.application.set_platform_admin import (
    SetPlatformAdmin,
)
from mc_server_dashboard_api.identity.application.set_user_active import SetUserActive
from mc_server_dashboard_api.identity.application.update_profile import UpdateProfile
from mc_server_dashboard_api.identity.domain.brute_force import BruteForceConfig
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import InvalidAccessTokenError
from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.registration import RegistrationConfig
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.clock import (
    SystemClock as ServersSystemClock,
)
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.adapters.file_store import (
    StorageFileStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.jar_provisioner import (
    CatalogJarProvisioner,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.adapters.version_validator import (
    CatalogVersionValidator,
)
from mc_server_dashboard_api.servers.application.backups import (
    CreateBackup,
    DeleteBackup,
    DownloadBackup,
    GlobalBackupStatistics,
    ListBackups,
    RestoreBackup,
    ServerBackupStatistics,
    UploadBackup,
)
from mc_server_dashboard_api.servers.application.export_import import (
    ExportServer,
    ImportServer,
)
from mc_server_dashboard_api.servers.application.files import (
    DeleteFile,
    DownloadFile,
    ListDir,
    ListFileVersions,
    MakeDir,
    ReadFile,
    RenameFile,
    RollbackFile,
    SearchFiles,
    UploadFile,
    WriteFile,
)
from mc_server_dashboard_api.servers.application.groups import (
    AddPlayer,
    AttachGroup,
    CreateGroup,
    DeleteGroup,
    DetachGroup,
    ListGroups,
    ListGroupServers,
    ListServerGroups,
    ReadGroup,
    RemovePlayer,
    RenameGroup,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    RestartServer,
    SendServerCommand,
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    DeleteServer,
    ListServers,
    LookupServerCommunity,
    ReadServer,
    UpdateServer,
)
from mc_server_dashboard_api.servers.application.port_availability import (
    CheckPort,
    ListAvailablePorts,
)
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.backup_store import (
    BackupArchiveStore,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    ControlPlane as ServersControlPlane,
)
from mc_server_dashboard_api.servers.domain.file_store import (
    FileStore as ServersFileStore,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.versions.adapters.clock import (
    SystemClock as VersionsSystemClock,
)
from mc_server_dashboard_api.versions.adapters.http_jar_fetcher import HttpxJarFetcher
from mc_server_dashboard_api.versions.adapters.server_jar_references import (
    ServerJarReferences,
)
from mc_server_dashboard_api.versions.adapters.storage_jar_pool import StorageJarPool
from mc_server_dashboard_api.versions.application.catalog_refresh import CatalogRefresh
from mc_server_dashboard_api.versions.application.ensure_jar import EnsureJar
from mc_server_dashboard_api.versions.application.jar_gc import RunJarPoolGc
from mc_server_dashboard_api.versions.application.jar_pool_stats import GetJarPoolStats
from mc_server_dashboard_api.versions.application.list_versions import (
    ListServerTypes,
    ListVersions,
)
from mc_server_dashboard_api.versions.domain.cache import CacheInvalidator
from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.value_objects import (
    ServerType as CatalogServerType,
)


def get_engine(request: Request) -> AsyncEngine:
    """Return the async engine the app factory stored on application state."""

    engine: AsyncEngine = request.app.state.engine
    return engine


def get_settings(request: Request) -> Settings:
    """Return the resolved settings the app factory stored on application state."""

    settings: Settings = request.app.state.settings
    return settings


def get_storage(request: Request) -> Storage:
    """Return the process-wide :class:`Storage` Port adapter from app state.

    Bound to the config-selected backend by the app factory; the data-plane
    endpoints stream hydrate/snapshot bytes through it (issue #106).
    """

    storage: Storage = request.app.state.storage
    return storage


def get_version_catalog(request: Request) -> VersionCatalog:
    """Return the process-wide :class:`VersionCatalog` from app state.

    Built once by the app factory (with its in-process manifest cache, FR-VER-2);
    the catalog endpoints and the ensure-on-start use case read it.
    """

    catalog: VersionCatalog = request.app.state.version_catalog
    return catalog


def get_list_versions(
    catalog: Annotated[VersionCatalog, Depends(get_version_catalog)],
) -> ListVersions:
    """Assemble the :class:`ListVersions` use case (catalog read, FR-VER-1)."""

    return ListVersions(catalog=catalog)


def get_list_server_types() -> ListServerTypes:
    """Assemble the :class:`ListServerTypes` use case (catalog read, FR-VER-1)."""

    return ListServerTypes()


def get_ensure_jar(
    request: Request,
    catalog: Annotated[VersionCatalog, Depends(get_version_catalog)],
) -> EnsureJar:
    """Assemble the :class:`EnsureJar` use case (ensure-on-start, FR-VER-3).

    Binds the catalog, the httpx JAR downloader, and the versions ``JarPool`` seam
    bound to the process-wide storage ``JarStore`` (content-addressed reuse).
    """

    return EnsureJar(
        catalog=catalog,
        fetcher=HttpxJarFetcher(),
        pool=StorageJarPool(jars=get_storage(request)),
    )


def get_catalog_refresh(request: Request) -> CatalogRefresh:
    """Assemble the :class:`CatalogRefresh` use case (manual refresh, issue #286).

    Binds the catalog's manifest-cache invalidator and the per-type source-prefix
    map the app factory stored on app state; the route is platform-admin gated.
    """

    invalidator: CacheInvalidator = request.app.state.version_cache_invalidator
    prefixes: dict[CatalogServerType, str] = request.app.state.version_source_prefixes
    return CatalogRefresh(invalidator=invalidator, prefixes=prefixes)


def get_jar_pool_stats(request: Request) -> GetJarPoolStats:
    """Assemble the :class:`GetJarPoolStats` use case (JAR-pool stats, issue #286)."""

    return GetJarPoolStats(pool=StorageJarPool(jars=get_storage(request)))


def get_jar_pool_gc(request: Request) -> RunJarPoolGc:
    """Assemble the :class:`RunJarPoolGc` use case (JAR-pool GC, issue #293).

    The pool seam over storage's ``JarStore`` (list + delete) plus the live
    reference set read from server rows (the ``ServerJarReferences`` adapter over
    the servers repository). The same use case the periodic loop drives.
    """

    session_factory = create_session_factory(get_engine(request))
    return RunJarPoolGc(
        pool=StorageJarPool(jars=get_storage(request)),
        references=ServerJarReferences(uow=ServersUnitOfWork(session_factory)),
        clock=VersionsSystemClock(),
    )


# An async lookup of a server's recorded resolved-JAR content key (SHA-256), or
# None if unset. The data-plane hydrate endpoint uses it to inject ``server.jar``
# into the working-set tar (issue #118).
ResolvedJarLookup = Callable[[uuid.UUID, uuid.UUID], Awaitable[str | None]]


def get_resolved_jar_lookup(request: Request) -> ResolvedJarLookup:
    """Provide a lookup of a server's recorded resolved-JAR content key (#118).

    Reads the server row's ``config`` blob for the JAR reference StartServer
    recorded (``JAR_KEY_CONFIG_FIELD``). Returns ``None`` when the server is unknown
    or has no resolved JAR yet, so the hydrate endpoint sends the working set alone.
    """

    from mc_server_dashboard_api.servers.domain.value_objects import (
        JAR_KEY_CONFIG_FIELD,
    )
    from mc_server_dashboard_api.servers.domain.value_objects import (
        ServerId as ServersServerId,
    )

    session_factory = create_session_factory(get_engine(request))

    async def _lookup(community_id: uuid.UUID, server_id: uuid.UUID) -> str | None:
        async with ServersUnitOfWork(session_factory) as uow:
            server = await uow.servers.get_by_id(ServersServerId(server_id))
        if server is None or server.community_id.value != community_id:
            return None
        value = server.config.get(JAR_KEY_CONFIG_FIELD)
        return value if isinstance(value, str) else None

    return _lookup


def get_worker_registry(request: Request) -> WorkerRegistry:
    """Return the process-wide WorkerRegistry stored on application state.

    The same instance is fed by the control-plane gRPC server and read by the
    platform-admin endpoint, so both observe one live view of the fleet.
    """

    registry: WorkerRegistry = request.app.state.worker_registry
    return registry


def get_real_time_events(websocket: WebSocket) -> RealTimeEvents:
    """Return the process-wide in-process real-time event bus from app state.

    The same instance the control-plane gRPC servicer publishes onto; the
    WebSocket events endpoint subscribes through it (FR-MON-1..4). Injected from
    the ``WebSocket`` connection, the only consumer of this Port.
    """

    bus: RealTimeEvents = websocket.app.state.real_time_events
    return bus


ServerCommunityLookup = Callable[..., Awaitable[uuid.UUID | None]]


def get_server_community_lookup(websocket: WebSocket) -> ServerCommunityLookup:
    """Provide a lookup of which community a server belongs to, by id (#288).

    The community-scoped events stream reads a firehose of every server's status
    events and routes each by community; this resolves a worker-reported server
    id (the UUID's text form) to its owning community UUID, or ``None`` when the
    id is unknown or not a UUID. Injected from the ``WebSocket`` connection (the
    only consumer); the endpoint caches results per connection to bound queries.
    """

    from mc_server_dashboard_api.servers.domain.value_objects import (
        ServerId as ServersServerId,
    )

    engine: AsyncEngine = websocket.app.state.engine
    session_factory = create_session_factory(engine)
    use_case = LookupServerCommunity(uow=ServersUnitOfWork(session_factory))

    async def _lookup(*, server_id: str) -> uuid.UUID | None:
        try:
            sid = ServersServerId(uuid.UUID(server_id))
        except ValueError:
            return None
        community = await use_case(server_id=sid)
        return community.value if community is not None else None

    return _lookup


def get_list_workers(
    registry: Annotated[WorkerRegistry, Depends(get_worker_registry)],
) -> ListWorkers:
    """Assemble the :class:`ListWorkers` use case (platform-admin only)."""

    return ListWorkers(registry=registry)


def get_set_worker_drain(
    registry: Annotated[WorkerRegistry, Depends(get_worker_registry)],
) -> SetWorkerDrain:
    """Assemble the :class:`SetWorkerDrain` use case (platform-admin only)."""

    return SetWorkerDrain(registry=registry)


def get_database_ping(request: Request) -> DatabasePing:
    """Bind the :class:`DatabasePing` Port to its SQLAlchemy adapter."""

    return SqlAlchemyDatabasePing(get_engine(request))


def get_control_plane_readiness(request: Request) -> ControlPlaneReadiness:
    """Bind the :class:`ControlPlaneReadiness` Port from the enabled+started flags.

    Enabled is config; started is the ``grpc_started`` flag the app factory sets
    on app state once the control-plane gRPC server has started (issue #282). When
    the control plane is disabled the component is trivially ready.
    """

    enabled = get_settings(request).control.enabled
    started = bool(getattr(request.app.state, "grpc_started", False))
    return FlagControlPlaneReadiness(enabled=enabled, started=started)


def get_metrics_session_factory(
    request: Request,
) -> async_sessionmaker[AsyncSession]:
    """Provide a short-lived session factory for the /metrics scrape query (#282)."""

    return create_session_factory(get_engine(request))


def get_audit_recorder(request: Request) -> AuditRecorder:
    """Bind the must-not-raise :class:`AuditRecorder` (FR-AUD-2).

    Wraps the SQLAlchemy ``AuditWriter`` (own short transaction per write,
    independent of any business UoW) in the logging recorder that swallows write
    failures. Routes inject this and call it *after* their UoW commit with a typed
    :class:`AuditEvent`.
    """

    session_factory = create_session_factory(get_engine(request))
    return LoggingAuditRecorder(
        SqlAlchemyAuditWriter(session_factory, clock=AuditSystemClock())
    )


def get_list_audit_log(request: Request) -> ListAuditLog:
    """Assemble the :class:`ListAuditLog` use case (audit query, FR-AUD-3)."""

    session_factory = create_session_factory(get_engine(request))
    return ListAuditLog(query=SqlAlchemyAuditQuery(session_factory))


def _build_password_hasher(password: PasswordSettings) -> PasswordHasher:
    """Construct the PasswordHasher adapter named by ``auth.password.hash``."""

    if password.hash == "bcrypt":
        return BcryptPasswordHasher()
    return Argon2PasswordHasher()


# A throwaway value to derive the dummy verification hash from. Verifying a real
# password against this hash always fails; its only purpose is to give the
# unknown-user login path the same cost as a wrong-password verify.
_DUMMY_VERIFY_PLAINTEXT = "dummy-password-for-timing-equalization"


@lru_cache(maxsize=2)
def _dummy_password_hash(algorithm: str) -> str:
    """Pre-compute the static dummy hash for ``algorithm`` once (login timing)."""

    hasher = BcryptPasswordHasher() if algorithm == "bcrypt" else Argon2PasswordHasher()
    return hasher.hash(_DUMMY_VERIFY_PLAINTEXT)


@lru_cache(maxsize=1)
def _common_passwords() -> frozenset[str]:
    """Load the common-password blocklist once and reuse it across requests."""

    return load_common_passwords()


def _build_password_policy(password: PasswordSettings) -> PasswordPolicy:
    """Build the pure :class:`PasswordPolicy` from the configured knobs."""

    common = _common_passwords() if password.check_common_list else frozenset()
    # bcrypt ignores bytes past 72; cap the effective byte length so two passwords
    # sharing a 72-byte prefix cannot collide. argon2 has no such limit.
    max_bytes = 72 if password.hash == "bcrypt" else None
    return PasswordPolicy(
        min_length=password.min_length,
        max_length=password.max_length,
        max_bytes=max_bytes,
        require_complexity=password.require_complexity,
        check_common_list=password.check_common_list,
        forbid_user_info=password.forbid_user_info,
        forbid_simple_patterns=password.forbid_simple_patterns,
        common_passwords=common,
    )


def build_registration_config(
    registration: RegistrationSettings,
) -> RegistrationConfig:
    """Map the ``auth.registration.*`` knobs to the domain config value."""

    return RegistrationConfig(
        open=registration.open,
        ip_limit_enabled=registration.ip_limit_enabled,
        ip_threshold=registration.ip_threshold,
        ip_window=dt.timedelta(seconds=registration.ip_window_seconds),
    )


def get_register_user(request: Request) -> RegisterUser:
    """Assemble the :class:`RegisterUser` use case from config-selected adapters.

    Binds the per-IP registration cap to the same ``login_attempt``-backed store
    FR-AUTH-4's login throttle uses (issue #362).
    """

    settings = get_settings(request)
    session_factory = create_session_factory(get_engine(request))
    return RegisterUser(
        uow=SqlAlchemyUnitOfWork(session_factory),
        hasher=_build_password_hasher(settings.auth.password),
        clock=SystemClock(),
        policy=_build_password_policy(settings.auth.password),
        attempts=SqlAlchemyLoginAttemptStore(session_factory),
        registration=build_registration_config(settings.auth.registration),
    )


def get_change_password(request: Request) -> ChangePassword:
    """Assemble the :class:`ChangePassword` use case from config-selected adapters."""

    settings = get_settings(request)
    session_factory = create_session_factory(get_engine(request))
    return ChangePassword(
        uow=SqlAlchemyUnitOfWork(session_factory),
        hasher=_build_password_hasher(settings.auth.password),
        clock=SystemClock(),
        policy=_build_password_policy(settings.auth.password),
    )


def get_update_profile(request: Request) -> UpdateProfile:
    """Assemble the :class:`UpdateProfile` use case from config-selected adapters."""

    session_factory = create_session_factory(get_engine(request))
    return UpdateProfile(
        uow=SqlAlchemyUnitOfWork(session_factory),
        clock=SystemClock(),
    )


def get_delete_account(request: Request) -> DeleteAccount:
    """Assemble the :class:`DeleteAccount` use case from config-selected adapters.

    Binds the community-ownership seam to the community store so the self-delete
    guard can refuse a community owner without identity importing community.
    """

    session_factory = create_session_factory(get_engine(request))
    return DeleteAccount(
        uow=SqlAlchemyUnitOfWork(session_factory),
        ownership=CommunityBackedOwnership(CommunityUnitOfWork(session_factory)),
        clock=SystemClock(),
    )


def get_list_users(request: Request) -> ListUsers:
    """Assemble the :class:`ListUsers` use case (platform-admin only, #278)."""

    session_factory = create_session_factory(get_engine(request))
    return ListUsers(uow=SqlAlchemyUnitOfWork(session_factory))


def get_set_user_active(request: Request) -> SetUserActive:
    """Assemble the :class:`SetUserActive` use case (platform-admin only, #278)."""

    session_factory = create_session_factory(get_engine(request))
    return SetUserActive(
        uow=SqlAlchemyUnitOfWork(session_factory),
        clock=SystemClock(),
    )


def get_admin_delete_user(request: Request) -> AdminDeleteUser:
    """Assemble the :class:`AdminDeleteUser` use case (platform-admin only, #278).

    Binds the community-ownership seam to the community store so the admin delete
    refuses a community owner, the same as the self-delete guard.
    """

    session_factory = create_session_factory(get_engine(request))
    return AdminDeleteUser(
        uow=SqlAlchemyUnitOfWork(session_factory),
        ownership=CommunityBackedOwnership(CommunityUnitOfWork(session_factory)),
        clock=SystemClock(),
    )


def get_set_platform_admin(request: Request) -> SetPlatformAdmin:
    """Assemble the :class:`SetPlatformAdmin` use case (platform-admin only, #278)."""

    session_factory = create_session_factory(get_engine(request))
    return SetPlatformAdmin(
        uow=SqlAlchemyUnitOfWork(session_factory),
        clock=SystemClock(),
    )


def _build_token_service(token: TokenSettings, clock: SystemClock) -> TokenService:
    """Construct the JWT TokenService adapter from ``auth.token.*``.

    The signing key is required to mount the auth endpoints; the app factory
    enforces that at startup, so it is non-None here.
    """

    assert token.signing_key is not None
    return JwtTokenService(
        signing_key=token.signing_key,
        algorithm=token.algorithm,
        access_ttl=dt.timedelta(seconds=token.access_ttl_seconds),
        clock=clock,
    )


def build_brute_force_config(brute_force: BruteForceSettings) -> BruteForceConfig:
    """Map the ``auth.brute_force.*`` knobs to the domain config value."""

    return BruteForceConfig(
        enabled=brute_force.enabled,
        username_threshold=brute_force.username_threshold,
        username_window=dt.timedelta(seconds=brute_force.username_window_seconds),
        ip_threshold=brute_force.ip_threshold,
        ip_window=dt.timedelta(seconds=brute_force.ip_window_seconds),
        lockout_base=dt.timedelta(seconds=brute_force.lockout_base_seconds),
        lockout_max=dt.timedelta(seconds=brute_force.lockout_max_seconds),
        delay=dt.timedelta(milliseconds=brute_force.delay_ms),
    )


def get_login(request: Request) -> Login:
    """Assemble the :class:`Login` use case from config-selected adapters."""

    settings = get_settings(request)
    clock = SystemClock()
    session_factory = create_session_factory(get_engine(request))
    brute_force = build_brute_force_config(settings.auth.brute_force)
    return Login(
        uow=SqlAlchemyUnitOfWork(session_factory),
        attempts=SqlAlchemyLoginAttemptStore(session_factory),
        brute_force=brute_force,
        hasher=_build_password_hasher(settings.auth.password),
        dummy_password_hash=_dummy_password_hash(settings.auth.password.hash),
        tokens=_build_token_service(settings.auth.token, clock),
        clock=clock,
        failure_delay=FixedLoginFailureDelay(
            delay=brute_force.delay, sleeper=AsyncioSleeper()
        ),
        refresh_ttl=dt.timedelta(seconds=settings.auth.token.refresh_ttl_seconds),
        registration=build_registration_config(settings.auth.registration),
    )


def get_client_ip(request: Request) -> str | None:
    """Resolve the trustworthy client IP for the request (SECURITY.md Section 4).

    Honors the forwarded-for header only from configured trusted peers; otherwise
    the immediate socket peer. Feeds the per-IP brute-force counter.
    """

    proxy = get_settings(request).auth.proxy
    peer_ip = request.client.host if request.client is not None else None
    return resolve_client_ip(
        peer_ip=peer_ip,
        forwarded_for=forwarded_for_header(request.headers),
        trust_forwarded_headers=proxy.trust_forwarded_headers,
        trusted_proxies=proxy.trusted_proxies,
    )


def get_refresh_session(request: Request) -> RefreshSession:
    """Assemble the :class:`RefreshSession` use case from config-selected adapters."""

    settings = get_settings(request)
    clock = SystemClock()
    session_factory = create_session_factory(get_engine(request))
    return RefreshSession(
        uow=SqlAlchemyUnitOfWork(session_factory),
        tokens=_build_token_service(settings.auth.token, clock),
        clock=clock,
        refresh_ttl=dt.timedelta(seconds=settings.auth.token.refresh_ttl_seconds),
        reuse_grace=dt.timedelta(
            seconds=settings.auth.token.refresh_reuse_grace_seconds
        ),
    )


def get_logout(request: Request) -> Logout:
    """Assemble the :class:`Logout` use case from config-selected adapters."""

    settings = get_settings(request)
    clock = SystemClock()
    session_factory = create_session_factory(get_engine(request))
    return Logout(
        uow=SqlAlchemyUnitOfWork(session_factory),
        tokens=_build_token_service(settings.auth.token, clock),
        clock=clock,
    )


def get_authenticate_request(request: Request) -> AuthenticateRequest:
    """Assemble the :class:`AuthenticateRequest` use case (current-user lookup)."""

    settings = get_settings(request)
    session_factory = create_session_factory(get_engine(request))
    return AuthenticateRequest(
        uow=SqlAlchemyUnitOfWork(session_factory),
        tokens=_build_token_service(settings.auth.token, SystemClock()),
    )


# Extracts the ``Authorization: Bearer <token>`` header; a missing/blank header
# yields 403 by default. ``auto_error=False`` lets us return a uniform 401.
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    use_case: Annotated[AuthenticateRequest, Depends(get_authenticate_request)],
) -> User:
    """FastAPI dependency: the authenticated user behind a Bearer access token.

    Every protected endpoint depends on this. A missing, malformed, or expired
    token is a uniform 401 (no detail that aids enumeration).
    """

    if credentials is None:
        raise _unauthenticated()
    try:
        return await use_case(access_token=credentials.credentials)
    except InvalidAccessTokenError as exc:
        raise _unauthenticated() from exc


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid_token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _ws_access_token(websocket: WebSocket) -> str | None:
    """Resolve the Bearer access token from a WebSocket handshake.

    Browsers cannot set request headers on a WebSocket upgrade, so the token is
    accepted from the ``token`` query parameter; the ``Authorization: Bearer``
    header is also honoured for non-browser clients (and the test client).
    """

    header = websocket.headers.get("authorization")
    if header is not None and header.startswith("Bearer "):
        return header[len("Bearer ") :]
    return websocket.query_params.get("token")


async def get_current_user_ws(websocket: WebSocket) -> User | None:
    """The authenticated user behind a WebSocket handshake, or ``None``.

    Returns ``None`` for a missing/malformed/expired token rather than raising:
    a WebSocket route must reject by closing the connection with a policy code
    (it cannot return an HTTP status once the upgrade is in flight), so the route
    decides the close, not an exception (Section 6.13).
    """

    token = _ws_access_token(websocket)
    if token is None:
        return None
    settings: Settings = websocket.app.state.settings
    engine: AsyncEngine = websocket.app.state.engine
    use_case = AuthenticateRequest(
        uow=SqlAlchemyUnitOfWork(create_session_factory(engine)),
        tokens=_build_token_service(settings.auth.token, SystemClock()),
    )
    try:
        return await use_case(access_token=token)
    except InvalidAccessTokenError:
        return None


# --- authorization (community context, Section 6.4) ------------------------


def get_membership_visibility(request: Request) -> MembershipVisibility:
    """Bind the Layer-1 :class:`MembershipVisibility` Port to its evaluator."""

    session_factory = create_session_factory(get_engine(request))
    return RepositoryMembershipVisibility(CommunityUnitOfWork(session_factory))


def get_permission_checker(request: Request) -> PermissionChecker:
    """Bind the Layer-2 :class:`PermissionChecker` Port to the role+grant evaluator."""

    session_factory = create_session_factory(get_engine(request))
    return RoleGrantPermissionChecker(CommunityUnitOfWork(session_factory))


def get_provision_community(request: Request) -> ProvisionCommunity:
    """Assemble the :class:`ProvisionCommunity` use case (platform-admin only)."""

    session_factory = create_session_factory(get_engine(request))
    return ProvisionCommunity(
        uow=CommunityUnitOfWork(session_factory),
        users=IdentityUserDirectory(SqlAlchemyUnitOfWork(session_factory)),
        clock=CommunitySystemClock(),
    )


def get_read_community(request: Request) -> ReadCommunity:
    """Assemble the :class:`ReadCommunity` use case."""

    session_factory = create_session_factory(get_engine(request))
    return ReadCommunity(uow=CommunityUnitOfWork(session_factory))


def get_rename_community(request: Request) -> RenameCommunity:
    """Assemble the :class:`RenameCommunity` use case."""

    session_factory = create_session_factory(get_engine(request))
    return RenameCommunity(
        uow=CommunityUnitOfWork(session_factory),
        clock=CommunitySystemClock(),
    )


def get_delete_community(request: Request) -> DeleteCommunity:
    """Assemble the :class:`DeleteCommunity` use case."""

    session_factory = create_session_factory(get_engine(request))
    return DeleteCommunity(uow=CommunityUnitOfWork(session_factory))


def get_list_my_communities(request: Request) -> ListMyCommunities:
    """Assemble the :class:`ListMyCommunities` use case (FR-MEM-4)."""

    session_factory = create_session_factory(get_engine(request))
    return ListMyCommunities(uow=CommunityUnitOfWork(session_factory))


def get_read_my_effective_permissions(request: Request) -> ReadMyEffectivePermissions:
    """Assemble the :class:`ReadMyEffectivePermissions` use case (issue #354)."""

    session_factory = create_session_factory(get_engine(request))
    return ReadMyEffectivePermissions(uow=CommunityUnitOfWork(session_factory))


def get_add_member(request: Request) -> AddMember:
    """Assemble the :class:`AddMember` use case (FR-MEM-1)."""

    session_factory = create_session_factory(get_engine(request))
    return AddMember(
        uow=CommunityUnitOfWork(session_factory),
        users=IdentityUserDirectory(SqlAlchemyUnitOfWork(session_factory)),
        clock=CommunitySystemClock(),
    )


def get_remove_member(request: Request) -> RemoveMember:
    """Assemble the :class:`RemoveMember` use case (FR-MEM-3)."""

    session_factory = create_session_factory(get_engine(request))
    return RemoveMember(uow=CommunityUnitOfWork(session_factory))


def get_list_members(request: Request) -> ListMembers:
    """Assemble the :class:`ListMembers` use case (member:read).

    Binds the user-directory seam so the listing is enriched with usernames in one
    batch lookup against the identity store (issue #78).
    """

    session_factory = create_session_factory(get_engine(request))
    return ListMembers(
        uow=CommunityUnitOfWork(session_factory),
        users=IdentityUserDirectory(SqlAlchemyUnitOfWork(session_factory)),
    )


def get_assign_role(request: Request) -> AssignRole:
    """Assemble the :class:`AssignRole` use case (role:manage)."""

    session_factory = create_session_factory(get_engine(request))
    return AssignRole(uow=CommunityUnitOfWork(session_factory))


def get_unassign_role(request: Request) -> UnassignRole:
    """Assemble the :class:`UnassignRole` use case (role:manage)."""

    session_factory = create_session_factory(get_engine(request))
    return UnassignRole(uow=CommunityUnitOfWork(session_factory))


def get_list_roles(request: Request) -> ListRoles:
    """Assemble the :class:`ListRoles` use case (role:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ListRoles(uow=CommunityUnitOfWork(session_factory))


def get_read_role(request: Request) -> ReadRole:
    """Assemble the :class:`ReadRole` use case (role:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ReadRole(uow=CommunityUnitOfWork(session_factory))


def get_create_role(request: Request) -> CreateRole:
    """Assemble the :class:`CreateRole` use case (role:manage)."""

    session_factory = create_session_factory(get_engine(request))
    return CreateRole(
        uow=CommunityUnitOfWork(session_factory),
        clock=CommunitySystemClock(),
    )


def get_update_role(request: Request) -> UpdateRole:
    """Assemble the :class:`UpdateRole` use case (role:manage)."""

    session_factory = create_session_factory(get_engine(request))
    return UpdateRole(
        uow=CommunityUnitOfWork(session_factory),
        clock=CommunitySystemClock(),
    )


def get_delete_role(request: Request) -> DeleteRole:
    """Assemble the :class:`DeleteRole` use case (role:manage)."""

    session_factory = create_session_factory(get_engine(request))
    return DeleteRole(uow=CommunityUnitOfWork(session_factory))


def get_list_grants(request: Request) -> ListGrants:
    """Assemble the :class:`ListGrants` use case (grant:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ListGrants(uow=CommunityUnitOfWork(session_factory))


def get_create_grant(request: Request) -> CreateGrant:
    """Assemble the :class:`CreateGrant` use case (grant:manage)."""

    session_factory = create_session_factory(get_engine(request))
    return CreateGrant(
        uow=CommunityUnitOfWork(session_factory),
        clock=CommunitySystemClock(),
    )


def get_revoke_grant(request: Request) -> RevokeGrant:
    """Assemble the :class:`RevokeGrant` use case (grant:manage)."""

    session_factory = create_session_factory(get_engine(request))
    return RevokeGrant(uow=CommunityUnitOfWork(session_factory))


def get_create_server(
    request: Request,
    catalog: Annotated[VersionCatalog, Depends(get_version_catalog)],
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> CreateServer:
    """Assemble the :class:`CreateServer` use case (server:create).

    Binds the version-validation seam to the global catalog so create rejects an
    unsupported type / unoffered version before staging the row (FR-VER-1), the
    file seam to Storage so create can seed the initial working set (server.properties
    + eula.txt, issues #243/#198), and the configured port range for game-port
    auto-assignment (issue #243).
    """

    session_factory = create_session_factory(get_engine(request))
    ports = get_settings(request).ports
    return CreateServer(
        uow=ServersUnitOfWork(session_factory),
        clock=ServersSystemClock(),
        version_validator=CatalogVersionValidator(catalog=catalog),
        file_store=file_store,
        port_range=PortRange(start=ports.range_start, end=ports.range_end),
    )


def get_read_server(request: Request) -> ReadServer:
    """Assemble the :class:`ReadServer` use case (server:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ReadServer(uow=ServersUnitOfWork(session_factory))


def get_list_servers(request: Request) -> ListServers:
    """Assemble the :class:`ListServers` use case (server:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ListServers(uow=ServersUnitOfWork(session_factory))


def get_update_server(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> UpdateServer:
    """Assemble the :class:`UpdateServer` use case (server:update).

    Binds the file seam to Storage and the configured port range so an at-rest
    game-port change can validate the new port and rewrite ``server-port`` in
    ``server.properties`` (issue #311), keeping the DB and bind port in sync.
    """

    session_factory = create_session_factory(get_engine(request))
    ports = get_settings(request).ports
    return UpdateServer(
        uow=ServersUnitOfWork(session_factory),
        clock=ServersSystemClock(),
        file_store=file_store,
        port_range=PortRange(start=ports.range_start, end=ports.range_end),
        # The per-server snapshot-interval override carried on config is validated
        # against the configured floor here (CONFIGURATION.md Section 5.4).
        min_interval_seconds=get_settings(request).snapshot.min_interval_seconds,
    )


def get_delete_server(request: Request) -> DeleteServer:
    """Assemble the :class:`DeleteServer` use case (server:delete)."""

    session_factory = create_session_factory(get_engine(request))
    return DeleteServer(uow=ServersUnitOfWork(session_factory))


def get_create_group(request: Request) -> CreateGroup:
    """Assemble the :class:`CreateGroup` use case (group:manage, issue #276)."""

    session_factory = create_session_factory(get_engine(request))
    return CreateGroup(uow=ServersUnitOfWork(session_factory))


def get_list_groups(request: Request) -> ListGroups:
    """Assemble the :class:`ListGroups` use case (group:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ListGroups(uow=ServersUnitOfWork(session_factory))


def get_read_group(request: Request) -> ReadGroup:
    """Assemble the :class:`ReadGroup` use case (group:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ReadGroup(uow=ServersUnitOfWork(session_factory))


def get_rename_group(request: Request) -> RenameGroup:
    """Assemble the :class:`RenameGroup` use case (group:manage)."""

    session_factory = create_session_factory(get_engine(request))
    return RenameGroup(uow=ServersUnitOfWork(session_factory))


def get_delete_group(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> DeleteGroup:
    """Assemble the :class:`DeleteGroup` use case (group:manage).

    Binds the file seam so deleting a group resyncs the at-rest servers it was
    attached to (its players leave their ops.json / whitelist.json).
    """

    session_factory = create_session_factory(get_engine(request))
    return DeleteGroup(uow=ServersUnitOfWork(session_factory), file_store=file_store)


def get_add_player(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> AddPlayer:
    """Assemble the :class:`AddPlayer` use case (group:manage)."""

    session_factory = create_session_factory(get_engine(request))
    return AddPlayer(uow=ServersUnitOfWork(session_factory), file_store=file_store)


def get_remove_player(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> RemovePlayer:
    """Assemble the :class:`RemovePlayer` use case (group:manage)."""

    session_factory = create_session_factory(get_engine(request))
    return RemovePlayer(uow=ServersUnitOfWork(session_factory), file_store=file_store)


def get_attach_group(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> AttachGroup:
    """Assemble the :class:`AttachGroup` use case (group:manage)."""

    session_factory = create_session_factory(get_engine(request))
    return AttachGroup(uow=ServersUnitOfWork(session_factory), file_store=file_store)


def get_detach_group(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> DetachGroup:
    """Assemble the :class:`DetachGroup` use case (group:manage)."""

    session_factory = create_session_factory(get_engine(request))
    return DetachGroup(uow=ServersUnitOfWork(session_factory), file_store=file_store)


def get_list_group_servers(request: Request) -> ListGroupServers:
    """Assemble the :class:`ListGroupServers` use case (group:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ListGroupServers(uow=ServersUnitOfWork(session_factory))


def get_list_server_groups(request: Request) -> ListServerGroups:
    """Assemble the :class:`ListServerGroups` use case (group:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ListServerGroups(uow=ServersUnitOfWork(session_factory))


def _port_range(request: Request) -> PortRange:
    ports = get_settings(request).ports
    return PortRange(start=ports.range_start, end=ports.range_end)


def get_check_port(request: Request) -> CheckPort:
    """Assemble the :class:`CheckPort` read use case (issue #243)."""

    session_factory = create_session_factory(get_engine(request))
    return CheckPort(
        uow=ServersUnitOfWork(session_factory), port_range=_port_range(request)
    )


def get_list_available_ports(request: Request) -> ListAvailablePorts:
    """Assemble the :class:`ListAvailablePorts` read use case (issue #243)."""

    session_factory = create_session_factory(get_engine(request))
    return ListAvailablePorts(
        uow=ServersUnitOfWork(session_factory), port_range=_port_range(request)
    )


def get_fleet_control_plane(request: Request) -> FleetControlPlane:
    """Return the process-wide fleet ``ControlPlane`` adapter from app state.

    The same instance the control-plane gRPC servicer dispatches through; the
    lifecycle use cases reach it via the servers control-plane seam below.
    """

    control_plane: FleetControlPlane = request.app.state.control_plane
    return control_plane


def get_servers_control_plane(
    request: Request,
    registry: Annotated[WorkerRegistry, Depends(get_worker_registry)],
    fleet_control_plane: Annotated[FleetControlPlane, Depends(get_fleet_control_plane)],
) -> ServersControlPlane:
    """Bind the servers control-plane seam to the registry + fleet control plane.

    The data-plane base URL and the shared Worker credential (the transfer token)
    are passed so the seam can build hydrate/snapshot transfer URLs (issue #106).
    """

    settings = get_settings(request)
    return FleetControlPlaneAdapter(
        registry=registry,
        control_plane=fleet_control_plane,
        data_plane_base_url=settings.server.public_base_url,
        worker_credential=settings.control.worker_credential,
    )


def get_start_server(
    request: Request,
    control_plane: Annotated[ServersControlPlane, Depends(get_servers_control_plane)],
    ensure_jar: Annotated[EnsureJar, Depends(get_ensure_jar)],
) -> StartServer:
    """Assemble the :class:`StartServer` use case (server:start).

    Binds the JAR-provisioning seam to the versions ``EnsureJar`` use case so start
    ensures the resolved JAR is pooled before placement (FR-VER-3).
    """

    session_factory = create_session_factory(get_engine(request))
    return StartServer(
        uow=ServersUnitOfWork(session_factory),
        control_plane=control_plane,
        clock=ServersSystemClock(),
        jar_provisioner=CatalogJarProvisioner(ensure_jar=ensure_jar),
    )


def get_stop_server(
    request: Request,
    control_plane: Annotated[ServersControlPlane, Depends(get_servers_control_plane)],
) -> StopServer:
    """Assemble the :class:`StopServer` use case (server:stop)."""

    session_factory = create_session_factory(get_engine(request))
    return StopServer(
        uow=ServersUnitOfWork(session_factory),
        control_plane=control_plane,
        clock=ServersSystemClock(),
    )


def get_restart_server(
    request: Request,
    control_plane: Annotated[ServersControlPlane, Depends(get_servers_control_plane)],
) -> RestartServer:
    """Assemble the :class:`RestartServer` use case (server:restart)."""

    session_factory = create_session_factory(get_engine(request))
    return RestartServer(
        uow=ServersUnitOfWork(session_factory),
        control_plane=control_plane,
        clock=ServersSystemClock(),
    )


def get_send_server_command(
    request: Request,
    control_plane: Annotated[ServersControlPlane, Depends(get_servers_control_plane)],
) -> SendServerCommand:
    """Assemble the :class:`SendServerCommand` use case (server:command)."""

    session_factory = create_session_factory(get_engine(request))
    return SendServerCommand(
        uow=ServersUnitOfWork(session_factory),
        control_plane=control_plane,
    )


def get_servers_file_store(
    storage: Annotated[Storage, Depends(get_storage)],
) -> ServersFileStore:
    """Bind the servers file seam to the authoritative Storage file slices."""

    return StorageFileStoreAdapter(storage=storage)


def get_read_file(
    request: Request,
    control_plane: Annotated[ServersControlPlane, Depends(get_servers_control_plane)],
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> ReadFile:
    """Assemble the :class:`ReadFile` use case (file:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ReadFile(
        uow=ServersUnitOfWork(session_factory),
        control_plane=control_plane,
        file_store=file_store,
    )


def get_list_dir(
    request: Request,
    control_plane: Annotated[ServersControlPlane, Depends(get_servers_control_plane)],
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> ListDir:
    """Assemble the :class:`ListDir` use case (file:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ListDir(
        uow=ServersUnitOfWork(session_factory),
        control_plane=control_plane,
        file_store=file_store,
    )


def get_write_file(
    request: Request,
    control_plane: Annotated[ServersControlPlane, Depends(get_servers_control_plane)],
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> WriteFile:
    """Assemble the :class:`WriteFile` use case (file:edit)."""

    session_factory = create_session_factory(get_engine(request))
    return WriteFile(
        uow=ServersUnitOfWork(session_factory),
        control_plane=control_plane,
        file_store=file_store,
    )


def get_list_file_versions(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> ListFileVersions:
    """Assemble the :class:`ListFileVersions` use case (file:history)."""

    session_factory = create_session_factory(get_engine(request))
    return ListFileVersions(
        uow=ServersUnitOfWork(session_factory),
        file_store=file_store,
    )


def get_rollback_file(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> RollbackFile:
    """Assemble the :class:`RollbackFile` use case (file:rollback)."""

    session_factory = create_session_factory(get_engine(request))
    return RollbackFile(
        uow=ServersUnitOfWork(session_factory),
        file_store=file_store,
    )


def get_upload_file(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> UploadFile:
    """Assemble the :class:`UploadFile` use case (file:edit)."""

    session_factory = create_session_factory(get_engine(request))
    return UploadFile(
        uow=ServersUnitOfWork(session_factory),
        file_store=file_store,
    )


def get_download_file(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> DownloadFile:
    """Assemble the :class:`DownloadFile` use case (file:read)."""

    session_factory = create_session_factory(get_engine(request))
    return DownloadFile(
        uow=ServersUnitOfWork(session_factory),
        file_store=file_store,
    )


def get_export_server(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> ExportServer:
    """Assemble the :class:`ExportServer` use case (file:read, issue #274).

    Binds the file seam to Storage (the export source is the authoritative working
    set) and the clock seam so ``export_metadata.json``'s ``exported_at`` comes
    from the time Port, never a direct wall-clock read.
    """

    session_factory = create_session_factory(get_engine(request))
    return ExportServer(
        uow=ServersUnitOfWork(session_factory),
        clock=ServersSystemClock(),
        file_store=file_store,
    )


def get_import_server(
    request: Request,
    create_server: Annotated[CreateServer, Depends(get_create_server)],
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> ImportServer:
    """Assemble the :class:`ImportServer` use case (server:create, issue #274).

    Composes :class:`CreateServer` so import reuses the SAME validation (version
    validator, edition, name uniqueness) and the #243 port auto-assign, and binds
    the file seam so the archive contents publish as the new working set through
    the hardened extraction.
    """

    return ImportServer(
        create_server=create_server,
        file_store=file_store,
    )


def get_delete_file(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> DeleteFile:
    """Assemble the :class:`DeleteFile` use case (file:edit, issue #259)."""

    session_factory = create_session_factory(get_engine(request))
    return DeleteFile(
        uow=ServersUnitOfWork(session_factory),
        file_store=file_store,
    )


def get_make_dir(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> MakeDir:
    """Assemble the :class:`MakeDir` use case (file:edit, issue #259)."""

    session_factory = create_session_factory(get_engine(request))
    return MakeDir(
        uow=ServersUnitOfWork(session_factory),
        file_store=file_store,
    )


def get_rename_file(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> RenameFile:
    """Assemble the :class:`RenameFile` use case (file:edit, issue #259)."""

    session_factory = create_session_factory(get_engine(request))
    return RenameFile(
        uow=ServersUnitOfWork(session_factory),
        file_store=file_store,
    )


def get_search_files(
    request: Request,
    file_store: Annotated[ServersFileStore, Depends(get_servers_file_store)],
) -> SearchFiles:
    """Assemble the :class:`SearchFiles` use case (file:read, issue #259)."""

    session_factory = create_session_factory(get_engine(request))
    return SearchFiles(
        uow=ServersUnitOfWork(session_factory),
        file_store=file_store,
    )


def get_servers_backup_store(
    storage: Annotated[Storage, Depends(get_storage)],
) -> BackupArchiveStore:
    """Bind the servers backup seam to the authoritative Storage backup slice."""

    return StorageBackupStoreAdapter(storage=storage)


def get_create_backup(
    request: Request,
    control_plane: Annotated[ServersControlPlane, Depends(get_servers_control_plane)],
    backup_store: Annotated[BackupArchiveStore, Depends(get_servers_backup_store)],
) -> CreateBackup:
    """Assemble the :class:`CreateBackup` use case (backup:create).

    Binds the RCON/snapshot seam (running path) and the Storage backup seam; the
    on-demand snapshot hook is the :class:`SnapshotServer` use case (PR #114).
    """

    session_factory = create_session_factory(get_engine(request))
    return CreateBackup(
        uow=ServersUnitOfWork(session_factory),
        control_plane=control_plane,
        backup_store=backup_store,
        snapshot_server=SnapshotServer(
            uow=ServersUnitOfWork(session_factory),
            control_plane=control_plane,
        ),
        clock=ServersSystemClock(),
    )


def get_list_backups(request: Request) -> ListBackups:
    """Assemble the :class:`ListBackups` use case (backup:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ListBackups(uow=ServersUnitOfWork(session_factory))


def get_restore_backup(
    request: Request,
    backup_store: Annotated[BackupArchiveStore, Depends(get_servers_backup_store)],
) -> RestoreBackup:
    """Assemble the :class:`RestoreBackup` use case (backup:restore)."""

    session_factory = create_session_factory(get_engine(request))
    return RestoreBackup(
        uow=ServersUnitOfWork(session_factory),
        backup_store=backup_store,
    )


def get_delete_backup(
    request: Request,
    backup_store: Annotated[BackupArchiveStore, Depends(get_servers_backup_store)],
) -> DeleteBackup:
    """Assemble the :class:`DeleteBackup` use case (backup:delete)."""

    session_factory = create_session_factory(get_engine(request))
    return DeleteBackup(
        uow=ServersUnitOfWork(session_factory),
        backup_store=backup_store,
    )


def get_download_backup(
    request: Request,
    backup_store: Annotated[BackupArchiveStore, Depends(get_servers_backup_store)],
) -> DownloadBackup:
    """Assemble the :class:`DownloadBackup` use case (backup:read, issue #281)."""

    session_factory = create_session_factory(get_engine(request))
    return DownloadBackup(
        uow=ServersUnitOfWork(session_factory),
        backup_store=backup_store,
    )


def get_upload_backup(
    request: Request,
    backup_store: Annotated[BackupArchiveStore, Depends(get_servers_backup_store)],
) -> UploadBackup:
    """Assemble the :class:`UploadBackup` use case (backup:create, issue #281)."""

    session_factory = create_session_factory(get_engine(request))
    return UploadBackup(
        uow=ServersUnitOfWork(session_factory),
        backup_store=backup_store,
        clock=ServersSystemClock(),
    )


def get_server_backup_statistics(request: Request) -> ServerBackupStatistics:
    """Assemble the per-server :class:`ServerBackupStatistics` (backup:read)."""

    session_factory = create_session_factory(get_engine(request))
    return ServerBackupStatistics(uow=ServersUnitOfWork(session_factory))


def get_global_backup_statistics(request: Request) -> GlobalBackupStatistics:
    """Assemble the platform-wide :class:`GlobalBackupStatistics` use case (#281)."""

    session_factory = create_session_factory(get_engine(request))
    return GlobalBackupStatistics(uow=ServersUnitOfWork(session_factory))


def _to_auth_user(user: User) -> AuthUser:
    """Project the identity ``User`` onto the community-domain authorization subject."""

    return AuthUser(
        user_id=CommunityUserId(user.id.value),
        is_platform_admin=user.is_platform_admin,
    )


def require_permission(
    operation: Permission,
    *,
    resource_type: str | None = None,
    resource_id_param: str | None = None,
) -> Callable[..., Awaitable[AuthUser]]:
    """Build a dependency enforcing the two-layer check for ``operation``.

    The dependency reads ``community_id`` from the path, runs Layer-1 visibility
    (non-member -> 404, no existence signal), then Layer-2 ``can`` for
    ``operation`` (member without the permission -> 403). For per-resource
    operations, pass ``resource_type`` and the path-parameter name carrying the
    resource id (``resource_id_param``) so the grant lookup is scoped to the
    exact resource (FR-AUTHZ-2). Returns the authorized :class:`AuthUser`.

    ``resource_type`` and ``resource_id_param`` are both-or-neither: pass both
    for a per-resource check or neither for a community-level check. Passing
    exactly one is a wiring mistake that would silently degrade a per-resource
    operation to a community-level grant lookup, so it raises here at
    dependency-construction time (fail-fast, before any request).

    Route convention: the community path segment must be named ``community_id``
    (see ``_dependency`` below); #69+ routes are expected to follow this.
    """

    if (resource_type is None) != (resource_id_param is None):
        raise ValueError(
            "require_permission: resource_type and resource_id_param are "
            "both-or-neither; pass both for a per-resource check or neither "
            f"for a community-level check (got resource_type={resource_type!r}, "
            f"resource_id_param={resource_id_param!r})"
        )

    async def _dependency(
        community_id: uuid.UUID,
        request: Request,
        user: Annotated[User, Depends(get_current_user)],
        visibility: Annotated[MembershipVisibility, Depends(get_membership_visibility)],
        checker: Annotated[PermissionChecker, Depends(get_permission_checker)],
    ) -> AuthUser:
        auth_user = _to_auth_user(user)
        community = CommunityId(community_id)

        if not await visibility.is_member(
            user_id=auth_user.user_id, community_id=community
        ):
            raise _not_found()

        resource_id = _resource_id_from_path(request, resource_id_param)
        resource = ResourceRef(
            community_id=community,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if not await checker.can(
            user=auth_user, operation=operation, resource=resource
        ):
            raise _forbidden()
        return auth_user

    return _dependency


async def require_platform_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Dependency requiring the platform-admin axis (FR-AUTHZ-5).

    This axis lives outside any Community, so it is decided directly on the
    user's ``is_platform_admin`` flag; a non-admin gets 403.
    """

    if not user.is_platform_admin:
        raise _forbidden()
    return user


def _resource_id_from_path(request: Request, param: str | None) -> uuid.UUID | None:
    if param is None:
        return None
    if param not in request.path_params:
        # Fail-closed by design: a per-resource check whose route does not
        # declare the named path param is a server-side misconfiguration. Raise
        # a diagnosable RuntimeError (-> 500) instead of an opaque KeyError.
        raise RuntimeError(
            f"require_permission: path param {param!r} is not declared by route "
            f"{request.url.path!r}; the route must include a "
            f"{{{param}}} path segment"
        )
    raw = request.path_params[param]
    return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))


def _not_found() -> HTTPException:
    # Layer-1: non-members get no existence signal (FR-COMM-3, Section 6.4).
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
