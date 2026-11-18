"""Whether this database names any stored object (STORAGE.md Section 4.3).

The store owner claim asks it once, at startup, of a store that has no marker yet
and already holds objects (issue #3886). A database with such rows is an existing
deployment's, upgraded to the marker, and claims its store. A fresh database, a
probe or dev stack's, names nothing there and must not claim a store whose every
object its GCs would then reclaim as an orphan.

Two tables answer it. A ``server`` row names its working set, snapshots and
version ring, and every other row that names a stored object hangs off one: a
``backup`` row its archive and a ``server_plugin`` row its plugin-cache blob (each
by a non-null foreign key to ``server``), and the server row's ``config`` its
pooled JAR. A ``resource_packs`` row, which belongs to no server, names its
uploaded pack.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.servers.adapters.models import ServerModel
from mc_server_dashboard_api.servers.adapters.resource_pack_models import (
    ResourcePackModel,
)


async def database_names_stored_objects(
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Whether a ``server`` or ``resource_packs`` row exists."""

    async with session_factory() as session:
        return (
            await session.execute(
                select(
                    or_(
                        select(ServerModel.id).exists(),
                        select(ResourcePackModel.id).exists(),
                    )
                )
            )
        ).scalar_one()
