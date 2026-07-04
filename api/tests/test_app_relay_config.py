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


def test_create_app_fails_when_relay_enabled_but_control_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The RelayService shares the control-plane gRPC listener, which only starts
    # when control.enabled. relay.enabled with control.enabled=false would leave
    # the relay silently unserved while still exposing join_hostname (PR #973
    # review) — fail fast instead.
    monkeypatch.setenv("MCD_API_RELAY__ENABLED", "true")
    monkeypatch.setenv("MCD_API_RELAY__CREDENTIAL", "relay-secret")
    monkeypatch.setenv("MCD_API_RELAY__BASE_DOMAIN", "mc.example.com")
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "false")
    with pytest.raises(ValueError, match="relay.enabled requires control.enabled"):
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


def test_create_app_warns_when_bedrock_enabled_without_relay_enabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # bedrock_enabled has no effect without relay.enabled (issue #1552): the
    # deployment gate is relay.enabled AND relay.bedrock_enabled, so with the
    # relay off no bedrock_port is ever allocated. Warn (not fatal) rather than
    # leaving the contradictory config undiagnosed.
    monkeypatch.setenv("MCD_API_RELAY__ENABLED", "false")
    monkeypatch.setenv("MCD_API_RELAY__BEDROCK_ENABLED", "true")
    with caplog.at_level("WARNING"):
        create_app()
    assert any("relay.bedrock_enabled" in r.message for r in caplog.records)


def test_create_app_does_not_warn_when_bedrock_enabled_with_relay_enabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MCD_API_RELAY__ENABLED", "true")
    monkeypatch.setenv("MCD_API_RELAY__CREDENTIAL", "relay-secret")
    monkeypatch.setenv("MCD_API_RELAY__BASE_DOMAIN", "mc.example.com")
    monkeypatch.setenv("MCD_API_RELAY__BEDROCK_ENABLED", "true")
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "shared-secret")
    monkeypatch.setenv("MCD_API_CONTROL__TLS__INSECURE", "true")
    with caplog.at_level("WARNING"):
        create_app()
    assert not any("relay.bedrock_enabled" in r.message for r in caplog.records)
