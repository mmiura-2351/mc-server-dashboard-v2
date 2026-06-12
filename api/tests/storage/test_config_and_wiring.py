"""Storage config keys + edge wiring.

References CONFIGURATION.md Section 5.2 and STORAGE.md Section 7. Covers the
defaults, TOML/env overrides, the backend selector admitting future backends, and
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


def test_app_factory_fails_fast_on_object_without_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The object backend is implemented (#105) but requires its endpoint/bucket/
    # credentials; a missing one fails fast at boot (CONFIGURATION.md Section 3).
    cfg = _write_toml(tmp_path, '[storage]\nbackend = "object"\n')
    settings = load_settings(config_file=cfg)
    with pytest.raises(ValueError, match="storage.object"):
        create_app(settings)


def test_app_factory_fails_fast_on_object_with_blank_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # compose interpolates an unset ``${MCD_API_STORAGE__OBJECT__ACCESS_KEY}`` to an
    # EMPTY string, not None; an `is None`-only guard would boot a silently
    # unauthenticated deployment against SeaweedFS. Empty/whitespace values must fail
    # fast with the same error as a missing one (#702).
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ENDPOINT", "https://s3.example:9000")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__BUCKET", "mcsd")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ACCESS_KEY", "")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__SECRET_KEY", "   ")
    cfg = _write_toml(tmp_path, '[storage]\nbackend = "object"\n')
    settings = load_settings(config_file=cfg)
    with pytest.raises(ValueError, match="access_key"):
        create_app(settings)


def test_app_factory_builds_object_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mc_server_dashboard_api.app import _build_storage
    from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage

    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "object")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ENDPOINT", "https://s3.example:9000")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__BUCKET", "mcsd")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ACCESS_KEY", "ak")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__SECRET_KEY", "sk")
    settings = load_settings(config_file=None)
    # Building the adapter does not open a connection (aioboto3 is lazy), so the
    # wiring is exercised without any real cloud.
    assert isinstance(_build_storage(settings), ObjectStorage)


def test_object_keys_from_toml_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_toml(
        tmp_path,
        '[storage]\nbackend = "object"\n'
        '[storage.object]\nendpoint = "https://s3.example:9000"\nbucket = "mcsd"\n',
    )
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ACCESS_KEY", "ak")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__SECRET_KEY", "sk")
    settings = load_settings(config_file=cfg)
    assert settings.storage.object.endpoint == "https://s3.example:9000"
    assert settings.storage.object.bucket == "mcsd"
    assert settings.storage.object.access_key == "ak"


def test_storage_keys_in_masked_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings(config_file=None)
    dump = settings.masked_dump()
    assert dump["storage"]["backend"] == "fs"
    assert dump["storage"]["fs"]["root"] == "./data"


def test_object_secret_keys_masked_in_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ENDPOINT", "https://s3.example:9000")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__BUCKET", "mcsd")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ACCESS_KEY", "ak-secret")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__SECRET_KEY", "sk-secret")
    dump = load_settings(config_file=None).masked_dump()
    obj = dump["storage"]["object"]
    # Endpoint/bucket are not secrets; access/secret keys are masked (Section 5.2).
    assert obj["endpoint"] == "https://s3.example:9000"
    assert obj["bucket"] == "mcsd"
    assert obj["access_key"] == "***"
    assert obj["secret_key"] == "***"
