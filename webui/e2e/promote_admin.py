"""Promote a user to platform admin directly in the database.

The first platform admin cannot be granted through the (admin-gated) API, so the
deploy bootstrap does it with one SQL UPDATE (DEPLOYMENT.md Section 6). This is
that step, scripted for the E2E harness: it reads the same async DSN the API
uses (``MCD_API_DATABASE__URL``) and flips ``is_platform_admin`` for one
username. Run it via ``uv run`` from ``api/`` so it reuses the API's pinned
asyncpg — no ``psql`` binary required, so it works the same locally and in CI.
"""

import asyncio
import os
import sys

import asyncpg


async def main() -> None:
    dsn = os.environ["MCD_API_DATABASE__URL"]
    username = sys.argv[1]
    # asyncpg speaks the plain postgres scheme, not SQLAlchemy's +asyncpg form.
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        result = await conn.execute(
            'UPDATE "user" SET is_platform_admin = true WHERE username = $1',
            username,
        )
    finally:
        await conn.close()
    if result == "UPDATE 0":
        raise SystemExit(f"no user named {username!r} to promote")
    print(f"promoted {username} to platform admin")


asyncio.run(main())
