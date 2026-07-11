"""Async-SQLAlchemy implementations of the schedule repository Ports (#1835).

Work on an ``AsyncSession`` owned by the enclosing ``UnitOfWork``; they stage
rows and run reads but never commit — commit is the unit of work's job
(DATABASE.md Section 1). Rows are translated to/from the framework-free domain
entities here, including the ``payload`` jsonb: the ``Schedule`` entity carries
the per-action payload as typed fields (``command`` / ``warning_steps``), which
serialize to ``{"command": ...}`` / ``{"warnings": [...]}`` / ``{}``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.servers.adapters.schedule_models import (
    ScheduleModel,
    ScheduleRunModel,
)
from mc_server_dashboard_api.servers.domain.schedule import (
    Cadence,
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
    ScheduleRunId,
    ScheduleRunOutcome,
    WarningStep,
)
from mc_server_dashboard_api.servers.domain.schedule_repository import (
    ScheduleRepository,
    ScheduleRunRepository,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


def _payload_json(schedule: Schedule) -> dict[str, Any]:
    if schedule.command is not None:
        return {"command": schedule.command}
    if schedule.warning_steps:
        return {
            "warnings": [
                {"offset_minutes": step.offset_minutes, "message": step.message}
                for step in schedule.warning_steps
            ]
        }
    return {}


def _warning_steps(payload: dict[str, Any]) -> tuple[WarningStep, ...]:
    return tuple(
        WarningStep(offset_minutes=step["offset_minutes"], message=step["message"])
        for step in payload.get("warnings", ())
    )


def _to_schedule(row: ScheduleModel) -> Schedule:
    return Schedule(
        id=ScheduleId(row.id),
        server_id=ServerId(row.server_id),
        name=row.name,
        action=ScheduleAction(row.action),
        cadence=Cadence(cron=row.cron, interval_seconds=row.interval_seconds),
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        timezone=row.timezone,
        command=row.payload.get("command"),
        warning_steps=_warning_steps(row.payload),
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        created_by=row.created_by,
    )


class SqlAlchemyScheduleRepository(ScheduleRepository):
    """:class:`ScheduleRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, schedule: Schedule) -> None:
        self._session.add(
            ScheduleModel(
                id=schedule.id.value,
                server_id=schedule.server_id.value,
                name=schedule.name,
                action=schedule.action.value,
                payload=_payload_json(schedule),
                cron=schedule.cadence.cron,
                interval_seconds=schedule.cadence.interval_seconds,
                timezone=schedule.timezone,
                enabled=schedule.enabled,
                next_run_at=schedule.next_run_at,
                last_run_at=schedule.last_run_at,
                created_by=schedule.created_by,
                created_at=schedule.created_at,
                updated_at=schedule.updated_at,
            )
        )

    async def get_by_id(self, schedule_id: ScheduleId) -> Schedule | None:
        row = await self._session.get(ScheduleModel, schedule_id.value)
        return _to_schedule(row) if row is not None else None

    async def list_for_server(self, server_id: ServerId) -> list[Schedule]:
        stmt = (
            select(ScheduleModel)
            .where(ScheduleModel.server_id == server_id.value)
            .order_by(ScheduleModel.name)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_schedule(row) for row in rows]

    async def update(self, schedule: Schedule) -> None:
        # A staged UPDATE of the mutable fields; commit is the unit of work's
        # job. A missing id matches no row — a harmless no-op.
        stmt = (
            update(ScheduleModel)
            .where(ScheduleModel.id == schedule.id.value)
            .values(
                name=schedule.name,
                action=schedule.action.value,
                payload=_payload_json(schedule),
                cron=schedule.cadence.cron,
                interval_seconds=schedule.cadence.interval_seconds,
                timezone=schedule.timezone,
                enabled=schedule.enabled,
                next_run_at=schedule.next_run_at,
                last_run_at=schedule.last_run_at,
                updated_at=schedule.updated_at,
            )
        )
        await self._session.execute(stmt)

    async def delete(self, schedule_id: ScheduleId) -> None:
        stmt = delete(ScheduleModel).where(ScheduleModel.id == schedule_id.value)
        await self._session.execute(stmt)


def _to_run(row: ScheduleRunModel) -> ScheduleRun:
    return ScheduleRun(
        id=ScheduleRunId(row.id),
        schedule_id=ScheduleId(row.schedule_id),
        started_at=row.started_at,
        finished_at=row.finished_at,
        outcome=ScheduleRunOutcome(row.outcome),
        detail=row.detail,
    )


class SqlAlchemyScheduleRunRepository(ScheduleRunRepository):
    """:class:`ScheduleRunRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: ScheduleRun) -> None:
        self._session.add(
            ScheduleRunModel(
                id=run.id.value,
                schedule_id=run.schedule_id.value,
                started_at=run.started_at,
                finished_at=run.finished_at,
                outcome=run.outcome.value,
                detail=run.detail,
            )
        )

    async def list_for_schedule(self, schedule_id: ScheduleId) -> list[ScheduleRun]:
        stmt = (
            select(ScheduleRunModel)
            .where(ScheduleRunModel.schedule_id == schedule_id.value)
            .order_by(ScheduleRunModel.started_at.desc(), ScheduleRunModel.id.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_run(row) for row in rows]
