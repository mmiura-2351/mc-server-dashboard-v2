"""Unit tests for the pure per-server memory-limit validation (#705).

Standard-library-only domain validation: read/validate the per-server memory
limit (MiB) from the config blob, with a sane floor and an absurd-value ceiling.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidMemoryLimitError,
)
from mc_server_dashboard_api.servers.domain.memory_limit import (
    MEMORY_LIMIT_CEILING_MB,
    MEMORY_LIMIT_CONFIG_KEY,
    MEMORY_LIMIT_FLOOR_MB,
    memory_limit_from_config,
)


def test_absent_limit_returns_none() -> None:
    # Default-unset: no key written, behavior unchanged (the worker picks a
    # proportionate default via the MemoryLimitMB == 0 path, #706).
    assert memory_limit_from_config({}) is None


def test_valid_limit_returns_mib() -> None:
    assert memory_limit_from_config({MEMORY_LIMIT_CONFIG_KEY: 2048}) == 2048


def test_floor_is_accepted() -> None:
    assert (
        memory_limit_from_config({MEMORY_LIMIT_CONFIG_KEY: MEMORY_LIMIT_FLOOR_MB})
        == MEMORY_LIMIT_FLOOR_MB
    )


def test_ceiling_is_accepted() -> None:
    assert (
        memory_limit_from_config({MEMORY_LIMIT_CONFIG_KEY: MEMORY_LIMIT_CEILING_MB})
        == MEMORY_LIMIT_CEILING_MB
    )


@pytest.mark.parametrize(
    "bad",
    [
        0,  # zero
        -1,  # negative
        MEMORY_LIMIT_FLOOR_MB - 1,  # below the floor
        MEMORY_LIMIT_CEILING_MB + 1,  # absurd (above the ceiling)
        "2048",  # non-integer
        2048.0,  # float
        True,  # bool is not an accepted integer
    ],
)
def test_invalid_limit_rejected(bad: object) -> None:
    with pytest.raises(InvalidMemoryLimitError):
        memory_limit_from_config({MEMORY_LIMIT_CONFIG_KEY: bad})


def test_round_trip_through_config_blob() -> None:
    # The value survives a write/read round trip through the config blob unchanged
    # (no transformation, unlike the hours->seconds backup schedule).
    config = {"motd": "hi", MEMORY_LIMIT_CONFIG_KEY: 4096}
    assert memory_limit_from_config(config) == 4096
    assert config[MEMORY_LIMIT_CONFIG_KEY] == 4096
