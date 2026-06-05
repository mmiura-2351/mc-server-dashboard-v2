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


def test_missing_required_database_url_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCD_API_DATABASE__URL", raising=False)
    with pytest.raises(ValueError):
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
    assert settings.auth.password.min_length == 12
    assert settings.auth.password.max_length == 128
    assert settings.auth.password.require_complexity is True
    assert settings.auth.password.check_common_list is True
    assert settings.auth.password.forbid_user_info is True
    assert settings.auth.password.forbid_simple_patterns is True


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


@pytest.mark.parametrize("field", ["access_ttl_seconds", "refresh_ttl_seconds"])
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
        ("storage", "version_retention", -1),
        ("snapshot", "default_interval_seconds", 0),
        ("snapshot", "min_interval_seconds", 0),
        ("backup", "schedule_tick_seconds", 0),
        ("reconciler", "interval_seconds", 0),
        ("reconciler", "grace_seconds", 0),
        ("reconciler", "backoff_base_seconds", 0),
        ("reconciler", "backoff_max_seconds", 0),
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


@pytest.mark.parametrize("field", ["min_length", "max_length"])
def test_password_length_must_be_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, f"[auth.password]\n{field} = 0\n")
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
    assert settings.reconciler.grace_seconds == 120
    assert settings.reconciler.backoff_base_seconds == 30
    assert settings.reconciler.backoff_max_seconds == 3600


def test_reconciler_from_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[reconciler]\n"
        "interval_seconds = 30\n"
        "grace_seconds = 90\n"
        "backoff_base_seconds = 15\n"
        "backoff_max_seconds = 1800\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.reconciler.interval_seconds == 30
    assert settings.reconciler.grace_seconds == 90
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
        "[reconciler]\nbackoff_base_seconds = 30\nbackoff_max_seconds = 30\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.reconciler.backoff_max_seconds == 30


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
    cfg = _write_toml(tmp_path, '[auth.password]\nhash = "bcrypt"\nmin_length = 10\n')
    settings = load_settings(config_file=cfg)
    assert settings.auth.password.hash == "bcrypt"
    assert settings.auth.password.min_length == 10


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


# --- Cross-field consistency (issue #163) -----------------------------------
# Pairs that pass their individual field bounds but are semantically
# inconsistent (e.g. a min above its max). Each validator names both fields and
# accepts the equal-values boundary where ``<=`` allows it; the access/refresh
# TTL pair is strict (``<``) since equal lifetimes defeat the refresh mechanism.


def test_password_min_length_above_max_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[auth.password]\nmin_length = 130\nmax_length = 128\n",
    )
    with pytest.raises(ValidationError, match="min_length"):
        load_settings(config_file=cfg)


def test_password_min_length_equal_to_max_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(
        tmp_path,
        "[auth.password]\nmin_length = 64\nmax_length = 64\n",
    )
    settings = load_settings(config_file=cfg)
    assert settings.auth.password.min_length == 64
    assert settings.auth.password.max_length == 64


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
