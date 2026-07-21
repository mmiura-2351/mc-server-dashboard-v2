"""Self-hosted API docs (issue #1990).

Serves the Swagger UI and ReDoc pages from same-origin static assets instead
of the default CDN, so they comply with the ``script-src 'self'`` CSP without
requiring an external-origin carve-out.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from starlette.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).parent / "static" / "docs"
_ASSETS_PATH = "/api/docs-assets"


def mount_docs(app: FastAPI) -> None:
    """Mount self-hosted Swagger UI and ReDoc on *app*.

    The caller must set ``docs_url=None, redoc_url=None`` on the ``FastAPI``
    constructor so the default CDN-backed routes are not registered.
    """

    app.mount(_ASSETS_PATH, StaticFiles(directory=_STATIC_DIR), name="docs-assets")

    @app.get("/api/docs", include_in_schema=False)
    async def swagger_ui_html() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/api/openapi.json",
            title=f"{app.title} - Swagger UI",
            swagger_js_url=f"{_ASSETS_PATH}/swagger-ui-bundle.js",
            swagger_css_url=f"{_ASSETS_PATH}/swagger-ui.css",
        )

    @app.get("/api/redoc", include_in_schema=False)
    async def redoc_html() -> HTMLResponse:
        return get_redoc_html(
            openapi_url=app.openapi_url or "/api/openapi.json",
            title=f"{app.title} - ReDoc",
            redoc_js_url=f"{_ASSETS_PATH}/redoc.standalone.js",
            with_google_fonts=False,
        )
