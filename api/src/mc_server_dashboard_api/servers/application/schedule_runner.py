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

``next_run_at`` advances to the first occurrence strictly after the advance
instant on every fired occurrence (success, failure, or skip) — there is no
tick-level retry.

Missed-run semantics: a non-``backup`` occurrence executes only while it is at
most :data:`LATE_RUN_GRACE` past its due instant; a staler occurrence does *not*
fire late — ``next_run_at`` is advanced past ``now`` with no execution and no
run row (a stop that was due hours ago must not stop tonight's players). A
``backup`` has no staleness cut-off: however late, it fires exactly once —
advancing past ``now`` coalesces every missed occurrence into that one catch-up.

Backup-only bounded retry: a failed ``backup`` occurrence arms one in-memory
retry ~30 minutes later (lost on restart — accepted). The retry records (and, on
failure, notifies) but never moves ``next_run_at``; it fires at most once, then
the schedule waits for its next occurrence.

Delivery is at-least-once across a crash: the run executes before ``next_run_at``
advances, so an API crash in the window between the two replays the occurrence
on restart. Accepted — a replayed ``backup`` is a harmless duplicate archive, and
a replayed lifecycle/command occurrence is bounded by the same
:data:`LATE_RUN_GRACE` staleness gate (a restart later than the grace advances
without executing).

One schedule's exception never stops the tick — each is isolated and logged.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.operations import (
    SCHEDULE_RUN,
    TARGET_SCHEDULE,
)
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.servers.application.backups import (
    CreateBackup,
    PruneScheduledBackups,
)
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
    MAX_WARNING_OFFSET_MINUTES,
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
    ScheduleRunId,
    ScheduleRunOutcome,
    WarningStep,
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

# How late a non-backup occurrence may still execute. Generous against the
# runner's tick resolution (~20 s) and a brief API restart, yet small against any
# human-meaningful cadence (the interval floor is one minute; cron is
# minute-granular), so a lifecycle/command occurrence missed by an outage never
# fires hours late — a daily 04:00 stop must not stop the evening's players.
# Anything staler advances without executing (no run row).
LATE_RUN_GRACE = dt.timedelta(seconds=300)

# The floor of the warning-send grace: how late a due player warning may still
# broadcast (issue #1839). The effective grace is derived at wiring time as
# max(floor, tick_seconds) — see :func:`effective_warning_grace` — so a step
# whose offset exceeds the tick always fires: at worst one tick late, and always
# strictly before the occurrence (the look-ahead only surfaces still-future
# occurrences). A step whose whole send window nonetheless passes unsent — an
# offset smaller than a coarse tick, or a schedule created/enabled after the
# warn instant — is consumed and logged instead of broadcast late: a warning
# that missed its moment is worthless, unlike the occurrence it heralds
# (LATE_RUN_GRACE). The in-memory sent-state makes each step fire once.
WARNING_GRACE_FLOOR = dt.timedelta(seconds=60)


def effective_warning_grace(tick_seconds: float) -> dt.timedelta:
    """The warning-send grace for a runner loop ticking every ``tick_seconds``.

    At least one tick wide, so a warn instant landing anywhere between two
    healthy ticks is still within its send window at the next one; floored at
    :data:`WARNING_GRACE_FLOOR` for finer ticks.
    """

    return max(WARNING_GRACE_FLOOR, dt.timedelta(seconds=tick_seconds))


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
        except Exception:  # noqa: BLE001 - every execution error is a run outcome
            # An unexpected error (a Storage/OS failure mid-archive, a DB error
            # inside the use case) is still a *failure of this occurrence*: it
            # must produce a run row + audit + notification and let next_run_at
            # advance like any other failure. Letting it escape would bypass the
            # taxonomy and re-execute the occurrence every tick (no tick-level
            # retry exists by design). The traceback is logged here; the recorded
            # detail stays a sanitized category.
            _LOG.exception(
                "scheduled %s for server %s raised unexpectedly",
                action.value,
                server_id.value,
            )
            return ActionResult(
                ScheduleRunOutcome.FAILURE, "action failed", community_id
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
    send_command: SendServerCommand
    calculator: NextRunCalculator
    audit: AuditRecorder
    notifier: ServerNotifier
    clock: Clock
    history_cap: int = _HISTORY_CAP
    backup_retry_delay: dt.timedelta = _BACKUP_RETRY_DELAY
    # How late a due player warning may still broadcast; the wiring derives it
    # from the loop's tick via effective_warning_grace so a send window can
    # never fall entirely between two healthy ticks.
    warning_grace: dt.timedelta = WARNING_GRACE_FLOOR
    # Retention prune hook (issue #1841): fires after ANY successful
    # backup-action execution — the regular occurrence and the one-shot retry.
    # Optional so callers without retention wiring are unaffected. Isolated: a
    # prune failure never turns the successful backup run into a failure (owner
    # spec), and prune emits nothing on the notification stream.
    prune_backups: PruneScheduledBackups | None = None
    # Per-schedule pending backup-retry instant; in-memory (lost on restart).
    _backup_retry: dict[ScheduleId, dt.datetime] = field(default_factory=dict)
    # Warnings already sent this process, keyed by (schedule, occurrence, offset):
    # each step fires once (issue #1839). In-memory — a restart may re-send a step
    # still within its grace window at most once. Pruned as occurrences pass.
    _warned: set[tuple[ScheduleId, dt.datetime, int]] = field(default_factory=set)

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
        # Broadcast any player warnings whose moment has arrived for still-upcoming
        # stop/restart occurrences. Independent of the due poll above: the two
        # never overlap (a warning fires while now < T, the occurrence while
        # now >= T), so ordering does not matter.
        await self._send_warnings(now)

    async def _run_due(self, schedule: Schedule, now: dt.datetime) -> None:
        if schedule.action is not ScheduleAction.BACKUP and self._is_stale(
            schedule, now
        ):
            # A stale non-backup occurrence does not fire late (module docstring):
            # advance past now with no run row and no last_run_at change.
            await self._advance(schedule, last_run_at=schedule.last_run_at)
            return
        # A warning step still unsent now can never fire (a warning broadcasts
        # only strictly before its occurrence): its window fell between two
        # coarse ticks, or a restart lost the sent-state. Best-effort means
        # observable — name each such step before the action runs.
        occurrence = schedule.next_run_at
        if occurrence is not None:
            for step in schedule.warning_steps:
                if (schedule.id, occurrence, step.offset_minutes) not in self._warned:
                    self._log_missed_step(schedule, step)
        result, started_at, finished_at = await self._execute(schedule)
        await self._record(schedule, result, started_at, finished_at)
        await self._advance(schedule, last_run_at=started_at)
        if schedule.action is ScheduleAction.BACKUP:
            self._reschedule_retry(schedule.id, result.outcome, finished_at)
        await self._prune_after_backup(schedule, result)

    async def _run_retry(self, schedule_id: ScheduleId, now: dt.datetime) -> None:
        # One-shot: the single retry is spent up front, REGARDLESS of its outcome
        # (even a skip on a transitional server consumes it) — the next regular
        # occurrence takes over. A retry-of-the-retry chain is exactly the
        # unbounded tail the owner-confirmed "retry once" bound excludes.
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
        await self._prune_after_backup(schedule, result)

    async def _prune_after_backup(
        self, schedule: Schedule, result: ActionResult
    ) -> None:
        """Prune per the retention policy after a successful backup run (#1841).

        Fires only for a ``backup`` action that succeeded (never on a skip or
        failure — nothing new was archived). Isolated: a prune error is logged
        and swallowed so it cannot turn the successful backup run into a
        failure, and nothing is emitted on the notification stream for prune
        (owner spec: notifications are for run failures only). What was pruned
        is logged and audited inside :class:`PruneScheduledBackups`.
        """

        if (
            self.prune_backups is None
            or schedule.action is not ScheduleAction.BACKUP
            or result.outcome is not ScheduleRunOutcome.SUCCESS
            or result.community_id is None
        ):
            return
        try:
            await self.prune_backups(
                community_id=result.community_id, server_id=schedule.server_id
            )
        except Exception:  # noqa: BLE001 - prune must not fail the backup run
            _LOG.exception(
                "retention prune after schedule %s failed; continuing",
                schedule.id.value,
            )

    async def _send_warnings(self, now: dt.datetime) -> None:
        """Broadcast due player warnings for still-upcoming stop/restart runs (#1839).

        The look-ahead sibling of the due poll: for each enabled stop/restart
        schedule whose occurrence T is ahead but within the maximum warning
        offset, broadcast every step whose warn instant (``T - offset``) has just
        arrived, as the fixed ``say <message>`` form through
        :class:`SendServerCommand`. Best-effort — an offline server or a failed
        dispatch is logged and skipped, never a run row and never touching the
        occurrence at T. The in-memory sent-state fires each step once; stale
        entries (occurrences now in the past) are pruned so it stays bounded.
        """

        self._warned = {key for key in self._warned if key[1] > now}
        until = now + dt.timedelta(minutes=MAX_WARNING_OFFSET_MINUTES)
        async with self.uow:
            candidates = await self.uow.schedules.list_warning_candidates(now, until)
        for schedule in candidates:
            try:
                await self._warn_schedule(schedule, now)
            except Exception:  # noqa: BLE001 - one schedule must not stop the tick
                _LOG.exception(
                    "warnings for schedule %s failed; continuing", schedule.id.value
                )

    async def _warn_schedule(self, schedule: Schedule, now: dt.datetime) -> None:
        occurrence = schedule.next_run_at
        assert occurrence is not None  # the look-ahead window guarantees it
        due: list[WarningStep] = []
        for step in schedule.warning_steps:
            key = (schedule.id, occurrence, step.offset_minutes)
            if key in self._warned:
                continue
            warn_at = occurrence - dt.timedelta(minutes=step.offset_minutes)
            if warn_at > now:
                continue  # not yet due
            if now - warn_at > self.warning_grace:
                # The whole send window passed unsent — the schedule was created
                # or enabled after the warn instant, or the runner was down
                # across the window. Consume the step and log once: best-effort
                # means observable, and a warning never broadcasts this late.
                self._warned.add(key)
                self._log_missed_step(schedule, step)
                continue
            due.append(step)
        if not due:
            return
        async with self.uow:
            server = await self.uow.servers.get_by_id(schedule.server_id)
        # Consume each due step up front (whether it broadcasts or is skipped) so a
        # transient offline server or a failed dispatch is never retried — a warning
        # that missed its moment is worthless.
        for step in due:
            self._warned.add((schedule.id, occurrence, step.offset_minutes))
        if server is None or not _is_running(server):
            _LOG.debug(
                "schedule %s warnings skipped: server not running",
                schedule.id.value,
            )
            return
        for step in due:
            try:
                await self.send_command(
                    community_id=server.community_id,
                    server_id=schedule.server_id,
                    line=f"say {step.message}",
                )
            except ServerError as exc:
                # Offline/refused between the pre-check and dispatch: log and move
                # on — a warning never fails the run it heralds.
                _LOG.info("schedule %s warning skipped: %r", schedule.id.value, exc)

    def _log_missed_step(self, schedule: Schedule, step: WarningStep) -> None:
        _LOG.warning(
            "schedule %s: %d-minute warning missed its send window; not sent",
            schedule.id.value,
            step.offset_minutes,
        )

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
        self, schedule: Schedule, *, last_run_at: dt.datetime | None
    ) -> None:
        # Recompute now HERE, not at tick start: after a long execution the next
        # occurrence must be strictly in the future of the advance instant, or a
        # slow run could land next_run_at already in the past. The write goes
        # through the narrow bookkeeping UPDATE (guarded WHERE enabled) so a
        # concurrent CRUD edit is never clobbered and a concurrently disabled
        # schedule stays disabled — 0 rows matched is the silent-skip contract.
        now = self.clock.now()
        async with self.uow:
            await self.uow.schedules.advance_run_state(
                schedule.id,
                next_run_at=self._next_after(schedule, now),
                last_run_at=last_run_at,
            )
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

    def _is_stale(self, schedule: Schedule, now: dt.datetime) -> bool:
        """Whether the due occurrence is too old to still execute (non-backup)."""

        if schedule.next_run_at is None:
            return False
        return now - schedule.next_run_at > LATE_RUN_GRACE

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
