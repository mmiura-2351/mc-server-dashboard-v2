"""The app factory must fail fast on an incomplete ``[relay]`` config (issue #956).

``relay.credential`` and ``relay.base_domain`` are required secrets/values when
``relay.enabled`` is true (RELAY.md Section 13); ``create_app`` raises at boot
rather than serving a RelayService that would admit any relay (NFR-SEC-1) or
build a ``join_hostname`` with no base domain. Mirrors the Worker-credential
required-when-enabled guard.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.app import create_app


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("credential", None, id="missing-credential"),
        pytest.param("credential", "", id="empty-credential"),
        pytest.param("credential", "   ", id="whitespace-credential"),
        pytest.param("base_domain", None, id="missing-base-domain"),
        pytest.param("base_domain", "", id="empty-base-domain"),
        pytest.param("base_domain", "   ", id="whitespace-base-domain"),
    ],
)
def test_create_app_rejects_missing_or_blank_relay_requirements(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str | None
) -> None:
    monkeypatch.setenv("MCD_API_RELAY__ENABLED", "true")
    monkeypatch.setenv("MCD_API_RELAY__CREDENTIAL", "relay-secret")
    monkeypatch.setenv("MCD_API_RELAY__BASE_DOMAIN", "mc.example.com")
    env_key = "MCD_API_RELAY__" + field.upper()
    if value is None:
        monkeypatch.delenv(env_key, raising=False)
    else:
        monkeypatch.setenv(env_key, value)
    with pytest.raises(ValueError, match=f"relay.{field} is required"):
        create_app()


def test_create_app_fails_when_relay_enabled_but_control_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The RelayService shares the control-plane gRPC listener; leaving control
    # disabled would silently make the advertised relay unavailable.
    monkeypatch.setenv("MCD_API_RELAY__ENABLED", "true")
    monkeypatch.setenv("MCD_API_RELAY__CREDENTIAL", "relay-secret")
    monkeypatch.setenv("MCD_API_RELAY__BASE_DOMAIN", "mc.example.com")
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "false")
    with pytest.raises(ValueError, match="relay.enabled requires control.enabled"):
        create_app()


def test_create_app_succeeds_when_relay_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The default-off deployment needs neither a relay credential nor a domain.
    monkeypatch.delenv("MCD_API_RELAY__ENABLED", raising=False)
    monkeypatch.delenv("MCD_API_RELAY__CREDENTIAL", raising=False)
    monkeypatch.delenv("MCD_API_RELAY__BASE_DOMAIN", raising=False)
    create_app()


@pytest.mark.parametrize(
    ("relay_enabled", "warns"),
    [
        pytest.param(False, True, id="bedrock-without-relay-warns"),
        pytest.param(True, False, id="bedrock-with-relay-does-not-warn"),
    ],
)
def test_bedrock_warning_matches_relay_gate(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    relay_enabled: bool,
    warns: bool,
) -> None:
    # Bedrock has no ingress path unless the relay is also enabled (#1552).
    monkeypatch.setenv("MCD_API_RELAY__ENABLED", str(relay_enabled).lower())
    monkeypatch.setenv("MCD_API_RELAY__BEDROCK_ENABLED", "true")
    if relay_enabled:
        monkeypatch.setenv("MCD_API_RELAY__CREDENTIAL", "relay-secret")
        monkeypatch.setenv("MCD_API_RELAY__BASE_DOMAIN", "mc.example.com")
        monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
        monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "shared-secret")
        monkeypatch.setenv("MCD_API_CONTROL__TLS__INSECURE", "true")
    with caplog.at_level("WARNING"):
        create_app()
    assert (
        any("relay.bedrock_enabled" in record.message for record in caplog.records)
        is warns
    )
