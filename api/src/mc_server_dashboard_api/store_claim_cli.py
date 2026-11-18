"""Admin command to claim the store for this database (issue #3886).

At startup the API claims a store without an owner marker only if the store holds
no object or its database has rows that name stored objects. A store that holds
objects its database knows nothing of may be another database's, so the API
leaves it unclaimed, and every GC and destructive sweep refuses (STORAGE.md
Section 4.3). This command is the operator's claim of such a store: it writes the
marker naming this database's installation id. It refuses when a marker already
exists, whichever installation it names, and it never deletes anything. Restart
the API afterwards: a running API takes the marker up only at its next start.

The API's GCs then reclaim every object in the store that no row of this database
names, so run it only for the database that owns the store.

Usage::

    cd api && uv run python -m mc_server_dashboard_api.store_claim_cli

Configuration (database URL, storage backend) is read from the environment, the
same ``MCD_API_*`` settings the API server uses.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from mc_server_dashboard_api.app import _build_storage, _resolve_config_file
from mc_server_dashboard_api.config import load_settings
from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    create_session_factory,
)
from mc_server_dashboard_api.core.adapters.installation import read_installation_id
from mc_server_dashboard_api.storage.adapters.ownership import CLAIM_COMMAND


async def run() -> str | None:
    """Claim the store; why it was refused, or ``None`` once claimed."""

    settings = load_settings(_resolve_config_file())
    engine = create_engine(settings.database.url, pool_size=1, max_overflow=0)
    try:
        installation_id = await read_installation_id(create_session_factory(engine))
    finally:
        await engine.dispose()
    _storage, ownership = _build_storage(settings)
    return await ownership.claim_explicitly(installation_id)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        prog=CLAIM_COMMAND,
        description=(
            "Write the store owner marker naming this database's installation, on "
            "a store that has none (issue #3886)."
        ),
    )
    parser.parse_args(argv)

    refusal = asyncio.run(run())
    if refusal is not None:
        sys.stderr.write(f"store not claimed: {refusal}\n")
        return 1
    print("store claimed; restart the API so its GCs and sweeps run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
