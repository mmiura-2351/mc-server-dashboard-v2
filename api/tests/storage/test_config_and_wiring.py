"""Storage config keys + edge wiring (CONFIGURATION.md §5.2, STORAGE.md §7).

Defaults, TOML/env overrides, the backend selector admitting future backends, and
the app-factory fail-fast on an unimplemented backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.config import load_settings


def _write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "api.toml"
    path.write_text(body)
    return path


def test_storage_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings(config_file=None)
    assert settings.storage.backend == "fs"
    assert settings.storage.fs.root == "./data"
    assert settings.storage.version_retention == 10


def test_storage_fs_root_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_toml(tmp_path, '[storage.fs]\nroot = "/srv/mcsd-data"\n')
    settings = load_settings(config_file=cfg)
    assert settings.storage.fs.root == "/srv/mcsd-data"


def test_storage_backend_selector_admits_future_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_toml(tmp_path, '[storage]\nbackend = "object"\n')
    settings = load_settings(config_file=cfg)
    assert settings.storage.backend == "object"


def test_storage_unknown_backend_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_toml(tmp_path, '[storage]\nbackend = "s3-but-typo"\n')
    with pytest.raises(ValueError):
        load_settings(config_file=cfg)


def test_storage_root_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", "/data/from/env")
    settings = load_settings(config_file=None)
    assert settings.storage.fs.root == "/data/from/env"


def test_app_factory_builds_fs_storage(tmp_path: Path) -> None:
    settings = load_settings(config_file=None)
    # create_app must succeed with the default fs backend (storage bound at boot).
    app = create_app(settings)
    assert app is not None


def test_app_factory_fails_fast_on_unimplemented_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_toml(tmp_path, '[storage]\nbackend = "object"\n')
    settings = load_settings(config_file=cfg)
    with pytest.raises(ValueError, match="storage.backend"):
        create_app(settings)


def test_storage_keys_in_masked_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings(config_file=None)
    dump = settings.masked_dump()
    assert dump["storage"]["backend"] == "fs"
    assert dump["storage"]["fs"]["root"] == "./data"
