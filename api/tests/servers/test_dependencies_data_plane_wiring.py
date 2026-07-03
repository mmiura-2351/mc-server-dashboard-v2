"""Wiring-level regression test for the data-plane base URL (issue #1549).

Pins a REAL construction site — ``get_servers_control_plane`` in
``dependencies.py`` — to thread ``server.effective_data_plane_base_url``
through to the rendered transfer URL. Before #1549 the wiring passed
``server.public_base_url`` directly, so a deployment whose public URL routes
through a body-size-capped edge proxy hairpinned every worker snapshot/hydrate
upload through that edge and 413ed. The adapter-level tests cannot catch a
regression here: they construct the adapter themselves.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from mc_server_dashboard_api.config import load_settings
from mc_server_dashboard_api.dependencies import get_servers_control_plane
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.control_plane import (
    Command,
    CommandResult,
    CommandResultCode,
    HydrateCommand,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    ControlPlane as FleetControlPlane,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
    WorkerId,
)
from tests.fleet.fakes import FakeClock


class _CapturingFleetControlPlane(FleetControlPlane):
    def __init__(self) -> None:
        self.last: Command | None = None

    async def dispatch(
        self,
        *,
        worker_id: FleetWorkerId,
        server_id: str,
        command: Command,
        timeout_override: float | None = None,
        snapshot_is_final: bool = False,
    ) -> CommandResult:
        self.last = command
        return CommandResult(code=CommandResultCode.OK)


async def test_get_servers_control_plane_renders_urls_from_data_plane_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_SERVER__PUBLIC_BASE_URL", "https://edge.example.com")
    monkeypatch.setenv("MCD_API_SERVER__DATA_PLANE_BASE_URL", "http://api:8000")
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "shhh")
    app = FastAPI()
    app.state.settings = load_settings(config_file=None)
    request = Request(scope={"type": "http", "app": app})
    fleet = _CapturingFleetControlPlane()

    registry = InMemoryWorkerRegistry(
        clock=FakeClock(dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)),
        heartbeat_timeout=dt.timedelta(seconds=30),
    )
    adapter = get_servers_control_plane(
        request, registry=registry, fleet_control_plane=fleet
    )
    community = uuid.uuid4()
    server = uuid.uuid4()
    await adapter.hydrate(
        worker_id=WorkerId(uuid.uuid4()),
        community_id=CommunityId(community),
        server_id=ServerId(server),
    )

    assert isinstance(fleet.last, HydrateCommand)
    assert fleet.last.transfer_url == (
        f"http://api:8000/api/data-plane/communities/{community}"
        f"/servers/{server}/working-set"
    )
