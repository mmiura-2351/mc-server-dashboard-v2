"""Unit tests for the pure per-server CPU-allocation validation (#722).

Standard-library-only domain validation: read/validate the per-server CPU
allocation (millicores) from the config blob, with a loose sanity floor and an
absurd-value ceiling. The allocation is a soft, rough relative share, not a hard
cap.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.servers.domain.cpu_allocation import (
    CPU_ALLOCATION_CEILING_MILLIS,
    CPU_ALLOCATION_CONFIG_KEY,
    CPU_ALLOCATION_FLOOR_MILLIS,
    cpu_allocation_from_config,
)
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidCpuAllocationError,
)


def test_absent_allocation_returns_none() -> None:
    # Default-unset: no key written, behavior unchanged (the server runs with the
    # driver's default share, #722).
    assert cpu_allocation_from_config({}) is None


def test_valid_allocation_returns_millis() -> None:
    assert cpu_allocation_from_config({CPU_ALLOCATION_CONFIG_KEY: 2000}) == 2000


def test_floor_is_accepted() -> None:
    assert (
        cpu_allocation_from_config(
            {CPU_ALLOCATION_CONFIG_KEY: CPU_ALLOCATION_FLOOR_MILLIS}
        )
        == CPU_ALLOCATION_FLOOR_MILLIS
    )


def test_ceiling_is_accepted() -> None:
    assert (
        cpu_allocation_from_config(
            {CPU_ALLOCATION_CONFIG_KEY: CPU_ALLOCATION_CEILING_MILLIS}
        )
        == CPU_ALLOCATION_CEILING_MILLIS
    )


@pytest.mark.parametrize(
    "bad",
    [
        0,  # zero
        -1,  # negative
        CPU_ALLOCATION_FLOOR_MILLIS - 1,  # below the floor
        CPU_ALLOCATION_CEILING_MILLIS + 1,  # absurd (above the ceiling)
        "2000",  # non-integer
        2000.0,  # float
        True,  # bool is not an accepted integer
    ],
)
def test_invalid_allocation_rejected(bad: object) -> None:
    with pytest.raises(InvalidCpuAllocationError):
        cpu_allocation_from_config({CPU_ALLOCATION_CONFIG_KEY: bad})


def test_round_trip_through_config_blob() -> None:
    # The value survives a write/read round trip through the config blob unchanged.
    config = {"motd": "hi", CPU_ALLOCATION_CONFIG_KEY: 4000}
    assert cpu_allocation_from_config(config) == 4000
    assert config[CPU_ALLOCATION_CONFIG_KEY] == 4000
