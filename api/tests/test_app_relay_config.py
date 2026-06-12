"""The app factory must fail fast on an incomplete ``[relay]`` config (issue #956).

``relay.credential`` and ``relay.base_domain`` are required secrets/values when
``relay.enabled`` is true (RELAY.md Section 12); ``create_app`` raises at boot
rather than serving a RelayService that would admit any relay (NFR-SEC-1) or
build a ``join_hostname`` with no base domain. Mirrors the Worker-credential
required-when-enabled guard.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.app import create_app


def test_create_app_fails_when_relay_enabled_without_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCD_API_RELAY__ENABLED", "true")
    monkeypatch.delenv("MCD_API_RELAY__CREDENTIAL", raising=False)
    monkeypatch.setenv("MCD_API_RELAY__BASE_DOMAIN", "mc.example.com")
    with pytest.raises(ValueError, match="relay.credential is required"):
        create_app()


@pytest.mark.parametrize("blank", ["", "   "])
def test_create_app_fails_on_blank_relay_credential(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    monkeypatch.setenv("MCD_API_RELAY__ENABLED", "true")
    monkeypatch.setenv("MCD_API_RELAY__CREDENTIAL", blank)
    monkeypatch.setenv("MCD_API_RELAY__BASE_DOMAIN", "mc.example.com")
    with pytest.raises(ValueError, match="relay.credential is required"):
        create_app()


def test_create_app_fails_when_relay_enabled_without_base_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCD_API_RELAY__ENABLED", "true")
    monkeypatch.setenv("MCD_API_RELAY__CREDENTIAL", "relay-secret")
    monkeypatch.delenv("MCD_API_RELAY__BASE_DOMAIN", raising=False)
    with pytest.raises(ValueError, match="relay.base_domain is required"):
        create_app()


def test_create_app_succeeds_when_relay_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default off: a deployment that leaves the relay disabled need not supply
    # the credential or base domain.
    monkeypatch.delenv("MCD_API_RELAY__ENABLED", raising=False)
    monkeypatch.delenv("MCD_API_RELAY__CREDENTIAL", raising=False)
    monkeypatch.delenv("MCD_API_RELAY__BASE_DOMAIN", raising=False)
    create_app()
