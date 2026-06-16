"""HTTP edge for server plugin/mod content management (issue #1150).

Routes live under ``/communities/{community_id}/servers/{server_id}/plugins``
and are *per-resource* gated (``resource_type='server'``,
``resource_id_param='server_id'``) like the server, file, and backup routes: a
grant on one server opens exactly that server's plugins (FR-AUTHZ-2). The
catalog codes are ``plugin:read`` (list) and ``plugin:manage`` (install, remove,
enable, disable).

All mutations require the server at rest (Section 6.9); a transitional server
is 409 ``server_unsettled``. Install accepts a multipart jar upload capped at
512 MiB (the same cap as file uploads).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, UploadFile, status
from pydantic import BaseModel

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.community.domain.value_objects import AuthUser, Permission
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_install_plugin,
    get_list_plugins,
    get_remove_plugin,
    get_toggle_plugin,
    require_permission,
)
from mc_server_dashboard_api.http_datetime import UtcDatetime
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.servers.application.plugins import (
    MAX_PLUGIN_BYTES,
    InstallPlugin,
    ListPlugins,
    RemovePlugin,
    TogglePlugin,
)
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidFilePathError,
    PluginAlreadyExistsError,
    PluginNotFoundError,
    ServerBusyError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
    UnsupportedPluginServerTypeError,
)
from mc_server_dashboard_api.servers.domain.plugin import PluginId, ServerPlugin
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

router = APIRouter()

_SERVER_RESOURCE_TYPE = "server"

# How much of the multipart body to pull per chunk while counting it against the
# upload cap (the bounded-read loop).
_UPLOAD_CHUNK_BYTES = 1024 * 1024


class PluginResponse(BaseModel):
    """One plugin's metadata."""

    id: uuid.UUID
    server_id: uuid.UUID
    rel_path: str
    filename: str
    display_name: str
    description: str | None
    loader_type: str
    source: str
    source_project_id: str | None
    source_version_id: str | None
    version_number: str | None
    checksum_sha512: str | None
    size_bytes: int | None
    enabled: bool
    installed_by: uuid.UUID | None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @classmethod
    def from_plugin(cls, plugin: ServerPlugin) -> "PluginResponse":
        return cls(
            id=plugin.id.value,
            server_id=plugin.server_id.value,
            rel_path=plugin.rel_path,
            filename=plugin.filename,
            display_name=plugin.display_name,
            description=plugin.description,
            loader_type=plugin.loader_type.value,
            source=plugin.source.value,
            source_project_id=plugin.source_project_id,
            source_version_id=plugin.source_version_id,
            version_number=plugin.version_number,
            checksum_sha512=plugin.checksum_sha512,
            size_bytes=plugin.size_bytes,
            enabled=plugin.enabled,
            installed_by=plugin.installed_by,
            created_at=plugin.created_at,
            updated_at=plugin.updated_at,
        )


class PluginListResponse(BaseModel):
    plugins: list[PluginResponse]


@router.get("/communities/{community_id}/servers/{server_id}/plugins")
async def list_plugins(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    _authorized: Annotated[
        object,
        Depends(
            require_permission(
                Permission("plugin:read"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[ListPlugins, Depends(get_list_plugins)],
) -> PluginListResponse:
    """List installed plugins for a server (plugin:read)."""

    try:
        plugins = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except UnsupportedPluginServerTypeError as exc:
        raise _unprocessable("unsupported_server_type") from exc
    return PluginListResponse(plugins=[PluginResponse.from_plugin(p) for p in plugins])


@router.post(
    "/communities/{community_id}/servers/{server_id}/plugins",
    status_code=status.HTTP_201_CREATED,
)
async def install_plugin(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    file: UploadFile,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("plugin:manage"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[InstallPlugin, Depends(get_install_plugin)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    display_name: Annotated[str, Form()],
) -> PluginResponse:
    """Install a plugin jar via multipart upload (plugin:manage)."""

    filename = file.filename or ""
    content = await _read_capped_upload(file)
    try:
        plugin = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            filename=filename,
            display_name=display_name,
            content=content,
            installed_by=authorized.user_id.value,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except UnsupportedPluginServerTypeError as exc:
        raise _unprocessable("unsupported_server_type") from exc
    except InvalidFilePathError as exc:
        raise _unprocessable("invalid_path") from exc
    except FileTooLargeError as exc:
        raise _too_large() from exc
    except PluginAlreadyExistsError as exc:
        raise _conflict("plugin_already_exists") from exc
    except ServerFilesUnsettledError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_INSTALL, authorized, community_id, server_id
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_INSTALL, authorized, community_id, server_id
        )
        raise _conflict("server_busy") from exc
    await _record_plugin(
        recorder, ops.PLUGIN_INSTALL, authorized, community_id, plugin.id.value
    )
    return PluginResponse.from_plugin(plugin)


@router.delete(
    "/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_plugin(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    plugin_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("plugin:manage"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[RemovePlugin, Depends(get_remove_plugin)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    """Remove an installed plugin (plugin:manage)."""

    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            plugin_id=PluginId(plugin_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except PluginNotFoundError as exc:
        raise _not_found() from exc
    except ServerFilesUnsettledError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_REMOVE, authorized, community_id, plugin_id
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_REMOVE, authorized, community_id, plugin_id
        )
        raise _conflict("server_busy") from exc
    await _record_plugin(
        recorder, ops.PLUGIN_REMOVE, authorized, community_id, plugin_id
    )


@router.post(
    "/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}/enable",
)
async def enable_plugin(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    plugin_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("plugin:manage"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[TogglePlugin, Depends(get_toggle_plugin)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> PluginResponse:
    """Enable a disabled plugin (plugin:manage)."""

    try:
        plugin = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            plugin_id=PluginId(plugin_id),
            enable=True,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except PluginNotFoundError as exc:
        raise _not_found() from exc
    except PluginAlreadyExistsError as exc:
        raise _conflict("plugin_already_exists") from exc
    except ServerFilesUnsettledError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_ENABLE, authorized, community_id, plugin_id
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_ENABLE, authorized, community_id, plugin_id
        )
        raise _conflict("server_busy") from exc
    await _record_plugin(
        recorder, ops.PLUGIN_ENABLE, authorized, community_id, plugin_id
    )
    return PluginResponse.from_plugin(plugin)


@router.post(
    "/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}/disable",
)
async def disable_plugin(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    plugin_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("plugin:manage"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[TogglePlugin, Depends(get_toggle_plugin)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> PluginResponse:
    """Disable an enabled plugin (plugin:manage)."""

    try:
        plugin = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            plugin_id=PluginId(plugin_id),
            enable=False,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except PluginNotFoundError as exc:
        raise _not_found() from exc
    except PluginAlreadyExistsError as exc:
        raise _conflict("plugin_already_exists") from exc
    except ServerFilesUnsettledError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_DISABLE, authorized, community_id, plugin_id
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_DISABLE, authorized, community_id, plugin_id
        )
        raise _conflict("server_busy") from exc
    await _record_plugin(
        recorder, ops.PLUGIN_DISABLE, authorized, community_id, plugin_id
    )
    return PluginResponse.from_plugin(plugin)


async def _read_capped_upload(file: UploadFile) -> bytes:
    """Pull the multipart body in chunks, aborting with 413 past the upload cap."""

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_PLUGIN_BYTES:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


async def _record_plugin(
    recorder: AuditRecorder,
    operation: str,
    authorized: AuthUser,
    community_id: uuid.UUID,
    target_id: uuid.UUID,
) -> None:
    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_PLUGIN,
            target_id=target_id,
        )
    )


async def _record_plugin_failure(
    recorder: AuditRecorder,
    operation: str,
    authorized: AuthUser,
    community_id: uuid.UUID,
    target_id: uuid.UUID,
) -> None:
    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=Outcome.DENIED,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_PLUGIN,
            target_id=target_id,
        )
    )


def _unprocessable(reason: str) -> ProblemException:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, reason)


def _too_large() -> ProblemException:
    return problem(status.HTTP_413_CONTENT_TOO_LARGE, "file_too_large")


def _conflict(reason: str) -> ProblemException:
    return problem(status.HTTP_409_CONFLICT, reason)


def _not_found() -> ProblemException:
    return problem(status.HTTP_404_NOT_FOUND, "not_found")
