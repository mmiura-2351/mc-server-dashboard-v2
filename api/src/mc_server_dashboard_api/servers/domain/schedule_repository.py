"""Persistence Ports for schedules and their run history (epic #649, #1835).

The ``ScheduleRepository`` / ``ScheduleRunRepository`` the scheduler use cases
depend on; concrete async-SQLAlchemy adapters implement them on the
unit-of-work's session. Lookups return ``None`` when absent rather than
raising, so callers decide policy (mirroring :class:`BackupRepository`).
"""

from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.schedule import (
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


@dataclass(frozen=True)
class ScheduleRef:
    """A schedule's identity, owning server and action — read without hydrating.

    What a caller needs to *authorize and delete* a schedule, as opposed to show
    or run it: the owning server (cross-scope not-found) and the action (the
    write gate's second layer). Deliberately carries nothing that can fail
    domain validation, so it resolves for a corrupt row too (issue #2712) — the
    row a quarantine (issue #2150) tells the operator to delete.
    """

    id: ScheduleId
    server_id: ServerId
    action: ScheduleAction


class ScheduleRepository(abc.ABC):
    """Port: persistence for :class:`Schedule` rows."""

    @abc.abstractmethod
    async def add(self, schedule: Schedule) -> None:
        """Stage a new schedule row for persistence within the current transaction."""

    @abc.abstractmethod
    async def get_by_id(self, schedule_id: ScheduleId) -> Schedule | None:
        """Return the schedule with ``schedule_id``, or ``None`` if absent.

        A row that fails domain validation reads as absent (issue #2712): it is
        logged and reported ``None`` rather than raising, so a corrupt row 404s
        at the edge instead of 500ing every caller. Use :meth:`get_ref` where
        the row must resolve regardless — the delete path.
        """

    @abc.abstractmethod
    async def get_ref(self, schedule_id: ScheduleId) -> ScheduleRef | None:
        """Return the schedule's :class:`ScheduleRef`, or ``None`` if absent.

        The un-hydrated lookup the delete path uses (issue #2712): identity,
        owning server and action straight off the row, with no domain
        validation, so a corrupt row the quarantine (issue #2150) disabled can
        still be authorized and deleted.
        """

    @abc.abstractmethod
    async def list_due(self, now: dt.datetime) -> list[Schedule]:
        """Return enabled schedules whose ``next_run_at`` is at or before ``now``.

        The runner's due poll (issue #1838), backed by the partial index
        ``ix_schedule_next_run_at`` on ``(next_run_at) WHERE enabled``. A disabled
        schedule carries no ``next_run_at`` and is never returned. Ordered by
        ``next_run_at`` (id tie-break) so the poll is deterministic.

        A row that fails to hydrate is quarantined (issue #2150) — disabled with
        ``next_run_at`` cleared — as a staged write, so a corrupt row does not
        stay perpetually due. The poller must commit its transaction or the
        quarantine rolls back and respools every tick.
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

        As with :meth:`list_due`, a row that fails to hydrate is quarantined
        (issue #2150) as a staged write; the poller must commit its transaction.
        """

    @abc.abstractmethod
    async def list_for_server(self, server_id: ServerId) -> list[Schedule]:
        """Return a server's schedules ordered by name.

        Community scoping is enforced by the caller, which loads the
        (community-checked) server before listing; this is keyed by
        ``server_id`` only. Names are unique per server, so the order is total.

        A row that fails domain validation is logged and omitted (issue #2712)
        rather than failing the whole listing — one corrupt row must not 500 the
        page an operator is told to go fix it on. Unlike the runner's polls this
        is a pure read: it never quarantines.
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
        fired_occurrence: dt.datetime,
        next_run_at: dt.datetime,
        last_run_at: dt.datetime | None,
    ) -> None:
        """Persist only the runner's bookkeeping columns (issue #1838).

        A staged UPDATE of ``next_run_at`` / ``last_run_at`` guarded ``WHERE
        enabled AND next_run_at = fired_occurrence``: the runner works on a row
        read before a possibly long execution, so writing the whole entity back
        would clobber a concurrent CRUD edit — and re-setting ``next_run_at`` on
        a concurrently *disabled* schedule would resurrect it (a disabled row
        keeps ``next_run_at`` NULL, the domain invariant). The CAS on
        ``next_run_at`` ensures a concurrent PATCH that changed the cadence and
        recomputed ``next_run_at`` is never overwritten by the runner's stale
        advance. Zero rows affected means the schedule was disabled, deleted, or
        edited concurrently; the advance is silently skipped.
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
