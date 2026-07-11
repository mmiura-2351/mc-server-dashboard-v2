"""Use-case tests for the Bedrock gate-flip sweep (issue #1588).

When ``relay.bedrock_enabled`` transitions from off to on, servers that already
have an installed, enabled Geyser plugin but no ``bedrock_port`` must receive one
via a one-shot startup sweep.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.servers.application.bedrock_sweep import (
    SweepBedrockPorts,
)
from mc_server_dashboard_api.servers.domain.entities import Server
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
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import FakeClock, FakeUnitOfWork

_NOW = dt.datetime(2026, 7, 10, 12, 0, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())
_BEDROCK_RANGE = PortRange(start=19132, end=19141)


def _server(
    *,
    community_id: CommunityId = _COMMUNITY,
    bedrock_port: int | None = None,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=community_id,
        name=ServerName("test-server"),
        mc_edition="java",
        mc_version="1.20.4",
        server_type=ServerType.PAPER,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
        bedrock_port=bedrock_port,
    )


def _geyser_plugin(*, server_id: ServerId, enabled: bool = True) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path="plugins/Geyser-Spigot.jar",
        filename="Geyser-Spigot.jar",
        display_name="Geyser-Spigot",
        description=None,
        loader_type=LoaderType.PLUGIN,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
        version_number=None,
        checksum_sha512="abc",
        sha256=None,
        size_bytes=100,
        enabled=enabled,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        mod_identifier="Geyser-Spigot",
    )


def _non_geyser_plugin(*, server_id: ServerId) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path="plugins/WorldGuard.jar",
        filename="WorldGuard.jar",
        display_name="WorldGuard",
        description=None,
        loader_type=LoaderType.PLUGIN,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
        version_number=None,
        checksum_sha512="def",
        sha256=None,
        size_bytes=200,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


async def test_sweep_allocates_port_for_server_with_enabled_geyser() -> None:
    """The core scenario: Geyser installed while gate was off, gate flips on."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uow.plugins.seed(_geyser_plugin(server_id=server.id))

    sweep = SweepBedrockPorts(uow=uow, port_range=_BEDROCK_RANGE, clock=FakeClock(_NOW))
    count = await sweep()

    assert count == 1
    assert uow.servers.by_id[server.id].bedrock_port == 19132


async def test_sweep_skips_server_already_having_port() -> None:
    """A server that already has a bedrock_port is not re-allocated."""
    uow = FakeUnitOfWork()
    server = _server(bedrock_port=19135)
    uow.servers.seed(server)
    uow.plugins.seed(_geyser_plugin(server_id=server.id))

    sweep = SweepBedrockPorts(uow=uow, port_range=_BEDROCK_RANGE, clock=FakeClock(_NOW))
    count = await sweep()

    assert count == 0
    assert uow.servers.by_id[server.id].bedrock_port == 19135


async def test_sweep_skips_server_without_geyser() -> None:
    """A server with no Geyser plugin gets no port."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uow.plugins.seed(_non_geyser_plugin(server_id=server.id))

    sweep = SweepBedrockPorts(uow=uow, port_range=_BEDROCK_RANGE, clock=FakeClock(_NOW))
    count = await sweep()

    assert count == 0
    assert uow.servers.by_id[server.id].bedrock_port is None


async def test_sweep_skips_server_with_disabled_geyser() -> None:
    """A server with a disabled Geyser does not qualify."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uow.plugins.seed(_geyser_plugin(server_id=server.id, enabled=False))

    sweep = SweepBedrockPorts(uow=uow, port_range=_BEDROCK_RANGE, clock=FakeClock(_NOW))
    count = await sweep()

    assert count == 0
    assert uow.servers.by_id[server.id].bedrock_port is None


async def test_sweep_allocates_multiple_servers() -> None:
    """Two qualifying servers each get a distinct port."""
    uow = FakeUnitOfWork()
    s1 = _server()
    s2 = _server()
    uow.servers.seed(s1)
    uow.servers.seed(s2)
    uow.plugins.seed(_geyser_plugin(server_id=s1.id))
    uow.plugins.seed(_geyser_plugin(server_id=s2.id))

    sweep = SweepBedrockPorts(uow=uow, port_range=_BEDROCK_RANGE, clock=FakeClock(_NOW))
    count = await sweep()

    assert count == 2
    ports = {
        uow.servers.by_id[s1.id].bedrock_port,
        uow.servers.by_id[s2.id].bedrock_port,
    }
    assert ports == {19132, 19133}


async def test_sweep_respects_already_taken_ports() -> None:
    """Ports already taken by other servers are skipped."""
    uow = FakeUnitOfWork()
    # Existing server already has port 19132
    existing = _server(bedrock_port=19132)
    uow.servers.seed(existing)
    # New server needs a port
    server = _server()
    uow.servers.seed(server)
    uow.plugins.seed(_geyser_plugin(server_id=server.id))

    sweep = SweepBedrockPorts(uow=uow, port_range=_BEDROCK_RANGE, clock=FakeClock(_NOW))
    count = await sweep()

    assert count == 1
    assert uow.servers.by_id[server.id].bedrock_port == 19133


async def test_sweep_no_servers_returns_zero() -> None:
    """Empty fleet: nothing to sweep."""
    uow = FakeUnitOfWork()
    sweep = SweepBedrockPorts(uow=uow, port_range=_BEDROCK_RANGE, clock=FakeClock(_NOW))
    count = await sweep()
    assert count == 0


async def test_sweep_sets_updated_at() -> None:
    """The sweep stamps updated_at on allocated servers."""
    sweep_time = dt.datetime(2026, 7, 11, 8, 0, 0, tzinfo=dt.timezone.utc)
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uow.plugins.seed(_geyser_plugin(server_id=server.id))

    sweep = SweepBedrockPorts(
        uow=uow, port_range=_BEDROCK_RANGE, clock=FakeClock(sweep_time)
    )
    await sweep()

    assert uow.servers.by_id[server.id].updated_at == sweep_time


async def test_sweep_handles_exhausted_range_gracefully() -> None:
    """When the port range is full, partial progress is committed."""
    uow = FakeUnitOfWork()
    # Fill the single-port range with an existing server
    existing = _server(bedrock_port=19132)
    uow.servers.seed(existing)
    # Two servers need ports but only one slot... wait, zero slots remain.
    s1 = _server()
    uow.servers.seed(s1)
    uow.plugins.seed(_geyser_plugin(server_id=s1.id))

    tiny_range = PortRange(start=19132, end=19132)
    sweep = SweepBedrockPorts(uow=uow, port_range=tiny_range, clock=FakeClock(_NOW))
    # Should not raise; logs a warning and returns 0 (no allocations possible).
    count = await sweep()

    assert count == 0
    assert uow.servers.by_id[s1.id].bedrock_port is None


async def test_sweep_commits_partial_progress_on_exhaustion() -> None:
    """When the range fills mid-sweep, already-allocated ports are committed."""
    uow = FakeUnitOfWork()
    s1 = _server()
    s2 = _server()
    uow.servers.seed(s1)
    uow.servers.seed(s2)
    uow.plugins.seed(_geyser_plugin(server_id=s1.id))
    uow.plugins.seed(_geyser_plugin(server_id=s2.id))

    # Only one port available: one server gets it, the other is skipped.
    tiny_range = PortRange(start=19132, end=19132)
    sweep = SweepBedrockPorts(uow=uow, port_range=tiny_range, clock=FakeClock(_NOW))
    count = await sweep()

    assert count == 1
    allocated = [
        s for s in [s1, s2] if uow.servers.by_id[s.id].bedrock_port is not None
    ]
    assert len(allocated) == 1
    assert uow.commits == 1
