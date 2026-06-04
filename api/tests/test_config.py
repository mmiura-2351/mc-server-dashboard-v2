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
