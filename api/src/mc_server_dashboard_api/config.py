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
    """HTTP transport (CONFIGURATION.md Section 5.1)."""

    host: str = "0.0.0.0"
    http_port: int = 8000


class LogSettings(_Section):
    """Observability (CONFIGURATION.md Section 5.5)."""

    level: Literal["debug", "info", "warning", "error"] = "info"
    format: Literal["json", "text"] = "json"


class DatabaseSettings(_Section):
    """Persistence (CONFIGURATION.md Section 5.2). ``url`` is a secret."""

    url: str


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


class AuthSettings(_Section):
    """Authentication configuration (CONFIGURATION.md Section 5.3 / 7).

    Only the password sub-group is modelled here; token and brute-force knobs
    land with their features.
    """

    password: PasswordSettings = Field(default_factory=PasswordSettings)


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
    log: LogSettings = Field(default_factory=LogSettings)
    database: DatabaseSettings
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

        return {
            "server": self.server.model_dump(),
            "log": self.log.model_dump(),
            "database": {"url": _MASK},
            "auth": self.auth.model_dump(),
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
