"""Pure port-allocation policy (issue #243).

The deployment-wide game-port allocator: a configured inclusive range, the set of
already-taken ports, and three operations the create flow and the availability
endpoints share -- pick the lowest free port, validate an explicit port, and list
the next free ports. Pure standard-library, no I/O.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.servers.domain.errors import (
    PortAlreadyTakenError,
    PortOutOfRangeError,
    PortRangeExhaustedError,
)
from mc_server_dashboard_api.servers.domain.ports import (
    PortRange,
    next_free_ports,
    pick_lowest_free_port,
    validate_explicit_port,
)


def _range(start: int = 25565, end: int = 25567) -> PortRange:
    return PortRange(start=start, end=end)


# --- pick_lowest_free_port -------------------------------------------------


def test_pick_returns_range_start_when_nothing_taken() -> None:
    assert pick_lowest_free_port(_range(), taken=set()) == 25565


def test_pick_skips_taken_and_returns_lowest_free() -> None:
    assert pick_lowest_free_port(_range(), taken={25565, 25566}) == 25567


def test_pick_ignores_taken_ports_outside_range() -> None:
    # A port taken outside the configured range must not shift the assignment.
    assert pick_lowest_free_port(_range(), taken={25565, 30000}) == 25566


def test_pick_raises_when_range_exhausted() -> None:
    with pytest.raises(PortRangeExhaustedError):
        pick_lowest_free_port(_range(), taken={25565, 25566, 25567})


# --- validate_explicit_port ------------------------------------------------


def test_validate_accepts_free_in_range_port() -> None:
    # Returns the port unchanged when it is in range and free.
    assert validate_explicit_port(25566, _range(), taken={25565}) == 25566


@pytest.mark.parametrize("port", [25564, 25568])
def test_validate_rejects_out_of_range_port(port: int) -> None:
    with pytest.raises(PortOutOfRangeError):
        validate_explicit_port(port, _range(), taken=set())


def test_validate_rejects_taken_port() -> None:
    with pytest.raises(PortAlreadyTakenError):
        validate_explicit_port(25565, _range(), taken={25565})


def test_validate_out_of_range_precedes_taken() -> None:
    # An out-of-range value is a 422 even if it happens to be in the taken set;
    # range is checked before availability.
    with pytest.raises(PortOutOfRangeError):
        validate_explicit_port(30000, _range(), taken={30000})


# --- next_free_ports -------------------------------------------------------


def test_next_free_ports_returns_lowest_n_in_order() -> None:
    assert next_free_ports(_range(), taken=set(), count=2) == [25565, 25566]


def test_next_free_ports_skips_taken() -> None:
    assert next_free_ports(_range(), taken={25565}, count=2) == [25566, 25567]


def test_next_free_ports_caps_at_available() -> None:
    # Asking for more than the range can offer returns only what is free, not an
    # error: the endpoint reports availability, it does not reserve.
    assert next_free_ports(_range(), taken={25566}, count=5) == [25565, 25567]


def test_next_free_ports_empty_when_exhausted() -> None:
    assert next_free_ports(_range(), taken={25565, 25566, 25567}, count=3) == []
