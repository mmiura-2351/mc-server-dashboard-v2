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
    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", "postgresql+asyncpg://user:secret@host/db"
    )
    settings = load_settings(config_file=None)
    dump = settings.masked_dump()
    assert "secret" not in repr(dump)
    assert dump["database"]["url"] == "***"


def test_settings_is_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    settings = load_settings(config_file=None)
    with pytest.raises(ValidationError):
        settings.server.http_port = 1234


def test_unknown_key_in_toml_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[server]\nbogus_key = 1\n")
    with pytest.raises(ValueError):
        load_settings(config_file=cfg)
