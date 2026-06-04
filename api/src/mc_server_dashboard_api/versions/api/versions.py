"""HTTP edge for the global version catalog (FR-VER-1).

Two read-only endpoints:

- ``GET /versions`` — the server types the catalog can resolve at M1.
- ``GET /versions/{server_type}`` — the versions offered for a type.

**Auth choice.** The catalog is *global* — it carries no community/server scope
(STORAGE.md Section 8.1: JARs are shared platform-wide). There is no
``version:*`` permission code in the authorization catalog (Appendix A), and
adding one would be speculative. So these endpoints require only an authenticated
user (``get_current_user``), not a community membership/permission check:
read-only access to a public version listing for any logged-in user is
proportionate, and avoids inventing a permission with no resource to scope it to.

The router is thin: resolve the use cases via DI, run them, serialise, and map
the catalog's domain errors to HTTP codes here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from mc_server_dashboard_api.dependencies import (
    get_current_user,
    get_list_server_types,
    get_list_versions,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.versions.application.list_versions import (
    ListServerTypes,
    ListVersions,
)
from mc_server_dashboard_api.versions.domain.errors import CatalogUnavailableError
from mc_server_dashboard_api.versions.domain.value_objects import ServerType

router = APIRouter(prefix="/versions")


@router.get("")
async def list_server_types(
    _user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[ListServerTypes, Depends(get_list_server_types)],
) -> dict[str, list[str]]:
    """List the server types the catalog can resolve at M1."""

    types = await use_case()
    return {"server_types": [t.value for t in types]}


@router.get("/{server_type}")
async def list_versions(
    server_type: str,
    _user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[ListVersions, Depends(get_list_versions)],
) -> dict[str, list[str]]:
    """List the versions offered for ``server_type``."""

    parsed = _parse_server_type(server_type)
    try:
        versions = await use_case(server_type=parsed)
    except CatalogUnavailableError as exc:
        raise _service_unavailable() from exc
    return {"versions": [v.version for v in versions]}


def _parse_server_type(value: str) -> ServerType:
    try:
        return ServerType(value)
    except ValueError as exc:
        # forge (and any other non-catalogued type) is unsupported at M1: a clean
        # 404 on the catalog surface, distinct from a transient source outage.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_server_type"},
        ) from exc


def _service_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"reason": "catalog_unavailable"},
    )
