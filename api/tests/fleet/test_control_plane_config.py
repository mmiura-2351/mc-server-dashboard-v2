"""Control-plane config: fail-fast on a missing credential, secret masking.

The Worker credential is a required secret whenever the control plane is enabled
(CONFIGURATION.md Section 5.1); ``create_app`` raises at boot rather than
starting a server that would admit any Worker (NFR-SEC-1). When present, the
credential is masked in the logged config dump (NFR-OBS-1).
"""

from __future__ import annotations

import re
from pathlib import Path

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


@pytest.mark.parametrize("blank", ["", "   "])
def test_create_app_fails_when_control_enabled_with_blank_credential(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    # A blank ``${MCD_API_CONTROL__WORKER_CREDENTIAL}`` interpolation arrives as ""
    # rather than unset; it is collapsed to None so the same "required" fail-fast
    # fires rather than admitting any Worker on an empty credential (#939).
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", blank)
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


def _enable_control(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "shared-secret")
    monkeypatch.setenv("MCD_API_CONTROL__TLS__INSECURE", "true")


def test_create_app_warns_when_grace_below_hydrate_plus_command_floor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Duplicate-start safety (issue #774/#812/#822) requires
    # grace > hydrate_timeout + command_timeout. At/below the floor, create_app
    # warns (not fatal): 80 <= 60 + 30.
    _enable_control(monkeypatch)
    monkeypatch.setenv("MCD_API_CONTROL__HYDRATE_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("MCD_API_CONTROL__COMMAND_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("MCD_API_RECONCILER__GRACE_SECONDS", "80")
    with caplog.at_level("WARNING"):
        create_app()
    assert any("grace_seconds" in r.message for r in caplog.records)


def test_create_app_does_not_warn_when_grace_above_floor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Above the floor (200 > max(60 + 30, snapshot=120, stop=150)), no warning fires.
    _enable_control(monkeypatch)
    monkeypatch.setenv("MCD_API_CONTROL__HYDRATE_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("MCD_API_CONTROL__COMMAND_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("MCD_API_CONTROL__SNAPSHOT_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("MCD_API_CONTROL__STOP_TIMEOUT_SECONDS", "150")
    monkeypatch.setenv("MCD_API_RECONCILER__GRACE_SECONDS", "200")
    with caplog.at_level("WARNING"):
        create_app()
    assert not any("grace_seconds" in r.message for r in caplog.records)


def test_create_app_warns_when_snapshot_timeout_exceeds_grace(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Issue #847 (round-3): the floor is max(hydrate + command, snapshot_timeout),
    # so an operator who raises snapshot_timeout above grace is warned EVEN when
    # hydrate + command is below grace. Without this term the stale-stop arm could
    # clear a still-healthy final-snapshot hold mid-upload, reopening the race.
    # Here 100 <= max(60 + 30, 200, stop=80) = 200, driven by the snapshot term
    # alone (stop is pinned below grace so it is not the binding term).
    _enable_control(monkeypatch)
    monkeypatch.setenv("MCD_API_CONTROL__HYDRATE_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("MCD_API_CONTROL__COMMAND_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("MCD_API_CONTROL__SNAPSHOT_TIMEOUT_SECONDS", "200")
    monkeypatch.setenv("MCD_API_CONTROL__STOP_TIMEOUT_SECONDS", "80")
    monkeypatch.setenv("MCD_API_RECONCILER__GRACE_SECONDS", "100")
    with caplog.at_level("WARNING"):
        create_app()
    assert any("grace_seconds" in r.message for r in caplog.records)


def test_create_app_warns_when_stop_timeout_exceeds_grace(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Issue #930: the floor is max(hydrate + command, snapshot_timeout,
    # stop_timeout), so an operator who raises stop_timeout above grace is warned
    # even when the other terms are below grace. Without this term a stale stop the
    # reconciler replays could be re-dispatched before its first round-trip settles.
    # Here 100 <= max(60 + 30, snapshot=80, stop=200) = 200, driven by stop alone.
    _enable_control(monkeypatch)
    monkeypatch.setenv("MCD_API_CONTROL__HYDRATE_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("MCD_API_CONTROL__COMMAND_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("MCD_API_CONTROL__SNAPSHOT_TIMEOUT_SECONDS", "80")
    monkeypatch.setenv("MCD_API_CONTROL__STOP_TIMEOUT_SECONDS", "200")
    monkeypatch.setenv("MCD_API_RECONCILER__GRACE_SECONDS", "100")
    with caplog.at_level("WARNING"):
        create_app()
    assert any("grace_seconds" in r.message for r in caplog.records)


def test_stock_defaults_do_not_warn_reconciler_grace_floor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The PR's central claim: stock config (grace=660, hydrate=600, command=30)
    # satisfies the floor (660 > 600 + 30), so _warn_reconciler_grace_floor
    # must not fire. Catch a future default regression without needing env overrides.
    _enable_control(monkeypatch)
    monkeypatch.delenv("MCD_API_RECONCILER__GRACE_SECONDS", raising=False)
    monkeypatch.delenv("MCD_API_CONTROL__HYDRATE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MCD_API_CONTROL__COMMAND_TIMEOUT_SECONDS", raising=False)
    with caplog.at_level("WARNING"):
        create_app()
    assert not any("grace_seconds" in r.message for r in caplog.records)


def test_create_app_warns_when_held_start_grace_below_command_floor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The held-start short grace (issue #999) only covers a command-only
    # redispatch_start, so its floor is command_timeout. At/below it, create_app
    # warns: 20 <= 30. The full grace stays comfortably above its own floor so this
    # warning is the only one — proving the held floor is enforced independently.
    _enable_control(monkeypatch)
    monkeypatch.setenv("MCD_API_CONTROL__COMMAND_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("MCD_API_RECONCILER__HELD_START_GRACE_SECONDS", "20")
    with caplog.at_level("WARNING"):
        create_app()
    assert any("held_start_grace_seconds" in r.message for r in caplog.records)


def test_stock_held_start_grace_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Stock held_start_grace_seconds=90 is comfortably above command_timeout=30, so
    # the #999 held floor warning must not fire by default. The full #822/#847 floor
    # behavior is unchanged (stock grace=660 still satisfies its floor).
    _enable_control(monkeypatch)
    monkeypatch.delenv("MCD_API_RECONCILER__HELD_START_GRACE_SECONDS", raising=False)
    monkeypatch.delenv("MCD_API_CONTROL__COMMAND_TIMEOUT_SECONDS", raising=False)
    with caplog.at_level("WARNING"):
        create_app()
    assert not any("held_start_grace_seconds" in r.message for r in caplog.records)


# --- data-plane URL fallback warning (issue #2564) ---


def _set_base_urls(
    monkeypatch: pytest.MonkeyPatch, *, public: str | None, data_plane: str | None
) -> None:
    for var, value in (
        ("MCD_API_SERVER__PUBLIC_BASE_URL", public),
        ("MCD_API_SERVER__DATA_PLANE_BASE_URL", data_plane),
    ):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)


def test_create_app_warns_when_data_plane_base_url_falls_back_to_public(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # With data_plane_base_url unset, effective_data_plane_base_url falls back to
    # public_base_url (issue #1549), so Workers are handed the public URL for
    # hydrate/snapshot transfers. Behind a body-size-capped edge (Cloudflare
    # Tunnel, ~100 MB) that first fails at snapshot time — on a cross-host Worker,
    # on a different machine — with nothing pointing back at the unset setting
    # (issue #2564). Surface it at boot instead.
    _enable_control(monkeypatch)
    _set_base_urls(monkeypatch, public="https://mc.example.com", data_plane=None)
    with caplog.at_level("WARNING"):
        create_app()
    warnings = [r.message for r in caplog.records if "data_plane_base_url" in r.message]
    assert warnings, "expected a data_plane_base_url fallback warning"
    # The warning has to name the URL Workers will actually be handed, or an
    # operator cannot tell which side is misconfigured.
    assert "https://mc.example.com" in warnings[0]


def test_create_app_does_not_warn_when_data_plane_base_url_is_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _enable_control(monkeypatch)
    _set_base_urls(
        monkeypatch, public="https://mc.example.com", data_plane="http://10.0.0.5:8000"
    )
    with caplog.at_level("WARNING"):
        create_app()
    assert not any("data_plane_base_url" in r.message for r in caplog.records)


def test_create_app_does_not_warn_when_data_plane_base_url_equals_public(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The guard fires on the *fallback*, not on the two URLs being equal. Setting
    # data_plane_base_url explicitly — including to the same value as
    # public_base_url — is how an operator whose public URL is directly reachable
    # states that intent, and it is what makes this a warning rather than a hard
    # failure: there is a one-line way to say "yes, I mean it" (issue #2564).
    _enable_control(monkeypatch)
    _set_base_urls(
        monkeypatch,
        public="https://mc.example.com",
        data_plane="https://mc.example.com",
    )
    with caplog.at_level("WARNING"):
        create_app()
    assert not any("data_plane_base_url" in r.message for r in caplog.records)


def test_create_app_does_not_warn_when_public_base_url_is_unset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # With neither set the effective URL is None: no Worker is handed a tunnel
    # hostname, so there is nothing to warn about (the transfer layer already
    # fails loudly when it needs a URL and has none).
    _enable_control(monkeypatch)
    _set_base_urls(monkeypatch, public=None, data_plane=None)
    with caplog.at_level("WARNING"):
        create_app()
    assert not any("data_plane_base_url" in r.message for r in caplog.records)


_COMPOSE_FILE = Path(__file__).resolve().parents[3] / "compose.yaml"


def _compose_env_default(variable: str) -> str:
    """What the shipped ``compose.yaml`` gives the api service with ``.env`` unset.

    ``compose.yaml`` pins ``MCD_API_SERVER__DATA_PLANE_BASE_URL`` to the internal
    compose address regardless of ``PUBLIC_BASE_URL`` (issue #1549). Read the pins
    out of the file rather than restating them, so this tracks the shipped values
    instead of a copy of them.
    """

    match = re.search(
        rf"^\s*{variable}:\s*(\S+)\s*$", _COMPOSE_FILE.read_text(), re.MULTILINE
    )
    assert match is not None, (
        f"compose.yaml no longer sets {variable} for the api service; if the "
        "#1549 data-plane pin is gone, the #2564 warning fires on the shipped path"
    )
    # `${VAR:-default}` -> `default`; a bare literal is returned as-is.
    interpolated = re.fullmatch(r"\$\{[^:}]+:-([^}]*)\}", match.group(1))
    return interpolated.group(1) if interpolated else match.group(1)


@pytest.mark.parametrize(
    "public",
    [
        # `.env` untouched: compose defaults PUBLIC_BASE_URL to the same internal
        # address it pins the data plane to, so the two URLs are EQUAL on the most
        # common deployment there is. A guard keyed on "the two URLs match" would
        # fire here — on the normal path — which is why this one is keyed on the
        # fallback instead.
        None,
        # `.env` overriding PUBLIC_BASE_URL to a tunnel hostname: the exact #1549
        # topology this warning is about, still silent because compose keeps the
        # data-plane pin independent of PUBLIC_BASE_URL.
        "https://mc.example.com",
    ],
)
def test_shipped_compose_file_does_not_trip_the_data_plane_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    public: str | None,
) -> None:
    """The guard must stay silent for every deployment using the shipped compose
    file (issue #2564).

    A warning that fires on the normal path is worse than no warning: it trains
    operators to ignore startup output. ``compose.yaml`` always sets the
    worker-facing URL, so the fallback this guard detects cannot arise there under
    either ``.env``.
    """

    _enable_control(monkeypatch)
    _set_base_urls(
        monkeypatch,
        public=public or _compose_env_default("MCD_API_SERVER__PUBLIC_BASE_URL"),
        data_plane=_compose_env_default("MCD_API_SERVER__DATA_PLANE_BASE_URL"),
    )
    with caplog.at_level("WARNING"):
        create_app()
    assert not any("data_plane_base_url" in r.message for r in caplog.records)
