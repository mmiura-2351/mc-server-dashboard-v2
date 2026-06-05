"""Pure game-port allocation policy (issue #243).

The deployment-wide game port a server listens on is tracked on the ``server``
row (``game_port``, DATABASE.md Section 7) and assigned at create from a
configured inclusive range (``ports.range_start..range_end``,
CONFIGURATION.md Section 5.6). This module is the standard-library-only policy the
create flow and the availability endpoints share: pick the lowest free port,
validate an explicit request, and list the next free ports. It owns no I/O -- the
caller passes the already-taken set (read from the repository) and the range.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.errors import (
    PortAlreadyTakenError,
    PortOutOfRangeError,
    PortRangeExhaustedError,
)


@dataclass(frozen=True)
class PortRange:
    """The inclusive assignable port range (``start <= end``, validated at config)."""

    start: int
    end: int

    def __contains__(self, port: int) -> bool:
        return self.start <= port <= self.end


def pick_lowest_free_port(port_range: PortRange, *, taken: set[int]) -> int:
    """Return the lowest in-range port not in ``taken`` (the auto-assign rule).

    Raises :class:`PortRangeExhaustedError` when every in-range port is taken.
    """

    for port in range(port_range.start, port_range.end + 1):
        if port not in taken:
            return port
    raise PortRangeExhaustedError(f"{port_range.start}-{port_range.end}")


def validate_explicit_port(port: int, port_range: PortRange, *, taken: set[int]) -> int:
    """Validate an operator-supplied ``game_port`` and return it unchanged.

    Range is checked before availability, so an out-of-range value is a
    :class:`PortOutOfRangeError` (422) even when it is also taken; a free-but-
    out-of-range value cannot slip through. A taken in-range port raises
    :class:`PortAlreadyTakenError` (409).
    """

    if port not in port_range:
        raise PortOutOfRangeError(str(port))
    if port in taken:
        raise PortAlreadyTakenError(str(port))
    return port


def next_free_ports(port_range: PortRange, *, taken: set[int], count: int) -> list[int]:
    """Return up to ``count`` lowest free in-range ports, ascending.

    Reports availability without reserving, so a range with fewer free ports than
    requested simply returns the shorter list (and an empty list when exhausted)
    rather than raising.
    """

    free: list[int] = []
    for port in range(port_range.start, port_range.end + 1):
        if len(free) >= count:
            break
        if port not in taken:
            free.append(port)
    return free
