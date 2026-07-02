"""HTTP edge for Modrinth catalog integration (issue #1151).

Routes live under ``/communities/{community_id}/servers/{server_id}/catalog``
and are per-resource gated (``resource_type='server'``,
``resource_id_param='server_id'``). Search and project detail require
``plugin:read``; install requires ``plugin:manage``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.community.domain.value_objects import AuthUser, Permission
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_get_catalog_project,
    get_install_from_catalog,
    get_search_catalog,
    require_permission,
)
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.servers.api.plugins import PluginResponse
from mc_server_dashboard_api.servers.application.catalog import (
    GetCatalogProject,
    InstallFromCatalog,
    SearchCatalog,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogDependency as CatalogDependencyDomain,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogProject as CatalogProjectDomain,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogSearchResult as CatalogSearchResultDomain,
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
    PluginAlreadyExistsError,
    PortAlreadyTakenError,
    PortRangeExhaustedError,
    ServerBusyError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
    UnsupportedPluginServerTypeError,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

router = APIRouter()

_SERVER_RESOURCE_TYPE = "server"


# -- Pydantic response models --


class CatalogSearchResultResponse(BaseModel):
    project_id: str
    slug: str
    title: str
    description: str
    author: str
    icon_url: str | None
    downloads: int
    categories: list[str]
    latest_game_versions: list[str]

    @classmethod
    def from_domain(cls, r: CatalogSearchResultDomain) -> CatalogSearchResultResponse:
        return cls(
            project_id=r.project_id,
            slug=r.slug,
            title=r.title,
            description=r.description,
            author=r.author,
            icon_url=r.icon_url,
            downloads=r.downloads,
            categories=r.categories,
            latest_game_versions=r.latest_game_versions,
        )


class CatalogSearchListResponse(BaseModel):
    hits: list[CatalogSearchResultResponse]
    total_hits: int
    offset: int
    limit: int


class CatalogFileResponse(BaseModel):
    url: str
    filename: str
    size: int
    sha512: str
    primary: bool


class CatalogDependencyResponse(BaseModel):
    version_id: str | None
    project_id: str
    dependency_type: str

    @classmethod
    def from_domain(cls, d: CatalogDependencyDomain) -> CatalogDependencyResponse:
        return cls(
            version_id=d.version_id,
            project_id=d.project_id,
            dependency_type=d.dependency_type,
        )


class CatalogVersionResponse(BaseModel):
    version_id: str
    version_number: str
    name: str
    game_versions: list[str]
    loaders: list[str]
    files: list[CatalogFileResponse]
    date_published: str
    dependencies: list[CatalogDependencyResponse]

    @classmethod
    def from_domain(cls, v: CatalogVersionDomain) -> CatalogVersionResponse:
        return cls(
            version_id=v.version_id,
            version_number=v.version_number,
            name=v.name,
            game_versions=v.game_versions,
            loaders=v.loaders,
            files=[
                CatalogFileResponse(
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
                CatalogDependencyResponse.from_domain(d) for d in v.dependencies
            ],
        )


class CatalogProjectResponse(BaseModel):
    project_id: str
    slug: str
    title: str
    description: str
    body: str
    author: str | None
    icon_url: str | None
    downloads: int
    categories: list[str]
    game_versions: list[str]
    loaders: list[str]

    @classmethod
    def from_domain(cls, p: CatalogProjectDomain) -> CatalogProjectResponse:
        return cls(
            project_id=p.project_id,
            slug=p.slug,
            title=p.title,
            description=p.description,
            body=p.body,
            author=p.author,
            icon_url=p.icon_url,
            downloads=p.downloads,
            categories=p.categories,
            game_versions=p.game_versions,
            loaders=p.loaders,
        )


class CatalogProjectDetailResponse(BaseModel):
    project: CatalogProjectResponse
    versions: list[CatalogVersionResponse]


class CatalogInstallRequest(BaseModel):
    project_id: str
    version_id: str


# -- Routes --


@router.get(
    "/communities/{community_id}/servers/{server_id}/catalog/search",
)
async def search_catalog(
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
    use_case: Annotated[SearchCatalog, Depends(get_search_catalog)],
    q: Annotated[str, Query()] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CatalogSearchListResponse:
    """Search the Modrinth catalog with auto-applied server facets (plugin:read)."""

    try:
        result = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            query=q,
            limit=limit,
            offset=offset,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except UnsupportedPluginServerTypeError as exc:
        raise _unprocessable("unsupported_server_type") from exc
    except CatalogUnavailableError as exc:
        raise _bad_gateway("catalog_unavailable") from exc
    return CatalogSearchListResponse(
        hits=[CatalogSearchResultResponse.from_domain(h) for h in result.hits],
        total_hits=result.total_hits,
        offset=result.offset,
        limit=result.limit,
    )


@router.get(
    "/communities/{community_id}/servers/{server_id}/catalog/projects/{project_id_or_slug}",
)
async def get_catalog_project(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    project_id_or_slug: str,
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
    use_case: Annotated[GetCatalogProject, Depends(get_get_catalog_project)],
) -> CatalogProjectDetailResponse:
    """Fetch catalog project detail + compatible versions (plugin:read)."""

    try:
        project, versions = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            project_id_or_slug=project_id_or_slug,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except UnsupportedPluginServerTypeError as exc:
        raise _unprocessable("unsupported_server_type") from exc
    except CatalogProjectNotFoundError as exc:
        raise _not_found_catalog() from exc
    except CatalogUnavailableError as exc:
        raise _bad_gateway("catalog_unavailable") from exc
    return CatalogProjectDetailResponse(
        project=CatalogProjectResponse.from_domain(project),
        versions=[CatalogVersionResponse.from_domain(v) for v in versions],
    )


@router.post(
    "/communities/{community_id}/servers/{server_id}/catalog/install",
    status_code=status.HTTP_201_CREATED,
)
async def install_from_catalog(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    body: CatalogInstallRequest,
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
    use_case: Annotated[InstallFromCatalog, Depends(get_install_from_catalog)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> PluginResponse:
    """Install a plugin/mod from the Modrinth catalog (plugin:manage)."""

    try:
        plugin = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            project_id=body.project_id,
            version_id=body.version_id,
            installed_by=authorized.user_id.value,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except UnsupportedPluginServerTypeError as exc:
        raise _unprocessable("unsupported_server_type") from exc
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
    except PortRangeExhaustedError as exc:
        # A Geyser install found no free port in the Bedrock UDP window (issue
        # #1541): transient capacity, like game-port exhaustion at create.
        raise _service_unavailable("bedrock_port_range_exhausted") from exc
    except PortAlreadyTakenError as exc:
        # The UNIQUE(bedrock_port) backstop fired on a concurrent allocation
        # racer (issue #1541), translated by the adapter; a retry re-picks a
        # free port.
        raise _conflict("bedrock_port_taken") from exc
    except ServerFilesUnsettledError as exc:
        await _record_failure(
            recorder,
            authorized,
            community_id,
            server_id,
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_failure(
            recorder,
            authorized,
            community_id,
            server_id,
        )
        raise _conflict("server_busy") from exc
    await recorder.record(
        AuditEvent(
            operation=ops.PLUGIN_INSTALL,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_PLUGIN,
            target_id=plugin.id.value,
        )
    )
    return PluginResponse.from_plugin(plugin)


# -- helpers --


async def _record_failure(
    recorder: AuditRecorder,
    authorized: AuthUser,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
) -> None:
    await recorder.record(
        AuditEvent(
            operation=ops.PLUGIN_INSTALL,
            outcome=Outcome.DENIED,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_SERVER,
            target_id=server_id,
        )
    )


def _not_found() -> ProblemException:
    return problem(status.HTTP_404_NOT_FOUND, "not_found")


def _not_found_catalog() -> ProblemException:
    return problem(status.HTTP_404_NOT_FOUND, "catalog_project_not_found")


def _unprocessable(reason: str) -> ProblemException:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, reason)


def _bad_gateway(reason: str) -> ProblemException:
    return problem(status.HTTP_502_BAD_GATEWAY, reason)


def _conflict(reason: str) -> ProblemException:
    return problem(status.HTTP_409_CONFLICT, reason)


def _service_unavailable(reason: str) -> ProblemException:
    return problem(status.HTTP_503_SERVICE_UNAVAILABLE, reason)


def _too_large() -> ProblemException:
    return problem(status.HTTP_413_CONTENT_TOO_LARGE, "file_too_large")
