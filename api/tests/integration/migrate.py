"""Helpers to drive Alembic against a real database from integration tests.

Alembic's ``env.py`` reads the URL from the app configuration (``MCD_API_`` env),
so these set ``MCD_API_DATABASE__URL`` to the test database and invoke the
Alembic command API. Run in a worker thread because ``env.py`` calls
``asyncio.run`` internally and must not nest inside the test's event loop.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from alembic.command import downgrade, upgrade
from alembic.config import Config

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _config() -> Config:
    return Config(str(_ALEMBIC_INI))


def _run(revision: str, *, direction: str, url: str) -> None:
    os.environ["MCD_API_DATABASE__URL"] = url
    cfg = _config()
    if direction == "up":
        upgrade(cfg, revision)
    else:
        downgrade(cfg, revision)


async def upgrade_head(url: str) -> None:
    await asyncio.to_thread(_run, "head", direction="up", url=url)


async def downgrade_base(url: str) -> None:
    await asyncio.to_thread(_run, "base", direction="down", url=url)
