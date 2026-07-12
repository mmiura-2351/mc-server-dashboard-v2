"""Tests for the edge configuration loader (CONFIGURATION.md Sections 1-3).

Precedence is defaults < TOML file < MCD_API_ env; secrets are env-only and
masked in any config dump; a missing required key fails fast at load.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from mc_server_dashboard_api.config import load_settings


def _write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "api.toml"
    path.write_text(body)
    return path


def test_defaults_apply_when_only_required_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.server.host == "0.0.0.0"
    assert settings.server.http_port == 8000
    assert settings.log.level == "info"
    assert settings.log.format == "json"
    # JAR-pool GC defaults to daily (issue #293).
    assert settings.jar_gc.interval_seconds == 86400
    # The graceful-stop worker round-trip gets its own generous budget (#930),
    # mirroring the hydrate (#822) and final-snapshot (#847) budgets, so a slow
    # host's stop does not time out under the general 30s command deadline.
    assert settings.control.stop_timeout_seconds == 600


def test_missing_required_database_url_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCD_API_DATABASE__URL", raising=False)
    with pytest.raises(ValueError):
        load_settings(config_file=None)


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_database_url_fails_fast(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    # ``database.url`` is required; a blank ``${MCD_API_DATABASE__URL}``
    # interpolation arrives as "" and passes the presence check but boots an
    # engine that cannot connect. Reject the blank value at load (#939).
    monkeypatch.setenv("MCD_API_DATABASE__URL", blank)
    with pytest.raises(ValueError, match="url"):
        load_settings(config_file=None)


def test_toml_file_overrides_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        '[server]\nhost = "127.0.0.1"\nhttp_port = 9001\n[log]\nlevel = "debug"\n',
    )
    settings = load_settings(config_file=cfg)
    assert settings.server.host == "127.0.0.1"
    assert settings.server.http_port == 9001
    assert settings.log.level == "debug"


def test_env_overrides_toml_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_SERVER__HTTP_PORT", "7777")
    cfg = _write_toml(tmp_path, "[server]\nhttp_port = 9001\n")
    settings = load_settings(config_file=cfg)
    assert settings.server.http_port == 7777


def test_database_url_masked_in_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    # A distinct sentinel password so the assertion checks the URL's secret value
    # is absent, not a config-key name that merely contains "secret" (e.g. the
    # storage.object.secret_key field name).
    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", "postgresql+asyncpg://user:hunter2pw@host/db"
    )
    settings = load_settings(config_file=None)
    dump = settings.masked_dump()
    assert "hunter2pw" not in repr(dump)
    assert dump["database"]["url"] == "***"


def test_settings_is_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    with pytest.raises(ValidationError):
        settings.server.http_port = 1234


def test_password_policy_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.auth.password.hash == "argon2"
    # Default strength preset is ``middle`` (issue #536), not the historical
    # high-equivalent fixed posture.
    assert settings.auth.password.policy == "middle"
    assert settings.auth.password.max_length == 128


def test_password_policy_preset_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_AUTH__PASSWORD__POLICY", "high")
    settings = load_settings(config_file=None)
    assert settings.auth.password.policy == "high"


def test_unknown_password_policy_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_AUTH__PASSWORD__POLICY", "paranoid")
    with pytest.raises(ValidationError, match="policy"):
        load_settings(config_file=None)


def test_brute_force_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    bf = settings.auth.brute_force
    assert bf.enabled is True
    assert bf.username_threshold == 5
    assert bf.username_window_seconds == 900
    assert bf.ip_threshold == 20
    assert bf.ip_window_seconds == 300
    assert bf.lockout_base_seconds == 900
    assert bf.lockout_max_seconds == 86400
    assert bf.delay_ms == 200
    assert bf.prune_interval_seconds == 3600


def test_brute_force_prune_interval_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[auth.brute_force]\nprune_interval_seconds = 60\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.auth.brute_force.prune_interval_seconds == 60


def test_brute_force_prune_interval_must_be_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[auth.brute_force]\nprune_interval_seconds = 0\n",
    )
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("username_threshold", 0),
        ("username_window_seconds", 0),
        ("ip_threshold", 0),
        ("ip_window_seconds", 0),
        ("lockout_base_seconds", 0),
        ("lockout_max_seconds", 0),
        ("delay_ms", -1),
    ],
)
def test_brute_force_field_rejects_out_of_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, bad_value: int
) -> None:
    # Windows/lockouts/intervals must be > 0; thresholds >= 1; delay_ms >= 0. A
    # value below the bound silently weakens or breaks brute-force protection
    # (e.g. window=0 means thresholds never trip), so the loader rejects it
    # (issue #140, SECURITY.md Section 2).
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        f"[auth.brute_force]\n{field} = {bad_value}\n",
    )
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


@pytest.mark.parametrize(
    ("field", "ok_value"),
    [
        ("username_threshold", 1),
        ("username_window_seconds", 1),
        ("ip_threshold", 1),
        ("ip_window_seconds", 1),
        ("lockout_base_seconds", 1),
        ("lockout_max_seconds", 1),
        ("delay_ms", 0),
    ],
)
def test_brute_force_field_accepts_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, ok_value: int
) -> None:
    # delay_ms = 0 is the explicit disable of the artificial failure delay
    # (CONFIGURATION.md Section 7.2); the rest accept their lowest legal value.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    body = f"[auth.brute_force]\n{field} = {ok_value}\n"
    # lockout_max_seconds = 1 would fall below the default base (900) and trip
    # the base <= max cross-field rule (issue #163); lower the base too so this
    # case still exercises only the field-level lower bound.
    if field == "lockout_max_seconds":
        body += "lockout_base_seconds = 1\n"
    cfg = _write_toml(tmp_path, body)
    settings = load_settings(config_file=cfg)
    assert getattr(settings.auth.brute_force, field) == ok_value


def test_token_algorithm_accepts_rs256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, '[auth.token]\nalgorithm = "RS256"\n')
    settings = load_settings(config_file=cfg)
    assert settings.auth.token.algorithm == "RS256"


def test_token_algorithm_rejects_miscased_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A miscased "hs256" must be rejected at load rather than slipping past the
    # HS256 key-length floor and only failing at first JWT use (issue #140).
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, '[auth.token]\nalgorithm = "hs256"\n')
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


@pytest.mark.parametrize(
    "field",
    ["access_ttl_seconds", "refresh_ttl_seconds", "refresh_reuse_grace_seconds"],
)
def test_token_ttl_must_be_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, f"[auth.token]\n{field} = 0\n")
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


@pytest.mark.parametrize(
    ("section", "field", "bad_value"),
    [
        ("server", "http_port", -1),
        ("server", "http_port", 65536),
        ("server", "grpc_port", -1),
        ("server", "grpc_port", 65536),
        ("control", "heartbeat_timeout_seconds", 0),
        ("control", "command_timeout_seconds", 0),
        ("control", "hydrate_timeout_seconds", 0),
        ("control", "snapshot_timeout_seconds", 0),
        ("control", "stop_timeout_seconds", 0),
        ("storage", "version_retention", -1),
        ("snapshot", "default_interval_seconds", 0),
        ("snapshot", "min_interval_seconds", 0),
        ("backup", "schedule_tick_seconds", 0),
        ("schedule", "tick_seconds", 0),
        # Above the runner's fixed 300 s late-run grace every non-backup
        # occurrence would be judged stale before the loop ever saw it (#1838).
        ("schedule", "tick_seconds", 301),
        ("reconciler", "interval_seconds", 0),
        ("reconciler", "grace_seconds", 0),
        ("reconciler", "held_start_grace_seconds", 0),
        ("reconciler", "backoff_base_seconds", 0),
        ("reconciler", "backoff_max_seconds", 0),
        ("jar_gc", "interval_seconds", 0),
    ],
)
def test_numeric_setting_rejects_out_of_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    bad_value: int,
) -> None:
    # A zero/negative port, timeout, retention or interval would break behavior
    # silently; the loader rejects it at boot (issue #140).
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, f"[{section}]\n{field} = {bad_value}\n")
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


def test_password_max_length_must_be_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[auth.password]\nmax_length = 0\n")
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


@pytest.mark.parametrize("field", ["http_port", "grpc_port"])
def test_port_zero_is_accepted_as_ephemeral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    # 0 is the conventional "bind an OS-assigned ephemeral port" value (the gRPC
    # lifespan test relies on it); it must not be rejected.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, f"[server]\n{field} = 0\n")
    settings = load_settings(config_file=cfg)
    assert getattr(settings.server, field) == 0


def test_version_retention_zero_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 0 retained versions is a legitimate "keep none" choice, distinct from a
    # negative count which is meaningless.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[storage]\nversion_retention = 0\n")
    settings = load_settings(config_file=cfg)
    assert settings.storage.version_retention == 0


def test_snapshot_cadence_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.snapshot.default_interval_seconds == 3600
    assert settings.snapshot.min_interval_seconds == 300


def test_snapshot_cadence_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[snapshot]\ndefault_interval_seconds = 1800\nmin_interval_seconds = 60\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.snapshot.default_interval_seconds == 1800
    assert settings.snapshot.min_interval_seconds == 60


def test_reconciler_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.reconciler.interval_seconds == 60
    assert settings.reconciler.grace_seconds == 660
    assert settings.reconciler.held_start_grace_seconds == 90
    assert settings.reconciler.backoff_base_seconds == 30
    assert settings.reconciler.backoff_max_seconds == 3600


def test_reconciler_from_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[reconciler]\n"
        "interval_seconds = 30\n"
        "grace_seconds = 90\n"
        "held_start_grace_seconds = 45\n"
        "backoff_base_seconds = 15\n"
        "backoff_max_seconds = 1800\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.reconciler.interval_seconds == 30
    assert settings.reconciler.grace_seconds == 90
    assert settings.reconciler.held_start_grace_seconds == 45
    assert settings.reconciler.backoff_base_seconds == 15
    assert settings.reconciler.backoff_max_seconds == 1800


def test_reconciler_backoff_max_below_base_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A backoff max below the base would clamp the first retry below its base,
    # making the cap meaningless; reject the inverted range at load (fail-fast).
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[reconciler]\nbackoff_base_seconds = 60\nbackoff_max_seconds = 30\n",
    )
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


def test_reconciler_backoff_max_equal_base_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[reconciler]\nbackoff_base_seconds = 600\nbackoff_max_seconds = 600\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.reconciler.backoff_max_seconds == 600


def test_reconciler_backoff_max_below_slack_floor_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # backoff_max_seconds doubles as the expiry slack that keeps crash-loop
    # damping alive across a slow boot's starting window (#346). A slack below the
    # plausible-boot floor lets a still-diverged server expire and reset its
    # failure count, re-arming the boot-crash loop, so reject it at load (#353).
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[reconciler]\nbackoff_base_seconds = 30\nbackoff_max_seconds = 599\n",
    )
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


def test_reconciler_backoff_max_at_slack_floor_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[reconciler]\nbackoff_base_seconds = 30\nbackoff_max_seconds = 600\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.reconciler.backoff_max_seconds == 600


def test_registration_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    reg = settings.auth.registration
    assert reg.open is True
    assert reg.ip_limit_enabled is True
    assert reg.ip_threshold == 5
    assert reg.ip_window_seconds == 3600


def test_registration_disabled_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[auth.registration]\nopen = false\nip_threshold = 2\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.auth.registration.open is False
    assert settings.auth.registration.ip_threshold == 2


def test_registration_ip_threshold_must_be_at_least_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[auth.registration]\nip_threshold = 0\n",
    )
    with pytest.raises(ValueError):
        load_settings(config_file=cfg)


def test_proxy_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.auth.proxy.trust_forwarded_headers is False
    assert settings.auth.proxy.trusted_proxies == ()


def test_proxy_trusted_proxies_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[auth.proxy]\n"
        "trust_forwarded_headers = true\n"
        'trusted_proxies = ["10.0.0.0/8"]\n',
    )
    settings = load_settings(config_file=cfg)
    assert settings.auth.proxy.trust_forwarded_headers is True
    assert settings.auth.proxy.trusted_proxies == ("10.0.0.0/8",)


def test_password_hash_selector_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, '[auth.password]\nhash = "bcrypt"\npolicy = "low"\n')
    settings = load_settings(config_file=cfg)
    assert settings.auth.password.hash == "bcrypt"
    assert settings.auth.password.policy == "low"


def test_unknown_password_hash_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, '[auth.password]\nhash = "scrypt"\n')
    with pytest.raises(ValueError):
        load_settings(config_file=cfg)


def test_unknown_key_in_toml_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[server]\nbogus_key = 1\n")
    with pytest.raises(ValueError):
        load_settings(config_file=cfg)


def test_token_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.delenv("MCD_API_AUTH__TOKEN__SIGNING_KEY", raising=False)
    settings = load_settings(config_file=None)
    assert settings.auth.token.algorithm == "HS256"
    assert settings.auth.token.signing_key is None
    assert settings.auth.token.access_ttl_seconds == 900
    assert settings.auth.token.refresh_ttl_seconds == 1209600
    # Reuse grace window (issue #369) defaults to 60 s.
    assert settings.auth.token.refresh_reuse_grace_seconds == 60
    # Refresh-cookie transport (issue #363): name + Secure flag default safely.
    assert settings.auth.token.refresh_cookie_name == "mcd_refresh"
    assert settings.auth.token.refresh_cookie_secure is True


def test_refresh_cookie_settings_overridable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Plain-HTTP localhost dev needs Secure off so the browser stores the cookie;
    # the name is operator-configurable too (issue #363).
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        '[auth.token]\nrefresh_cookie_name = "sess"\nrefresh_cookie_secure = false\n',
    )
    settings = load_settings(config_file=cfg)
    assert settings.auth.token.refresh_cookie_name == "sess"
    assert settings.auth.token.refresh_cookie_secure is False


def test_token_signing_key_from_env_is_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    secret = "top-secret-signing-key-of-32-byte"
    monkeypatch.setenv("MCD_API_AUTH__TOKEN__SIGNING_KEY", secret)
    settings = load_settings(config_file=None)
    assert settings.auth.token.signing_key == secret
    dump = settings.masked_dump()
    assert secret not in repr(dump)
    assert dump["auth"]["token"]["signing_key"] == "***"


def test_short_hs256_signing_key_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An HS256 signing key shorter than 32 bytes is too weak; the loader rejects
    # it at boot (CONFIGURATION.md Section 5.3, fail-fast).
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_AUTH__TOKEN__SIGNING_KEY", "x" * 31)
    with pytest.raises(ValidationError, match="signing_key"):
        load_settings(config_file=None)


def test_hs256_signing_key_at_32_bytes_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_AUTH__TOKEN__SIGNING_KEY", "x" * 32)
    settings = load_settings(config_file=None)
    assert settings.auth.token.signing_key == "x" * 32


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_signing_key_is_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    # A blank ``${MCD_API_AUTH__TOKEN__SIGNING_KEY}`` interpolation arrives as ""
    # (or whitespace); collapse it to None so the app factory's "required" guard
    # fires rather than the HS256 length floor (or no guard at all for RS256)
    # admitting an empty key (#939).
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_AUTH__TOKEN__SIGNING_KEY", blank)
    settings = load_settings(config_file=None)
    assert settings.auth.token.signing_key is None


# --- Cross-field consistency (issue #163) -----------------------------------
# Pairs that pass their individual field bounds but are semantically
# inconsistent (e.g. a min above its max). Each validator names both fields and
# accepts the equal-values boundary where ``<=`` allows it; the access/refresh
# TTL pair is strict (``<``) since equal lifetimes defeat the refresh mechanism.


def test_preset_min_length_above_max_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The high preset's 12-char minimum cannot coexist with a max_length of 8.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        '[auth.password]\npolicy = "high"\nmax_length = 8\n',
    )
    with pytest.raises(ValidationError, match="min_length"):
        load_settings(config_file=cfg)


def test_preset_min_length_equal_to_max_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # max_length equal to the preset minimum is a valid fixed-length requirement.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        '[auth.password]\npolicy = "high"\nmax_length = 12\n',
    )
    settings = load_settings(config_file=cfg)
    assert settings.auth.password.policy == "high"
    assert settings.auth.password.max_length == 12


def test_lockout_base_above_max_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[auth.brute_force]\nlockout_base_seconds = 1000\nlockout_max_seconds = 900\n",
    )
    with pytest.raises(ValidationError, match="lockout_base_seconds"):
        load_settings(config_file=cfg)


def test_lockout_base_equal_to_max_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[auth.brute_force]\nlockout_base_seconds = 900\nlockout_max_seconds = 900\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.auth.brute_force.lockout_base_seconds == 900
    assert settings.auth.brute_force.lockout_max_seconds == 900


def test_snapshot_min_above_default_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[snapshot]\ndefault_interval_seconds = 300\nmin_interval_seconds = 600\n",
    )
    with pytest.raises(ValidationError, match="min_interval_seconds"):
        load_settings(config_file=cfg)


def test_snapshot_min_equal_to_default_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[snapshot]\ndefault_interval_seconds = 300\nmin_interval_seconds = 300\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.snapshot.default_interval_seconds == 300
    assert settings.snapshot.min_interval_seconds == 300


def test_token_access_ttl_not_below_refresh_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[auth.token]\naccess_ttl_seconds = 1000\nrefresh_ttl_seconds = 900\n",
    )
    with pytest.raises(ValidationError, match="access_ttl_seconds"):
        load_settings(config_file=cfg)


def test_token_access_ttl_equal_to_refresh_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Equal lifetimes are rejected too: the refresh token would expire no later
    # than the access token, defeating the refresh mechanism (strict ``<``).
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[auth.token]\naccess_ttl_seconds = 900\nrefresh_ttl_seconds = 900\n",
    )
    with pytest.raises(ValidationError, match="access_ttl_seconds"):
        load_settings(config_file=cfg)


def test_token_access_ttl_below_refresh_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[auth.token]\naccess_ttl_seconds = 900\nrefresh_ttl_seconds = 901\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.auth.token.access_ttl_seconds == 900
    assert settings.auth.token.refresh_ttl_seconds == 901


def test_ports_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.ports.range_start == 25565
    assert settings.ports.range_end == 25664


def test_ports_from_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[ports]\nrange_start = 30000\nrange_end = 30010\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.ports.range_start == 30000
    assert settings.ports.range_end == 30010


@pytest.mark.parametrize("value", [0, 65536])
def test_ports_range_start_rejects_out_of_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: int
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, f"[ports]\nrange_start = {value}\nrange_end = 65535\n")
    with pytest.raises(ValidationError, match="range_start"):
        load_settings(config_file=cfg)


@pytest.mark.parametrize("value", [0, 65536])
def test_ports_range_end_rejects_out_of_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: int
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, f"[ports]\nrange_start = 1\nrange_end = {value}\n")
    with pytest.raises(ValidationError, match="range_end"):
        load_settings(config_file=cfg)


def test_ports_start_above_end_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[ports]\nrange_start = 30000\nrange_end = 29999\n")
    with pytest.raises(ValidationError, match="range_start"):
        load_settings(config_file=cfg)


def test_ports_start_equal_to_end_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A single-port range is valid (start == end): exactly one assignable port.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[ports]\nrange_start = 25565\nrange_end = 25565\n")
    settings = load_settings(config_file=cfg)
    assert settings.ports.range_start == 25565
    assert settings.ports.range_end == 25565


# --- Bedrock UDP port window (issue #1541) -----------------------------------


def test_bedrock_ports_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default window starts at Bedrock's conventional port 19132 so the
    # first allocation lands there.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.ports.bedrock_range_start == 19132
    assert settings.ports.bedrock_range_end == 19231


def test_bedrock_ports_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[ports]\nbedrock_range_start = 20000\nbedrock_range_end = 20010\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.ports.bedrock_range_start == 20000
    assert settings.ports.bedrock_range_end == 20010


def test_bedrock_ports_start_above_end_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[ports]\nbedrock_range_start = 20000\nbedrock_range_end = 19999\n",
    )
    with pytest.raises(ValidationError, match="bedrock_range_start"):
        load_settings(config_file=cfg)


def test_relay_bedrock_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Bedrock capability defaults off; the relay's Bedrock tunnel UDP
    # listener port defaults outside the default Bedrock window.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.relay.bedrock_enabled is False
    assert settings.relay.bedrock_tunnel_port == 25675


def test_relay_bedrock_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[relay]\nbedrock_enabled = true\nbedrock_tunnel_port = 19200\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.relay.bedrock_enabled is True
    assert settings.relay.bedrock_tunnel_port == 19200


# --- database pool sizing (issue #884) --------------------------------------


def test_database_pool_size_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # SQLAlchemy's default pool_size is 5; we mirror it so existing deployments
    # see no behavioural change after the config key lands.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.database.pool_size == 5


def test_database_max_overflow_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # SQLAlchemy's default max_overflow is 10; we mirror it.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.database.max_overflow == 10


def test_database_pool_size_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[database]\npool_size = 20\n")
    settings = load_settings(config_file=cfg)
    assert settings.database.pool_size == 20


def test_database_max_overflow_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[database]\nmax_overflow = 0\n")
    settings = load_settings(config_file=cfg)
    assert settings.database.max_overflow == 0


def test_database_pool_size_must_be_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pool_size = 0 would lift the connection cap entirely (SQLAlchemy treats 0
    # as no-limit); reject it to avoid accidentally uncapped pools.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[database]\npool_size = 0\n")
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


def test_database_max_overflow_must_be_nonnegative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # max_overflow = 0 disables overflow (valid); -1 is meaningless.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[database]\nmax_overflow = -1\n")
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


def test_database_pool_size_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_DATABASE__POOL_SIZE", "20")
    settings = load_settings(config_file=None)
    assert settings.database.pool_size == 20


def test_database_max_overflow_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_DATABASE__MAX_OVERFLOW", "15")
    settings = load_settings(config_file=None)
    assert settings.database.max_overflow == 15


# --- [relay] section (issue #956, RELAY.md Section 13) ---


def test_relay_defaults_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.relay.enabled is False
    assert settings.relay.credential is None
    assert settings.relay.base_domain is None
    assert settings.relay.session_retention_days == 90


def test_relay_section_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The deploy wiring (PR #972) expects the standard pydantic-settings ``__``
    # nesting: MCD_API_RELAY__ENABLED / __CREDENTIAL / __BASE_DOMAIN.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_RELAY__ENABLED", "true")
    monkeypatch.setenv("MCD_API_RELAY__CREDENTIAL", "relay-secret")
    monkeypatch.setenv("MCD_API_RELAY__BASE_DOMAIN", "mc.example.com")
    settings = load_settings(config_file=None)
    assert settings.relay.enabled is True
    assert settings.relay.credential == "relay-secret"
    assert settings.relay.base_domain == "mc.example.com"


@pytest.mark.parametrize("blank", ["", "   "])
def test_relay_blank_credential_is_missing(
    blank: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A blank ``${MCD_API_RELAY__CREDENTIAL}`` interpolation collapses to None so
    # the app factory's required-when-enabled guard treats it as missing (#943).
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_RELAY__CREDENTIAL", blank)
    settings = load_settings(config_file=None)
    assert settings.relay.credential is None


def test_relay_session_retention_days_must_be_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[relay]\nsession_retention_days = 0\n")
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


def test_relay_credential_masked_in_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_RELAY__CREDENTIAL", "relay-secret")
    settings = load_settings(config_file=None)
    assert settings.masked_dump()["relay"]["credential"] == "***"


# --- [memory_limit] section (issue #1069) ---


def test_memory_limit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.memory_limit.default_mb is None
    assert settings.memory_limit.max_mb is None


def test_memory_limit_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_MEMORY_LIMIT__DEFAULT_MB", "2048")
    monkeypatch.setenv("MCD_API_MEMORY_LIMIT__MAX_MB", "8192")
    settings = load_settings(config_file=None)
    assert settings.memory_limit.default_mb == 2048
    assert settings.memory_limit.max_mb == 8192


def test_memory_limit_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[memory_limit]\ndefault_mb = 4096\nmax_mb = 16384\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.memory_limit.default_mb == 4096
    assert settings.memory_limit.max_mb == 16384


def test_memory_limit_default_below_floor_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[memory_limit]\ndefault_mb = 256\n")
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


def test_memory_limit_max_below_floor_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[memory_limit]\nmax_mb = 256\n")
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


def test_memory_limit_default_above_max_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[memory_limit]\ndefault_mb = 8192\nmax_mb = 4096\n",
    )
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


def test_memory_limit_default_above_ceiling_without_max_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """default_mb above the 1 TiB ceiling with max_mb unset must fail at startup."""
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[memory_limit]\ndefault_mb = 1048577\n")
    with pytest.raises(ValidationError):
        load_settings(config_file=cfg)


def test_memory_limit_default_equal_to_max_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[memory_limit]\ndefault_mb = 4096\nmax_mb = 4096\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.memory_limit.default_mb == 4096
    assert settings.memory_limit.max_mb == 4096


# --- server.data_plane_base_url (issue #1549) ---


def test_data_plane_base_url_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.server.data_plane_base_url is None


def test_effective_data_plane_base_url_falls_back_to_public_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A split deployment with no edge proxy sets only public_base_url and never
    # sets data_plane_base_url; the effective data-plane URL must still resolve
    # so behavior is unchanged (issue #1549 fix direction).
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_SERVER__PUBLIC_BASE_URL", "https://api.example.com")
    settings = load_settings(config_file=None)
    assert settings.server.effective_data_plane_base_url == "https://api.example.com"


def test_effective_data_plane_base_url_overrides_public_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A single-host deployment behind a body-size-capped edge proxy (e.g.
    # Cloudflare Tunnel, ~100 MB) sets data_plane_base_url to an internal
    # address so worker-facing snapshot/hydrate transfers bypass the edge
    # (issue #1549).
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("MCD_API_SERVER__PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("MCD_API_SERVER__DATA_PLANE_BASE_URL", "http://api:8000")
    settings = load_settings(config_file=None)
    assert settings.server.data_plane_base_url == "http://api:8000"
    assert settings.server.effective_data_plane_base_url == "http://api:8000"


def test_effective_data_plane_base_url_none_when_both_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    assert settings.server.effective_data_plane_base_url is None
