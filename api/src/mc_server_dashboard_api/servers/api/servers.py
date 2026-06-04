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

# ``Permission`` is community-context-owned (the catalog lives there); the servers
# routes reference the ``server:*`` codes through it, as the community routes do.
from mc_server_dashboard_api.community.domain.value_objects import Permission
from mc_server_dashboard_api.dependencies import (
    get_create_server,
    get_delete_server,
    get_list_servers,
    get_read_server,
    get_update_server,
    require_permission,
)
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    DeleteServer,
    ListServers,
    ReadServer,
    UpdateServer,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    ExecutionBackendImmutableError,
    InvalidServerNameError,
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
    ServerNotStoppedError,
    UnknownExecutionBackendError,
    UnknownServerTypeError,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

router = APIRouter()

_SERVER_RESOURCE_TYPE = "server"


class CreateServerRequest(BaseModel):
    name: str = Field(min_length=1)
    mc_edition: str = Field(min_length=1)
    mc_version: str = Field(min_length=1)
    server_type: str = Field(min_length=1)
    execution_backend: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


class UpdateServerRequest(BaseModel):
    name: str | None = None
    config: dict[str, Any] | None = None
    execution_backend: str | None = None


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
    _authorized: Annotated[
        object, Depends(require_permission(Permission("server:create")))
    ],
    use_case: Annotated[CreateServer, Depends(get_create_server)],
) -> ServerResponse:
    try:
        server = await use_case(
            community_id=CommunityId(community_id),
            name=body.name,
            mc_edition=body.mc_edition,
            mc_version=body.mc_version,
            server_type=body.server_type,
            execution_backend=body.execution_backend,
            config=body.config,
        )
    except UnknownServerTypeError as exc:
        raise _unprocessable("invalid_server_type") from exc
    except UnknownExecutionBackendError as exc:
        raise _unprocessable("invalid_execution_backend") from exc
    except InvalidServerNameError as exc:
        raise _unprocessable("invalid_server_name") from exc
    except ServerNameAlreadyExistsError as exc:
        raise _conflict("server_name_exists") from exc
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
    _authorized: Annotated[
        object,
        Depends(
            require_permission(
                Permission("server:update"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[UpdateServer, Depends(get_update_server)],
) -> ServerResponse:
    try:
        server = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            name=body.name,
            config=body.config,
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
    except ServerNameAlreadyExistsError as exc:
        raise _conflict("server_name_exists") from exc
    return ServerResponse.from_entity(server)


@router.delete(
    "/communities/{community_id}/servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_server(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    _authorized: Annotated[
        object,
        Depends(
            require_permission(
                Permission("server:delete"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[DeleteServer, Depends(get_delete_server)],
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


def _unprocessable(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
