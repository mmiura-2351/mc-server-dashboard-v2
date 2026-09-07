"""Unit tests for the pure config-bounds validator (issue #94)."""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.servers.domain.config_bounds import (
    MAX_CONFIG_BYTES,
    MAX_CONFIG_DEPTH,
    ConfigInvalidShapeError,
    ConfigLoneSurrogateError,
    ConfigNullValueError,
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


def test_top_level_null_value_is_rejected() -> None:
    # A null value is the shape that enabled the key-presence smuggle (issue #140,
    # PR #148); the bounds validator rejects it outright.
    with pytest.raises(ConfigNullValueError):
        validate_config({"motd": None})


def test_nested_null_value_is_rejected() -> None:
    with pytest.raises(ConfigNullValueError):
        validate_config({"outer": {"inner": None}})


def test_null_inside_list_is_rejected() -> None:
    with pytest.raises(ConfigNullValueError):
        validate_config({"items": [1, None]})


def test_lone_surrogate_value_is_rejected() -> None:
    # ``json.loads`` merges a well-formed escape pair into one astral character,
    # so a surrogate that survives decoding is always an unpaired one (issue
    # #2838) — and it cannot be encoded into the UTF-8 the column stores.
    with pytest.raises(ConfigLoneSurrogateError):
        validate_config({"motd": "hi \ud800"})


def test_lone_surrogate_in_key_is_rejected() -> None:
    with pytest.raises(ConfigLoneSurrogateError):
        validate_config({"mot\udfffd": "hi"})


def test_lone_surrogate_inside_list_is_rejected() -> None:
    with pytest.raises(ConfigLoneSurrogateError):
        validate_config({"items": ["ok", "\udcff"]})


def test_astral_character_passes() -> None:
    # The rule rejects surrogate code points, not non-BMP text: an emoji encodes
    # to UTF-8 fine and stays acceptable.
    config = {"motd": "\U0001f600"}
    assert validate_config(config) is config


def test_lone_surrogate_wins_over_size_bound() -> None:
    # Both bounds apply to this blob; the surrogate refusal is the answer, because
    # the serialized size of text that cannot be encoded is not defined.
    with pytest.raises(ConfigLoneSurrogateError):
        validate_config({"k": "a" * (MAX_CONFIG_BYTES + 1) + "\ud800"})
