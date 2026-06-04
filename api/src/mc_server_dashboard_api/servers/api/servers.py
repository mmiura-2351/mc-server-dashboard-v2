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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder

# ``Permission`` is community-context-owned (the catalog lives there); the servers
# routes reference the ``server:*`` codes through it, as the community routes do.
from mc_server_dashboard_api.community.domain.value_objects import AuthUser, Permission
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_create_server,
    get_delete_server,
    get_list_servers,
    get_read_server,
    get_restart_server,
    get_send_server_command,
    get_start_server,
    get_stop_server,
    get_update_server,
    require_permission,
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
    ReadServer,
    UpdateServer,
)
from mc_server_dashboard_api.servers.domain.config_bounds import (
    ConfigInvalidShapeError,
    ConfigTooLargeError,
    validate_config,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    ExecutionBackendImmutableError,
    InvalidBackupScheduleError,
    InvalidLifecycleTransitionError,
    InvalidServerNameError,
    InvalidSnapshotIntervalError,
    LifecycleTransitionConflictError,
    NoEligibleWorkerError,
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
    ServerNotRunningError,
    ServerNotStoppedError,
    UnknownExecutionBackendError,
    UnknownServerTypeError,
    UnsupportedEditionError,
)
from mc_server_dashboard_api.servers.domain.jar_provisioner import JarProvisioningError
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from mc_server_dashboard_api.servers.domain.version_validator import (
    CatalogUnavailableError,
    UnsupportedServerTypeError,
)
from mc_server_dashboard_api.servers.domain.version_validator import (
    UnknownVersionError as CatalogUnknownVersionError,
)

router = APIRouter()

_SERVER_RESOURCE_TYPE = "server"


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


class UpdateServerRequest(BaseModel):
    name: str | None = None
    config: Any = None
    execution_backend: str | None = None


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
    desired_state: str
    observed_state: str
    observed_at: str | None
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
            desired_state=server.desired_state.value,
            observed_state=server.observed_state.value,
            observed_at=(
                None if server.observed_at is None else server.observed_at.isoformat()
            ),
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
        )
    except UnsupportedEditionError as exc:
        # The catalog is Java-only at M1 (FR-VER-1): a non-java edition is rejected
        # before staging the row, distinct from an invalid type/version.
        raise _unprocessable("unsupported_edition") from exc
    except UnknownServerTypeError as exc:
        raise _unprocessable("invalid_server_type") from exc
    except UnknownExecutionBackendError as exc:
        raise _unprocessable("invalid_execution_backend") from exc
    except UnsupportedServerTypeError as exc:
        # A schema-valid type the catalog cannot resolve at M1 (forge): rejected
        # as unsupported, distinct from a wholly invalid type (FR-VER-1).
        raise _unprocessable("unsupported_server_type") from exc
    except CatalogUnknownVersionError as exc:
        raise _unprocessable("unknown_version") from exc
    except CatalogUnavailableError as exc:
        # The catalog source is down with no usable cache: create cannot validate
        # the version, so fail transiently (FR-VER-2) rather than 500.
        raise _service_unavailable("catalog_unavailable") from exc
    except InvalidServerNameError as exc:
        raise _unprocessable("invalid_server_name") from exc
    except ServerNameAlreadyExistsError as exc:
        raise _conflict("server_name_exists") from exc
    await _record(
        recorder, ops.SERVER_CREATE, authorized, community_id, server.id.value
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


@router.patch("/communities/{community_id}/servers/{server_id}")
async def update_server(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    body: UpdateServerRequest,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("server:update"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[UpdateServer, Depends(get_update_server)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> ServerResponse:
    config = None if body.config is None else _validated_config(body.config)
    try:
        server = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            name=body.name,
            config=config,
            execution_backend=body.execution_backend,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ExecutionBackendImmutableError as exc:
        raise _conflict("execution_backend_immutable") from exc
    except ServerNotStoppedError as exc:
        raise _conflict("server_not_stopped") from exc
    except InvalidServerNameError as exc:
        raise _unprocessable("invalid_server_name") from exc
    except InvalidSnapshotIntervalError as exc:
        raise _unprocessable("invalid_snapshot_interval") from exc
    except InvalidBackupScheduleError as exc:
        raise _unprocessable("invalid_backup_schedule") from exc
    except ServerNameAlreadyExistsError as exc:
        raise _conflict("server_name_exists") from exc
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
    except InvalidLifecycleTransitionError as exc:
        raise _conflict("invalid_transition") from exc
    except LifecycleTransitionConflictError as exc:
        raise _conflict("transition_conflict") from exc
    except NoEligibleWorkerError as exc:
        raise _service_unavailable("no_eligible_worker") from exc
    except WorkerUnavailableError as exc:
        raise _service_unavailable("worker_unavailable") from exc
    except JarProvisioningError as exc:
        # The resolved JAR could not be fetched/verified/stored: the start failed
        # before placement (FR-VER-3). Transient (source down) or integrity
        # (hash mismatch); surfaced as a typed 503 so a retry is invited.
        raise _service_unavailable("jar_unavailable") from exc
    except CommandDispatchError as exc:
        raise _conflict("command_failed") from exc
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
) -> ServerResponse:
    try:
        server = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except InvalidLifecycleTransitionError as exc:
        raise _conflict("invalid_transition") from exc
    except LifecycleTransitionConflictError as exc:
        raise _conflict("transition_conflict") from exc
    except WorkerUnavailableError as exc:
        raise _service_unavailable("worker_unavailable") from exc
    except CommandDispatchError as exc:
        raise _conflict("command_failed") from exc
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
    except InvalidLifecycleTransitionError as exc:
        raise _conflict("invalid_transition") from exc
    except LifecycleTransitionConflictError as exc:
        raise _conflict("transition_conflict") from exc
    except WorkerUnavailableError as exc:
        raise _service_unavailable("worker_unavailable") from exc
    except CommandDispatchError as exc:
        raise _conflict("command_failed") from exc
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
    except ServerNotRunningError as exc:
        raise _conflict("server_not_running") from exc
    except WorkerUnavailableError as exc:
        raise _service_unavailable("worker_unavailable") from exc
    except CommandDispatchError as exc:
        raise _conflict("command_failed") from exc
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


def _validated_config(config: Any) -> dict[str, Any]:
    """Bound the client config blob before it is staged (issue #94)."""

    try:
        return validate_config(config)
    except ConfigTooLargeError as exc:
        raise _unprocessable("config_too_large") from exc
    except ConfigInvalidShapeError as exc:
        raise _unprocessable("config_invalid_shape") from exc


def _unprocessable(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"reason": reason},
    )


def _service_unavailable(reason: str) -> HTTPException:
    # Placement found no Worker, or the assigned Worker is gone / timed out: a
    # transient fleet-capacity condition, not a client-state conflict. 503 tells
    # the client to retry once the fleet has capacity again (FR-WRK-3/4).
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"reason": reason},
    )


def _conflict(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"reason": reason},
    )


def _not_found() -> HTTPException:
    # Keep the no-existence-signal posture (Section 6.4): a server outside this
    # community 404s the same as a wholly unknown one.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
