"""HTTP edge for the global version catalog (FR-VER-1, issue #286).

Read-only catalog endpoints plus two platform-admin operational windows:

- ``GET /versions`` — the server types the catalog can resolve at M1.
- ``GET /versions/{server_type}`` — the versions offered for a type.
- ``POST /versions/refresh[?server_type=]`` — invalidate the in-process manifest
  cache (all types or one); the cache is a source-down fallback, so this clears
  the last-good payloads (platform admin).
- ``GET /versions/jar-pool/stats`` — count + total bytes of the pooled JARs
  (platform admin).
- ``POST /versions/jar-pool/gc`` — reference-counted garbage collection of the
  pool; returns {scanned, deleted, freed_bytes} (audited, platform admin).

**Auth choice.** The catalog is *global* — it carries no community/server scope
(STORAGE.md Section 8.1: JARs are shared platform-wide). There is no
``version:*`` permission code in the authorization catalog (Appendix A), and
adding one would be speculative. So the read-only listing endpoints require only
an authenticated user (``get_current_user``); the operational windows are gated by
the cross-cutting platform-admin axis (``require_platform_admin``), the same
posture as the /workers and admin-user surfaces.

The router is thin: resolve the use cases via DI, run them, serialise, and map
the catalog's domain errors to HTTP codes here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_catalog_refresh,
    get_current_user,
    get_jar_pool_gc,
    get_jar_pool_stats,
    get_list_server_types,
    get_list_versions,
    require_platform_admin,
)
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.versions.application.catalog_refresh import CatalogRefresh
from mc_server_dashboard_api.versions.application.jar_gc import RunJarPoolGc
from mc_server_dashboard_api.versions.application.jar_pool_stats import GetJarPoolStats
from mc_server_dashboard_api.versions.application.list_versions import (
    ListServerTypes,
    ListVersions,
)
from mc_server_dashboard_api.versions.domain.errors import (
    CatalogUnavailableError,
    UnknownServerTypeError,
)
from mc_server_dashboard_api.versions.domain.value_objects import ServerType

router = APIRouter(prefix="/versions")


class ServerTypesResponse(BaseModel):
    """The server types the catalog can resolve at M1 (issue #286)."""

    server_types: list[str]


class VersionsResponse(BaseModel):
    """The versions offered for a server type (issue #286)."""

    versions: list[str]


class RefreshResponse(BaseModel):
    """The catalogs the refresh invalidated (issue #286)."""

    invalidated: list[str]


class JarPoolStatsResponse(BaseModel):
    """Count + total bytes of the pooled JARs (issue #286)."""

    count: int
    total_bytes: int


class JarPoolGcResponse(BaseModel):
    """What a JAR-pool GC pass scanned and reclaimed (issue #293)."""

    scanned: int
    deleted: int
    freed_bytes: int


@router.get("")
async def list_server_types(
    _user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[ListServerTypes, Depends(get_list_server_types)],
) -> ServerTypesResponse:
    """List the server types the catalog can resolve at M1."""

    types = await use_case()
    return ServerTypesResponse(server_types=[t.value for t in types])


@router.post("/refresh")
async def refresh_catalog(
    admin: Annotated[User, Depends(require_platform_admin)],
    use_case: Annotated[CatalogRefresh, Depends(get_catalog_refresh)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    server_type: Annotated[str | None, Query()] = None,
) -> RefreshResponse:
    """Invalidate the manifest cache for one type or all of them (platform admin).

    The cache is a source-down fallback, so this drops the last-good payloads (a
    successful listing GET always refetches from the source anyway). ``server_type``
    filters to one catalogued type; omitting it refreshes every type. An unknown
    type is a 404 (mirroring the listing surface).
    """

    parsed = _parse_server_type(server_type) if server_type is not None else None
    try:
        invalidated = await use_case(server_type=parsed)
    except UnknownServerTypeError as exc:
        raise _unknown_type() from exc
    await recorder.record(
        AuditEvent(
            operation=ops.VERSION_REFRESH,
            outcome=Outcome.SUCCESS,
            actor_id=admin.id.value,
        )
    )
    return RefreshResponse(invalidated=[t.value for t in invalidated])


@router.get("/jar-pool/stats")
async def jar_pool_stats(
    _admin: Annotated[User, Depends(require_platform_admin)],
    use_case: Annotated[GetJarPoolStats, Depends(get_jar_pool_stats)],
) -> JarPoolStatsResponse:
    """Count + total bytes of the pooled JARs (platform admin, issue #286)."""

    stats = await use_case()
    return JarPoolStatsResponse(count=stats.count, total_bytes=stats.total_bytes)


@router.post("/jar-pool/gc")
async def jar_pool_gc(
    admin: Annotated[User, Depends(require_platform_admin)],
    use_case: Annotated[RunJarPoolGc, Depends(get_jar_pool_gc)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> JarPoolGcResponse:
    """Reclaim pooled JARs no live server row references (platform admin, #293).

    A bounded scan diffing the pool against the live reference set, skipping JARs
    inside the safety window; returns what it scanned and freed. Audited like the
    catalog refresh.
    """

    result = await use_case()
    await recorder.record(
        AuditEvent(
            operation=ops.VERSION_JAR_GC,
            outcome=Outcome.SUCCESS,
            actor_id=admin.id.value,
        )
    )
    return JarPoolGcResponse(
        scanned=result.scanned,
        deleted=result.deleted,
        freed_bytes=result.freed_bytes,
    )


@router.get("/{server_type}")
async def list_versions(
    server_type: str,
    _user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[ListVersions, Depends(get_list_versions)],
) -> VersionsResponse:
    """List the versions offered for ``server_type``."""

    parsed = _parse_server_type(server_type)
    try:
        versions = await use_case(server_type=parsed)
    except CatalogUnavailableError as exc:
        raise _service_unavailable() from exc
    return VersionsResponse(versions=[v.version for v in versions])


def _parse_server_type(value: str) -> ServerType:
    try:
        return ServerType(value)
    except ValueError as exc:
        # spigot (and any other non-catalogued type) is unsupported: a clean 404 on
        # the catalog surface, distinct from a transient source outage.
        raise _unknown_type() from exc


def _unknown_type() -> ProblemException:
    return problem(status.HTTP_404_NOT_FOUND, "unknown_server_type")


def _service_unavailable() -> ProblemException:
    return problem(status.HTTP_503_SERVICE_UNAVAILABLE, "catalog_unavailable")
