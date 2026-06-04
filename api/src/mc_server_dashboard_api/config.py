"""Edge configuration loader (CONFIGURATION.md Sections 1-3).

Read **only** at the edge / wiring layer (ARCHITECTURE.md Section 2.1). Sources,
lowest to highest precedence: code defaults < TOML config file < ``MCD_API_``
environment variables. Secrets come from the environment and are masked in any
dump. A missing required key or an unknown key is a fatal startup error: the
loader raises rather than starting half-configured.

Only the keys needed by the current skeleton are modelled (CONFIGURATION.md
Section 5.1/5.5); the rest land with their features.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

_MASK = "***"


class _Section(BaseModel):
    # Reject unknown keys (fail-fast) and forbid mutation after load.
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerSettings(_Section):
    """HTTP + control-plane transport (CONFIGURATION.md Section 5.1)."""

    host: str = "0.0.0.0"
    http_port: int = 8000
    grpc_port: int = 50051


class ControlSettings(_Section):
    """Control-plane (Worker channel) settings (CONFIGURATION.md Section 5.1).

    ``enabled`` gates whether the API hosts the control-plane gRPC server in
    this process; ``heartbeat_timeout_seconds`` is the liveness window past
    which a Worker missing heartbeats is marked offline (FR-WRK-2). The
    ``heartbeat_interval`` the API advertises in ``RegisterAck`` is derived from
    the timeout so a Worker normally beats several times before the window
    lapses.

    ``worker_credential`` is the shared secret a Worker presents to authenticate
    its stream (NFR-SEC-1); it is the API-side counterpart of the Worker's
    ``api.credential`` (CONFIGURATION.md Section 6.1). Like the token signing key
    it is declared optional here so a process that disables the control plane
    need not supply it; the app factory fails fast when the control plane is
    enabled without a credential (Section 3, fail-fast on a missing required
    secret).
    """

    enabled: bool = True
    heartbeat_timeout_seconds: int = 30
    # Deadline for a dispatched ApiCommand to be answered by a CommandResult
    # (CONTROL_PLANE.md Section 4.2); a command unanswered within it is a typed
    # timeout the lifecycle layer treats as a failure.
    command_timeout_seconds: int = 30
    worker_credential: str | None = None


class LogSettings(_Section):
    """Observability (CONFIGURATION.md Section 5.5)."""

    level: Literal["debug", "info", "warning", "error"] = "info"
    format: Literal["json", "text"] = "json"


class DatabaseSettings(_Section):
    """Persistence (CONFIGURATION.md Section 5.2). ``url`` is a secret."""

    url: str


class StorageFsSettings(_Section):
    """Filesystem-backend settings (CONFIGURATION.md Section 5.2).

    ``root`` is the directory the ``fs`` adapter roots its tree at (STORAGE.md
    Section 2). Only read when ``storage.backend = fs``.
    """

    root: str = "./data"


class StorageSettings(_Section):
    """Storage adapter selection + tuning (CONFIGURATION.md Section 5.2).

    ``backend`` selects the :class:`Storage` Port adapter; ``fs`` is the M1 default
    and the only one implemented here. ``remote-fs`` and ``object`` are admitted by
    the selector so their adapters can be bound without a config-schema change
    (#105+); choosing one before its adapter lands fails fast at the edge.
    ``version_retention`` bounds per-file retained versions (STORAGE.md Section 5,
    the count-bounded retention knob).
    """

    backend: Literal["fs", "remote-fs", "object"] = "fs"
    fs: StorageFsSettings = Field(default_factory=StorageFsSettings)
    version_retention: int = 10


class PasswordSettings(_Section):
    """Password hashing + policy (CONFIGURATION.md Sections 5.3 and 7.1).

    ``hash`` selects the :class:`PasswordHasher` adapter (Section 4); the rest
    are the password-policy knobs enforced at registration (SECURITY.md
    Section 1). Defaults are the proven legacy baseline (Section 7.1).
    """

    hash: Literal["argon2", "bcrypt"] = "argon2"
    min_length: int = 12
    max_length: int = 128
    require_complexity: bool = True
    check_common_list: bool = True
    forbid_user_info: bool = True
    forbid_simple_patterns: bool = True


class TokenSettings(_Section):
    """Token issuing/verification (CONFIGURATION.md Section 5.3).

    Parameters of the single ``TokenService`` JWT adapter (Section 4 note):
    ``algorithm`` is HS256 by default (RS256 supported by supplying the matching
    key); ``signing_key`` is the secret used to sign access tokens. The key is
    declared optional here so the loader does not force it on processes that
    mount no auth endpoints; the app factory fails fast when it mounts the auth
    routers without a key (Section 3, fail-fast on a missing required secret).
    """

    algorithm: str = "HS256"
    signing_key: str | None = None
    access_ttl_seconds: int = 900
    refresh_ttl_seconds: int = 1209600


class BruteForceSettings(_Section):
    """Brute-force protection (CONFIGURATION.md Section 7.2).

    Per-username and per-IP failure thresholds over sliding windows, lockout with
    exponential back-off, and the artificial failure delay against timing-based
    enumeration (SECURITY.md Section 2). Defaults are the proven legacy baseline.
    """

    enabled: bool = True
    username_threshold: int = 5
    username_window_seconds: int = 900
    ip_threshold: int = 20
    ip_window_seconds: int = 300
    lockout_base_seconds: int = 900
    lockout_max_seconds: int = 86400
    delay_ms: int = 200


class ProxySettings(_Section):
    """Reverse-proxy trust (CONFIGURATION.md Section 7.3).

    Forwarded client IPs are honored only from explicitly trusted proxy peers
    (SECURITY.md Section 4); the per-IP brute-force counter depends on a
    trustworthy client IP.
    """

    trust_forwarded_headers: bool = False
    trusted_proxies: tuple[str, ...] = ()


class AuthSettings(_Section):
    """Authentication configuration (CONFIGURATION.md Section 5.3 / 7).

    The password, token, brute-force, and proxy-trust sub-groups are modelled
    here.
    """

    password: PasswordSettings = Field(default_factory=PasswordSettings)
    token: TokenSettings = Field(default_factory=TokenSettings)
    brute_force: BruteForceSettings = Field(default_factory=BruteForceSettings)
    proxy: ProxySettings = Field(default_factory=ProxySettings)


class Settings(BaseSettings):
    """The fully-resolved configuration injected into the wiring layer."""

    # Known, deliberate asymmetry: ``extra="forbid"`` fails fast on unknown TOML
    # keys, but unknown ``MCD_API_*`` env vars are silently ignored — env sources
    # in pydantic-settings only feed declared fields, so stray env vars never
    # reach this check. Accepted: the environment is shared and may legitimately
    # carry unrelated ``MCD_API_*``-prefixed names.
    model_config = SettingsConfigDict(
        env_prefix="MCD_API_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    control: ControlSettings = Field(default_factory=ControlSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    database: DatabaseSettings
    storage: StorageSettings = Field(default_factory=StorageSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Reorder so environment outranks the explicitly-passed file values
        # (init kwargs): env > TOML file > code defaults (CONFIGURATION.md
        # Section 2). dotenv / file-secret sources are unused by this loader.
        return (env_settings, init_settings)

    def masked_dump(self) -> dict[str, Any]:
        """Config snapshot safe to log: secret values replaced with ``***``."""

        auth = self.auth.model_dump()
        # The token signing key is a secret (CONFIGURATION.md Section 5.3); mask
        # it whenever present. ``None`` (no key configured) is not a secret.
        if auth["token"]["signing_key"] is not None:
            auth["token"]["signing_key"] = _MASK
        control = self.control.model_dump()
        # The Worker credential is a secret (CONFIGURATION.md Section 5.1); mask
        # it whenever present. ``None`` (control plane disabled) is not a secret.
        if control["worker_credential"] is not None:
            control["worker_credential"] = _MASK
        return {
            "server": self.server.model_dump(),
            "control": control,
            "log": self.log.model_dump(),
            "database": {"url": _MASK},
            "storage": self.storage.model_dump(),
            "auth": auth,
        }


def _read_toml(config_file: Path) -> dict[str, Any]:
    with config_file.open("rb") as handle:
        return tomllib.load(handle)


def load_settings(config_file: Path | None) -> Settings:
    """Load and validate settings with defaults < file < env precedence.

    Raises ``ValueError`` (pydantic ``ValidationError``) on a missing required
    key or an unknown key, so the caller fails fast at boot (CONFIGURATION.md
    Section 2).
    """

    file_values = _read_toml(config_file) if config_file is not None else {}
    return Settings(**file_values)
