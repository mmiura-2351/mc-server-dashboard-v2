"""Use-case tests for the port-availability reads (issue #243).

CheckPort reports whether a given port is in range and free; ListAvailablePorts
returns the next free in-range ports. Both read the deployment-wide taken set
from the repository and apply the pure allocation policy. Against in-memory fakes
(TESTING.md Section 4).
"""

from __future__ import annotations

import uuid

from mc_server_dashboard_api.servers.application.port_availability import (
    CheckPort,
    ListAvailablePorts,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId
from tests.servers.fakes import FakeUnitOfWork
from tests.servers.test_manage_server import _server

_PORTS = PortRange(start=25565, end=25567)


def _uow_with_ports(*ports: int) -> FakeUnitOfWork:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    for i, port in enumerate(ports):
        server = _server(community_id=community, name=f"s{i}")
        server.game_port = port
        uow.servers.seed(server)
    return uow


async def test_check_free_in_range_port() -> None:
    uow = _uow_with_ports(25565)
    result = await CheckPort(uow=uow, port_range=_PORTS)(port=25566)
    assert result == {"port": 25566, "in_range": True, "available": True}


async def test_check_taken_in_range_port() -> None:
    uow = _uow_with_ports(25565)
    result = await CheckPort(uow=uow, port_range=_PORTS)(port=25565)
    assert result == {"port": 25565, "in_range": True, "available": False}


async def test_check_out_of_range_port_is_unavailable() -> None:
    # Out of range: in_range False and available False (it cannot be assigned).
    uow = _uow_with_ports()
    result = await CheckPort(uow=uow, port_range=_PORTS)(port=30000)
    assert result == {"port": 30000, "in_range": False, "available": False}


async def test_list_available_returns_next_free() -> None:
    uow = _uow_with_ports(25565)
    result = await ListAvailablePorts(uow=uow, port_range=_PORTS)(count=2)
    assert result == [25566, 25567]


async def test_list_available_caps_at_what_is_free() -> None:
    uow = _uow_with_ports(25566)
    result = await ListAvailablePorts(uow=uow, port_range=_PORTS)(count=5)
    assert result == [25565, 25567]
