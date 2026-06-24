"""Round-trip for the 0016 backfill of ``server.slug``.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Seeds server rows at revision 0015
(no slug column), then upgrades to 0016 and asserts every row received a
unique, valid ``<word>-<word>-<NN>`` slug; also asserts downgrade drops the
column (issue #955).
"""

from __future__ import annotations

import datetime as dt
import os
import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.migrate import (
    downgrade_base,
    downgrade_to,
    upgrade_to,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 12, 12, 0, tzinfo=dt.timezone.utc)
_SLUG_RE = re.compile(r"^[a-z]+-[a-z]+-\d{2}$")


async def test_backfill_assigns_unique_valid_slugs() -> None:
    """Server rows seeded before 0016 receive unique well-formed slugs after upgrade."""
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to("0015_backup_health", _DB_URL)

    community_id = uuid.uuid4()
    server_ids = [uuid.uuid4() for _ in range(3)]

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO community (id, name, created_at, updated_at) "
                    "VALUES (:id, :name, :at, :at)"
                ),
                {"id": community_id, "name": "guild", "at": _NOW},
            )
            for i, server_id in enumerate(server_ids):
                await conn.execute(
                    text(
                        "INSERT INTO server "
                        "(id, community_id, name, mc_edition, mc_version, "
                        "server_type, config, "
                        "desired_state, observed_state, created_at, updated_at) "
                        "VALUES (:id, :cid, :name, 'java', '1.21.1', 'vanilla', "
                        "'{}', 'stopped', 'stopped', :at, :at)"
                    ),
                    {
                        "id": server_id,
                        "cid": community_id,
                        "name": f"srv-{i}",
                        "at": _NOW,
                    },
                )
    finally:
        await engine.dispose()

    # Apply the 0016 migration which adds slug and backfills all rows.
    await upgrade_to("0016_relay_ingress", _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT id, slug FROM server WHERE community_id = :cid"),
                    {"cid": community_id},
                )
            ).fetchall()

        # Every row must have a slug.
        assert len(rows) == len(server_ids)
        slugs = [slug for _, slug in rows]

        # Every slug must be non-null and non-empty.
        assert all(slug for slug in slugs), f"blank slug found: {slugs}"

        # Every slug must match the <word>-<word>-<NN> pattern.
        for slug in slugs:
            assert _SLUG_RE.match(slug), f"slug does not match pattern: {slug!r}"

        # Slugs must be unique across the backfilled rows.
        assert len(set(slugs)) == len(slugs), f"duplicate slugs: {slugs}"

        # Downgrade drops the slug column.
        await downgrade_to("0015_backup_health", _DB_URL)

        async with engine.connect() as conn:
            columns = [
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'server'"
                        )
                    )
                ).fetchall()
            ]
        assert "slug" not in columns
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)
