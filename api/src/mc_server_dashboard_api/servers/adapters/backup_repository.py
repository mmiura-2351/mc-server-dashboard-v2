"""Async-SQLAlchemy implementation of the ``BackupRepository`` Port.

Works on an ``AsyncSession`` owned by the enclosing ``UnitOfWork``; it stages
rows and runs reads but never commits — commit is the unit of work's job
(DATABASE.md Section 1). Rows are translated to/from the framework-free domain
entity here.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.servers.adapters.backup_models import BackupModel
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.backup_repository import (
    BackupRepository,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


def _to_backup(row: BackupModel) -> Backup:
    return Backup(
        id=BackupId(row.id),
        server_id=ServerId(row.server_id),
        storage_ref=row.storage_ref,
        size_bytes=row.size_bytes,
        source=BackupSource(row.source),
        created_by=row.created_by,
        created_at=row.created_at,
    )


class SqlAlchemyBackupRepository(BackupRepository):
    """:class:`BackupRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, backup: Backup) -> None:
        self._session.add(
            BackupModel(
                id=backup.id.value,
                server_id=backup.server_id.value,
                storage_ref=backup.storage_ref,
                size_bytes=backup.size_bytes,
                source=backup.source.value,
                created_by=backup.created_by,
                created_at=backup.created_at,
            )
        )

    async def get_by_id(self, backup_id: BackupId) -> Backup | None:
        row = await self._session.get(BackupModel, backup_id.value)
        return _to_backup(row) if row is not None else None

    async def list_for_server(self, server_id: ServerId) -> list[Backup]:
        stmt = (
            select(BackupModel)
            .where(BackupModel.server_id == server_id.value)
            .order_by(BackupModel.created_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_backup(row) for row in rows]

    async def delete(self, backup_id: BackupId) -> None:
        stmt = delete(BackupModel).where(BackupModel.id == backup_id.value)
        await self._session.execute(stmt)
