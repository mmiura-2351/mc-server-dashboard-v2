"""Self-hosted API docs (issue #1990).

Serves the Swagger UI and ReDoc pages from same-origin static assets instead
of the default CDN, so they comply with the ``script-src 'self'`` CSP without
requiring an external-origin carve-out.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from starlette.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).parent / "static" / "docs"
_ASSETS_PATH = "/api/docs-assets"

# The openapi URL the docs pages load. Matches the ``FastAPI(openapi_url=...)``
# set in ``app.create_app`` (which the routes below read via ``app.openapi_url``);
# the init-script CSP hash is computed from it, and a test asserts the hash
# matches the actually-rendered page so any drift fails CI (issue #2154).
_OPENAPI_URL = "/api/openapi.json"

# Self-authored favicon as an inline SVG ``data:`` URI, replacing FastAPI's
# default ``https://fastapi.tiangolo.com/img/favicon.png`` which the docs
# ``img-src 'self' data:`` CSP blocks (issue #2154). A data: URI avoids adding a
# binary asset (and its provenance burden) while img-src's ``data:`` permits it.
_FAVICON_URL = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiA"
    "zMiI+PHJlY3Qgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiByeD0iNiIgZmlsbD0iIzNiN2QzZiIvPj"
    "xyZWN0IHg9IjciIHk9IjciIHdpZHRoPSI4IiBoZWlnaHQ9IjgiIGZpbGw9IiM4YmMzNGEiLz48c"
    "mVjdCB4PSIxNyIgeT0iNyIgd2lkdGg9IjgiIGhlaWdodD0iOCIgZmlsbD0iIzZhYTg0ZiIvPjxy"
    "ZWN0IHg9IjciIHk9IjE3IiB3aWR0aD0iOCIgaGVpZ2h0PSI4IiBmaWxsPSIjNmFhODRmIi8+PHJ"
    "lY3QgeD0iMTciIHk9IjE3IiB3aWR0aD0iOCIgaGVpZ2h0PSI4IiBmaWxsPSIjOGJjMzRhIi8+PC"
    "9zdmc+"
)


def _swagger_ui_response(*, openapi_url: str, title: str) -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=openapi_url,
        title=title,
        swagger_js_url=f"{_ASSETS_PATH}/swagger-ui-bundle.js",
        swagger_css_url=f"{_ASSETS_PATH}/swagger-ui.css",
        swagger_favicon_url=_FAVICON_URL,
    )


def _swagger_init_script_csp_hash() -> str:
    """CSP ``'sha256-...'`` source for the swagger-ui inline init ``<script>``.

    ``get_swagger_ui_html`` emits a single fixed inline ``<script>`` (the
    ``SwaggerUIBundle({...})`` initializer). The browser enforces a hash source
    over the exact bytes between the script tags, so compute it from the rendered
    page rather than pinning a literal — a FastAPI bump that changes the script
    updates the hash automatically (and the drift test still guards it).
    """

    body = _swagger_ui_response(openapi_url=_OPENAPI_URL, title="").body
    html = bytes(body).decode()
    match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert match is not None, "swagger-ui inline init <script> not found"
    digest = hashlib.sha256(match.group(1).encode()).digest()
    return "sha256-" + base64.b64encode(digest).decode()


# Computed once at import; consumed by the security-headers middleware to build
# the docs-path CSP (issue #2154).
SWAGGER_INIT_SCRIPT_CSP_HASH = _swagger_init_script_csp_hash()


def mount_docs(app: FastAPI) -> None:
    """Mount self-hosted Swagger UI and ReDoc on *app*.

    The caller must set ``docs_url=None, redoc_url=None`` on the ``FastAPI``
    constructor so the default CDN-backed routes are not registered.
    """

    app.mount(_ASSETS_PATH, StaticFiles(directory=_STATIC_DIR), name="docs-assets")

    @app.get("/api/docs", include_in_schema=False)
    async def swagger_ui_html() -> HTMLResponse:
        return _swagger_ui_response(
            openapi_url=app.openapi_url or _OPENAPI_URL,
            title=f"{app.title} - Swagger UI",
        )

    @app.get("/api/redoc", include_in_schema=False)
    async def redoc_html() -> HTMLResponse:
        return get_redoc_html(
            openapi_url=app.openapi_url or _OPENAPI_URL,
            title=f"{app.title} - ReDoc",
            redoc_js_url=f"{_ASSETS_PATH}/redoc.standalone.js",
            redoc_favicon_url=_FAVICON_URL,
            with_google_fonts=False,
        )
