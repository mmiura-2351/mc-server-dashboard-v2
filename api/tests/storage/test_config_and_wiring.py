"""Storage config keys + edge wiring.

References CONFIGURATION.md Section 5.2 and STORAGE.md Section 7. Covers the
defaults, TOML/env overrides, the backend selector admitting future backends, and
the app-factory fail-fast on an unimplemented backend.
"""

from __future__ import annotations

import re
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


_COMPOSE_FILE = Path(__file__).resolve().parents[3] / "compose.yaml"


def _weed_server_command() -> str:
    """The shipped ``weed server`` invocation from ``compose.yaml``'s entrypoint.

    Read out of the file rather than restated, so these pins track the shipped
    flags instead of a copy of them (the #1549 compose-pin precedent). Backslash
    line continuations inside the entrypoint are part of the one command.
    """

    text = _COMPOSE_FILE.read_text()
    match = re.search(
        r"^\s*exec weed server\b.*?(?<!\\)$", text, re.MULTILINE | re.DOTALL
    )
    assert match is not None, "compose.yaml no longer runs `weed server` for seaweedfs"
    return match.group(0)


def test_compose_keeps_seaweedfs_component_ports_off_the_network() -> None:
    """Filer/master/volume must not answer on the compose network (issue #2599).

    The worker attaches every Minecraft server container -- which runs
    community-supplied JARs, plugins and mods -- to the same compose network as
    ``seaweedfs``. The S3 gateway is credential-gated by the identities file, but the
    filer (8888), master (9333) and volume (8080) HTTP APIs are not: from that network
    they answered unauthenticated reads, writes and deletes against every community's
    prefix. ``weed server`` binds every component to ``-ip``, so pinning it to loopback
    takes those ports off the network entirely -- no second credential to distribute,
    and nothing left listening to authenticate.
    """

    assert "-ip=127.0.0.1" in _weed_server_command(), (
        "compose.yaml no longer pins `weed server -ip` to loopback; the SeaweedFS "
        "filer/master/volume ports are then reachable unauthenticated from every "
        "Minecraft server container on the compose network (issue #2599)"
    )


def test_compose_keeps_the_seaweedfs_s3_gateway_on_the_network() -> None:
    """The one credentialed door stays reachable at ``seaweedfs:8333`` (issue #2599).

    ``-ip=127.0.0.1`` alone would also unbind the S3 gateway the api and the
    ``seaweedfs-lifecycle`` one-shot dial by service name, turning a data-exposure fix
    into an outage. ``-s3.ip.bind`` re-opens exactly that listener.
    """

    assert "-s3.ip.bind=0.0.0.0" in _weed_server_command(), (
        "compose.yaml no longer binds the SeaweedFS S3 gateway to all interfaces; "
        "with `-ip=127.0.0.1` in force the api cannot reach seaweedfs:8333"
    )
