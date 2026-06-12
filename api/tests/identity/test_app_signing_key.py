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


@pytest.mark.parametrize("algorithm", ["HS256", "RS256"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_create_app_fails_on_blank_signing_key(
    monkeypatch: pytest.MonkeyPatch, algorithm: str, blank: str
) -> None:
    # compose interpolates an unset ``${MCD_API_AUTH__TOKEN__SIGNING_KEY}`` to an
    # EMPTY string, not None; an `is None`-only guard would boot the API with an
    # empty signing key. The RS256 case is the live hole: its key length is not
    # floored (unlike HS256), so a blank RS256 key otherwise slips past every
    # guard (#939). A blank value must fail fast with the same "required" error
    # as a missing one.
    monkeypatch.setenv("MCD_API_AUTH__TOKEN__ALGORITHM", algorithm)
    monkeypatch.setenv("MCD_API_AUTH__TOKEN__SIGNING_KEY", blank)
    with pytest.raises(ValueError, match="signing_key is required"):
        create_app()
