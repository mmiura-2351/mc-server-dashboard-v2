"""Admin command to claim the store for this database (issue #3886).

At startup the API claims a store without an owner marker only on evidence that
the store is its database's: an empty store, ids in its keys that are rows of the
database, or, for a store of JARs and plugin blobs only, a database that is not a
fresh one. Otherwise the store may be another database's, so the API leaves it
unclaimed, and every GC and destructive sweep refuses (STORAGE.md Section 4.3).
This command is the operator's claim of such a store: it writes the marker naming
this database's installation id. It refuses when a marker already exists,
whichever installation it names, and it never deletes anything. Restart the API
afterwards: an API that left its store unclaimed at startup claims it only at its
next start.

The API's GCs then reclaim every object in the store that no row of this database
names, so run it only for the database that owns the store.

Usage::

    cd api && uv run python -m mc_server_dashboard_api.store_claim_cli

Configuration (database URL, storage backend, log level and format) is read from
the environment, the same ``MCD_API_*`` settings the API server uses, and the
command logs through the API's handler (issue #3942).

Exit status: 0 when it claimed the store; 1 when it refused because the store has
a marker; 2 when it could not tell, because the configuration, the database or
the store failed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from mc_server_dashboard_api.app import _build_storage, _resolve_config_file
from mc_server_dashboard_api.config import (
    Settings,
    describe_settings_error,
    load_settings,
)
from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    create_session_factory,
)
from mc_server_dashboard_api.core.adapters.installation import read_installation_id
from mc_server_dashboard_api.logging import configure_logging
from mc_server_dashboard_api.storage.adapters.ownership import CLAIM_COMMAND


async def run(settings: Settings) -> str | None:
    """Claim the store; why it was refused, or ``None`` once claimed."""

    engine = create_engine(settings.database.url, pool_size=1, max_overflow=0)
    try:
        installation_id = await read_installation_id(create_session_factory(engine))
    finally:
        await engine.dispose()
    _storage, ownership = _build_storage(settings)
    return await ownership.claim_explicitly(installation_id)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=CLAIM_COMMAND,
        description=(
            "Write the store owner marker naming this database's installation, on "
            "a store that has none (issue #3886)."
        ),
    )
    parser.parse_args(argv)

    try:
        settings = load_settings(_resolve_config_file())
    except (ValueError, OSError) as exc:
        # Settings that do not validate, or a config file that cannot be read: one
        # line rather than pydantic's message over several (issue #3942).
        sys.stderr.write(
            f"store claim failed: invalid configuration: "
            f"{describe_settings_error(exc)}\n"
        )
        return 2
    # The API's handler, level and format (issue #3942), so this command's lines
    # are escaped as the API's are (issue #3938).
    configure_logging(settings.log.level, settings.log.format)
    try:
        refusal = asyncio.run(run(settings))
    except Exception as exc:
        # The database or the store failed, so the command cannot tell whether
        # the store is claimed: a fault after the write (the read-back) leaves the
        # marker written, and a re-run then answers. One line, not a traceback,
        # and a status of its own, so a script tells it from a refusal.
        sys.stderr.write(f"store claim failed: {type(exc).__name__}: {exc}\n")
        return 2
    if refusal is not None:
        # The reason says whether the store is this database's already or
        # another's, so it carries no "not claimed" prefix of its own.
        sys.stderr.write(f"{refusal}\n")
        return 1
    print("store claimed; restart the API so its GCs and sweeps run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
