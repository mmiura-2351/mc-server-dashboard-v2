"""The observability listener: the Prometheus exposition on its own port (#2565).

The exposition used to be a route on the public API app (``GET /api/metrics``).
The bundled Compose deployment fronts the API with ``cloudflared``, which
forwards the whole public hostname to ``api:8000`` — so every path the API
served was on the internet, and the exposition's server/worker counts, per-route
request **and auth-outcome** counters, process start timestamps and control-plane
liveness were world-readable (confirmed by probe on the live deployments).

Serving it from a second listener makes it unreachable from the tunnel *by
construction* rather than reachable-and-rejected: ``cloudflared`` targets
``:8000``, and the exposition is no longer on ``:8000``. Nothing a future
routing or middleware change can silently undo. The listener is opt-in and off
by default — the posture the relay already takes for its own metrics endpoint
(RELAY.md Section 17), so the two modules answer this question the same way.

Reachability is governed by the deployment, not by this module: the container
port is simply never published, so only the compose network can reach it. See
:class:`~mc_server_dashboard_api.config.MetricsSettings` for why the bind
address is deliberately NOT loopback, and what that obliges an operator to do.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Generator

import uvicorn
from fastapi import FastAPI

from mc_server_dashboard_api.core.api import metrics

_LOG = logging.getLogger(__name__)

# Graceful-shutdown budget when the API lifespan tears the listener down. A
# scrape is a single short GET, so this only bounds a client holding a
# connection open; mirrors the relay's own 5 s metrics-shutdown bound.
_SHUTDOWN_TIMEOUT_SECONDS = 5


def create_observability_app() -> FastAPI:
    """Build the app the observability listener serves: the exposition, nothing else.

    No OpenAPI schema and no docs routes — this app exists to answer one scrape,
    and every extra route on it is another surface an operator must reason
    about. It carries none of the public app's middleware either, so a scrape no
    longer counts itself into ``http_requests_total``.

    The caller must put ``engine`` and ``worker_registry`` on ``app.state``: the
    route's dependencies read them from there, exactly as they do on the public
    app (``dependencies.get_metrics_session_factory`` / ``get_worker_registry``).
    """

    app = FastAPI(
        title="mc-server-dashboard observability",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(metrics.router)
    return app


class _EmbeddedServer(uvicorn.Server):
    """A uvicorn server that keeps its hands off process signal handling.

    ``uvicorn.Server.serve`` installs its own SIGINT/SIGTERM handlers, which
    would shadow those of the outer uvicorn server this listener runs inside —
    a container SIGTERM would reach this listener rather than the API's own
    server. This listener's lifetime belongs to the API lifespan, which stops it
    in its teardown, so it must not touch signals at all.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class ObservabilityListener:
    """A bound, serving observability listener owned by the API lifespan."""

    def __init__(
        self, server: uvicorn.Server, task: asyncio.Task[None], port: int
    ) -> None:
        self._server = server
        self._task = task
        self.port = port

    async def stop(self) -> None:
        """Stop serving and wait for the server task to finish.

        Never raises: this runs first in the lifespan's teardown, and an
        exception here would skip everything after it (gRPC stop, engine
        dispose).
        """

        self._server.should_exit = True
        try:
            await self._task
        except Exception:
            _LOG.error("observability listener stopped with an error", exc_info=True)


def start_observability_listener(
    app: FastAPI, *, host: str, port: int
) -> ObservabilityListener | None:
    """Bind ``host:port``, serve ``app`` on it, and return the handle.

    A bind failure is **non-fatal** — it is logged and ``None`` is returned, so
    the API keeps serving without the exposition. This mirrors the relay, whose
    metrics bind is non-fatal for the same reason: observability must never take
    down the service it observes.

    The socket is bound here rather than by uvicorn because uvicorn's own bind
    path calls ``sys.exit(1)``, which from a lifespan task would take the API
    process down — the opposite of the intended posture.
    """

    try:
        sock = socket.create_server((host, port))
    except OSError:
        _LOG.error(
            "metrics listener bind failed; continuing without the metrics endpoint",
            extra={"host": host, "port": port},
            exc_info=True,
        )
        return None
    bound_port: int = sock.getsockname()[1]
    config = uvicorn.Config(
        app,
        host=host,
        port=bound_port,
        # A scrape every few seconds would otherwise emit an access-log line
        # every few seconds, forever.
        access_log=False,
        # One GET, no WebSocket routes: skip loading the WS protocol entirely.
        ws="none",
        # The process already configured logging (``create_app``); uvicorn's
        # ``Config`` applies its own ``dictConfig`` unless this is None.
        log_config=None,
        timeout_graceful_shutdown=_SHUTDOWN_TIMEOUT_SECONDS,
    )
    server = _EmbeddedServer(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    _LOG.info("metrics endpoint listening", extra={"host": host, "port": bound_port})
    return ObservabilityListener(server, task, bound_port)
