"""HTTP edge for server CRUD (Section 6.5).

Routes live under ``/communities/{community_id}/servers`` (+ ``/{server_id}``),
keeping ``community_id`` as the path param ``require_permission`` reads. Create and
list are gated by the *community-level* ``server:create`` / ``server:read``; read,
update, and delete are *per-resource* (``resource_type='server'``,
``resource_id_param='server_id'``) so a resource grant on one server opens exactly
that server (FR-AUTHZ-2) — a member with only a grant on server X cannot reach
server Y. (List stays community-level: a grant on a single server does not grant
listing the whole community's servers.)

The router is thin: it resolves use cases via dependency injection, runs them, and
serialises the result. Domain errors are translated to HTTP codes here.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder

# ``Permission`` is community-context-owned (the catalog lives there); the servers
# routes reference the ``server:*`` codes through it, as the community routes do.
from mc_server_dashboard_api.community.domain.value_objects import AuthUser, Permission
from mc_server_dashboard_api.dependencies import (
    ServerUpdateAuthz,
    get_audit_recorder,
    get_create_server,
    get_delete_server,
    get_export_server,
    get_import_server,
    get_list_servers,
    get_read_server,
    get_restart_server,
    get_send_server_command,
    get_start_server,
    get_stop_server,
    get_update_server,
    require_permission,
    require_server_update_authz,
)
from mc_server_dashboard_api.http_datetime import UtcDatetime
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.servers.application.export_import import (
    ExportServer,
    ImportServer,
)
from mc_server_dashboard_api.servers.application.files import MAX_UPLOAD_BYTES
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
    ReadServer,
    UpdateServer,
)
from mc_server_dashboard_api.servers.domain.config_bounds import (
    ConfigInvalidShapeError,
    ConfigNullValueError,
    ConfigTooLargeError,
    validate_config,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.cpu_allocation import (
    cpu_allocation_from_config,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    ExecutionBackendImmutableError,
    FileTooLargeError,
    InvalidBackupScheduleError,
    InvalidCpuAllocationError,
    InvalidExportMetadataError,
    InvalidLifecycleTransitionError,
    InvalidMemoryLimitError,
    InvalidServerNameError,
    InvalidSlugError,
    InvalidSnapshotIntervalError,
    LifecycleTransitionConflictError,
    NoEligibleWorkerError,
    PermissionDeniedError,
    PortAlreadyTakenError,
    PortOutOfRangeError,
    PortRangeExhaustedError,
    RemovedExecutionBackendError,
    ServerBusyError,
    ServerFilesUnsettledError,
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
    ServerNotRunningError,
    ServerNotStoppedError,
    SlugAlreadyTakenError,
    SlugExhaustedError,
    UnknownExecutionBackendError,
    UnknownServerTypeError,
    UnsupportedEditionError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.jar_provisioner import JarProvisioningError
from mc_server_dashboard_api.servers.domain.memory_limit import (
    memory_limit_from_config,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from mc_server_dashboard_api.servers.domain.version_validator import (
    CatalogUnavailableError,
    SpigotUnsupportedError,
    UnsupportedServerTypeError,
)
from mc_server_dashboard_api.servers.domain.version_validator import (
    UnknownVersionError as CatalogUnknownVersionError,
)

router = APIRouter()

_SERVER_RESOURCE_TYPE = "server"

# How much of the multipart import body to pull per chunk while counting it
# against the upload cap (the bounded-read loop in ``_read_capped_upload``).
_UPLOAD_CHUNK_BYTES = 1024 * 1024


class CreateServerRequest(BaseModel):
    name: str = Field(min_length=1)
    mc_edition: str = Field(min_length=1)
    mc_version: str = Field(min_length=1)
    server_type: str = Field(min_length=1)
    execution_backend: str = Field(min_length=1)
    # Typed ``Any`` (not ``dict``) so a non-object top level reaches
    # ``validate_config`` and yields the typed ``config_invalid_shape`` 422 rather
    # than Pydantic's generic validation error.
    config: Any = Field(default_factory=dict)
    # Explicit EULA consent (issue #198). When true, create seeds ``eula.txt`` with
    # ``eula=true`` into the server's initial working set so the first start does
    # not crash on Mojang's default ``eula=false``. Default false keeps today's
    # create behavior (no eula.txt; first start crashes and is repairable).
    accept_eula: bool = False
    # Optional explicit game port (issue #243). Omitted (None) lets create assign
    # the lowest free in-range port; supplied, it is validated against the range
    # (422 out of range) and the taken set (409 taken). Schema bounds it to a valid
    # TCP port so a wildly invalid value is a 422 at parse time.
    game_port: int | None = Field(default=None, gt=0, le=65535)


class UpdateServerRequest(BaseModel):
    name: str | None = None
    config: Any = None
    execution_backend: str | None = None
    # Optional new game port (issue #311). Omitted (None) leaves the port
    # unchanged; supplied, it is validated against the range (422 out of range)
    # and the taken set (409 taken), at rest only, and rewritten into
    # server.properties. Schema-bounded to a valid TCP port so a wildly invalid
    # value is a 422 at parse time.
    game_port: int | None = Field(default=None, gt=0, le=65535)
    # Optional slug rename (issue #955). Omitted (None) leaves the slug unchanged;
    # supplied, it is validated (422 invalid/reserved) and checked for global
    # uniqueness (409 taken), at rest only. Released slugs are immediately reusable
    # (owner decision, RELAY.md Section 15).
    slug: str | None = None


class ServerCommandRequest(BaseModel):
    line: str = Field(min_length=1)


class ServerCommandResponse(BaseModel):
    output: str


class ServerResponse(BaseModel):
    """Public view of a server (DATABASE.md Section 7)."""

    id: str
    community_id: str
    name: str
    mc_edition: str
    mc_version: str
    server_type: str
    execution_backend: str
    config: dict[str, Any]
    # The per-server memory limit in mebibytes (#705), surfaced as a typed field
    # for clients that should not parse the reserved config key themselves. It is
    # derived from ``config['memory_limit_mb']`` (None when unset); the full blob
    # still carries the raw key for round-tripping.
    memory_limit_mb: int | None
    # The per-server CPU allocation in millicores (#722; 1000 = one core), surfaced
    # as a typed field for clients that should not parse the reserved config key
    # themselves. It is derived from ``config['cpu_millis']`` (None when unset); the
    # full blob still carries the raw key for round-tripping. A soft relative share,
    # not a hard cap (owner decision).
    cpu_millis: int | None
    game_port: int | None
    # The relay slug (issue #955): auto-assigned at create, renameable via PATCH.
    slug: str
    desired_state: str
    observed_state: str
    observed_at: UtcDatetime | None
    assigned_worker_id: str | None

    @classmethod
    def from_entity(cls, server: Server) -> "ServerResponse":
        return cls(
            id=str(server.id.value),
            community_id=str(server.community_id.value),
            name=server.name.value,
            mc_edition=server.mc_edition,
            mc_version=server.mc_version,
            server_type=server.server_type.value,
            execution_backend=server.execution_backend.value,
            config=server.config,
            memory_limit_mb=memory_limit_from_config(server.config),
            cpu_millis=cpu_allocation_from_config(server.config),
            game_port=server.game_port,
            slug=server.slug,
            desired_state=server.desired_state.value,
            observed_state=server.observed_state.value,
            observed_at=server.observed_at,
            assigned_worker_id=(
                None
                if server.assigned_worker_id is None
                else str(server.assigned_worker_id.value)
            ),
        )


@router.post(
    "/communities/{community_id}/servers",
    status_code=status.HTTP_201_CREATED,
)
async def create_server(
    community_id: uuid.UUID,
    body: CreateServerRequest,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("server:create")))
    ],
    use_case: Annotated[CreateServer, Depends(get_create_server)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> ServerResponse:
    config = _validated_config(body.config)
    try:
        server = await use_case(
            community_id=CommunityId(community_id),
            name=body.name,
            mc_edition=body.mc_edition,
            mc_version=body.mc_version,
            server_type=body.server_type,
            execution_backend=body.execution_backend,
            config=config,
            accept_eula=body.accept_eula,
            game_port=body.game_port,
        )
    except UnsupportedEditionError as exc:
        # The catalog is Java-only at M1 (FR-VER-1): a non-java edition is rejected
        # before staging the row, distinct from an invalid type/version.
        raise _unprocessable("unsupported_edition") from exc
    except UnknownServerTypeError as exc:
        raise _unprocessable("invalid_server_type") from exc
    except UnknownExecutionBackendError as exc:
        raise _unprocessable("invalid_execution_backend") from exc
    except RemovedExecutionBackendError as exc:
        # A known-but-removed backend (host_process, issue #781): no Worker can run
        # it, so reject create rather than stage an unplaceable server.
        raise _unprocessable("removed_execution_backend") from exc
    except SpigotUnsupportedError as exc:
        # Spigot is schema-valid but has no official distribution API
        # (BuildTools-only): a distinct reason so the client can surface the
        # recommendation to use Paper (FR-VER-1).
        raise _unprocessable("spigot_unsupported") from exc
    except UnsupportedServerTypeError as exc:
        # A schema-valid type the catalog cannot resolve (forge): rejected as
        # unsupported, distinct from a wholly invalid type (FR-VER-1).
        raise _unprocessable("unsupported_server_type") from exc
    except CatalogUnknownVersionError as exc:
        raise _unprocessable("unknown_version") from exc
    except CatalogUnavailableError as exc:
        # The catalog source is down with no usable cache: create cannot validate
        # the version, so fail transiently (FR-VER-2) rather than 500.
        raise _service_unavailable("catalog_unavailable") from exc
    except InvalidServerNameError as exc:
        raise _unprocessable("invalid_server_name") from exc
    except InvalidMemoryLimitError as exc:
        # A per-server memory limit outside the accepted shape/range (#705).
        raise _unprocessable("invalid_memory_limit") from exc
    except InvalidCpuAllocationError as exc:
        # A per-server CPU allocation outside the accepted shape/range (#722).
        raise _unprocessable("invalid_cpu_allocation") from exc
    except PortOutOfRangeError as exc:
        # An explicit game_port outside the configured range (issue #243).
        raise _unprocessable("port_out_of_range") from exc
    except PortAlreadyTakenError as exc:
        # An explicit game_port already held by another server (issue #243).
        raise _conflict("port_taken") from exc
    except PortRangeExhaustedError as exc:
        # Auto-assign found no free port; a transient capacity condition that a
        # delete frees up, so 503 (not a client-state 409). Issue #243.
        raise _service_unavailable("port_range_exhausted") from exc
    except ServerNameAlreadyExistsError as exc:
        raise _conflict("server_name_exists") from exc
    except SlugExhaustedError as exc:
        # Auto-generation could not find a unique slug within the retry budget
        # (extremely unlikely in practice); a transient capacity condition (issue
        # #955). Surface 503 so the caller can retry.
        raise _service_unavailable("slug_exhausted") from exc
    except WorkingSetSeedFailedError as exc:
        # The row committed but seeding the working set failed (issue #243). The
        # server is left in a degraded-but-repairable state (the files API can
        # write the missing eula.txt/server.properties); the use case WARN-logged
        # it. Surface a mapped 503 rather than an unmapped 500.
        raise _service_unavailable("seed_failed") from exc
    await _record(
        recorder, ops.SERVER_CREATE, authorized, community_id, server.id.value
    )
    return ServerResponse.from_entity(server)


@router.post(
    "/communities/{community_id}/servers/import",
    status_code=status.HTTP_201_CREATED,
)
async def import_server(
    community_id: uuid.UUID,
    file: UploadFile,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("server:create")))
    ],
    use_case: Annotated[ImportServer, Depends(get_import_server)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    name: Annotated[str, Form(min_length=1)],
    execution_backend: Annotated[str, Form(min_length=1)],
) -> ServerResponse:
    """Import a whole server from a ZIP export (server:create, issue #274).

    Multipart: the ``file`` is the export zip; ``name`` and ``execution_backend``
    come from the request (the name is NOT taken from the metadata, so the usual
    uniqueness 409 applies). The archive's ``export_metadata.json`` is parsed and
    validated (wrong/missing format or malformed -> 422 ``invalid_export_metadata``;
    the server_type/version run the SAME create-path validator, so spigot is 422
    ``spigot_unsupported`` etc.). A row is created with an auto-assigned game port
    (#243); ``accept_eula`` is never implied. The archive contents then publish as
    the initial working set through the hardened extraction (zip-slip / size / entry
    caps -> 413 / 422). A publish failure after the row commits is 503
    ``seed_failed`` (the row is repairable via the files API).
    """

    content = await _read_capped_upload(file)
    try:
        server = await use_case(
            community_id=CommunityId(community_id),
            name=name,
            execution_backend=execution_backend,
            content=content,
        )
    except InvalidExportMetadataError as exc:
        raise _unprocessable("invalid_export_metadata") from exc
    except UnsupportedEditionError as exc:
        raise _unprocessable("unsupported_edition") from exc
    except UnknownServerTypeError as exc:
        raise _unprocessable("invalid_server_type") from exc
    except UnknownExecutionBackendError as exc:
        raise _unprocessable("invalid_execution_backend") from exc
    except RemovedExecutionBackendError as exc:
        # Import shares the create use case, so a removed backend (host_process,
        # issue #781) is rejected here too.
        raise _unprocessable("removed_execution_backend") from exc
    except SpigotUnsupportedError as exc:
        raise _unprocessable("spigot_unsupported") from exc
    except UnsupportedServerTypeError as exc:
        raise _unprocessable("unsupported_server_type") from exc
    except CatalogUnknownVersionError as exc:
        raise _unprocessable("unknown_version") from exc
    except CatalogUnavailableError as exc:
        raise _service_unavailable("catalog_unavailable") from exc
    except InvalidServerNameError as exc:
        raise _unprocessable("invalid_server_name") from exc
    except FileTooLargeError as exc:
        # The uploaded archive (or its cumulative extracted size / entry count)
        # exceeded the caps reused from the upload path (issue #262).
        raise _too_large() from exc
    except PortRangeExhaustedError as exc:
        raise _service_unavailable("port_range_exhausted") from exc
    except ServerNameAlreadyExistsError as exc:
        raise _conflict("server_name_exists") from exc
    except WorkingSetSeedFailedError as exc:
        # The row committed but publishing the working set failed mid-way: the
        # server is degraded-but-repairable via the files API (same posture as
        # create's seeding failure, #243/#252). Surface a mapped 503.
        raise _service_unavailable("seed_failed") from exc
    await _record(
        recorder, ops.SERVER_IMPORT, authorized, community_id, server.id.value
    )
    return ServerResponse.from_entity(server)


@router.get("/communities/{community_id}/servers")
async def list_servers(
    community_id: uuid.UUID,
    _authorized: Annotated[
        object, Depends(require_permission(Permission("server:read")))
    ],
    use_case: Annotated[ListServers, Depends(get_list_servers)],
) -> list[ServerResponse]:
    servers = await use_case(community_id=CommunityId(community_id))
    return [ServerResponse.from_entity(server) for server in servers]


@router.get("/communities/{community_id}/servers/{server_id}")
async def read_server(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    _authorized: Annotated[
        object,
        Depends(
            require_permission(
                Permission("server:read"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[ReadServer, Depends(get_read_server)],
) -> ServerResponse:
    try:
        server = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    return ServerResponse.from_entity(server)


@router.get("/communities/{community_id}/servers/{server_id}/export")
async def export_server(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("file:read"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[ExportServer, Depends(get_export_server)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> StreamingResponse:
    """Export a whole server as a streamed ZIP at rest (issue #274).

    Gated by ``file:read``: an export is a bulk read of the whole working set, so
    it reuses the file-read permission (and the per-resource grant) rather than a
    dedicated code -- the same permission the directory download uses. At rest only
    (Section 6.9): a running server is 409 ``server_unsettled`` (the authoritative
    copy is only well-defined at rest). The zip carries the working set plus an
    ``export_metadata.json`` descriptor. Recorded under ``server:export``.
    """

    try:
        stream = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFilesUnsettledError as exc:
        await _record_denied(
            recorder, ops.SERVER_EXPORT, authorized, community_id, server_id
        )
        raise _conflict("server_unsettled") from exc
    await _record(recorder, ops.SERVER_EXPORT, authorized, community_id, server_id)
    return StreamingResponse(stream, media_type="application/zip")


@router.patch("/communities/{community_id}/servers/{server_id}")
async def update_server(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    body: UpdateServerRequest,
    authz: Annotated[
        ServerUpdateAuthz,
        Depends(
            require_server_update_authz(
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[UpdateServer, Depends(get_update_server)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> ServerResponse:
    """Edit a server's name/config/game port.

    **Permission gate (issue #458).** The required permission branches by the
    changed-key set rather than a single fixed code: an edit that changes only the
    backup-scheduling key (``backup_interval_hours``) requires ``backup:schedule``;
    any other change (name, game port, backend, or any non-scheduling config key)
    requires ``server:update``; a mixed edit requires both. ``server:update`` no
    longer implies scheduling — a ``backup:schedule``-only holder may set the
    cadence, and a ``server:update``-only holder may not. A missing required
    permission is 403 carrying it in the ``permission`` member (#425/#555). Layer-1
    membership is checked at the edge (non-member -> 404); the changed-key decision
    runs in the use case, which has the current config in hand.

    **Error precedence (issue #115).** Validation runs first: config-bounds
    (``config_too_large`` / ``config_invalid_shape``), the cadence-override
    floor/shape (``invalid_snapshot_interval`` / ``invalid_backup_schedule``), and
    the game-port range (``port_out_of_range``) are 422 and are evaluated before
    any state gating. Only then does the state gate apply: an edit that requires
    the server to be at rest but finds it running is 409 (``server_not_stopped``).
    So a below-floor override (or an out-of-range port) on a running server is a
    422, not a 409.

    **Cadence-knob split (issue #115).** A config update that touches only the
    operationally-safe keys (``snapshot_interval_seconds``,
    ``backup_interval_hours``) bypasses the at-rest gate and is accepted while the
    server runs; the schedulers pick up the new value on their next tick. Any
    other config change — a name change, or a game-port change — keeps the at-rest
    requirement.

    **Game port (issue #311).** A new ``game_port`` is at rest only, validated
    like create (422 ``port_out_of_range`` / 409 ``port_taken``), and rewrites
    ``server-port`` in the at-rest ``server.properties`` so the DB and bind port
    stay in sync. A storage failure during that rewrite is 503 ``seed_failed`` and
    leaves the row unchanged.
    """

    authorized = authz.auth_user
    config = None if body.config is None else _validated_config(body.config)
    try:
        server = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            name=body.name,
            config=config,
            execution_backend=body.execution_backend,
            game_port=body.game_port,
            slug=body.slug,
            authorize=authz.authorize,
        )
    except PermissionDeniedError as exc:
        # The caller lacks a permission the changed-key set requires (issue #458);
        # carry the missing code in the 403 ``permission`` member (#425/#555).
        raise _forbidden(exc.permission) from exc
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ExecutionBackendImmutableError as exc:
        raise _conflict("execution_backend_immutable") from exc
    except ServerNotStoppedError as exc:
        raise _conflict("server_not_stopped") from exc
    except ServerBusyError as exc:
        # A concurrent lifecycle op held the per-server lock past the acquire
        # budget (issue #876): a transient 409 the caller retries.
        raise _conflict("server_busy") from exc
    except InvalidServerNameError as exc:
        raise _unprocessable("invalid_server_name") from exc
    except InvalidSnapshotIntervalError as exc:
        raise _unprocessable("invalid_snapshot_interval") from exc
    except InvalidBackupScheduleError as exc:
        raise _unprocessable("invalid_backup_schedule") from exc
    except InvalidMemoryLimitError as exc:
        # A per-server memory limit outside the accepted shape/range (#705).
        raise _unprocessable("invalid_memory_limit") from exc
    except InvalidCpuAllocationError as exc:
        # A per-server CPU allocation outside the accepted shape/range (#722).
        raise _unprocessable("invalid_cpu_allocation") from exc
    except PortOutOfRangeError as exc:
        # A new game_port outside the configured range (issue #311).
        raise _unprocessable("port_out_of_range") from exc
    except PortAlreadyTakenError as exc:
        # A new game_port already held by another server (issue #311).
        raise _conflict("port_taken") from exc
    except ServerNameAlreadyExistsError as exc:
        raise _conflict("server_name_exists") from exc
    except InvalidSlugError as exc:
        # Slug failed the DNS-label format check or is a reserved word (issue #955).
        raise _unprocessable("invalid_slug") from exc
    except SlugAlreadyTakenError as exc:
        # Slug is already held by another server (issue #955).
        raise _conflict("slug_taken") from exc
    except SlugExhaustedError as exc:
        # Auto-generation at create exhausted the retry budget (extremely unlikely);
        # a transient condition, so 503 (issue #955).
        raise _service_unavailable("slug_exhausted") from exc
    except WorkingSetSeedFailedError as exc:
        # Rewriting server.properties for the port change failed; the row was not
        # committed (no DB/file drift), surfaced as a mapped 503 (issue #311).
        raise _service_unavailable("seed_failed") from exc
    await _record(
        recorder, ops.SERVER_UPDATE, authorized, community_id, server.id.value
    )
    return ServerResponse.from_entity(server)


@router.delete(
    "/communities/{community_id}/servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_server(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("server:delete"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[DeleteServer, Depends(get_delete_server)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerNotStoppedError as exc:
        raise _conflict("server_not_stopped") from exc
    except ServerBusyError as exc:
        # A concurrent lifecycle op held the per-server lock past the acquire
        # budget (issue #876): a transient 409 the caller retries.
        raise _conflict("server_busy") from exc
    await _record(recorder, ops.SERVER_DELETE, authorized, community_id, server_id)


@router.post("/communities/{community_id}/servers/{server_id}/start")
async def start_server(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("server:start"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[StartServer, Depends(get_start_server)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> ServerResponse:
    try:
        server = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except _LIFECYCLE_FAILURES as exc:
        await _record_op_failure(
            recorder, ops.SERVER_START, authorized, community_id, server_id, exc
        )
        raise _lifecycle_http_error(exc) from exc
    await _record(recorder, ops.SERVER_START, authorized, community_id, server.id.value)
    return ServerResponse.from_entity(server)


@router.post("/communities/{community_id}/servers/{server_id}/stop")
async def stop_server(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("server:stop"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[StopServer, Depends(get_stop_server)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    # Default false = today's graceful stop; ?force=true skips the Worker's
    # graceful path and kills the process immediately (issue #270).
    force: Annotated[bool, Query()] = False,
) -> ServerResponse:
    try:
        server = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            force=force,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except _LIFECYCLE_FAILURES as exc:
        await _record_op_failure(
            recorder, ops.SERVER_STOP, authorized, community_id, server_id, exc
        )
        raise _lifecycle_http_error(exc) from exc
    await _record(recorder, ops.SERVER_STOP, authorized, community_id, server.id.value)
    return ServerResponse.from_entity(server)


@router.post("/communities/{community_id}/servers/{server_id}/restart")
async def restart_server(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("server:restart"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[RestartServer, Depends(get_restart_server)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> ServerResponse:
    try:
        server = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except _LIFECYCLE_FAILURES as exc:
        await _record_op_failure(
            recorder, ops.SERVER_RESTART, authorized, community_id, server_id, exc
        )
        raise _lifecycle_http_error(exc) from exc
    await _record(
        recorder, ops.SERVER_RESTART, authorized, community_id, server.id.value
    )
    return ServerResponse.from_entity(server)


@router.post("/communities/{community_id}/servers/{server_id}/command")
async def send_server_command(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    body: ServerCommandRequest,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("server:command"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[SendServerCommand, Depends(get_send_server_command)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> ServerCommandResponse:
    try:
        output = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            line=body.line,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except _LIFECYCLE_FAILURES as exc:
        await _record_op_failure(
            recorder, ops.SERVER_COMMAND, authorized, community_id, server_id, exc
        )
        raise _lifecycle_http_error(exc) from exc
    await _record(recorder, ops.SERVER_COMMAND, authorized, community_id, server_id)
    return ServerCommandResponse(output=output)


async def _record(
    recorder: AuditRecorder,
    operation: str,
    authorized: AuthUser,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
) -> None:
    """Record a successful server operation (FR-AUD-1), fire-after-commit."""

    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_SERVER,
            target_id=server_id,
        )
    )


async def _record_denied(
    recorder: AuditRecorder,
    operation: str,
    authorized: AuthUser,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
) -> None:
    """Record a refused server operation (DENIED), e.g. an export of a live server."""

    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=Outcome.DENIED,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_SERVER,
            target_id=server_id,
        )
    )


async def _read_capped_upload(file: UploadFile) -> bytes:
    """Pull a multipart body in chunks, aborting with 413 past the upload cap.

    Mirrors the files-router reader (issue #262): ``file.read()`` with no argument
    would pull the whole part into memory at once, so read in bounded chunks and
    refuse as soon as the running count crosses MAX_UPLOAD_BYTES. The use case
    re-checks the cap, so this is the edge's early-out, not the only guard.
    """

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


# Failed lifecycle/command attempts worth a row (issue #131): a refused state
# transition (DENIED) or a transient fleet/provisioning failure (ERROR). Each
# entry maps the typed domain error to its audit outcome and HTTP rendering, so
# the four lifecycle/command routes share one classification and stay thin.
# ``ServerNotFoundError`` is excluded on purpose (a 404 keeps the
# no-existence-signal posture; it is not a security-relevant refusal of a real
# resource).
_LIFECYCLE_CLASSIFICATION: dict[type[Exception], tuple[Outcome, str]] = {
    InvalidLifecycleTransitionError: (Outcome.DENIED, "invalid_transition"),
    LifecycleTransitionConflictError: (Outcome.DENIED, "transition_conflict"),
    # StartServer holds the per-server lifecycle lock for its flip; if a gated op
    # holds it past the acquire budget the start is refused as a transient 409
    # ``server_busy`` (issue #876), the same retry-able conflict the gated routes
    # surface. Only start takes the lock, so stop/restart never raise this.
    ServerBusyError: (Outcome.DENIED, "server_busy"),
    CommandDispatchError: (Outcome.DENIED, "command_failed"),
    ServerNotRunningError: (Outcome.DENIED, "server_not_running"),
    NoEligibleWorkerError: (Outcome.ERROR, "no_eligible_worker"),
    WorkerUnavailableError: (Outcome.ERROR, "worker_unavailable"),
    JarProvisioningError: (Outcome.ERROR, "jar_unavailable"),
}
_LIFECYCLE_FAILURES = tuple(_LIFECYCLE_CLASSIFICATION)
# The transient (ERROR) reasons render as 503; the refusals (DENIED) as 409.
_SERVICE_UNAVAILABLE_REASONS = {
    "no_eligible_worker",
    "worker_unavailable",
    "jar_unavailable",
}


def _lifecycle_http_error(exc: Exception) -> ProblemException:
    _, reason = _LIFECYCLE_CLASSIFICATION[type(exc)]
    # A start failure the Worker classified into a sanitized category (issue #225)
    # carries its own reason (e.g. port_conflict / image_missing), surfacing it in
    # the 409 body in place of the generic command_failed. The raw daemon text is
    # never the reason (log-only), so no Worker internals leak.
    if isinstance(exc, CommandDispatchError) and exc.reason is not None:
        return _conflict(exc.reason)
    if reason in _SERVICE_UNAVAILABLE_REASONS:
        return _service_unavailable(reason)
    return _conflict(reason)


async def _record_op_failure(
    recorder: AuditRecorder,
    operation: str,
    authorized: AuthUser,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    exc: Exception,
) -> None:
    """Record a failed privileged server op (FR-AUD-1): DENIED or ERROR per ``exc``."""

    outcome, _ = _LIFECYCLE_CLASSIFICATION[type(exc)]
    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=outcome,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_SERVER,
            target_id=server_id,
        )
    )


def _validated_config(config: Any) -> dict[str, Any]:
    """Bound the client config blob before it is staged (issue #94)."""

    try:
        return validate_config(config)
    except ConfigTooLargeError as exc:
        raise _unprocessable("config_too_large") from exc
    except ConfigNullValueError as exc:
        raise _unprocessable("config_null_value") from exc
    except ConfigInvalidShapeError as exc:
        raise _unprocessable("config_invalid_shape") from exc


def _unprocessable(reason: str) -> ProblemException:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, reason)


def _service_unavailable(reason: str) -> ProblemException:
    # Placement found no Worker, or the assigned Worker is gone / timed out: a
    # transient fleet-capacity condition, not a client-state conflict. 503 tells
    # the client to retry once the fleet has capacity again (FR-WRK-3/4).
    return problem(status.HTTP_503_SERVICE_UNAVAILABLE, reason)


def _conflict(reason: str) -> ProblemException:
    return problem(status.HTTP_409_CONFLICT, reason)


def _forbidden(permission: str) -> ProblemException:
    # The update gate denied a specific permission (issue #458); carry its code in
    # the ``permission`` extension member so the Web UI can name what is missing
    # (#425/#555). ``reason`` stays the stable ``"forbidden"`` code.
    return problem(
        status.HTTP_403_FORBIDDEN, "forbidden", extensions={"permission": permission}
    )


def _too_large() -> ProblemException:
    # The uploaded import archive (or its cumulative extracted size / entry count)
    # exceeded the caps reused from the upload path (issue #262).
    return problem(status.HTTP_413_CONTENT_TOO_LARGE, "file_too_large")


def _not_found() -> ProblemException:
    # Keep the no-existence-signal posture (Section 6.4): a server outside this
    # community 404s the same as a wholly unknown one.
    return problem(status.HTTP_404_NOT_FOUND, "not_found")
