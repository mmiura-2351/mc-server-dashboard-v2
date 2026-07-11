"""Persistence Ports for schedules and their run history (epic #649, #1835).

The ``ScheduleRepository`` / ``ScheduleRunRepository`` the scheduler use cases
depend on; concrete async-SQLAlchemy adapters implement them on the
unit-of-work's session. Lookups return ``None`` when absent rather than
raising, so callers decide policy (mirroring :class:`BackupRepository`).
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.schedule import (
    Schedule,
    ScheduleId,
    ScheduleRun,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class ScheduleRepository(abc.ABC):
    """Port: persistence for :class:`Schedule` rows."""

    @abc.abstractmethod
    async def add(self, schedule: Schedule) -> None:
        """Stage a new schedule row for persistence within the current transaction."""

    @abc.abstractmethod
    async def get_by_id(self, schedule_id: ScheduleId) -> Schedule | None:
        """Return the schedule with ``schedule_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def list_for_server(self, server_id: ServerId) -> list[Schedule]:
        """Return a server's schedules ordered by name.

        Community scoping is enforced by the caller, which loads the
        (community-checked) server before listing; this is keyed by
        ``server_id`` only. Names are unique per server, so the order is total.
        """

    @abc.abstractmethod
    async def update(self, schedule: Schedule) -> None:
        """Persist the mutable fields of an existing schedule.

        A staged UPDATE within the enclosing unit of work; a missing id matches
        no row — a harmless no-op (the caller has already loaded the row).
        ``id`` / ``server_id`` / ``created_at`` / ``created_by`` never change.
        """

    @abc.abstractmethod
    async def delete(self, schedule_id: ScheduleId) -> None:
        """Delete the schedule row (its runs go with it via the FK cascade)."""


class ScheduleRunRepository(abc.ABC):
    """Port: persistence for :class:`ScheduleRun` history rows."""

    @abc.abstractmethod
    async def add(self, run: ScheduleRun) -> None:
        """Stage a new run row for persistence within the current transaction."""

    @abc.abstractmethod
    async def list_for_schedule(self, schedule_id: ScheduleId) -> list[ScheduleRun]:
        """Return a schedule's runs newest-first (by ``started_at``, id tie-break).

        Backed by the ``(schedule_id, started_at)`` index; the history cap
        (50 per schedule, epic #649) is the runner's pruning concern, not a
        query limit here.
        """
