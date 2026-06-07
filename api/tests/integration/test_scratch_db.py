"""Unit tests for the per-run scratch-database helpers (issue #379).

These do not touch a real database; they pin the pure URL-derivation logic that
makes the integration fixture concurrency-safe. The create/drop round-trip
against a live Postgres is exercised implicitly by every DB-gated integration
test under this package.
"""

from __future__ import annotations

from tests.integration.scratch_db import derive_scratch_url


def test_derive_scratch_url_suffixes_the_database_name() -> None:
    base = "postgresql+asyncpg://mcsd:mcsd@localhost:5432/mcsd_test"
    scratch = derive_scratch_url(base, "abc123")
    assert scratch == "postgresql+asyncpg://mcsd:mcsd@localhost:5432/mcsd_test_abc123"


def test_derive_scratch_url_preserves_driver_and_credentials() -> None:
    base = "postgresql+asyncpg://user:p%40ss@db.example:6543/app"
    scratch = derive_scratch_url(base, "xy")
    assert scratch.startswith("postgresql+asyncpg://user:")
    assert scratch.endswith("/app_xy")


def test_derive_scratch_url_distinct_tokens_yield_distinct_names() -> None:
    base = "postgresql+asyncpg://mcsd:mcsd@localhost/mcsd_test"
    assert derive_scratch_url(base, "aaa") != derive_scratch_url(base, "bbb")
