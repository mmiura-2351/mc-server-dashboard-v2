"""Unit tests for the pure config-bounds validator (issue #94)."""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.servers.domain.config_bounds import (
    MAX_CONFIG_BYTES,
    MAX_CONFIG_DEPTH,
    ConfigInvalidShapeError,
    ConfigTooLargeError,
    validate_config,
)


def test_flat_config_passes_unchanged() -> None:
    config = {"motd": "hi", "max-players": 20}
    assert validate_config(config) is config


def test_non_object_top_level_is_invalid_shape() -> None:
    for value in (["a"], "x", 1, None):
        with pytest.raises(ConfigInvalidShapeError):
            validate_config(value)


def test_at_depth_cap_passes() -> None:
    # ``{"leaf": 1}`` is depth 2; wrapping MAX_CONFIG_DEPTH - 2 times lands on the
    # cap exactly.
    node: dict[str, object] = {"leaf": 1}
    for _ in range(MAX_CONFIG_DEPTH - 2):
        node = {"nested": node}
    assert validate_config(node) is node


def test_beyond_depth_cap_is_invalid_shape() -> None:
    node: dict[str, object] = {"leaf": 1}
    for _ in range(MAX_CONFIG_DEPTH - 1):
        node = {"nested": node}
    with pytest.raises(ConfigInvalidShapeError):
        validate_config(node)


def test_at_size_bound_passes() -> None:
    # One key whose value pads the serialization to exactly the ceiling.
    overhead = len('{"k": ""}')
    value = "a" * (MAX_CONFIG_BYTES - overhead)
    config = {"k": value}
    assert validate_config(config) is config


def test_over_size_bound_is_too_large() -> None:
    value = "a" * (MAX_CONFIG_BYTES + 1)
    with pytest.raises(ConfigTooLargeError):
        validate_config({"k": value})
