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


# --- reserved ports (relay host binds, issue #1002) ------------------------


def test_pick_skips_reserved_relay_port_even_when_lowest_free() -> None:
    # With relay enabled, the relay's game port (25565) is reserved: the
    # allocator must hand out the next free port, never the relay's bind.
    port_range = PortRange(start=25565, end=25567, reserved=frozenset({25565}))
    assert pick_lowest_free_port(port_range, taken=set()) == 25566


def test_pick_raises_when_only_free_port_is_reserved() -> None:
    # Reserved ports count against the range the same as taken ones; a range
    # whose only otherwise-free port is reserved is exhausted.
    port_range = PortRange(start=25565, end=25566, reserved=frozenset({25566}))
    with pytest.raises(PortRangeExhaustedError):
        pick_lowest_free_port(port_range, taken={25565})


def test_validate_rejects_reserved_relay_port() -> None:
    # An explicit request for the relay's reserved port is a 409, not accepted.
    port_range = PortRange(start=25565, end=25567, reserved=frozenset({25565}))
    with pytest.raises(PortAlreadyTakenError):
        validate_explicit_port(25565, port_range, taken=set())


def test_next_free_ports_excludes_reserved_relay_port() -> None:
    port_range = PortRange(start=25565, end=25567, reserved=frozenset({25565}))
    assert next_free_ports(port_range, taken=set(), count=3) == [25566, 25567]


def test_reserved_port_outside_range_is_inert() -> None:
    # The tunnel port (25665) sits above the default range; reserving it must
    # not affect allocation when it never falls inside [start, end].
    port_range = PortRange(start=25565, end=25567, reserved=frozenset({25665}))
    assert pick_lowest_free_port(port_range, taken=set()) == 25565
