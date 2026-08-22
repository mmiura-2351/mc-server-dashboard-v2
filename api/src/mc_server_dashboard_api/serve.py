"""The API server entry point: bind the HTTP listener from configuration (#2585).

The container image used to run ``uvicorn ... --host 0.0.0.0 --port 8000``, so
``server.host`` and ``server.http_port`` — real, typed, documented settings
(CONFIGURATION.md Section 5.1) — could not change anything on the only way this
project is deployed. An operator who set ``MCD_API_SERVER__HTTP_PORT`` got no
error, no warning and no effect; a config knob that silently no-ops is worse than
an absent one, because the operator reasons from having changed something.

The settings are authoritative, so the image's entry command carries no bind
arguments at all and this module supplies them from the same loader
``create_app`` uses. ``server.host`` already governed the control-plane gRPC bind
(``app.py``); the HTTP listener now follows the same key instead of a literal
baked into one image.

Run as ``python -m mc_server_dashboard_api.serve`` (the image's ``CMD``).
"""

from __future__ import annotations

import uvicorn

from mc_server_dashboard_api.app import _resolve_config_file, create_app
from mc_server_dashboard_api.config import load_settings


def main() -> None:
    settings = load_settings(_resolve_config_file())
    # Passed as a *factory*, not as a built app: uvicorn configures logging in
    # ``Config.__init__`` and calls the factory later (``Config.load``), so
    # ``create_app``'s own ``configure_logging`` still runs last — the ordering
    # the ``uvicorn`` CLI form gave, where the factory was likewise imported and
    # called after uvicorn had configured its own logging. Building the app here
    # and handing over the instance would let uvicorn's ``dictConfig`` land on
    # top of the configured handler instead.
    #
    # Everything except host/port stays at uvicorn's defaults, matching the CLI
    # invocation this replaces — notably ``ws="auto"``, which is what lets the
    # installed ``websockets`` back the ``/events`` sockets (issue #507).
    uvicorn.run(
        lambda: create_app(settings),
        factory=True,
        host=settings.server.host,
        port=settings.server.http_port,
    )


if __name__ == "__main__":
    main()
