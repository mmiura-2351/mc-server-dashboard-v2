"""Control-plane config: fail-fast on a missing credential, secret masking.

The Worker credential is a required secret whenever the control plane is enabled
(CONFIGURATION.md Section 5.1); ``create_app`` raises at boot rather than
starting a server that would admit any Worker (NFR-SEC-1). When present, the
credential is masked in the logged config dump (NFR-OBS-1).
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.config import load_settings


def test_create_app_fails_when_control_enabled_without_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
    monkeypatch.delenv("MCD_API_CONTROL__WORKER_CREDENTIAL", raising=False)
    with pytest.raises(ValueError, match="worker_credential"):
        create_app()


def test_create_app_succeeds_when_control_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "false")
    monkeypatch.delenv("MCD_API_CONTROL__WORKER_CREDENTIAL", raising=False)
    create_app()  # no raise


def test_worker_credential_is_masked_in_dump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "super-secret")
    settings = load_settings(None)
    dump = settings.masked_dump()
    assert dump["control"]["worker_credential"] == "***"


def test_grpc_port_default(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings(None)
    assert settings.server.grpc_port == 50051


def test_create_app_fails_when_control_enabled_without_tls_or_insecure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The control channel must be encrypted (NFR-SEC-1): with neither a cert/key
    # pair nor control.tls.insecure=true, the gRPC listener has no valid posture,
    # so create_app fails fast rather than silently binding plaintext.
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "shared-secret")
    monkeypatch.delenv("MCD_API_CONTROL__TLS__INSECURE", raising=False)
    monkeypatch.delenv("MCD_API_CONTROL__TLS__CERT_FILE", raising=False)
    monkeypatch.delenv("MCD_API_CONTROL__TLS__KEY_FILE", raising=False)
    with pytest.raises(ValueError, match="control.tls"):
        create_app()


def test_create_app_fails_when_only_one_of_cert_or_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cert_file and key_file must be set together; one without the other cannot
    # serve TLS, so it fails fast (CONFIGURATION.md Section 5.1).
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "shared-secret")
    monkeypatch.delenv("MCD_API_CONTROL__TLS__INSECURE", raising=False)
    monkeypatch.setenv("MCD_API_CONTROL__TLS__CERT_FILE", "/path/to/cert.pem")
    monkeypatch.delenv("MCD_API_CONTROL__TLS__KEY_FILE", raising=False)
    with pytest.raises(ValueError, match="set together"):
        create_app()


def test_create_app_succeeds_with_control_tls_insecure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The explicit local/dev opt-out is accepted (CONFIGURATION.md Section 5.1).
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "shared-secret")
    monkeypatch.setenv("MCD_API_CONTROL__TLS__INSECURE", "true")
    create_app()  # no raise
