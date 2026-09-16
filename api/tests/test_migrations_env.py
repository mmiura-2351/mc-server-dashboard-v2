"""The Alembic environment reads the same configuration sources the app does (#2953).

``migrations/env.py`` resolves the database URL through ``load_settings``, and it
must hand that loader the same config-file path the API process uses
(``MCD_API_CONFIG_FILE``, CONFIGURATION.md Section 2). Passing ``None`` there
made the ``migrate`` service ignore the file channel entirely: a deployment that
puts ``database.url`` in the TOML file got an API on one database and Alembic on
another -- or, with the key supplied *only* by file, an Alembic that failed on a
missing required key, naming nothing about the cause.

The tests drive the real ``env.py`` in offline mode with the ``alembic.context``
proxy stubbed, so what they observe is the URL the module actually configures
Alembic with rather than a re-derivation of it.
"""

from __future__ import annotations

import importlib.util
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from alembic import context

_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def _configured_url(monkeypatch: pytest.MonkeyPatch, module_name: str) -> str:
    """Execute ``migrations/env.py`` offline, returning the URL it configures.

    Offline mode is the branch that hands the URL to ``context.configure``
    directly; the online branch feeds the same ``_database_url()`` to the engine
    factory, so stubbing the cheaper of the two observes the same resolution.
    """

    captured: dict[str, Any] = {}

    monkeypatch.setattr(context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(context, "configure", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(context, "begin_transaction", nullcontext)
    monkeypatch.setattr(context, "run_migrations", lambda: None)
    # ``alembic.ini`` puts ``migrations/`` on the path (``prepend_sys_path``) so
    # ``env.py`` can import its sibling ``model_registry``; do the same here.
    monkeypatch.syspath_prepend(str(_MIGRATIONS))

    spec = importlib.util.spec_from_file_location(module_name, _MIGRATIONS / "env.py")
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(importlib.util.module_from_spec(spec))

    return str(captured["url"])


def test_config_file_supplies_the_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With ``MCD_API_CONFIG_FILE`` set, Alembic takes the file's URL.

    The environment variable is cleared so the file is the only source of
    ``database.url`` -- exactly the deployment shape the issue describes, and the
    one that previously left Alembic with no URL at all.
    """

    config_file = tmp_path / "api.toml"
    config_file.write_text(
        '[database]\nurl = "postgresql+asyncpg://file:file@filehost/filedb"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("MCD_API_DATABASE__URL", raising=False)
    monkeypatch.setenv("MCD_API_CONFIG_FILE", str(config_file))

    url = _configured_url(monkeypatch, "migrations_env_with_config_file")

    assert url == "postgresql+asyncpg://file:file@filehost/filedb"


def test_unset_config_file_leaves_the_environment_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the variable unset, the environment channel still supplies the URL."""

    monkeypatch.delenv("MCD_API_CONFIG_FILE", raising=False)
    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", "postgresql+asyncpg://env:env@envhost/envdb"
    )

    url = _configured_url(monkeypatch, "migrations_env_without_config_file")

    assert url == "postgresql+asyncpg://env:env@envhost/envdb"
