"""Persistence Ports for schedules and their run history (epic #649, #1835).

The ``ScheduleRepository`` / ``ScheduleRunRepository`` the scheduler use cases
depend on; concrete async-SQLAlchemy adapters implement them on the
unit-of-work's session. Lookups return ``None`` when absent rather than
raising, so callers decide policy (mirroring :class:`BackupRepository`).
"""

from __future__ import annotations

import abc
import datetime as dt

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
    async def list_due(self, now: dt.datetime) -> list[Schedule]:
        """Return enabled schedules whose ``next_run_at`` is at or before ``now``.

        The runner's due poll (issue #1838), backed by the partial index
        ``ix_schedule_next_run_at`` on ``(next_run_at) WHERE enabled``. A disabled
        schedule carries no ``next_run_at`` and is never returned. Ordered by
        ``next_run_at`` (id tie-break) so the poll is deterministic.
        """

    @abc.abstractmethod
    async def list_warning_candidates(
        self, now: dt.datetime, until: dt.datetime
    ) -> list[Schedule]:
        """Return enabled stop/restart schedules whose occurrence is still ahead.

        The runner's warning look-ahead (issue #1839): schedules whose next
        occurrence falls in ``(now, until]`` and whose action can carry player
        warnings (``stop`` / ``restart``). ``until`` is ``now`` plus the maximum
        warning offset, so a step's warn instant (``next_run_at - offset``) can
        only have arrived for a returned row; the runner filters to the rows
        actually carrying warning steps and decides which steps are due. Rides
        the same ``ix_schedule_next_run_at`` partial index as :meth:`list_due`.
        A past-or-present occurrence (``next_run_at <= now``) is excluded — that
        is the due poll's job, and its warnings would be firing late. Ordered by
        ``next_run_at`` (id tie-break) so the poll is deterministic.
        """

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
    async def advance_run_state(
        self,
        schedule_id: ScheduleId,
        *,
        next_run_at: dt.datetime,
        last_run_at: dt.datetime | None,
    ) -> None:
        """Persist only the runner's bookkeeping columns (issue #1838).

        A staged UPDATE of ``next_run_at`` / ``last_run_at`` guarded ``WHERE
        enabled``: the runner works on a row read before a possibly long
        execution, so writing the whole entity back would clobber a concurrent
        CRUD edit — and re-setting ``next_run_at`` on a concurrently *disabled*
        schedule would resurrect it (a disabled row keeps ``next_run_at`` NULL,
        the domain invariant). Zero rows affected means the schedule was
        disabled or deleted concurrently; the advance is silently skipped.
        Never writes name/action/payload/cadence/enabled.
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

    @abc.abstractmethod
    async def prune_for_schedule(self, schedule_id: ScheduleId, *, keep: int) -> None:
        """Delete all but the newest ``keep`` runs of ``schedule_id``.

        The runner's history cap (issue #1838): run after each insert so a
        schedule's run history stays bounded. Newest is by ``started_at`` (id
        tie-break), matching :meth:`list_for_schedule`. A no-op when the schedule
        has at most ``keep`` runs.
        """
