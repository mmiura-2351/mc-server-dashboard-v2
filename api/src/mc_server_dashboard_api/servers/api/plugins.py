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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.community.domain.value_objects import AuthUser, Permission
from mc_server_dashboard_api.dependencies import (
    get_apply_plugin_resolution,
    get_audit_recorder,
    get_check_plugin_update,
    get_check_updates,
    get_download_client_modpack,
    get_get_plugin,
    get_install_plugin,
    get_list_client_mods,
    get_list_plugin_dependencies,
    get_list_plugins,
    get_remove_plugin,
    get_resolve_plugin_dependencies,
    get_set_plugin_side,
    get_toggle_plugin,
    get_update_plugin,
    get_validate_plugin_set,
    require_permission,
)
from mc_server_dashboard_api.http_datetime import UtcDatetime
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.servers.application.catalog import (
    CheckPluginUpdate,
    CheckUpdates,
    ListPluginDependencies,
    UpdatePlugin,
)
from mc_server_dashboard_api.servers.application.client_modpack import (
    DownloadClientModpack,
    ListClientMods,
)
from mc_server_dashboard_api.servers.application.plugin_resolution import (
    ApplyPluginResolution,
    ResolutionPlan,
    ResolvePluginDependencies,
)
from mc_server_dashboard_api.servers.application.plugin_validation import (
    PluginValidation,
)
from mc_server_dashboard_api.servers.application.plugins import (
    MAX_PLUGIN_BYTES,
    GetPlugin,
    InstallPlugin,
    ListPlugins,
    RemovePlugin,
    SetPluginSide,
    TogglePlugin,
    ValidatePluginSet,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogVersion as CatalogVersionDomain,
)
from mc_server_dashboard_api.servers.domain.errors import (
    CatalogChecksumMismatchError,
    CatalogProjectNotFoundError,
    CatalogUnavailableError,
    FileTooLargeError,
    InvalidFilePathError,
    InvalidPluginSideError,
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
    # The jar manifest's declared id (issue #1307), or None when the jar carried
    # no recognized manifest. Surfaced so the validation checklist can map a
    # finding's mod_id back to a human-friendly plugin name.
    mod_identifier: str | None
    # Where the content is needed (issue #1308): server / client / both.
    # Auto-detected at ingest and manually overridable; a client-only plugin is
    # tracked but never deployed to the running server.
    side: str

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
            mod_identifier=plugin.mod_identifier,
            side=plugin.side,
        )


class PluginListResponse(BaseModel):
    plugins: list[PluginResponse]


class CatalogFileItem(BaseModel):
    url: str
    filename: str
    size: int
    sha512: str
    primary: bool


class CatalogDependencyItem(BaseModel):
    version_id: str | None
    project_id: str
    dependency_type: str


class CatalogVersionItem(BaseModel):
    """Inline catalog version response to avoid circular import with catalog.py."""

    version_id: str
    version_number: str
    name: str
    game_versions: list[str]
    loaders: list[str]
    files: list[CatalogFileItem]
    date_published: str
    dependencies: list[CatalogDependencyItem]

    @classmethod
    def from_domain(cls, v: CatalogVersionDomain) -> CatalogVersionItem:
        return cls(
            version_id=v.version_id,
            version_number=v.version_number,
            name=v.name,
            game_versions=v.game_versions,
            loaders=v.loaders,
            files=[
                CatalogFileItem(
                    url=f.url,
                    filename=f.filename,
                    size=f.size,
                    sha512=f.sha512,
                    primary=f.primary,
                )
                for f in v.files
            ],
            date_published=v.date_published,
            dependencies=[
                CatalogDependencyItem(
                    version_id=d.version_id,
                    project_id=d.project_id,
                    dependency_type=d.dependency_type,
                )
                for d in v.dependencies
            ],
        )


class PluginUpdateInfoResponse(BaseModel):
    plugin: PluginResponse
    latest_version: CatalogVersionItem | None


class PluginUpdatesResponse(BaseModel):
    updates: list[PluginUpdateInfoResponse]


class UpdatePluginRequest(BaseModel):
    version_id: str


class SetPluginSideRequest(BaseModel):
    """Manual side override for an installed plugin (issue #1308)."""

    side: str


class ClientModsResponse(BaseModel):
    """A server's enabled client-relevant plugins (issue #1308)."""

    plugins: list[PluginResponse]


class PluginDependencyResponse(BaseModel):
    project_id: str
    version_id: str | None
    dependency_type: str
    project_title: str | None
    project_slug: str | None
    installed: bool


class PluginDependenciesResponse(BaseModel):
    dependencies: list[PluginDependencyResponse]


# -- Plugin-set validation (issue #1307) --


class MissingDependencyResponse(BaseModel):
    mod_id: str
    depends_on: str
    version_range: str


class MissingCatalogDependencyResponse(BaseModel):
    """A required Modrinth catalog dep no installed project covers (issue #1321)."""

    mod_id: str
    project_id: str
    slug: str | None
    title: str | None


class VersionUnsatisfiedResponse(BaseModel):
    mod_id: str
    depends_on: str
    version_range: str
    present_version: str


class ConflictResponse(BaseModel):
    mod_id: str
    conflicts_with: str


class McMismatchResponse(BaseModel):
    mod_id: str
    mod_mc_versions: list[str]
    server_mc_version: str


class PluginValidationResponse(BaseModel):
    """The phase-B dependency/compatibility checklist for a server's plugin set."""

    missing_deps: list[MissingDependencyResponse]
    missing_catalog_deps: list[MissingCatalogDependencyResponse]
    version_unsatisfied: list[VersionUnsatisfiedResponse]
    conflicts: list[ConflictResponse]
    mc_mismatch: list[McMismatchResponse]

    @classmethod
    def from_validation(cls, v: PluginValidation) -> "PluginValidationResponse":
        return cls(
            missing_deps=[
                MissingDependencyResponse(
                    mod_id=f.mod_id,
                    depends_on=f.depends_on,
                    version_range=f.version_range,
                )
                for f in v.missing_deps
            ],
            missing_catalog_deps=[
                MissingCatalogDependencyResponse(
                    mod_id=f.mod_id,
                    project_id=f.project_id,
                    slug=f.slug,
                    title=f.title,
                )
                for f in v.missing_catalog_deps
            ],
            version_unsatisfied=[
                VersionUnsatisfiedResponse(
                    mod_id=f.mod_id,
                    depends_on=f.depends_on,
                    version_range=f.version_range,
                    present_version=f.present_version,
                )
                for f in v.version_unsatisfied
            ],
            conflicts=[
                ConflictResponse(mod_id=f.mod_id, conflicts_with=f.conflicts_with)
                for f in v.conflicts
            ],
            mc_mismatch=[
                McMismatchResponse(
                    mod_id=f.mod_id,
                    mod_mc_versions=f.mod_mc_versions,
                    server_mc_version=f.server_mc_version,
                )
                for f in v.mc_mismatch
            ],
        )


# -- Dependency auto-resolution (issue #1309) --


class WillImportResponse(BaseModel):
    """The Modrinth project@version a ``needs_import`` dep resolves to."""

    project_id: str
    version_id: str
    slug: str
    version_number: str


class ResolutionEntryResponse(BaseModel):
    """One required dependency and how it resolves in the plan."""

    dep_identifier: str
    required_range: str
    status: str
    will_import: WillImportResponse | None
    depth: int
    required_by: str | None
    blocked: bool


class ResolutionPlanResponse(BaseModel):
    """The dependency-resolution plan plus the phase-B validation checklist."""

    entries: list[ResolutionEntryResponse]
    validation: PluginValidationResponse

    @classmethod
    def from_plan(cls, plan: ResolutionPlan) -> "ResolutionPlanResponse":
        return cls(
            entries=[
                ResolutionEntryResponse(
                    dep_identifier=e.dep_identifier,
                    required_range=e.required_range,
                    status=e.status,
                    will_import=(
                        WillImportResponse(
                            project_id=e.will_import.project_id,
                            version_id=e.will_import.version_id,
                            slug=e.will_import.slug,
                            version_number=e.will_import.version_number,
                        )
                        if e.will_import is not None
                        else None
                    ),
                    depth=e.depth,
                    required_by=e.required_by,
                    blocked=e.blocked,
                )
                for e in plan.entries
            ],
            validation=PluginValidationResponse.from_validation(plan.validation),
        )


class ApplyResolutionResponse(BaseModel):
    """The result of applying a resolution: the re-plan, installs, and failures.

    ``installed`` are the plugins newly installed from Modrinth; ``failed`` are
    the dep identifiers whose Modrinth lookup/install failed (isolated per dep);
    ``plan`` is the re-computed plan after the installs.
    """

    plan: ResolutionPlanResponse
    installed: list[PluginResponse]
    failed: list[str]


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


@router.get("/communities/{community_id}/servers/{server_id}/plugins/updates")
async def check_updates(
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
    use_case: Annotated[CheckUpdates, Depends(get_check_updates)],
) -> PluginUpdatesResponse:
    """Batch check for plugin updates (plugin:read)."""

    try:
        results = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except UnsupportedPluginServerTypeError as exc:
        raise _unprocessable("unsupported_server_type") from exc
    except CatalogUnavailableError as exc:
        raise _bad_gateway("catalog_unavailable") from exc
    return PluginUpdatesResponse(
        updates=[
            PluginUpdateInfoResponse(
                plugin=PluginResponse.from_plugin(r.plugin),
                latest_version=(
                    CatalogVersionItem.from_domain(r.latest_version)
                    if r.latest_version
                    else None
                ),
            )
            for r in results
        ]
    )


@router.get("/communities/{community_id}/servers/{server_id}/plugins/validate")
async def validate_plugins(
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
    use_case: Annotated[ValidatePluginSet, Depends(get_validate_plugin_set)],
) -> PluginValidationResponse:
    """Validate the server's installed plugin set (plugin:read, issue #1307).

    Returns the phase-B dependency/compatibility checklist (missing required
    deps, version-unsatisfied deps, conflicts, MC-version mismatch). Read-only:
    it never mutates the set.
    """

    try:
        result = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except UnsupportedPluginServerTypeError as exc:
        raise _unprocessable("unsupported_server_type") from exc
    return PluginValidationResponse.from_validation(result)


@router.post("/communities/{community_id}/servers/{server_id}/plugins/resolve")
async def resolve_plugin_dependencies(
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
    use_case: Annotated[
        ResolvePluginDependencies, Depends(get_resolve_plugin_dependencies)
    ],
) -> ResolutionPlanResponse:
    """Plan dependency auto-resolution (plugin:read, issue #1309).

    Computes the transitive closure of the server's required deps: each is
    classified already-satisfied (present in range), needs-import (a Modrinth
    project@version to install), unresolvable (no Modrinth match), or blocked (a
    transitive conflict). Read-only: nothing is downloaded or installed.
    """

    try:
        plan = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except UnsupportedPluginServerTypeError as exc:
        raise _unprocessable("unsupported_server_type") from exc
    except CatalogUnavailableError as exc:
        raise _bad_gateway("catalog_unavailable") from exc
    return ResolutionPlanResponse.from_plan(plan)


@router.post("/communities/{community_id}/servers/{server_id}/plugins/resolve/apply")
async def apply_plugin_resolution(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
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
    use_case: Annotated[ApplyPluginResolution, Depends(get_apply_plugin_resolution)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> ApplyResolutionResponse:
    """Apply dependency auto-resolution (plugin:manage, issue #1309).

    Installs each non-blocked needs-import dep from Modrinth onto the server via
    the catalog install path, then re-plans. At-rest gated (409
    ``server_unsettled`` while the server is running); a per-dep install failure
    is isolated and reported in ``failed``; a blocked (conflicting) dep is never
    installed.
    """

    try:
        plan, installed, failed = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            applied_by=authorized.user_id.value,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except UnsupportedPluginServerTypeError as exc:
        raise _unprocessable("unsupported_server_type") from exc
    except CatalogUnavailableError as exc:
        raise _bad_gateway("catalog_unavailable") from exc
    except ServerFilesUnsettledError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_RESOLVE, authorized, community_id, server_id
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_RESOLVE, authorized, community_id, server_id
        )
        raise _conflict("server_busy") from exc
    await _record_plugin(
        recorder, ops.PLUGIN_RESOLVE, authorized, community_id, server_id
    )
    return ApplyResolutionResponse(
        plan=ResolutionPlanResponse.from_plan(plan),
        installed=[PluginResponse.from_plugin(p) for p in installed],
        failed=failed,
    )


@router.get(
    "/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}/updates",
)
async def check_plugin_update(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    plugin_id: uuid.UUID,
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
    use_case: Annotated[CheckPluginUpdate, Depends(get_check_plugin_update)],
) -> PluginUpdateInfoResponse:
    """Check for a single plugin update (plugin:read)."""

    try:
        result = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            plugin_id=PluginId(plugin_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except PluginNotFoundError as exc:
        raise _not_found() from exc
    except CatalogUnavailableError as exc:
        raise _bad_gateway("catalog_unavailable") from exc
    return PluginUpdateInfoResponse(
        plugin=PluginResponse.from_plugin(result.plugin),
        latest_version=(
            CatalogVersionItem.from_domain(result.latest_version)
            if result.latest_version
            else None
        ),
    )


@router.get(
    "/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}",
)
async def get_plugin(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    plugin_id: uuid.UUID,
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
    use_case: Annotated[GetPlugin, Depends(get_get_plugin)],
) -> PluginResponse:
    """Get a single installed plugin by id (plugin:read)."""

    try:
        plugin = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            plugin_id=PluginId(plugin_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except PluginNotFoundError as exc:
        raise _not_found() from exc
    return PluginResponse.from_plugin(plugin)


@router.post(
    "/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}/update",
)
async def update_plugin(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    plugin_id: uuid.UUID,
    body: UpdatePluginRequest,
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
    use_case: Annotated[UpdatePlugin, Depends(get_update_plugin)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> PluginResponse:
    """Execute a plugin update to a specific version (plugin:manage)."""

    try:
        plugin = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            plugin_id=PluginId(plugin_id),
            version_id=body.version_id,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except PluginNotFoundError as exc:
        raise _not_found() from exc
    except CatalogProjectNotFoundError as exc:
        raise _not_found_catalog() from exc
    except CatalogUnavailableError as exc:
        raise _bad_gateway("catalog_unavailable") from exc
    except CatalogChecksumMismatchError as exc:
        raise _bad_gateway("checksum_mismatch") from exc
    except InvalidFilePathError as exc:
        raise _unprocessable("invalid_path") from exc
    except FileTooLargeError as exc:
        raise _too_large() from exc
    except PluginAlreadyExistsError as exc:
        raise _conflict("plugin_already_exists") from exc
    except ServerFilesUnsettledError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_UPDATE, authorized, community_id, plugin_id
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_UPDATE, authorized, community_id, plugin_id
        )
        raise _conflict("server_busy") from exc
    await _record_plugin(
        recorder, ops.PLUGIN_UPDATE, authorized, community_id, plugin.id.value
    )
    return PluginResponse.from_plugin(plugin)


@router.get(
    "/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}/dependencies",
)
async def list_plugin_dependencies(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    plugin_id: uuid.UUID,
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
    use_case: Annotated[ListPluginDependencies, Depends(get_list_plugin_dependencies)],
) -> PluginDependenciesResponse:
    """List dependencies for an installed plugin (plugin:read)."""

    try:
        deps = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            plugin_id=PluginId(plugin_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except PluginNotFoundError as exc:
        raise _not_found() from exc
    except CatalogUnavailableError as exc:
        raise _bad_gateway("catalog_unavailable") from exc
    return PluginDependenciesResponse(
        dependencies=[
            PluginDependencyResponse(
                project_id=d.project_id,
                version_id=d.version_id,
                dependency_type=d.dependency_type,
                project_title=d.project_title,
                project_slug=d.project_slug,
                installed=d.installed,
            )
            for d in deps
        ]
    )


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


@router.post(
    "/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}/side",
)
async def set_plugin_side(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    plugin_id: uuid.UUID,
    body: SetPluginSideRequest,
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
    use_case: Annotated[SetPluginSide, Depends(get_set_plugin_side)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> PluginResponse:
    """Override an installed plugin's side (plugin:manage, issue #1308).

    Changing the side re-materializes the working set: a client-only jar is
    removed from the running server, and a server-relevant jar is materialized
    from the content-addressed cache. At-rest gated (409 ``server_unsettled``).
    """

    try:
        plugin = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            plugin_id=PluginId(plugin_id),
            side=body.side,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except PluginNotFoundError as exc:
        raise _not_found() from exc
    except InvalidPluginSideError as exc:
        raise _unprocessable("invalid_side") from exc
    except ServerFilesUnsettledError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_SET_SIDE, authorized, community_id, plugin_id
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_plugin_failure(
            recorder, ops.PLUGIN_SET_SIDE, authorized, community_id, plugin_id
        )
        raise _conflict("server_busy") from exc
    await _record_plugin(
        recorder, ops.PLUGIN_SET_SIDE, authorized, community_id, plugin_id
    )
    return PluginResponse.from_plugin(plugin)


@router.get("/communities/{community_id}/servers/{server_id}/client-mods")
async def list_client_mods(
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
    use_case: Annotated[ListClientMods, Depends(get_list_client_mods)],
) -> ClientModsResponse:
    """List a server's enabled client-relevant plugins (plugin:read, issue #1308)."""

    try:
        plugins = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except UnsupportedPluginServerTypeError as exc:
        raise _unprocessable("unsupported_server_type") from exc
    return ClientModsResponse(plugins=[PluginResponse.from_plugin(p) for p in plugins])


@router.get("/communities/{community_id}/servers/{server_id}/client-mods/download")
async def download_client_modpack(
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
    use_case: Annotated[DownloadClientModpack, Depends(get_download_client_modpack)],
) -> StreamingResponse:
    """Download a server's client mods as a zip (plugin:read, issue #1308)."""

    try:
        stream = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except UnsupportedPluginServerTypeError as exc:
        raise _unprocessable("unsupported_server_type") from exc
    return StreamingResponse(
        stream,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="mods.zip"'},
    )


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


def _not_found_catalog() -> ProblemException:
    return problem(status.HTTP_404_NOT_FOUND, "catalog_project_not_found")


def _bad_gateway(reason: str) -> ProblemException:
    return problem(status.HTTP_502_BAD_GATEWAY, reason)
