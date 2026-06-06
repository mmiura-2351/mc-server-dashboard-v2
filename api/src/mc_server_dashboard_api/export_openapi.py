"""Dump the FastAPI app's OpenAPI schema to a JSON file, no server needed.

The Web UI codes against a client generated from this schema (WEBUI_SPEC.md
7.6). The generator runs without a live server: it builds the app in-process and
serialises ``app.openapi()``. Schema generation only walks the route table, so
the export is hermetic — no database connection, no network, no gRPC listener.

The app factory fails fast on a few required secrets even when only the schema
is wanted (a signing key to mount the auth routers, a worker credential / TLS
posture when the control plane is enabled). Hermetic placeholders satisfy those
checks; none are dialled, since the lifespan that would open them never runs.

Usage::

    cd api && uv run python -m mc_server_dashboard_api.export_openapi <out.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.config import (
    AuthSettings,
    ControlSettings,
    DatabaseSettings,
    Settings,
    TokenSettings,
)


def _hermetic_settings() -> Settings:
    """Settings that build the app without touching a DB, network, or gRPC port.

    The database URL is a dummy: SQLAlchemy engines are lazy and the lifespan
    (which would dial it) never runs during schema generation. The control plane
    is disabled so no gRPC listener is required, and a placeholder HS256 signing
    key satisfies the auth-router fail-fast.
    """

    return Settings(
        database=DatabaseSettings(
            url="postgresql+asyncpg://export:export@localhost/export"
        ),
        control=ControlSettings(enabled=False),
        auth=AuthSettings(
            token=TokenSettings(signing_key="openapi-export-placeholder-key-32b")
        ),
    )


def export(out_path: Path) -> None:
    app = create_app(_hermetic_settings())
    schema = app.openapi()
    out_path.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write(
            "usage: python -m mc_server_dashboard_api.export_openapi <out.json>\n"
        )
        return 2
    export(Path(argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
