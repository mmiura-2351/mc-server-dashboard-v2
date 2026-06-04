"""Shared test setup.

A dummy database URL satisfies the fail-fast config loader so the app factory
builds; SQLAlchemy engines are lazy, so no connection is opened. Tests that
exercise the DB seam override the :class:`DatabasePing` Port with a fake, so the
dummy URL is never dialed (NFR-TEST-1).
"""

import pytest


@pytest.fixture(autouse=True)
def _dummy_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", "postgresql+asyncpg://test:test@localhost/test"
    )
    # The auth endpoints require a token signing key to mount (CONFIGURATION.md
    # Section 5.3); a dummy value satisfies the fail-fast check. Tests that need
    # to verify the missing-key behaviour clear it explicitly.
    monkeypatch.setenv("MCD_API_AUTH__TOKEN__SIGNING_KEY", "test-signing-key")
