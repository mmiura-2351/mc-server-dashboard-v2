"""Storage adapter construction and startup guards (CONFIGURATION.md Section 5.2).

Value loading and masking are covered by the central configuration suite.
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


def test_app_factory_builds_fs_storage() -> None:
    settings = load_settings(config_file=None)
    # create_app must succeed with the default fs backend (storage bound at boot).
    app = create_app(settings)
    assert app is not None


def test_app_factory_fails_fast_on_object_without_keys(tmp_path: Path) -> None:
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


def test_app_factory_builds_object_storage(monkeypatch: pytest.MonkeyPatch) -> None:
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
