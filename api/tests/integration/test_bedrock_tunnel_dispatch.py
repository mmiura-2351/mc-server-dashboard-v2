"""Integration tests for the Bedrock relay tunnel lifecycle dispatch (issue #1544).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Drives the real
:class:`ServersServerStateSink` (the control-plane event path's write-back,
CONTROL_PLANE.md Section 4.3) against a real server + plugin row, with a real
:class:`GrpcControlPlane` over a fake worker outbound queue standing in for the
Worker's control-plane stream -- the same "fake worker stream" pattern
``tests/fleet/test_relay_service.py`` uses for ``TunnelDial`` dispatch.

Acceptance criteria mapped to tests (epic #1540 sub-issue #1544):

- a running Bedrock-enabled server dispatches ``OpenBedrockTunnel`` with a
  valid token + port;
- a stopped one dispatches ``CloseBedrockTunnel``, and the token used to open
  it no longer validates;
- a non-Bedrock server, and a Bedrock-enabled server whose only Geyser copy is
  disabled, dispatch neither (PM note on issue #1544).
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
)
from mc_server_dashboard_api.fleet.adapters.relay_state import (
    BedrockTunnelTable,
    RelayRegistration,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ServerId,
    WorkerId,
)
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import FakeClock, FakeFileStore, FakeVersionValidator

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 7, 2, 12, 0, tzinfo=dt.timezone.utc)
_RELAY_ENDPOINT = "relay.example.com:25665"
_RELAY_CA_PEM = "CA-PEM"
_BEDROCK_TUNNEL_PORT = 25675
_BEDROCK_PORT = 19132


class _AdvancingClock(Clock):
    """A clock that advances one second on every ``now()`` call.

    ``record_observed_state``'s monotonic write guard (issue #216) drops a
    same-instant duplicate; a test that calls it twice in a row (running, then
    stopped) needs each write stamped strictly later than the last, exactly as
    production wall-clock time does (mirrors ``test_lifecycle_scenarios.py``).
    """

    def __init__(self, start: dt.datetime) -> None:
        self._next = start

    def now(self) -> dt.datetime:
        current = self._next
        self._next = current + dt.timedelta(seconds=1)
        return current


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _seed_community(engine: AsyncEngine) -> uuid.UUID:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName(f"guild-{uuid.uuid4()}"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    factory = create_session_factory(engine)
    async with CommunityUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.commit()
    return community.id.value


async def _create_running_server(
    engine: AsyncEngine, *, bedrock_port: int | None, worker_id: uuid.UUID
) -> ServerId:
    """Create a server, assign it to ``worker_id``, and mark it desired=running.

    Mirrors the two-step write shape ``test_server_repositories.py`` /
    ``test_lifecycle_repositories.py`` use: ``update`` persists the (possibly
    Bedrock-enabled) config; ``update_lifecycle`` is the only write that
    persists ``assigned_worker_id`` (``record_observed_state``'s ownership
    guard requires it to match the reporting worker).
    """

    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    server = await CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="paper",
        config={},
    )
    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server.id)
        assert loaded is not None
        loaded.bedrock_port = bedrock_port
        await uow.servers.update(loaded)
        loaded.desired_state = DesiredState.RUNNING
        loaded.assigned_worker_id = WorkerId(worker_id)
        loaded.updated_at = _NOW
        applied = await uow.servers.update_lifecycle(
            loaded, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        assert applied is True
        await uow.commit()
    return server.id


def _geyser_plugin(server_id: ServerId, *, enabled: bool) -> ServerPlugin:
    clean_path = "plugins/Geyser-Spigot.jar"
    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path=clean_path if enabled else f"{clean_path}.disabled",
        filename="Geyser-Spigot.jar",
        display_name="Geyser-Spigot",
        description=None,
        loader_type=LoaderType.PLUGIN,
        source=PluginSource.MODRINTH,
        source_project_id="wKkoqHrH",
        source_version_id="v1",
        version_number="2.0.0",
        checksum_sha512="abc",
        sha256=None,
        size_bytes=100,
        enabled=enabled,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        mod_identifier="geyser-spigot",
    )


async def _add_plugin(engine: AsyncEngine, plugin: ServerPlugin) -> None:
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(plugin)
        await uow.commit()


class _Harness:
    """A real GrpcControlPlane over a fake worker outbound queue + a sink."""

    def __init__(self, engine: AsyncEngine, *, worker_id: uuid.UUID) -> None:
        self.worker_id = worker_id
        self.control_plane_state = ControlPlaneState()
        self.queue = self.control_plane_state.open_session(
            FleetWorkerId(str(worker_id)), session=0
        )
        self.control_plane = GrpcControlPlane(
            self.control_plane_state, timeout_seconds=5.0
        )
        self.registration = RelayRegistration()
        self.registration.set(endpoint=_RELAY_ENDPOINT, ca_pem=_RELAY_CA_PEM)
        self.bedrock_tunnel_table = BedrockTunnelTable()
        self.sink = ServersServerStateSink(
            create_session_factory(engine),
            clock=_AdvancingClock(_NOW),
            control_plane=self.control_plane,
            relay_registration=self.registration,
            bedrock_tunnel_table=self.bedrock_tunnel_table,
            bedrock_tunnel_port=_BEDROCK_TUNNEL_PORT,
        )

    async def record(self, *, server_id: ServerId, state: str) -> None:
        await self.sink.record_observed_state(
            server_id=str(server_id.value),
            worker_id=str(self.worker_id),
            state=state,
        )

    def queue_empty(self) -> bool:
        return self.queue.empty()


async def test_running_bedrock_server_dispatches_open_with_valid_token_and_port(
    engine: AsyncEngine,
) -> None:
    worker_id = uuid.uuid4()
    server_id = await _create_running_server(
        engine, bedrock_port=_BEDROCK_PORT, worker_id=worker_id
    )
    await _add_plugin(engine, _geyser_plugin(server_id, enabled=True))
    harness = _Harness(engine, worker_id=worker_id)

    await harness.record(server_id=server_id, state="running")

    message = await harness.queue.get()
    assert message.api_command.server_id == str(server_id.value)
    open_cmd = message.api_command.open_bedrock_tunnel
    assert open_cmd.server_id == str(server_id.value)
    assert open_cmd.bedrock_port == _BEDROCK_PORT
    assert open_cmd.relay_endpoint == f"relay.example.com:{_BEDROCK_TUNNEL_PORT}"
    assert open_cmd.tls_ca_pem == _RELAY_CA_PEM
    assert len(open_cmd.token) == 32
    assert harness.bedrock_tunnel_table.validate(
        server_id=str(server_id.value),
        bedrock_port=_BEDROCK_PORT,
        token=open_cmd.token,
    )


async def test_stopped_bedrock_server_dispatches_close_and_invalidates_token(
    engine: AsyncEngine,
) -> None:
    worker_id = uuid.uuid4()
    server_id = await _create_running_server(
        engine, bedrock_port=_BEDROCK_PORT, worker_id=worker_id
    )
    await _add_plugin(engine, _geyser_plugin(server_id, enabled=True))
    harness = _Harness(engine, worker_id=worker_id)

    await harness.record(server_id=server_id, state="running")
    open_message = await harness.queue.get()
    token = open_message.api_command.open_bedrock_tunnel.token

    await harness.record(server_id=server_id, state="stopped")

    close_message = await harness.queue.get()
    assert close_message.api_command.server_id == str(server_id.value)
    assert close_message.api_command.WhichOneof("command") == "close_bedrock_tunnel"
    assert close_message.api_command.close_bedrock_tunnel.server_id == str(
        server_id.value
    )
    # The token minted at open no longer validates once the tunnel is closed.
    assert not harness.bedrock_tunnel_table.validate(
        server_id=str(server_id.value), bedrock_port=_BEDROCK_PORT, token=token
    )


async def test_non_bedrock_server_dispatches_neither(engine: AsyncEngine) -> None:
    worker_id = uuid.uuid4()
    server_id = await _create_running_server(
        engine, bedrock_port=None, worker_id=worker_id
    )
    harness = _Harness(engine, worker_id=worker_id)

    await harness.record(server_id=server_id, state="running")
    await harness.record(server_id=server_id, state="stopped")

    assert harness.queue_empty()


async def test_disabled_geyser_dispatches_neither(engine: AsyncEngine) -> None:
    # PM note on issue #1544: a present-but-disabled Geyser keeps bedrock_port
    # (only uninstall releases it, issue #1541) but is not listening on RakNet,
    # so the tunnel dispatch is skipped -- open and close alike.
    worker_id = uuid.uuid4()
    server_id = await _create_running_server(
        engine, bedrock_port=_BEDROCK_PORT, worker_id=worker_id
    )
    await _add_plugin(engine, _geyser_plugin(server_id, enabled=False))
    harness = _Harness(engine, worker_id=worker_id)

    await harness.record(server_id=server_id, state="running")
    await harness.record(server_id=server_id, state="stopped")

    assert harness.queue_empty()


async def test_open_skipped_when_no_relay_registered(engine: AsyncEngine) -> None:
    # Mirrors relay_server.py's analogous ResolveJoin guard: an OpenBedrockTunnel
    # with no registered relay endpoint would carry nothing useful, so it is
    # skipped (logged) rather than dispatched with a garbage endpoint.
    worker_id = uuid.uuid4()
    server_id = await _create_running_server(
        engine, bedrock_port=_BEDROCK_PORT, worker_id=worker_id
    )
    await _add_plugin(engine, _geyser_plugin(server_id, enabled=True))
    harness = _Harness(engine, worker_id=worker_id)
    harness.registration = RelayRegistration()  # never .set(): no registration
    harness.sink = ServersServerStateSink(
        create_session_factory(engine),
        clock=_AdvancingClock(_NOW),
        control_plane=harness.control_plane,
        relay_registration=harness.registration,
        bedrock_tunnel_table=harness.bedrock_tunnel_table,
        bedrock_tunnel_port=_BEDROCK_TUNNEL_PORT,
    )

    await harness.record(server_id=server_id, state="running")

    assert harness.queue_empty()
