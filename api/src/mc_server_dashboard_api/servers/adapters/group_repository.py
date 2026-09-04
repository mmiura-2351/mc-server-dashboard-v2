"""Async-SQLAlchemy implementation of the ``GroupRepository`` Port (issue #276).

Works on an ``AsyncSession`` owned by the enclosing ``UnitOfWork``; it stages
rows and runs reads but never commits — commit is the unit of work's job
(DATABASE.md Section 1). Rows across ``player_group`` / ``group_player`` /
``server_group`` are translated to/from the framework-free domain aggregate here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.servers.adapters.group_models import (
    GroupPlayerModel,
    PlayerGroupModel,
    ServerGroupModel,
)
from mc_server_dashboard_api.servers.adapters.integrity import (
    translate_integrity_error,
)
from mc_server_dashboard_api.servers.domain.errors import GroupNotFoundError
from mc_server_dashboard_api.servers.domain.group_repository import GroupRepository
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId


class SqlAlchemyGroupRepository(GroupRepository):
    """:class:`GroupRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, group: PlayerGroup) -> None:
        self._session.add(
            PlayerGroupModel(
                id=group.id.value,
                community_id=group.community_id.value,
                name=group.name.value,
                kind=group.kind.value,
            )
        )
        # Flush the parent before the children: without an ORM relationship
        # SQLAlchemy does not order the INSERTs, so the group_player rows would
        # otherwise hit the FK to player_group before that row exists.
        # The flush is also the site where the group row's own constraints
        # surface, so translate them: uq_player_group_community_kind_name for a
        # concurrent create racer (issue #2000), and
        # fk_player_group_community_id_community for a community deleted since
        # the request's authorization gate read the membership (issue #2924).
        try:
            await self._session.flush()
        except IntegrityError as exc:
            translate_integrity_error(exc)
            raise
        for player in group.players:
            self._session.add(_player_model(group.id, player))

    async def get_by_id(self, group_id: GroupId) -> PlayerGroup | None:
        row = await self._session.get(PlayerGroupModel, group_id.value)
        if row is None:
            return None
        return await self._hydrate(row)

    async def get_by_community_kind_name(
        self, community_id: CommunityId, kind: GroupKind, name: GroupName
    ) -> PlayerGroup | None:
        stmt = select(PlayerGroupModel).where(
            PlayerGroupModel.community_id == community_id.value,
            PlayerGroupModel.kind == kind.value,
            PlayerGroupModel.name == name.value,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return None if row is None else await self._hydrate(row)

    async def list_for_community(self, community_id: CommunityId) -> list[PlayerGroup]:
        stmt = (
            select(PlayerGroupModel)
            .where(PlayerGroupModel.community_id == community_id.value)
            .order_by(PlayerGroupModel.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [await self._hydrate(row) for row in rows]

    async def save(self, group: PlayerGroup) -> None:
        # Re-assert the group before writing anything to it (issue #2613).
        # ``populate_existing`` forces the read to the database rather than to the
        # identity map, so this is an existence check and not an accident of what
        # the session happens to be holding. A group a racer deleted since the use
        # case's pre-read used to be reported to the caller as a successful edit
        # whenever there was nothing left to insert -- a rename of a player-less
        # group, or a removal that emptied the set -- because the writes that
        # would have violated the FK (#2583) were never staged. The read is the
        # one thing every save does, so it carries the assertion for all of them.
        row = await self._session.get(
            PlayerGroupModel, group.id.value, populate_existing=True
        )
        if row is None:
            raise GroupNotFoundError(str(group.id.value))
        row.name = group.name.value
        try:
            # Replace the player set wholesale (delete-then-insert): the in-memory
            # aggregate is the source of truth for the upsert/remove the caller made.
            # This execute autoflushes the name update above, so
            # uq_player_group_community_kind_name can surface here for a concurrent
            # rename racer (issue #2000).
            await self._session.execute(
                delete(GroupPlayerModel).where(
                    GroupPlayerModel.group_id == group.id.value
                )
            )
            for player in group.players:
                self._session.add(_player_model(group.id, player))
            # Flush the staged rows here rather than leaving them for whichever
            # autoflush the caller happens to trigger next: a concurrent group
            # delete makes them violate fk_group_player_group_id_player_group, and
            # only a flush this method owns can translate that (issue #2583).
            await self._session.flush()
        except IntegrityError as exc:
            translate_integrity_error(exc)
            raise

    async def delete(self, group_id: GroupId) -> None:
        # group_player and server_group rows cascade from the FK ON DELETE CASCADE.
        await self._session.execute(
            delete(PlayerGroupModel).where(PlayerGroupModel.id == group_id.value)
        )

    async def attach(self, group_id: GroupId, server_id: ServerId) -> None:
        # Ask PostgreSQL for the already-attached no-op rather than reading it
        # first: the read-then-stage this replaced let two concurrent attaches of
        # the same pair both pass the check, and the loser violated
        # pk_server_group at an untranslated commit (issue #2612). ON CONFLICT
        # DO NOTHING gives the loser the same silent success the pre-check gave.
        #
        # Executing the INSERT here rather than staging it also puts the two FKs
        # on a statement this method owns, so a group or server deleted since the
        # use case's pre-read is reported as the not-found that pre-read raises
        # instead of surfacing at whatever the caller does next.
        stmt = (
            pg_insert(ServerGroupModel)
            .values(group_id=group_id.value, server_id=server_id.value)
            .on_conflict_do_nothing(constraint="pk_server_group")
        )
        try:
            await self._session.execute(stmt)
        except IntegrityError as exc:
            translate_integrity_error(exc)
            raise

    async def detach(self, group_id: GroupId, server_id: ServerId) -> bool:
        if not await self.is_attached(group_id, server_id):
            return False
        await self._session.execute(
            delete(ServerGroupModel).where(
                ServerGroupModel.group_id == group_id.value,
                ServerGroupModel.server_id == server_id.value,
            )
        )
        return True

    async def is_attached(self, group_id: GroupId, server_id: ServerId) -> bool:
        row = await self._session.get(
            ServerGroupModel, (group_id.value, server_id.value)
        )
        return row is not None

    async def list_server_ids_for_group(self, group_id: GroupId) -> list[ServerId]:
        stmt = (
            select(ServerGroupModel.server_id)
            .where(ServerGroupModel.group_id == group_id.value)
            .order_by(ServerGroupModel.server_id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [ServerId(value) for value in rows]

    async def list_groups_for_server(self, server_id: ServerId) -> list[PlayerGroup]:
        rows = await self._attached_group_rows(server_id)
        return [await self._hydrate(row) for row in rows]

    async def list_groups_for_server_kind(
        self, server_id: ServerId, kind: GroupKind
    ) -> list[PlayerGroup]:
        rows = await self._attached_group_rows(server_id, kind=kind)
        return [await self._hydrate(row) for row in rows]

    async def _attached_group_rows(
        self, server_id: ServerId, *, kind: GroupKind | None = None
    ) -> list[PlayerGroupModel]:
        stmt = (
            select(PlayerGroupModel)
            .join(ServerGroupModel, ServerGroupModel.group_id == PlayerGroupModel.id)
            .where(ServerGroupModel.server_id == server_id.value)
            .order_by(PlayerGroupModel.id)
        )
        if kind is not None:
            stmt = stmt.where(PlayerGroupModel.kind == kind.value)
        return list((await self._session.execute(stmt)).scalars().all())

    async def _hydrate(self, row: PlayerGroupModel) -> PlayerGroup:
        stmt = (
            select(GroupPlayerModel)
            .where(GroupPlayerModel.group_id == row.id)
            .order_by(GroupPlayerModel.id)
        )
        player_rows = (await self._session.execute(stmt)).scalars().all()
        return PlayerGroup(
            id=GroupId(row.id),
            community_id=CommunityId(row.community_id),
            name=GroupName(row.name),
            kind=GroupKind(row.kind),
            players=[Player(p.player_uuid, p.username) for p in player_rows],
        )


def _player_model(group_id: GroupId, player: Player) -> GroupPlayerModel:
    return GroupPlayerModel(
        id=uuid.uuid4(),
        group_id=group_id.value,
        player_uuid=player.uuid,
        username=player.username,
    )
