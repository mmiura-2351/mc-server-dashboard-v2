"""Unit tests: create_engine forwards pool_size / max_overflow to SQLAlchemy.

These tests use unittest.mock to intercept ``create_async_engine`` so no real
database is needed and the inner loop stays fast (TESTING.md Section 4/5).
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy.ext.asyncio import create_async_engine

from mc_server_dashboard_api.core.adapters.database import create_engine


def test_create_engine_forwards_pool_size() -> None:
    with patch(
        "mc_server_dashboard_api.core.adapters.database.create_async_engine",
        wraps=create_async_engine,
    ) as mock_create:
        create_engine("postgresql+asyncpg://u:p@h/db", pool_size=20)
        _, kwargs = mock_create.call_args
        assert kwargs["pool_size"] == 20


def test_create_engine_forwards_max_overflow() -> None:
    with patch(
        "mc_server_dashboard_api.core.adapters.database.create_async_engine",
        wraps=create_async_engine,
    ) as mock_create:
        create_engine("postgresql+asyncpg://u:p@h/db", max_overflow=0)
        _, kwargs = mock_create.call_args
        assert kwargs["max_overflow"] == 0


def test_create_engine_uses_defaults_when_not_supplied() -> None:
    # When neither keyword is passed the SQLAlchemy defaults (5 / 10) apply;
    # the important thing is that our wrapper does not override them with None.
    with patch(
        "mc_server_dashboard_api.core.adapters.database.create_async_engine",
        wraps=create_async_engine,
    ) as mock_create:
        create_engine("postgresql+asyncpg://u:p@h/db")
        _, kwargs = mock_create.call_args
        assert "pool_size" not in kwargs
        assert "max_overflow" not in kwargs
