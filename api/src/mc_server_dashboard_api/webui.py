"""Same-origin static serving of the built Web UI SPA (WEBUI_SPEC 7.7, #490).

The API serves ``webui/dist`` from its own origin — no CORS, no reverse proxy,
no new Compose service. Mounted at ``/`` after every router, so API routes and
the WebSocket endpoints take strict precedence (Starlette resolves a ``Mount``
at ``/`` last). Any other path that does not name an existing asset falls back
to ``index.html`` so client-side routing works on deep links and reloads.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles


class SpaStaticFiles(StaticFiles):
    """``StaticFiles`` that falls back to ``index.html`` for missing paths.

    A real asset (``/assets/app-abc.js``) is served as-is; any other path that
    does not resolve to a file returns ``index.html`` with a 200 so the SPA
    router can render the client-side route. A genuinely missing asset (a path
    under a directory that the SPA would never own, e.g. a fingerprinted file
    that is gone) also resolves to ``index.html`` — acceptable for an SPA, and
    the precedent FastAPI pattern.

    The one carve-out is ``/api`` (issue #567): the whole HTTP API lives under
    that prefix (issue #498), so an unmatched ``/api/*`` GET is a wrong/removed
    route, not a client-side SPA route. Letting it fall back to ``index.html``
    returns a misleading 200 + HTML to an API client; re-raising the 404 lets
    the app-level problem handler render an honest ``application/problem+json``
    response instead. The contract stays "everything non-``/api`` serves the
    SPA".
    """

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            # ``path`` is relative to the ``/`` mount, so ``/api/users`` arrives
            # as ``api/users``; never shadow an unmatched /api path with the SPA.
            if exc.status_code == 404 and not (
                path == "api" or path.startswith("api/")
            ):
                return await super().get_response("index.html", scope)
            raise


def mount_webui(app: FastAPI, dist_dir: str) -> None:
    """Mount the built SPA at ``/`` with an SPA fallback.

    Fails fast if ``dist_dir`` is not a directory or has no ``index.html`` so a
    misconfigured deployment does not start serving 404s instead of the UI
    (CONFIGURATION.md Section 3).
    """

    root = Path(dist_dir)
    if not root.is_dir():
        raise ValueError(f"webui.dist_dir is not a directory: {dist_dir}")
    if not (root / "index.html").is_file():
        raise ValueError(f"webui.dist_dir has no index.html: {dist_dir}")
    app.mount("/", SpaStaticFiles(directory=root, html=True), name="webui")
