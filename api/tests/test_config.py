"""Tests for the edge configuration loader (CONFIGURATION.md Sections 1-3).

Precedence is defaults < TOML file < MCD_API_ env; secrets are env-only and
masked in any config dump; a missing required key fails fast at load.
"""

import json
from operator import attrgetter
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from mc_server_dashboard_api.config import Settings, load_settings


def _write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "api.toml"
    path.write_text(body)
    return path


def _section_values(settings: Settings, section: str) -> dict[str, object]:
    return cast(
        "dict[str, object]", attrgetter(section)(settings).model_dump(mode="json")
    )


# Each row exercises the same source order for one section. The env value
# deliberately differs from its TOML value so precedence has an observable pin.
_SECTION_LOADING_CASES = [
    pytest.param(
        "server",
        {"host": "0.0.0.0", "http_port": 8000, "data_plane_base_url": None},
        {"host": "127.0.0.1", "http_port": 9001},
        {"http_port": "7777"},
        {"http_port": 7777},
        id="server-bind",
    ),
    pytest.param(
        "control",
        {"stop_timeout_seconds": 600},
        {"stop_timeout_seconds": 450},
        {"stop_timeout_seconds": "300"},
        {"stop_timeout_seconds": 300},
        id="control-stop-budget",
    ),
    pytest.param(
        "log",
        {"level": "info", "format": "json"},
        {"level": "debug", "format": "text"},
        {"level": "warning"},
        {"level": "warning"},
        id="logging",
    ),
    pytest.param(
        "metrics",
        {"enabled": False, "host": "0.0.0.0", "port": 9090},
        {"enabled": True, "host": "127.0.0.1", "port": 9900},
        {"port": "9901"},
        {"port": 9901},
        id="metrics-listener",
    ),
    pytest.param(
        "database",
        {"pool_size": 5, "max_overflow": 10},
        {"pool_size": 20, "max_overflow": 0},
        {"pool_size": "30", "max_overflow": "15"},
        {"pool_size": 30, "max_overflow": 15},
        id="database-pool",
    ),
    pytest.param(
        "storage",
        {"backend": "fs", "version_retention": 10},
        {"backend": "object", "version_retention": 3},
        {"backend": "remote-fs"},
        {"backend": "remote-fs"},
        id="storage-selection",
    ),
    pytest.param(
        "storage.fs",
        {"root": "./data"},
        {"root": "/srv/mcsd-data"},
        {"root": "/data/from/env"},
        {"root": "/data/from/env"},
        id="storage-fs-root",
    ),
    pytest.param(
        "storage.object",
        {
            "endpoint": None,
            "bucket": None,
            "access_key": None,
            "secret_key": None,
            "connect_timeout_seconds": 10.0,
            "read_timeout_seconds": 60.0,
            "retry_max_attempts": 5,
        },
        {
            "endpoint": "https://s3.example:9000",
            "bucket": "mcsd",
            "connect_timeout_seconds": 5,
            "read_timeout_seconds": 120,
            "retry_max_attempts": 8,
        },
        {"read_timeout_seconds": "90"},
        {"read_timeout_seconds": 90.0},
        id="storage-object-transport",
    ),
    pytest.param(
        "snapshot",
        {"default_interval_seconds": 3600, "min_interval_seconds": 300},
        {"default_interval_seconds": 1800, "min_interval_seconds": 60},
        {"default_interval_seconds": "2000"},
        {"default_interval_seconds": 2000},
        id="snapshot-cadence",
    ),
    pytest.param(
        "schedule",
        {"tick_seconds": 20},
        {"tick_seconds": 30},
        {"tick_seconds": "40"},
        {"tick_seconds": 40},
        id="schedule-tick",
    ),
    pytest.param(
        "reconciler",
        {
            "interval_seconds": 60,
            "grace_seconds": 660,
            "held_start_grace_seconds": 90,
            "refused_stop_grace_seconds": 30,
            "backoff_base_seconds": 30,
            "backoff_max_seconds": 3600,
        },
        {
            "interval_seconds": 30,
            "grace_seconds": 90,
            "held_start_grace_seconds": 45,
            "refused_stop_grace_seconds": 20,
            "backoff_base_seconds": 15,
            "backoff_max_seconds": 1800,
        },
        {"backoff_max_seconds": "1900"},
        {"backoff_max_seconds": 1900},
        id="reconciler-cadence",
    ),
    pytest.param(
        "jar_gc",
        {"interval_seconds": 86400},
        {"interval_seconds": 1800},
        {"interval_seconds": "3600"},
        {"interval_seconds": 3600},
        id="jar-gc",
    ),
    pytest.param(
        "plugin_cache_gc",
        {"interval_seconds": 86400},
        {"interval_seconds": 1800},
        {"interval_seconds": "3600"},
        {"interval_seconds": 3600},
        id="plugin-cache-gc",
    ),
    pytest.param(
        "storage_sweep",
        {"interval_seconds": 86400},
        {"interval_seconds": 1800},
        {"interval_seconds": "3600"},
        {"interval_seconds": 3600},
        id="storage-sweep",
    ),
    pytest.param(
        "ports",
        {
            "range_start": 25565,
            "range_end": 25664,
            "bedrock_range_start": 19132,
            "bedrock_range_end": 19231,
        },
        {
            "range_start": 30000,
            "range_end": 30010,
            "bedrock_range_start": 20000,
            "bedrock_range_end": 20010,
        },
        {"range_start": "30001"},
        {"range_start": 30001},
        id="game-port-windows",
    ),
    pytest.param(
        "memory_limit",
        {"default_mb": None, "max_mb": None},
        {"default_mb": 4096, "max_mb": 16384},
        {"default_mb": "2048", "max_mb": "8192"},
        {"default_mb": 2048, "max_mb": 8192},
        id="server-memory-limits",
    ),
    pytest.param(
        "relay",
        {
            "enabled": False,
            "credential": None,
            "base_domain": None,
            "game_port": 25565,
            "tunnel_port": 25665,
            "bedrock_enabled": False,
            "bedrock_tunnel_port": 25675,
            "session_retention_days": 90,
        },
        {"bedrock_enabled": True, "bedrock_tunnel_port": 19200},
        {"bedrock_tunnel_port": "19201"},
        {"bedrock_tunnel_port": 19201},
        id="relay-bedrock",
    ),
    pytest.param(
        "auth.password",
        {"hash": "argon2", "policy": "middle", "max_length": 128},
        {"hash": "bcrypt", "policy": "low"},
        {"policy": "high"},
        {"policy": "high"},
        id="password-policy",
    ),
    pytest.param(
        "auth.token",
        {
            "algorithm": "HS256",
            "signing_key": None,
            "access_ttl_seconds": 900,
            "refresh_ttl_seconds": 1209600,
            "refresh_reuse_grace_seconds": 60,
            "refresh_cookie_name": "mcd_refresh",
            "refresh_cookie_secure": True,
        },
        {"refresh_cookie_name": "sess", "refresh_cookie_secure": False},
        {"refresh_cookie_secure": "true"},
        {"refresh_cookie_secure": True},
        id="token-cookie",
    ),
    pytest.param(
        "auth.brute_force",
        {
            "enabled": True,
            "username_threshold": 5,
            "username_window_seconds": 900,
            "ip_threshold": 20,
            "ip_window_seconds": 300,
            "lockout_base_seconds": 900,
            "lockout_max_seconds": 86400,
            "delay_ms": 200,
            "prune_interval_seconds": 3600,
        },
        {"prune_interval_seconds": 60},
        {"prune_interval_seconds": "120"},
        {"prune_interval_seconds": 120},
        id="brute-force-pruning",
    ),
    pytest.param(
        "auth.registration",
        {
            "open": True,
            "ip_limit_enabled": True,
            "ip_threshold": 5,
            "ip_window_seconds": 3600,
        },
        {"open": False, "ip_threshold": 2},
        {"open": "true"},
        {"open": True},
        id="registration",
    ),
    pytest.param(
        "auth.proxy",
        {"trust_forwarded_headers": False, "trusted_proxies": []},
        {"trust_forwarded_headers": True, "trusted_proxies": ["10.0.0.0/8"]},
        {"trust_forwarded_headers": "false"},
        {"trust_forwarded_headers": False},
        id="trusted-proxy",
    ),
]


@pytest.mark.parametrize(
    ("section", "defaults", "file_values", "env_values", "env_expected"),
    _SECTION_LOADING_CASES,
)
def test_section_loading_respects_defaults_file_and_env_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    defaults: dict[str, object],
    file_values: dict[str, object],
    env_values: dict[str, str],
    env_expected: dict[str, object],
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.delenv("MCD_API_AUTH__TOKEN__SIGNING_KEY", raising=False)
    assert set(env_values) <= set(file_values)

    settings = load_settings(config_file=None)
    actual = _section_values(settings, section)
    assert {key: actual[key] for key in defaults} == defaults

    body = f"[{section}]\n" + "".join(
        f"{key} = {json.dumps(value)}\n" for key, value in file_values.items()
    )
    config_file = _write_toml(tmp_path, body)
    settings = load_settings(config_file=config_file)
    actual = _section_values(settings, section)
    assert {key: actual[key] for key in file_values} == file_values

    prefix = "MCD_API_" + section.upper().replace(".", "__") + "__"
    for key, value in env_values.items():
        monkeypatch.setenv(prefix + key.upper(), value)
    settings = load_settings(config_file=config_file)
    actual = _section_values(settings, section)
    assert {key: actual[key] for key in env_expected} == env_expected
    assert {key: actual[key] for key in file_values.keys() - env_values.keys()} == {
        key: value for key, value in file_values.items() if key not in env_values
    }


# Values immediately outside each declared scalar bound, plus unsupported
# selectors. Cross-field rules have separate cases below with valid scalars.
_SCALAR_INVALID_CASES = [
    pytest.param("server", "http_port = -1", "http_port", id="http-port-negative"),
    pytest.param("server", "http_port = 65536", "http_port", id="http-port-too-large"),
    pytest.param("server", "grpc_port = -1", "grpc_port", id="grpc-port-negative"),
    pytest.param("server", "grpc_port = 65536", "grpc_port", id="grpc-port-too-large"),
    pytest.param(
        "control",
        "heartbeat_timeout_seconds = 0",
        "heartbeat_timeout_seconds",
        id="heartbeat-timeout-zero",
    ),
    pytest.param(
        "control",
        "command_timeout_seconds = 0",
        "command_timeout_seconds",
        id="command-timeout-zero",
    ),
    pytest.param(
        "control",
        "hydrate_timeout_seconds = 0",
        "hydrate_timeout_seconds",
        id="hydrate-timeout-zero",
    ),
    pytest.param(
        "control",
        "snapshot_timeout_seconds = 0",
        "snapshot_timeout_seconds",
        id="snapshot-timeout-zero",
    ),
    pytest.param(
        "control",
        "stop_timeout_seconds = 0",
        "stop_timeout_seconds",
        id="stop-timeout-zero",
    ),
    pytest.param("log", 'level = "trace"', "level", id="unknown-log-level"),
    pytest.param("log", 'format = "yaml"', "format", id="unknown-log-format"),
    pytest.param("metrics", "port = -1", "port", id="metrics-port-negative"),
    pytest.param("metrics", "port = 65536", "port", id="metrics-port-too-large"),
    pytest.param(
        "database", "pool_size = 0", "pool_size", id="unbounded-database-pool"
    ),
    pytest.param(
        "database", "max_overflow = -1", "max_overflow", id="negative-database-overflow"
    ),
    pytest.param(
        "storage", 'backend = "s3-but-typo"', "backend", id="unknown-storage-backend"
    ),
    pytest.param(
        "storage",
        "version_retention = -1",
        "version_retention",
        id="negative-version-retention",
    ),
    pytest.param(
        "storage.object",
        "connect_timeout_seconds = 0",
        "connect_timeout_seconds",
        id="zero-object-connect-timeout",
    ),
    pytest.param(
        "storage.object",
        "read_timeout_seconds = 0",
        "read_timeout_seconds",
        id="zero-object-read-timeout",
    ),
    pytest.param(
        "storage.object",
        "retry_max_attempts = 0",
        "retry_max_attempts",
        id="zero-object-attempts",
    ),
    pytest.param(
        "snapshot",
        "default_interval_seconds = 0",
        "default_interval_seconds",
        id="zero-default-snapshot-interval",
    ),
    pytest.param(
        "snapshot",
        "min_interval_seconds = 0",
        "min_interval_seconds",
        id="zero-min-snapshot-interval",
    ),
    pytest.param(
        "schedule", "tick_seconds = 0", "tick_seconds", id="zero-schedule-tick"
    ),
    # A tick above the runner's 300 s late-run grace makes ordinary runs stale.
    pytest.param(
        "schedule",
        "tick_seconds = 301",
        "tick_seconds",
        id="schedule-tick-exceeds-grace",
    ),
    pytest.param(
        "reconciler",
        "interval_seconds = 0",
        "interval_seconds",
        id="zero-reconcile-interval",
    ),
    pytest.param(
        "reconciler", "grace_seconds = 0", "grace_seconds", id="zero-reconcile-grace"
    ),
    pytest.param(
        "reconciler",
        "held_start_grace_seconds = 0",
        "held_start_grace_seconds",
        id="zero-held-start-grace",
    ),
    pytest.param(
        "reconciler",
        "refused_stop_grace_seconds = 0",
        "refused_stop_grace_seconds",
        id="zero-refused-stop-grace",
    ),
    pytest.param(
        "reconciler",
        "backoff_base_seconds = 0",
        "backoff_base_seconds",
        id="zero-backoff-base",
    ),
    pytest.param(
        "reconciler",
        "backoff_max_seconds = 0",
        "backoff_max_seconds",
        id="zero-backoff-max",
    ),
    pytest.param(
        "jar_gc", "interval_seconds = 0", "interval_seconds", id="zero-jar-gc-interval"
    ),
    pytest.param(
        "plugin_cache_gc",
        "interval_seconds = 0",
        "interval_seconds",
        id="zero-plugin-cache-gc-interval",
    ),
    pytest.param(
        "storage_sweep",
        "interval_seconds = 0",
        "interval_seconds",
        id="zero-storage-sweep-interval",
    ),
    pytest.param("ports", "range_start = 0", "range_start", id="zero-game-range-start"),
    pytest.param(
        "ports", "range_start = 65536", "range_start", id="game-range-start-too-large"
    ),
    pytest.param("ports", "range_end = 0", "range_end", id="zero-game-range-end"),
    pytest.param(
        "ports", "range_end = 65536", "range_end", id="game-range-end-too-large"
    ),
    pytest.param(
        "ports",
        "bedrock_range_start = 0",
        "bedrock_range_start",
        id="zero-bedrock-range-start",
    ),
    pytest.param(
        "ports",
        "bedrock_range_start = 65536",
        "bedrock_range_start",
        id="bedrock-range-start-too-large",
    ),
    pytest.param(
        "ports",
        "bedrock_range_end = 0",
        "bedrock_range_end",
        id="zero-bedrock-range-end",
    ),
    pytest.param(
        "ports",
        "bedrock_range_end = 65536",
        "bedrock_range_end",
        id="bedrock-range-end-too-large",
    ),
    pytest.param("relay", "game_port = 0", "game_port", id="zero-relay-game-port"),
    pytest.param(
        "relay", "game_port = 65536", "game_port", id="relay-game-port-too-large"
    ),
    pytest.param(
        "relay", "tunnel_port = 0", "tunnel_port", id="zero-relay-tunnel-port"
    ),
    pytest.param(
        "relay", "tunnel_port = 65536", "tunnel_port", id="relay-tunnel-port-too-large"
    ),
    pytest.param(
        "relay",
        "bedrock_tunnel_port = 0",
        "bedrock_tunnel_port",
        id="zero-bedrock-tunnel-port",
    ),
    pytest.param(
        "relay",
        "bedrock_tunnel_port = 65536",
        "bedrock_tunnel_port",
        id="bedrock-tunnel-port-too-large",
    ),
    pytest.param(
        "relay",
        "session_retention_days = 0",
        "session_retention_days",
        id="zero-session-retention",
    ),
    pytest.param(
        "auth.password", 'hash = "scrypt"', "hash", id="unknown-password-hash"
    ),
    pytest.param(
        "auth.password", 'policy = "paranoid"', "policy", id="unknown-password-policy"
    ),
    pytest.param(
        "auth.password", "max_length = 0", "max_length", id="zero-password-max-length"
    ),
    pytest.param(
        "auth.token", 'algorithm = "hs256"', "algorithm", id="miscased-token-algorithm"
    ),
    pytest.param(
        "auth.token",
        "access_ttl_seconds = 0",
        "access_ttl_seconds",
        id="zero-access-ttl",
    ),
    pytest.param(
        "auth.token",
        "refresh_ttl_seconds = 0",
        "refresh_ttl_seconds",
        id="zero-refresh-ttl",
    ),
    pytest.param(
        "auth.token",
        "refresh_reuse_grace_seconds = 0",
        "refresh_reuse_grace_seconds",
        id="zero-refresh-grace",
    ),
    pytest.param(
        "auth.token",
        'refresh_cookie_name = ""',
        "refresh_cookie_name",
        id="blank-refresh-cookie-name",
    ),
    pytest.param(
        "auth.token",
        "download_grant_ttl_seconds = 0",
        "download_grant_ttl_seconds",
        id="zero-download-grant-ttl",
    ),
    pytest.param(
        "auth.token",
        "download_cookie_ttl_seconds = 0",
        "download_cookie_ttl_seconds",
        id="zero-download-cookie-ttl",
    ),
    pytest.param(
        "auth.brute_force",
        "username_threshold = 0",
        "username_threshold",
        id="zero-username-threshold",
    ),
    pytest.param(
        "auth.brute_force",
        "username_window_seconds = 0",
        "username_window_seconds",
        id="zero-username-window",
    ),
    pytest.param(
        "auth.brute_force", "ip_threshold = 0", "ip_threshold", id="zero-ip-threshold"
    ),
    pytest.param(
        "auth.brute_force",
        "ip_window_seconds = 0",
        "ip_window_seconds",
        id="zero-ip-window",
    ),
    pytest.param(
        "auth.brute_force",
        "lockout_base_seconds = 0",
        "lockout_base_seconds",
        id="zero-lockout-base",
    ),
    pytest.param(
        "auth.brute_force",
        "lockout_max_seconds = 0",
        "lockout_max_seconds",
        id="zero-lockout-max",
    ),
    pytest.param(
        "auth.brute_force", "delay_ms = -1", "delay_ms", id="negative-login-delay"
    ),
    pytest.param(
        "auth.brute_force",
        "prune_interval_seconds = 0",
        "prune_interval_seconds",
        id="zero-login-prune-interval",
    ),
    pytest.param(
        "auth.registration",
        "ip_threshold = 0",
        "ip_threshold",
        id="zero-registration-threshold",
    ),
    pytest.param(
        "auth.registration",
        "ip_window_seconds = 0",
        "ip_window_seconds",
        id="zero-registration-window",
    ),
]


@pytest.mark.parametrize(("section", "assignment", "field"), _SCALAR_INVALID_CASES)
def test_scalar_setting_rejects_invalid_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    assignment: str,
    field: str,
) -> None:
    # These values would disable a guard, admit an impossible port, or select an
    # unsupported adapter. Each must fail during loading, before app startup.
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    config_file = _write_toml(tmp_path, f"[{section}]\n{assignment}\n")
    with pytest.raises(ValidationError, match=field):
        load_settings(config_file=config_file)


_SCALAR_VALID_CASES = [
    pytest.param("server", "http_port = 0", "http_port", 0, id="ephemeral-http-port"),
    pytest.param("server", "grpc_port = 0", "grpc_port", 0, id="ephemeral-grpc-port"),
    pytest.param("metrics", "port = 0", "port", 0, id="ephemeral-metrics-port"),
    pytest.param(
        "storage",
        "version_retention = 0",
        "version_retention",
        0,
        id="no-retained-file-versions",
    ),
    pytest.param(
        "database", "max_overflow = 0", "max_overflow", 0, id="no-database-overflow"
    ),
    pytest.param(
        "auth.brute_force",
        "username_threshold = 1",
        "username_threshold",
        1,
        id="first-username-attempt",
    ),
    pytest.param(
        "auth.brute_force",
        "username_window_seconds = 1",
        "username_window_seconds",
        1,
        id="one-second-username-window",
    ),
    pytest.param(
        "auth.brute_force", "ip_threshold = 1", "ip_threshold", 1, id="first-ip-attempt"
    ),
    pytest.param(
        "auth.brute_force",
        "ip_window_seconds = 1",
        "ip_window_seconds",
        1,
        id="one-second-ip-window",
    ),
    pytest.param(
        "auth.brute_force",
        "lockout_base_seconds = 1",
        "lockout_base_seconds",
        1,
        id="one-second-lockout-base",
    ),
    pytest.param(
        "auth.brute_force",
        "lockout_base_seconds = 1\nlockout_max_seconds = 1",
        "lockout_max_seconds",
        1,
        id="fixed-one-second-lockout",
    ),
    pytest.param(
        "auth.brute_force", "delay_ms = 0", "delay_ms", 0, id="disabled-login-delay"
    ),
    pytest.param(
        "auth.token", 'algorithm = "RS256"', "algorithm", "RS256", id="rs256-selector"
    ),
    pytest.param(
        "auth.token",
        'refresh_cookie_name = "s"',
        "refresh_cookie_name",
        "s",
        id="one-character-cookie-name",
    ),
    pytest.param(
        "schedule",
        "tick_seconds = 300",
        "tick_seconds",
        300,
        id="schedule-tick-at-grace",
    ),
    pytest.param(
        "ports",
        "range_start = 1\nrange_end = 1",
        "range_start",
        1,
        id="single-lowest-game-port",
    ),
    pytest.param(
        "ports",
        "bedrock_range_start = 65535\nbedrock_range_end = 65535",
        "bedrock_range_end",
        65535,
        id="single-highest-bedrock-port",
    ),
]


@pytest.mark.parametrize(
    ("section", "assignment", "field", "expected"), _SCALAR_VALID_CASES
)
def test_scalar_setting_accepts_valid_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    assignment: str,
    field: str,
    expected: object,
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    config_file = _write_toml(tmp_path, f"[{section}]\n{assignment}\n")
    assert (
        _section_values(load_settings(config_file=config_file), section)[field]
        == expected
    )


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


@pytest.mark.parametrize(
    ("section", "field"),
    [
        pytest.param("auth.token", "signing_key", id="token-signing-key"),
        pytest.param("control", "worker_credential", id="worker-credential"),
        pytest.param("control.tls", "cert_file", id="control-tls-certificate"),
        pytest.param("control.tls", "key_file", id="control-tls-private-key"),
        pytest.param("relay", "credential", id="relay-credential"),
        pytest.param("relay", "base_domain", id="relay-base-domain"),
    ],
)
@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
def test_blank_optional_credentials_and_paths_normalize_to_missing(
    monkeypatch: pytest.MonkeyPatch, section: str, field: str, blank: str
) -> None:
    # Compose can interpolate an unset variable as an empty string. The app's
    # required-when-enabled guards must see the same None as a missing value.
    key = "MCD_API_" + section.upper().replace(".", "__") + "__" + field.upper()
    monkeypatch.setenv(key, blank)
    settings = load_settings(config_file=None)
    assert _section_values(settings, section)[field] is None


def test_storage_object_values_load_from_file_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_toml(
        tmp_path,
        '[storage]\nbackend = "object"\n'
        '[storage.object]\nendpoint = "https://s3.example:9000"\nbucket = "mcsd"\n',
    )
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ACCESS_KEY", "ak")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__SECRET_KEY", "sk")
    settings = load_settings(config_file=config_file)
    assert settings.storage.backend == "object"
    assert settings.storage.object.endpoint == "https://s3.example:9000"
    assert settings.storage.object.bucket == "mcsd"
    assert settings.storage.object.access_key == "ak"
    assert settings.storage.object.secret_key == "sk"


def test_relay_values_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_RELAY__ENABLED", "true")
    monkeypatch.setenv("MCD_API_RELAY__CREDENTIAL", "relay-secret")
    monkeypatch.setenv("MCD_API_RELAY__BASE_DOMAIN", "mc.example.com")
    settings = load_settings(config_file=None)
    assert settings.relay.enabled is True
    assert settings.relay.credential == "relay-secret"
    assert settings.relay.base_domain == "mc.example.com"


_SECRET_PATHS = {
    ("database", "url"),
    ("control", "worker_credential"),
    ("control", "tls", "key_file"),
    ("storage", "object", "access_key"),
    ("storage", "object", "secret_key"),
    ("auth", "token", "signing_key"),
    ("relay", "credential"),
}


def _assert_masked_parity(
    source: dict[str, object], masked: dict[str, object], path: tuple[str, ...] = ()
) -> None:
    assert source.keys() == masked.keys(), path
    for key, value in source.items():
        field_path = (*path, key)
        observed = masked[key]
        if isinstance(value, dict):
            assert isinstance(observed, dict), field_path
            _assert_masked_parity(value, observed, field_path)
        elif field_path in _SECRET_PATHS and value is not None:
            assert observed == "***", field_path
        else:
            assert observed == value, field_path


@pytest.mark.parametrize("with_secrets", [False, True], ids=["unset", "configured"])
def test_masked_dump_recursively_matches_settings(
    monkeypatch: pytest.MonkeyPatch, with_secrets: bool
) -> None:
    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", "postgresql+asyncpg://user:hunter2pw@host/db"
    )
    monkeypatch.delenv("MCD_API_AUTH__TOKEN__SIGNING_KEY", raising=False)
    monkeypatch.setenv("MCD_API_DATABASE__POOL_SIZE", "10")
    monkeypatch.setenv("MCD_API_DATABASE__MAX_OVERFLOW", "20")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ENDPOINT", "https://s3.example:9000")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__BUCKET", "mcsd")
    sentinels = ["hunter2pw"]
    if with_secrets:
        secrets = {
            "MCD_API_CONTROL__WORKER_CREDENTIAL": "worker-secret-sentinel",
            "MCD_API_CONTROL__TLS__KEY_FILE": "/private/key-sentinel",
            "MCD_API_STORAGE__OBJECT__ACCESS_KEY": "access-key-sentinel",
            "MCD_API_STORAGE__OBJECT__SECRET_KEY": "secret-key-sentinel",
            "MCD_API_AUTH__TOKEN__SIGNING_KEY": "signing-key-sentinel-of-32-bytes!",
            "MCD_API_RELAY__CREDENTIAL": "relay-secret-sentinel",
        }
        for key, value in secrets.items():
            monkeypatch.setenv(key, value)
            sentinels.append(value)
    settings = load_settings(config_file=None)
    dump = settings.masked_dump()
    _assert_masked_parity(settings.model_dump(), dump)
    assert all(sentinel not in repr(dump) for sentinel in sentinels)


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


def test_unknown_key_in_toml_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[server]\nbogus_key = 1\n")
    with pytest.raises(ValueError):
        load_settings(config_file=cfg)


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


# --- [memory_limit] section (issue #1069) ---


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


# --- masked_dump completeness (issue #1993) ---


def test_storage_sweep_rejects_unknown_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@h/db")
    cfg = _write_toml(tmp_path, "[storage_sweep]\nbogus_key = 1\n")
    with pytest.raises(ValueError):
        load_settings(config_file=cfg)
