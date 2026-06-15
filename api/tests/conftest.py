"""Shared test setup.

A dummy database URL satisfies the fail-fast config loader so the app factory
builds; SQLAlchemy engines are lazy, so no connection is opened. Tests that
exercise the DB seam override the :class:`DatabasePing` Port with a fake, so the
dummy URL is never dialed (NFR-TEST-1).
"""

import os
import uuid

import pytest

_SCRATCH_DB_URL: str | None = None
"""The per-run scratch database created for this session, if any (issue #379)."""


def pytest_configure(config: pytest.Config) -> None:
    """Redirect ``MCD_TEST_DATABASE_URL`` to a fresh per-run database (#379).

    The DB-gated integration tests read ``MCD_TEST_DATABASE_URL`` at import time
    and run ``downgrade base`` / ``upgrade head`` against it. Treating that URL
    as a *base/maintenance* connection and creating a unique
    ``<dbname>_<short-uuid>`` database per session makes parallel runs (e.g.
    agent worktrees sharing one Postgres) disjoint, so they can no longer race
    on the schema or leave orphan tables behind. Runs before collection, so the
    test modules import the per-run URL.
    """
    global _SCRATCH_DB_URL
    base_url = os.environ.get("MCD_TEST_DATABASE_URL_BASE") or os.environ.get(
        "MCD_TEST_DATABASE_URL"
    )
    if base_url is None:
        return
    from tests.integration.scratch_db import (
        create_scratch_database,
        derive_scratch_url,
    )

    scratch_url = derive_scratch_url(base_url, uuid.uuid4().hex[:12])
    create_scratch_database(base_url, scratch_url)
    os.environ["MCD_TEST_DATABASE_URL_BASE"] = base_url
    os.environ["MCD_TEST_DATABASE_URL"] = scratch_url
    _SCRATCH_DB_URL = scratch_url


def pytest_unconfigure(config: pytest.Config) -> None:
    """Drop the per-run scratch database created in ``pytest_configure`` (#379)."""
    if _SCRATCH_DB_URL is None:
        return
    base_url = os.environ["MCD_TEST_DATABASE_URL_BASE"]
    from tests.integration.scratch_db import drop_scratch_database

    drop_scratch_database(base_url, _SCRATCH_DB_URL)


@pytest.fixture(autouse=True)
def _dummy_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", "postgresql+asyncpg://test:test@localhost/test"
    )
    # The auth endpoints require a token signing key to mount (CONFIGURATION.md
    # Section 5.3); a dummy value satisfies the fail-fast check. Tests that need
    # to verify the missing-key behaviour clear it explicitly.
    monkeypatch.setenv(
        "MCD_API_AUTH__TOKEN__SIGNING_KEY", "test-signing-key-of-at-least-32by"
    )
    # Disable the control-plane gRPC server by default so building the app under
    # TestClient does not bind a port (CONFIGURATION.md Section 5.1). The fleet
    # gRPC integration tests construct the server directly; tests that need the
    # control-plane fail-fast behaviour set these explicitly.
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "false")
    # Opt the control channel into a plaintext listener by default so tests that
    # DO enable the control plane build without TLS material (CONFIGURATION.md
    # Section 5.1, required-unless-insecure). Tests that exercise the TLS posture
    # or the fail-fast set these explicitly.
    monkeypatch.setenv("MCD_API_CONTROL__TLS__INSECURE", "true")
