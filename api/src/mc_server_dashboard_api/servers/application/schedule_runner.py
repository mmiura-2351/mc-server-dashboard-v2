"""The general-scheduler runner (epic #649, issue #1838).

:class:`RunScheduleTick` is one pass of the scheduler's lifespan loop: it polls
the schedules that are due (``enabled AND next_run_at <= now``, plus any pending
backup retry) and executes each through :class:`ExecuteScheduleAction`, recording
a run-history row, advancing ``next_run_at``, auditing, and — on a genuine
failure — publishing an operator notification. It dispatches only through the
existing lifecycle / command / backup use cases (the epic's "no new execution
route" constraint); the mapping and the precondition/outcome classification live
in :class:`ExecuteScheduleAction`, which is kept free of any time-trigger concept
so the deferred crash-restart policy (#653) can reuse it.

Outcome taxonomy (owner-confirmed):

* **skipped** — the action's precondition was unmet: a ``command`` / ``stop`` /
  ``restart`` on a server that is not settled-running, a ``start`` on a server
  that is not at rest, or any action while the server is transitional. Recorded
  as an honest history row, *not* notified (nothing was attempted).
* **failure** — the dispatch reached the Worker / use case and was refused or the
  Worker was unavailable. Recorded, audited, and notified.
* **success** — otherwise.

``next_run_at`` advances to the first occurrence strictly after ``now`` on every
fired occurrence (success, failure, or skip) — there is no tick-level retry.

Missed-run semantics: a due occurrence is *overdue* when the occurrence strictly
after its stored ``next_run_at`` is itself already past — i.e. a whole further
period elapsed while the loop was down (an API restart). A ``backup`` still fires
exactly once (advancing past ``now`` coalesces the missed occurrences into one
catch-up); ``command`` / ``start`` / ``stop`` / ``restart`` do *not* fire late —
``next_run_at`` is advanced past ``now`` with no execution and no run row.

Backup-only bounded retry: a failed ``backup`` occurrence arms one in-memory
retry ~30 minutes later (lost on restart — accepted). The retry records (and, on
failure, notifies) but never moves ``next_run_at``; it fires at most once, then
the schedule waits for its next occurrence.

One schedule's exception never stops the tick — each is isolated and logged.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field, replace

from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.operations import (
    SCHEDULE_RUN,
    TARGET_SCHEDULE,
)
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.servers.application.backups import CreateBackup
from mc_server_dashboard_api.servers.application.lifecycle import (
    RestartServer,
    SendServerCommand,
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.domain.backup import BackupSource
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import WorkerUnavailableError
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupUnsettledError,
    CommandDispatchError,
    InvalidLifecycleTransitionError,
    LifecycleTransitionConflictError,
    NoEligibleWorkerError,
    ServerError,
    ServerNotRunningError,
)
from mc_server_dashboard_api.servers.domain.next_run_calculator import NextRunCalculator
from mc_server_dashboard_api.servers.domain.notifier import ServerNotifier
from mc_server_dashboard_api.servers.domain.schedule import (
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
    ScheduleRunId,
    ScheduleRunOutcome,
    next_interval_run,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
)

_LOG = logging.getLogger(__name__)

# The run-history cap per schedule (epic #649): pruned after every insert.
_HISTORY_CAP = 50
# One bounded retry ~30 minutes after a failed backup occurrence (owner-confirmed).
_BACKUP_RETRY_DELAY = dt.timedelta(minutes=30)
# The notification discriminator a client routes on (issue #1836 payload).
_NOTIFY_KIND = "schedule_failed"

# Use-case exceptions that mean the action's precondition was unmet at dispatch
# time (a state change raced the runner's pre-check): classified as a skip, not a
# failure, so no spurious notification fires.
_SKIP_ERRORS = (
    InvalidLifecycleTransitionError,
    LifecycleTransitionConflictError,
    ServerNotRunningError,
    BackupUnsettledError,
)


def _is_running(server: Server) -> bool:
    """Whether ``server`` is settled-running (desired+observed running, assigned)."""

    return (
        server.desired_state is DesiredState.RUNNING
        and server.observed_state is ObservedState.RUNNING
        and server.assigned_worker_id is not None
    )


def _precondition_skip(server: Server, action: ScheduleAction) -> str | None:
    """Return a skip reason if ``action``'s precondition is unmet, else ``None``.

    ``command`` / ``stop`` / ``restart`` need a settled-running server; ``start``
    needs an at-rest one; ``backup`` needs either (only a *transitional* server is
    unsettled, matching :class:`CreateBackup`'s own gate). Every other case — the
    server mid-transition — is a skip, so a scheduled action never lands on a
    server that is starting, stopping, restarting, or crashed.
    """

    if action is ScheduleAction.START:
        return None if server.is_at_rest() else "server not stopped"
    if action is ScheduleAction.BACKUP:
        if _is_running(server) or server.is_at_rest():
            return None
        return "server transitional"
    return None if _is_running(server) else "server not running"


def _skip_detail(exc: ServerError) -> str:
    """Sanitized skip reason for a precondition exception raced at dispatch."""

    if isinstance(exc, ServerNotRunningError):
        return "server not running"
    if isinstance(exc, BackupUnsettledError):
        return "server transitional"
    return "precondition changed"


def _failure_detail(exc: ServerError) -> str:
    """Sanitized failure note for the run row — never the raw Worker message.

    Mirrors the ``command_dispatch`` posture: the raw Worker text stays in logs;
    the recorded ``detail`` is a stable category (a ``CommandDispatchError``'s
    sanitized ``reason`` where present, or a coarse fallback).
    """

    if isinstance(exc, CommandDispatchError):
        return exc.reason or "dispatch failed"
    if isinstance(exc, WorkerUnavailableError):
        return "worker unavailable"
    if isinstance(exc, NoEligibleWorkerError):
        return "no eligible worker"
    return "action failed"


@dataclass(frozen=True)
class ActionResult:
    """The classified outcome of one :class:`ExecuteScheduleAction` call.

    ``community_id`` is the scope of the acted-on server (``None`` only when the
    server has vanished), carried so the runner can scope the audit entry without
    re-loading the row.
    """

    outcome: ScheduleRunOutcome
    detail: str | None
    community_id: CommunityId | None


@dataclass(frozen=True)
class ExecuteScheduleAction:
    """Map a schedule action to its existing use case and classify the outcome.

    Deliberately time-trigger-free: it takes a server + action + command line, not
    a :class:`Schedule`, so a non-schedule caller (the deferred crash-restart
    policy, #653) can reuse the identical dispatch + precondition + outcome logic.
    It loads the server once to evaluate the precondition and to source the
    community scope the underlying use cases require.
    """

    uow: UnitOfWork
    send_command: SendServerCommand
    start_server: StartServer
    stop_server: StopServer
    restart_server: RestartServer
    create_backup: CreateBackup

    async def __call__(
        self, *, server_id: ServerId, action: ScheduleAction, command: str | None
    ) -> ActionResult:
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
        if server is None:
            # The FK cascade removes a schedule with its server, so a missing
            # server here is a rare race; skip quietly rather than notify.
            return ActionResult(ScheduleRunOutcome.SKIPPED, "server not found", None)
        community_id = server.community_id
        reason = _precondition_skip(server, action)
        if reason is not None:
            return ActionResult(ScheduleRunOutcome.SKIPPED, reason, community_id)
        try:
            await self._dispatch(server, action, command)
        except _SKIP_ERRORS as exc:
            return ActionResult(
                ScheduleRunOutcome.SKIPPED, _skip_detail(exc), community_id
            )
        except ServerError as exc:
            _LOG.warning(
                "scheduled %s for server %s failed: %r",
                action.value,
                server_id.value,
                exc,
            )
            return ActionResult(
                ScheduleRunOutcome.FAILURE, _failure_detail(exc), community_id
            )
        return ActionResult(ScheduleRunOutcome.SUCCESS, None, community_id)

    async def _dispatch(
        self, server: Server, action: ScheduleAction, command: str | None
    ) -> None:
        community_id = server.community_id
        server_id = server.id
        if action is ScheduleAction.COMMAND:
            # A command schedule always carries a line (the Schedule entity
            # invariant), so this narrows cleanly for the typed call below.
            assert command is not None
            await self.send_command(
                community_id=community_id, server_id=server_id, line=command
            )
        elif action is ScheduleAction.START:
            await self.start_server(community_id=community_id, server_id=server_id)
        elif action is ScheduleAction.STOP:
            await self.stop_server(community_id=community_id, server_id=server_id)
        elif action is ScheduleAction.RESTART:
            await self.restart_server(community_id=community_id, server_id=server_id)
        else:  # ScheduleAction.BACKUP
            await self.create_backup(
                community_id=community_id,
                server_id=server_id,
                source=BackupSource.SCHEDULED,
                created_by=None,
            )


@dataclass
class RunScheduleTick:
    """One pass of the scheduler loop (issue #1838).

    Not frozen: it owns the in-memory backup-retry map across ticks. A single
    instance is reused for the lifetime of the lifespan loop.
    """

    uow: UnitOfWork
    execute: ExecuteScheduleAction
    calculator: NextRunCalculator
    audit: AuditRecorder
    notifier: ServerNotifier
    clock: Clock
    history_cap: int = _HISTORY_CAP
    backup_retry_delay: dt.timedelta = _BACKUP_RETRY_DELAY
    # Per-schedule pending backup-retry instant; in-memory (lost on restart).
    _backup_retry: dict[ScheduleId, dt.datetime] = field(default_factory=dict)

    async def tick(self) -> None:
        now = self.clock.now()
        async with self.uow:
            due = await self.uow.schedules.list_due(now)
        handled: set[ScheduleId] = set()
        for schedule in due:
            handled.add(schedule.id)
            try:
                await self._run_due(schedule, now)
            except Exception:  # noqa: BLE001 - one schedule must not stop the tick
                _LOG.exception(
                    "schedule %s failed this tick; continuing", schedule.id.value
                )
        # Fire any pending backup retry that has come due and was not already
        # superseded by a scheduled occurrence handled above.
        for schedule_id, retry_at in list(self._backup_retry.items()):
            if schedule_id in handled or retry_at > now:
                continue
            try:
                await self._run_retry(schedule_id, now)
            except Exception:  # noqa: BLE001 - one schedule must not stop the tick
                _LOG.exception(
                    "schedule %s retry failed; continuing", schedule_id.value
                )

    async def _run_due(self, schedule: Schedule, now: dt.datetime) -> None:
        if schedule.action is not ScheduleAction.BACKUP and self._is_missed(
            schedule, now
        ):
            # Overdue non-backup: do not fire late. Advance past now with no run row
            # and no last_run_at change (nothing executed).
            await self._advance(schedule, now, last_run_at=schedule.last_run_at)
            return
        result, started_at, finished_at = await self._execute(schedule)
        await self._record(schedule, result, started_at, finished_at)
        await self._advance(schedule, now, last_run_at=now)
        if schedule.action is ScheduleAction.BACKUP:
            self._reschedule_retry(schedule.id, result.outcome, now)

    async def _run_retry(self, schedule_id: ScheduleId, now: dt.datetime) -> None:
        # One-shot: drop the retry state up front, whatever the outcome.
        self._backup_retry.pop(schedule_id, None)
        async with self.uow:
            schedule = await self.uow.schedules.get_by_id(schedule_id)
        if (
            schedule is None
            or not schedule.enabled
            or schedule.action is not ScheduleAction.BACKUP
        ):
            return
        result, started_at, finished_at = await self._execute(schedule)
        # The retry records (and notifies on failure) but never moves next_run_at:
        # it is a catch-up for the failed occurrence, not a new occurrence.
        await self._record(schedule, result, started_at, finished_at)

    async def _execute(
        self, schedule: Schedule
    ) -> tuple[ActionResult, dt.datetime, dt.datetime]:
        started_at = self.clock.now()
        result = await self.execute(
            server_id=schedule.server_id,
            action=schedule.action,
            command=schedule.command,
        )
        finished_at = self.clock.now()
        return result, started_at, finished_at

    async def _record(
        self,
        schedule: Schedule,
        result: ActionResult,
        started_at: dt.datetime,
        finished_at: dt.datetime,
    ) -> None:
        run = ScheduleRun(
            id=ScheduleRunId.new(),
            schedule_id=schedule.id,
            started_at=started_at,
            finished_at=finished_at,
            outcome=result.outcome,
            detail=result.detail,
        )
        async with self.uow:
            await self.uow.schedule_runs.add(run)
            await self.uow.schedule_runs.prune_for_schedule(
                schedule.id, keep=self.history_cap
            )
            await self.uow.commit()
        if result.outcome is ScheduleRunOutcome.SUCCESS:
            await self._audit(schedule, result.community_id, Outcome.SUCCESS)
        elif result.outcome is ScheduleRunOutcome.FAILURE:
            await self._audit(schedule, result.community_id, Outcome.ERROR)
            self.notifier.notify(
                server_id=schedule.server_id,
                kind=_NOTIFY_KIND,
                title=f"Scheduled {schedule.action.value} failed",
                detail=result.detail or "",
            )

    async def _advance(
        self, schedule: Schedule, now: dt.datetime, *, last_run_at: dt.datetime | None
    ) -> None:
        # Rebuild via ``replace`` so the entity re-runs its invariants (PR #1845),
        # rather than mutating a loaded row in place.
        updated = replace(
            schedule,
            next_run_at=self._next_after(schedule, now),
            last_run_at=last_run_at,
            updated_at=now,
        )
        async with self.uow:
            await self.uow.schedules.update(updated)
            await self.uow.commit()

    async def _audit(
        self,
        schedule: Schedule,
        community_id: CommunityId | None,
        outcome: Outcome,
    ) -> None:
        await self.audit.record(
            AuditEvent(
                operation=SCHEDULE_RUN,
                outcome=outcome,
                actor_id=None,
                community_id=community_id.value if community_id is not None else None,
                target_type=TARGET_SCHEDULE,
                target_id=schedule.id.value,
            )
        )

    def _reschedule_retry(
        self, schedule_id: ScheduleId, outcome: ScheduleRunOutcome, now: dt.datetime
    ) -> None:
        if outcome is ScheduleRunOutcome.FAILURE:
            self._backup_retry[schedule_id] = now + self.backup_retry_delay
        elif outcome is ScheduleRunOutcome.SUCCESS:
            self._backup_retry.pop(schedule_id, None)
        # SKIPPED: leave any pending retry untouched (a skip is not a fresh failure).

    def _is_missed(self, schedule: Schedule, now: dt.datetime) -> bool:
        if schedule.next_run_at is None:
            return False
        return self._next_after(schedule, schedule.next_run_at) <= now

    def _next_after(self, schedule: Schedule, after: dt.datetime) -> dt.datetime:
        interval = schedule.cadence.interval_seconds
        if interval is not None:
            return next_interval_run(
                schedule.id, interval_seconds=interval, after=after
            )
        assert schedule.cadence.cron is not None
        return self.calculator.next_after(
            schedule.cadence.cron, schedule.timezone, after
        )
