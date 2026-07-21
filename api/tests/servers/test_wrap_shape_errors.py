"""Tests for :func:`wrap_shape_errors` context manager (issue #2159).

Verifies that the shared context manager correctly wraps parse-level
exceptions into :class:`CatalogUnavailableError` and passes through
exceptions that should not be caught.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.servers.domain.errors import (
    CatalogUnavailableError,
    wrap_shape_errors,
)


@pytest.mark.parametrize(
    "exc_type",
    [AttributeError, KeyError, TypeError, IndexError],
    ids=["AttributeError", "KeyError", "TypeError", "IndexError"],
)
def test_wraps_shape_exception_into_catalog_unavailable(
    exc_type: type[Exception],
) -> None:
    with pytest.raises(CatalogUnavailableError) as exc_info:
        with wrap_shape_errors("test-source"):
            raise exc_type("bad field")
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, exc_type)


def test_message_contains_original_exception() -> None:
    with pytest.raises(CatalogUnavailableError, match="unexpected response shape"):
        with wrap_shape_errors("test-source"):
            raise KeyError("id")


def test_passes_through_catalog_unavailable_error() -> None:
    original = CatalogUnavailableError("original message")
    with pytest.raises(CatalogUnavailableError, match="original message"):
        with wrap_shape_errors("test-source"):
            raise original


def test_does_not_catch_unrelated_exceptions() -> None:
    with pytest.raises(ValueError, match="unrelated"):
        with wrap_shape_errors("test-source"):
            raise ValueError("unrelated")


def test_no_exception_passes_through() -> None:
    with wrap_shape_errors("test-source"):
        result = 1 + 1
    assert result == 2
