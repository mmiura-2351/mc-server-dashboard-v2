"""This database's answers to the store owner claim (STORAGE.md Section 4.3).

The claim asks them once, at startup, of a store that has no marker yet and
already holds objects (issue #3886). Claiming a store that is another database's
would make every one of its objects an orphan to this database's GCs, so the
claim wants evidence that the store is this database's.

- **Ids in its keys.** An object under ``communities/<id>/`` belongs to a
  community and one under ``resource-packs/<id>/`` to a resource pack. The store
  is this database's only if every community and resource pack the claim sampled
  is a row here (:meth:`SqlAlchemyStoreDatabase.has`).
- **Content-addressed objects only** (JARs, plugin blobs) name no row. The claim
  then asks whether this database names any stored object
  (:meth:`SqlAlchemyStoreDatabase.names_stored_objects`), so that a fresh
  database never claims. Two tables answer it. A ``server`` row names its working
  set, snapshots and version ring, and every other row that names a stored
  object hangs off one: a ``backup`` row its archive and a ``server_plugin`` row
  its plugin-cache blob (each by a non-null foreign key to ``server``), and the
  server row's ``config`` its pooled JAR. A ``resource_packs`` row, which belongs
  to no server, names its uploaded pack.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.community.adapters.models import CommunityModel
from mc_server_dashboard_api.servers.adapters.models import ServerModel
from mc_server_dashboard_api.servers.adapters.resource_pack_models import (
    ResourcePackModel,
)


@dataclass(frozen=True)
class SqlAlchemyStoreDatabase:
    """Answer the store owner claim from this database's rows."""

    session_factory: async_sessionmaker[AsyncSession]

    async def names_stored_objects(self) -> bool:
        """Whether a ``server`` or ``resource_packs`` row exists."""

        async with self.session_factory() as session:
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

    async def has(
        self,
        *,
        communities: frozenset[uuid.UUID],
        resource_packs: frozenset[uuid.UUID],
    ) -> bool:
        """Whether every one of these communities and resource packs is a row."""

        async with self.session_factory() as session:
            found = (
                await session.execute(
                    select(
                        select(func.count())
                        .where(CommunityModel.id.in_(communities))
                        .scalar_subquery(),
                        select(func.count())
                        .where(ResourcePackModel.id.in_(resource_packs))
                        .scalar_subquery(),
                    )
                )
            ).one()
        return tuple(found) == (len(communities), len(resource_packs))
