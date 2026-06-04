"""The app factory must fail fast when the token signing key is missing.

The signing key is a required secret to mount the auth endpoints (CONFIGURATION.md
Section 5.3); ``create_app`` raises at boot rather than starting unable to issue
or verify tokens.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.app import create_app


def test_create_app_fails_without_signing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCD_API_AUTH__TOKEN__SIGNING_KEY", raising=False)
    with pytest.raises(ValueError, match="signing_key"):
        create_app()
