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
  that is not at rest, a ``backup`` with ``only_when_running`` on a server that
  is at rest (issue #2236), or any action while the server is transitional. Recorded
  as an honest history row, *not* notified (nothing was attempted).
* **failure** — the dispatch reached the Worker / use case and was refused or the
  Worker was unavailable. Recorded and audited every occurrence; notified only on
  the failing *edge* (issue #2269): a schedule stuck failing every interval emits
  one operator toast for the whole incident, keyed by ``schedule_id`` in the
  in-memory ``_notifying`` map (no time window), and one recovery toast when the
  next success clears it. Skips leave that state untouched.
* **unknown** — the action may still take effect, but the run cannot confirm it
  (issues #3660, #3781). A command, stop or restart the Worker left unanswered:
  it did not answer in time, or its session ended while the command awaited the
  answer. Neither proves the command left the API, since one dropped unsent from
  the outbound queue looks the same, so both err toward unknown. A start whose
  intent still stands after the error: an unreachable Worker, a lost answer or a
  ``BUSY`` refusal at the start command, or a hydrate refused ``BUSY``,
  ``UNAVAILABLE`` or ``EXPIRED`` (issue #3890), after which the reconciler
  re-sends the start once the assigned Worker is connected (it skips the row
  while that Worker is gone). A stop the Worker refused ``BUSY``, whose intent
  stands too, which the reconciler converges (issue #3799). Audited and notified like a
  failure, under its own title, and never retried. It shares the failing edge,
  but a failure after an unknown notifies once more as an escalation; an unknown
  after a failure stays silent.

  Every other error stays a failure: a command, stop or restart whose Worker was
  not connected at dispatch or refused it (but for that ``BUSY`` stop), a start
  whose intent was reverted (its hydrate failed or went unanswered, or it was
  refused and compensated), and a backup whose snapshot failed or went unanswered
  (the archive is written only after the snapshot returns, so no backup exists).
  So is a start whose row re-read, which tells those apart, raised: it records
  ``outcome check failed`` and advances rather than re-execute every tick.
  ``COMMAND_DROPPED`` stays a failure too, though whether the command ran is
  uncertain (CONTROL_PLANE.md Section 7): the Worker answered, its usual cause is
  a packet the game server dropped without running it, and the run history shows
  the ``command_dropped`` detail for a failure where it would hide it for an
  unknown.
* **success** — otherwise. Clears any ``_notifying`` state and fires exactly one
  recovery notification when it ends a run of failures.

``next_run_at`` advances to the first occurrence strictly after the advance
instant on every fired occurrence (success, failure, or skip) — there is no
tick-level re-run, with one exception. A backup of a server at rest that a stop
still holds for its final snapshot is refused by :class:`CreateBackup` (409
``server_assigned``, issue #3998): an archive taken then would lack that
snapshot. Nothing was attempted and the release is normally seconds away, so the
occurrence is deferred rather than skipped: no run row, no advance, and the next
tick runs it again. One INFO line opens the deferral. The deferral is bounded
(issue #4047), since the row may stay held for as long as the Worker refuses the
snapshot. The bound, ``deferral_bound``, is ``reconciler.grace_seconds``, which
outlasts the stop's own final snapshot (``create_app`` warns when it does not
exceed ``control.snapshot_timeout_seconds``). It does not outlast every snapshot:
once the grace lapses, the reconciler's recovery takes a final snapshot of its
own before it clears the row. Past the bound, timed from the first deferral, the
occurrence is recorded as SKIPPED ``server_assigned``, with one WARNING line, and
advances. A pending backup retry is deferred and bounded the same way, without
advancing. An occurrence skipped this way takes a pending retry with it only when
that retry is already due, since it would meet the same hold; a retry not yet due
stays armed for its own time.

Missed-run semantics: a non-``backup`` occurrence executes only while it is at
most :data:`LATE_RUN_GRACE` past its due instant; a staler occurrence does *not*
fire late — ``next_run_at`` is advanced past ``now`` and a SKIPPED run row is
recorded (a stop that was due hours ago must not stop tonight's players). A
``backup`` has no staleness cut-off: however late, it fires exactly once —
advancing past ``now`` coalesces every missed occurrence into that one catch-up.
An occurrence that became due *after* the previous tick began is never considered
stale: its lateness is self-inflicted by a long-running sibling, not an outage
(issue #1947).

Concurrency: due schedules are dispatched concurrently (up to
:data:`_MAX_CONCURRENT_RUNS`), grouped by server so same-server schedules remain
serial. Each concurrent run gets its own :class:`ExecuteScheduleAction` via the
``make_execute`` factory (#871 pattern) so UoW sessions are never shared across
tasks.

Backup-only bounded retry: a failed ``backup`` occurrence arms one in-memory
retry ~30 minutes later (lost on restart — accepted). The retry records (and, on
failure, notifies) but never moves ``next_run_at``; it fires at most once, then
the schedule waits for its next occurrence. Only a failure arms it: an unknown
outcome may still have run, and a retry could repeat it. The retry is spent once
its claim (below) has read the row. A claim that raises, canonically on a
database still down, has attempted nothing: the retry stays armed for the next
tick, with a WARNING line (issue #4098).

Delivery is at most once per process, and at least once across a restart (issue
#4108). The run executes before its record and its advance, which commit in one
transaction. When that transaction raises, canonically on a database that is
down, the occurrence stays owed in memory with the outcome and times of its run:
each later tick records it again before its poll, and the schedule runs neither
the occurrence nor a backup retry while it is owed. One WARNING line marks the
first failure, and one INFO line the record that commits. A skip, whether stale
or past a deferral's bound, attempted nothing, so its record is not owed: the
next tick evaluates the occurrence again. An API restart or crash loses the owed
record, so the occurrence runs once more on restart, at most once per restart.
Accepted: a replayed ``backup`` is one more archive, not a harmless duplicate. It
takes storage, and under a keep-last retention it can push an older backup out.
A replayed lifecycle/command occurrence is bounded by the same
:data:`LATE_RUN_GRACE` staleness gate (a restart later than the grace advances
without executing). A backup's FAILURE run can also stand beside a complete
backup: :class:`CreateBackup` keeps the archive of a row whose COMMIT was sent
but not confirmed, and still raises (issue #4086). Running the occurrence once
bounds that to one backup per run, though the FAILURE arms the one-shot retry,
which can add another.

A schedule edited or deleted mid-run (issue #3314): the runner claims a due
schedule just before dispatching it, re-reading its row under ``SELECT ... FOR
UPDATE`` in a short transaction. A schedule deleted, disabled, or rescheduled
since the poll is not dispatched — one INFO line, no run row; the lock makes the
claim wait for a delete still in flight rather than read the row it is removing.
The run is then recorded and ``next_run_at`` advanced in one short transaction
that locks the row first: a delete arriving meanwhile waits for it and cascades
the run away, and one that committed earlier leaves nothing to record on — the
action is still audited, the run row skipped with one INFO line. Neither lock is
held across the dispatch: start, stop and backup can take minutes (hydrate,
final snapshot, archive), and a transaction open that long would block the
schedule's delete and edit and contend with the per-server lifecycle lock. So a
delete that commits between the claim and the dispatch is serialized after the
run, like one arriving during it: the action runs and is audited. The claim
advances nothing, so the delivery semantics above are unchanged.

One schedule's exception never stops the tick — each is isolated and logged, and
each schedule's run record and advance commit in a transaction of their own, so
a failure in one never rolls back another's.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import defaultdict
from collections.abc import Callable
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
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandStatus,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupCorruptError,
    BackupStorageUnavailableError,
    BackupTooLargeError,
    BackupUnsettledError,
    CommandDispatchError,
    InvalidLifecycleTransitionError,
    LifecycleTransitionConflictError,
    NoEligibleWorkerError,
    ServerAssignedError,
    ServerError,
    ServerNotRunningError,
    SnapshotUploadInFlightError,
)
from mc_server_dashboard_api.servers.domain.next_run_calculator import NextRunCalculator
from mc_server_dashboard_api.servers.domain.notifier import ServerNotifier
from mc_server_dashboard_api.servers.domain.schedule import (
    MAX_WARNING_OFFSET_MINUTES,
    WARNING_COMMAND_PREFIX,
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
# The one-shot recovery discriminator, fired when a schedule that was notifying
# failures produces a success again (issue #2269). Rendered generically by the
# webui like any other {kind, title, detail} frame — no frontend change.
_NOTIFY_KIND_RECOVERED = "schedule_recovered"

# How late a non-backup occurrence may still execute. Generous against the
# runner's tick resolution (~20 s) and a brief API restart, yet small against any
# human-meaningful cadence (the interval floor is one minute; cron is
# minute-granular), so a lifecycle/command occurrence missed by an outage never
# fires hours late — a daily 04:00 stop must not stop the evening's players.
# Anything staler advances with a SKIPPED run row.
LATE_RUN_GRACE = dt.timedelta(seconds=300)

# How many schedule executions may run concurrently within one tick (#1947).
# Same-server schedules are always serial within this bound.
_MAX_CONCURRENT_RUNS = 4

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


def _precondition_skip(
    server: Server, action: ScheduleAction, *, only_when_running: bool | None
) -> str | None:
    """Return a skip reason if ``action``'s precondition is unmet, else ``None``.

    ``command`` / ``stop`` / ``restart`` need a settled-running server; ``start``
    needs an at-rest one; ``backup`` needs either (only a *transitional* server is
    unsettled, matching :class:`CreateBackup`'s own gate) — or, when
    ``only_when_running`` is set (issue #2236), a settled-running one: an at-rest
    server then skips as "server not running". Every other case — the server
    mid-transition — is a skip, so a scheduled action never lands on a server
    that is starting, stopping, restarting, or crashed.
    """

    if action is ScheduleAction.START:
        return None if server.is_at_rest() else "server not stopped"
    if action is ScheduleAction.BACKUP:
        if _is_running(server):
            return None
        if server.is_at_rest():
            return "server not running" if only_when_running else None
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
        # ``upload_may_be_live`` is set only for a command not answered in time,
        # and ``session_ended_in_flight`` for one whose Worker disconnected while it
        # awaited its answer; neither is set for a Worker that is not connected.
        if exc.upload_may_be_live:
            return "worker timed out"
        if exc.session_ended_in_flight:
            return "worker disconnected"
        return "worker unavailable"
    if isinstance(exc, NoEligibleWorkerError):
        return "no eligible worker"
    if isinstance(exc, BackupCorruptError):
        return "backup corrupt"
    if isinstance(exc, BackupTooLargeError):
        return "backup too large"
    if isinstance(exc, BackupStorageUnavailableError):
        return "storage unavailable"
    if isinstance(exc, SnapshotUploadInFlightError):
        # A start whose wait for the Worker's uploads used the hydrate budget
        # (issue #3950), named as the HTTP start answers it.
        return "upload_in_flight"
    return "action failed"


def _is_storage_backend_error(exc: BaseException) -> bool:
    """Whether ``exc`` originates from the storage backend's S3 client (botocore).

    Detected by module rather than an ``import`` so the servers application layer
    keeps no dependency on the storage adapter's S3 library. Covers both
    ``botocore`` and its async wrapper ``aiobotocore``.
    """

    return any(
        cls.__module__.split(".", 1)[0] in ("botocore", "aiobotocore")
        for cls in type(exc).__mro__
    )


def _unexpected_detail(exc: BaseException) -> str:
    """Sanitized category for a non-``ServerError`` raised mid-execution (#2248).

    The backup seam only translates NotFound/IntegrityCheck; a raw storage-backend
    fault propagates here — a botocore read/connect timeout, or a 5xx like the
    SeaweedFS disk-full ``InternalError`` on ``UploadPart`` (the 2026-07-23 prod
    incident). Return a short, secret-free category so the dashboard can tell a
    storage timeout from a storage-backend error from any other fault; never an
    endpoint URL, key, or bucket path (those ride the botocore message, which stays
    in the traceback logged separately). Anything unrecognized falls back to the
    exception's class name — itself a safe, stable category.
    """

    if _is_storage_backend_error(exc):
        name = type(exc).__name__
        return "storage timeout" if "Timeout" in name else "storage error"
    return type(exc).__name__


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
    # Whether a FAILURE outcome is worth the one-shot backup retry (#2250). False
    # only for a provably deterministic failure (a corrupt working set, or one too
    # large to archive, #3859) whose re-archive can never succeed; True for
    # infra/unexpected faults that may heal.
    retryable: bool = True


@dataclass(frozen=True)
class Deferred:
    """An action :class:`ExecuteScheduleAction` did not attempt, owed at the next tick.

    A backup of a server at rest that a stop still holds for its final snapshot
    (issue #3998): :class:`CreateBackup` refused it under the lifecycle lock, since
    an archive taken then would lack that snapshot. Not an outcome: the runner
    records no run row and does not advance, until the deferral outlasts its
    bound (issue #4047).
    """


# What a backup deferred past the bound is recorded as (issue #4047), named as
# CreateBackup refused it. No community: a skip is not audited.
_SKIPPED_ASSIGNED = ActionResult(ScheduleRunOutcome.SKIPPED, "server_assigned", None)


@dataclass(frozen=True)
class _Ran:
    """An occurrence the runner executed, with what its run record needs.

    ``schedule`` is the schedule as polled: its ``next_run_at`` is the
    occurrence the record advances past. Kept as owed while the record raises
    (issue #4108).
    """

    schedule: Schedule
    result: ActionResult
    started_at: dt.datetime
    finished_at: dt.datetime


@dataclass(frozen=True)
class ExecuteScheduleAction:
    """Map a schedule action to its existing use case and classify the outcome.

    Deliberately time-trigger-free: it takes a server + action + the action's
    payload (command line, backup ``only_when_running``), not a
    :class:`Schedule`, so a non-schedule caller (the deferred crash-restart
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
        self,
        *,
        server_id: ServerId,
        action: ScheduleAction,
        command: str | None,
        only_when_running: bool | None,
    ) -> ActionResult | Deferred:
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
        if server is None:
            # The FK cascade removes a schedule with its server, so a missing
            # server here is a rare race; skip quietly rather than notify.
            return ActionResult(ScheduleRunOutcome.SKIPPED, "server not found", None)
        community_id = server.community_id
        reason = _precondition_skip(server, action, only_when_running=only_when_running)
        if reason is not None:
            return ActionResult(ScheduleRunOutcome.SKIPPED, reason, community_id)
        try:
            await self._dispatch(server, action, command)
        except _SKIP_ERRORS as exc:
            return ActionResult(
                ScheduleRunOutcome.SKIPPED, _skip_detail(exc), community_id
            )
        except ServerError as exc:
            if action is ScheduleAction.BACKUP and isinstance(exc, ServerAssignedError):
                # A server at rest that a stop still holds: whether the runner's
                # read already saw that or a stop landed since, CreateBackup
                # refused it under the lifecycle lock (issue #3998).
                return Deferred()
            _LOG.warning(
                "scheduled %s for server %s failed: %r",
                action.value,
                server_id.value,
                exc,
            )
            try:
                may_have_run = await self._may_have_run(server_id, action, exc)
            except Exception:  # noqa: BLE001 - the occurrence must still be recorded
                # The start's row re-read raised (canonically a database error).
                # Escaping would leave no run row and no advance, and the
                # occurrence would re-execute every tick (issue #3799).
                _LOG.exception(
                    "scheduled %s for server %s: its outcome check raised",
                    action.value,
                    server_id.value,
                )
                return ActionResult(
                    ScheduleRunOutcome.FAILURE, "outcome check failed", community_id
                )
            outcome = (
                ScheduleRunOutcome.UNKNOWN
                if may_have_run
                else ScheduleRunOutcome.FAILURE
            )
            return ActionResult(
                outcome,
                _failure_detail(exc),
                community_id,
                retryable=not isinstance(
                    exc, (BackupCorruptError, BackupTooLargeError)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - every execution error is a run outcome
            # An unexpected error (a Storage/OS failure mid-archive, a DB error
            # inside the use case) is still a *failure of this occurrence*: it
            # must produce a run row + audit + notification and let next_run_at
            # advance like any other failure. Letting it escape would bypass the
            # taxonomy and re-execute the occurrence every tick (no tick-level
            # retry exists by design). The traceback is logged here; the recorded
            # detail stays a sanitized category derived from the exception type
            # (#2248) so a storage timeout / backend error is distinguishable on
            # the dashboard without leaking secrets or paths.
            _LOG.exception(
                "scheduled %s for server %s raised unexpectedly",
                action.value,
                server_id.value,
            )
            return ActionResult(
                ScheduleRunOutcome.FAILURE, _unexpected_detail(exc), community_id
            )
        return ActionResult(ScheduleRunOutcome.SUCCESS, None, community_id)

    async def _may_have_run(
        self, server_id: ServerId, action: ScheduleAction, exc: ServerError
    ) -> bool:
        """Whether a failed action may still take effect (issues #3660, #3781).

        A backup never: its dispatch is the snapshot, which :class:`CreateBackup`
        awaits before it writes the archive, so whatever the snapshot did, no
        backup exists.

        A start can exactly while its intent stands, whatever the error. Before
        the start command, or on a refusal :class:`StartServer` compensates, the
        intent is reverted and the start will not happen. Once the start command
        is reached, an unreachable Worker, a lost answer (a timeout or a
        disconnect) or a ``BUSY`` refusal leaves the intent and assignment
        standing, and so does a hydrate refused ``BUSY``, ``UNAVAILABLE`` or
        ``EXPIRED`` (issue #3890), and a wait for the Worker's uploads that used
        the hydrate budget (issue #3950). The reconciler re-sends the start once the
        assigned Worker is connected (it skips the row while that Worker is gone).
        The row tells these apart.

        A stop the Worker refused ``BUSY`` can: :class:`StopServer` never reverts
        its intent, so ``desired=stopped`` stands, which the reconciler
        converges (issue #3799).

        A command, stop or restart can otherwise only when the Worker left its
        command unanswered: it did not answer in time (``upload_may_be_live``) or
        its session ended while the command awaited the answer
        (``session_ended_in_flight``). Either way the command may have reached
        the Worker and run, or it may never have left the API: the flags cover a
        command dropped unsent from the outbound queue too, and the API cannot
        tell. A Worker that was not connected at dispatch never ran it, and every
        other answered refusal stays a failure (``COMMAND_DROPPED`` too: see the
        module docstring). Nothing re-sends a refused command or restart (a
        restart commits no intent), and a stop refused for any other reason is an
        attempt that failed, recorded under its cause, though the reconciler
        still converges its intent.
        """

        if action is ScheduleAction.BACKUP:
            return False
        if action is ScheduleAction.START:
            async with self.uow:
                server = await self.uow.servers.get_by_id(server_id)
            return server is not None and server.desired_state is DesiredState.RUNNING
        if (
            action is ScheduleAction.STOP
            and isinstance(exc, CommandDispatchError)
            and exc.status is CommandStatus.BUSY
        ):
            return True
        return isinstance(exc, WorkerUnavailableError) and (
            exc.upload_may_be_live or exc.session_ended_in_flight
        )

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
    make_execute: Callable[[], ExecuteScheduleAction]
    send_command: SendServerCommand
    calculator: NextRunCalculator
    audit: AuditRecorder
    notifier: ServerNotifier
    clock: Clock
    # How long a backup waits on a server a stop still holds before it is
    # recorded as skipped (issue #4047): the wiring passes
    # reconciler.grace_seconds.
    deferral_bound: dt.timedelta
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
    # Per schedule, the backup a hold defers and when it was first deferred
    # (issue #4047): the occurrence's due instant or the retry's armed one. A
    # deferral of another instant starts over. Dropped once the deferral ends in
    # a run or a recorded skip, or the schedule is gone. In-memory, like
    # _backup_retry: a restart gives a held backup a whole bound again.
    _deferred: dict[ScheduleId, tuple[dt.datetime, dt.datetime]] = field(
        default_factory=dict
    )
    # Per schedule, the occurrence that ran but whose record and advance raised
    # (issue #4108). Each tick records it again before its poll, and the
    # schedule runs nothing while it is owed. Dropped once the record returns.
    # In-memory, like _backup_retry: a restart runs the occurrence once more.
    _owed: dict[ScheduleId, _Ran] = field(default_factory=dict)
    # Schedules currently in a failing state, so a sustained failure notifies once
    # rather than every occurrence (issue #2269). Each maps to the outcome already
    # notified for the incident: a FAILURE or UNKNOWN notifies only when the id is
    # absent, or as the escalation of an UNKNOWN to a FAILURE (issue #3781), then
    # records it; the next SUCCESS clears it and fires one recovery notification.
    # A deleted schedule's entry goes at the next tick's poll at the latest.
    # In-memory — same accepted "lost on api restart" trade as _backup_retry /
    # _warned above (a restart may re-notify one failure).
    _notifying: dict[ScheduleId, ScheduleRunOutcome] = field(default_factory=dict)
    # Warnings already sent this process, keyed by (schedule, occurrence, offset):
    # each step fires once (issue #1839). In-memory — a restart may re-send a step
    # still within its grace window at most once. Pruned as occurrences pass.
    _warned: set[tuple[ScheduleId, dt.datetime, int]] = field(default_factory=set)
    # When the previous tick began; used to exempt listing-time-stale occurrences
    # that became due after the previous tick started (#1947).
    _last_tick_at: dt.datetime | None = field(default=None, repr=False)
    # Serializes bookkeeping writes (_record, _advance, _prune_after_backup) so
    # concurrent server-group tasks do not interleave UoW sessions.
    _bookkeeping_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def tick(self) -> None:
        now = self.clock.now()
        prev_tick_at = self._last_tick_at
        self._last_tick_at = now
        # First the records earlier ticks owe (issue #4108): one that commits
        # advances its schedule from when it ran, so the poll below lists it
        # again only for an occurrence that came due meanwhile.
        for ran in list(self._owed.values()):
            await self._record_owed(ran)
        async with self.uow:
            due = await self.uow.schedules.list_due(now)
            # A schedule deleted between ticks never reaches the claim or the record
            # that forget it, so look up each one still notifying or deferred: the
            # due poll lists only the due ones, and a failing schedule outlives the
            # ticks between its occurrences (issue #3799), as a held one does its
            # deferral (issue #4047). Only that state goes: a pending backup retry
            # still reaches its claim, which logs the delete.
            gone = [
                schedule_id
                for schedule_id in {*self._notifying, *self._deferred}
                if await self.uow.schedules.get_ref(schedule_id) is None
            ]
            # Commit the poll transaction: list_due may have quarantined a corrupt
            # row (disabled it), and without this commit that write would roll back
            # and respool every tick (issue #2150). A read-only poll commits nothing.
            await self.uow.commit()
        for schedule_id in gone:
            self._notifying.pop(schedule_id, None)
            self._deferred.pop(schedule_id, None)
        handled: set[ScheduleId] = set()

        # Group due schedules by server so same-server schedules stay serial,
        # while different servers run concurrently under a bounded semaphore.
        by_server: dict[ServerId, list[Schedule]] = defaultdict(list)
        for schedule in due:
            handled.add(schedule.id)
            if schedule.id in self._owed:
                # It ran already: only its record is retried, never the run.
                continue
            by_server[schedule.server_id].append(schedule)

        sem = asyncio.Semaphore(_MAX_CONCURRENT_RUNS)

        async def _run_server_group(schedules: list[Schedule]) -> None:
            async with sem:
                for schedule in schedules:
                    try:
                        await self._run_due(schedule, now, prev_tick_at)
                    except Exception:  # noqa: BLE001 - one schedule must not stop the tick
                        _LOG.exception(
                            "schedule %s failed this tick; continuing",
                            schedule.id.value,
                        )

        await asyncio.gather(
            *(_run_server_group(group) for group in by_server.values())
        )

        # Fire any pending backup retry that has come due and was not already
        # superseded by a scheduled occurrence handled above.
        for schedule_id, retry_at in list(self._backup_retry.items()):
            if schedule_id in handled or schedule_id in self._owed or retry_at > now:
                continue
            try:
                await self._run_retry(schedule_id, retry_at)
            except Exception:  # noqa: BLE001 - one schedule must not stop the tick
                _LOG.exception(
                    "schedule %s retry failed; continuing", schedule_id.value
                )
        # Broadcast any player warnings whose moment has arrived for still-upcoming
        # stop/restart occurrences. Independent of the due poll above: the two
        # never overlap (a warning fires while now < T, the occurrence while
        # now >= T), so ordering does not matter.
        await self._send_warnings(now)

    async def _run_due(
        self,
        schedule: Schedule,
        now: dt.datetime,
        prev_tick_at: dt.datetime | None,
    ) -> None:
        if schedule.action is not ScheduleAction.BACKUP and self._is_stale(
            schedule, now, prev_tick_at
        ):
            # A stale non-backup occurrence does not fire late (module docstring):
            # record a SKIPPED run row and advance past now with no last_run_at
            # change.
            stale_now = self.clock.now()
            lateness = now - schedule.next_run_at if schedule.next_run_at else None
            _LOG.warning(
                "schedule %s: %s occurrence due %s is %s late; skipping",
                schedule.id.value,
                schedule.action.value,
                schedule.next_run_at,
                lateness,
            )
            stale_result = ActionResult(
                ScheduleRunOutcome.SKIPPED, "occurrence too stale", None
            )
            async with self._bookkeeping_lock:
                await self._record(
                    schedule,
                    stale_result,
                    stale_now,
                    stale_now,
                    advance=True,
                    last_run_at=schedule.last_run_at,
                )
            return
        async with self._bookkeeping_lock:
            current = await self._claim(schedule.id, "run")
        if current is None:
            return
        if not current.enabled or current.next_run_at != schedule.next_run_at:
            _LOG.info(
                "schedule %s was disabled or rescheduled before its run; "
                "not dispatched",
                schedule.id.value,
            )
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
        gave_up = False
        if isinstance(result, Deferred):
            assert occurrence is not None  # list_due predicate guarantees non-None
            if not self._deferral_outlived(
                schedule, occurrence, finished_at, "occurrence"
            ):
                return
            gave_up = True
            result = _SKIPPED_ASSIGNED
        else:
            # The hold, if there was one, has ended.
            self._deferred.pop(schedule.id, None)
        ran = _Ran(schedule, result, started_at, finished_at)
        try:
            recorded = await self._settle(ran)
        except Exception:
            if not gave_up:
                # It ran, and a second run would repeat it (a backup as one
                # more archive): only the record is retried (issue #4108). A
                # skip past the bound attempted nothing, so it runs again.
                self._owed[schedule.id] = ran
                _LOG.warning(
                    "schedule %s: its %s ran, but the run could not be recorded; "
                    "the record is retried each tick, and the %s does not run "
                    "again",
                    schedule.id.value,
                    schedule.action.value,
                    schedule.action.value,
                )
            raise
        if gave_up:
            self._end_deferral(schedule, "occurrence", recorded=recorded)
            # One skipped run stands for the backup the schedule owes: a retry
            # already due would meet the same hold. One not yet due keeps its
            # own time (issue #4047).
            retry_at = self._backup_retry.get(schedule.id)
            if retry_at is not None and retry_at <= now:
                del self._backup_retry[schedule.id]

    async def _settle(self, ran: _Ran) -> bool:
        """Record and advance an occurrence that ran, then arm its retry and prune.

        Returns :meth:`_record`'s answer. A record that raises propagates, with
        nothing else done.
        """

        schedule = ran.schedule
        async with self._bookkeeping_lock:
            recorded = await self._record(
                schedule,
                ran.result,
                ran.started_at,
                ran.finished_at,
                advance=True,
                last_run_at=ran.started_at,
            )
        if recorded and schedule.action is ScheduleAction.BACKUP:
            self._reschedule_retry(schedule.id, ran.result, ran.finished_at)
        async with self._bookkeeping_lock:
            await self._prune_after_backup(schedule, ran.result)
        return recorded

    async def _record_owed(self, ran: _Ran) -> None:
        """Record an occurrence that ran on an earlier tick (issue #4108).

        With the outcome and times of that run. A record that raises again
        leaves it owed, logged at DEBUG only: the WARNING when it was first
        owed stands for the whole outage.
        """

        schedule = ran.schedule
        try:
            recorded = await self._settle(ran)
        except Exception:  # noqa: BLE001 - owed until a later tick records it
            _LOG.debug(
                "schedule %s: its owed run still could not be recorded",
                schedule.id.value,
                exc_info=True,
            )
            return
        # Dropped on any return: a schedule deleted since has nothing left to
        # record on, and _record said so.
        del self._owed[schedule.id]
        if recorded:
            _LOG.info(
                "schedule %s: the owed run of its %s is recorded",
                schedule.id.value,
                schedule.action.value,
            )

    async def _run_retry(self, schedule_id: ScheduleId, retry_at: dt.datetime) -> None:
        try:
            schedule = await self._claim(schedule_id, "backup retry")
        except Exception:  # noqa: BLE001 - a claim that raises attempted nothing
            # Nothing was attempted, so the retry stays armed for the next tick
            # (issue #4098): canonically, the database is still down.
            _LOG.warning(
                "schedule %s: its backup retry could not be claimed; "
                "it is kept for the next tick",
                schedule_id.value,
                exc_info=True,
            )
            return
        if (
            schedule is None
            or not schedule.enabled
            or schedule.action is not ScheduleAction.BACKUP
        ):
            self._backup_retry.pop(schedule_id, None)
            return
        # _execute raises only before the dispatch, canonically when its first
        # server read meets a database that is down: nothing was attempted, so
        # the retry stays armed for the next tick.
        result, started_at, finished_at = await self._execute(schedule)
        # One-shot: the single retry is spent once it ran, REGARDLESS of its
        # outcome (even a skip on a transitional server consumes it) — the next
        # regular occurrence takes over. A retry-of-the-retry chain is exactly
        # the unbounded tail the owner-confirmed "retry once" bound excludes.
        self._backup_retry.pop(schedule_id, None)
        gave_up = False
        if isinstance(result, Deferred):
            if not self._deferral_outlived(schedule, retry_at, finished_at, "retry"):
                # Not spent: nothing was attempted (issue #3998).
                self._backup_retry[schedule_id] = retry_at
                return
            gave_up = True
            result = _SKIPPED_ASSIGNED
            # Armed until the skipped run is recorded: a record that fails
            # leaves it to the next tick, still past the bound.
            self._backup_retry[schedule_id] = retry_at
        else:
            # The hold, if there was one, has ended.
            self._deferred.pop(schedule_id, None)
        # The retry records (and notifies on failure) but never moves next_run_at:
        # it is a catch-up for the failed occurrence, not a new occurrence.
        recorded = await self._record(
            schedule, result, started_at, finished_at, advance=False
        )
        if gave_up:
            self._backup_retry.pop(schedule_id, None)
            self._end_deferral(schedule, "retry", recorded=recorded)
        await self._prune_after_backup(schedule, result)

    def _deferral_outlived(
        self, schedule: Schedule, owed: dt.datetime, now: dt.datetime, what: str
    ) -> bool:
        """Note a deferred backup; whether it has been deferred past the bound.

        ``owed`` is the instant the deferred backup was due. Its first deferral
        is logged once at INFO, and each later tick only checks the bound, so a
        long hold costs two log lines rather than one a tick (issue #4047). Past
        the bound the caller records the backup as skipped, then ends the
        deferral with :meth:`_end_deferral`; a record that fails leaves it to
        the next tick, still past the bound.
        """

        deferred = self._deferred.get(schedule.id)
        if deferred is None or deferred[0] != owed:
            self._deferred[schedule.id] = (owed, now)
            _LOG.info(
                "schedule %s: server %s is at rest but still assigned to a "
                "Worker; backup %s deferred until it is released, for at most %s",
                schedule.id.value,
                schedule.server_id.value,
                what,
                self.deferral_bound,
            )
            return False
        return now - deferred[1] > self.deferral_bound

    def _end_deferral(self, schedule: Schedule, what: str, *, recorded: bool) -> None:
        """Forget a deferral given up past the bound (issue #4047).

        ``recorded`` is :meth:`_record`'s answer: ``False`` when the schedule
        was deleted, so no skipped run stands for the backup.
        """

        self._deferred.pop(schedule.id, None)
        _LOG.warning(
            "schedule %s: server %s is still assigned to a Worker after %s; "
            "backup %s %s",
            schedule.id.value,
            schedule.server_id.value,
            self.deferral_bound,
            what,
            "recorded as skipped (server_assigned)"
            if recorded
            else "given up; the schedule was deleted, so no run was recorded",
        )

    async def _claim(self, schedule_id: ScheduleId, what: str) -> Schedule | None:
        """Re-read the schedule under its row lock just before dispatching it.

        Returns its current state, or ``None`` — logged once at INFO — when it
        was deleted since the poll. The lock is released when the read's
        transaction ends; it only makes the read wait for a delete or edit
        still in flight (module docstring).
        """

        async with self.uow:
            current = await self.uow.schedules.lock(schedule_id)
        if current is None:
            _LOG.info(
                "schedule %s was deleted before its %s; not dispatched",
                schedule_id.value,
                what,
            )
            self._forget(schedule_id)
        return current

    def _forget(self, schedule_id: ScheduleId) -> None:
        """Drop the in-memory state of a schedule that no longer exists."""

        self._notifying.pop(schedule_id, None)
        self._backup_retry.pop(schedule_id, None)
        self._deferred.pop(schedule_id, None)

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
            # Commit the poll transaction so a corrupt row quarantined by the
            # look-ahead is persisted rather than rolled back (issue #2150).
            await self.uow.commit()
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
                    line=f"{WARNING_COMMAND_PREFIX}{step.message}",
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
    ) -> tuple[ActionResult | Deferred, dt.datetime, dt.datetime]:
        started_at = self.clock.now()
        execute = self.make_execute()
        result = await execute(
            server_id=schedule.server_id,
            action=schedule.action,
            command=schedule.command,
            only_when_running=schedule.only_when_running,
        )
        finished_at = self.clock.now()
        return result, started_at, finished_at

    async def _record(
        self,
        schedule: Schedule,
        result: ActionResult,
        started_at: dt.datetime,
        finished_at: dt.datetime,
        *,
        advance: bool,
        last_run_at: dt.datetime | None = None,
    ) -> bool:
        """Record the run (and advance it when ``advance``), then audit/notify.

        Returns whether the run was recorded: ``False`` when the schedule was
        deleted before the record could lock its row (module docstring). The
        audit and notification still go out — the action did run.
        """

        run = ScheduleRun(
            id=ScheduleRunId.new(),
            schedule_id=schedule.id,
            started_at=started_at,
            finished_at=finished_at,
            outcome=result.outcome,
            detail=result.detail,
        )
        async with self.uow:
            recorded = await self.uow.schedules.lock(schedule.id) is not None
            if recorded:
                await self.uow.schedule_runs.add(run)
                await self.uow.schedule_runs.prune_for_schedule(
                    schedule.id, keep=self.history_cap
                )
                if advance:
                    await self._advance(
                        schedule, after=finished_at, last_run_at=last_run_at
                    )
                await self.uow.commit()
        if not recorded:
            _LOG.info(
                "schedule %s was deleted during its run; run not recorded",
                schedule.id.value,
            )
        if result.outcome is ScheduleRunOutcome.SUCCESS:
            await self._audit(schedule, result.community_id, Outcome.SUCCESS)
            # A success that ends a run of failures clears the notifying state and
            # fires exactly one recovery notification (issue #2269).
            if self._notifying.pop(schedule.id, None) is not None:
                self.notifier.notify(
                    server_id=schedule.server_id,
                    kind=_NOTIFY_KIND_RECOVERED,
                    title=f"Scheduled {schedule.action.value} recovered",
                    detail=result.detail or "",
                )
        elif result.outcome in (ScheduleRunOutcome.FAILURE, ScheduleRunOutcome.UNKNOWN):
            await self._audit(schedule, result.community_id, Outcome.ERROR)
            # State-transition suppression (issue #2269): notify only on the
            # failing edge; sustained failures record run rows every occurrence
            # (above) but emit a single toast until the schedule recovers. An
            # unknown outcome is on the same edge, but its title must not say the
            # action failed: it may have run (issue #3660). A failure after an
            # unknown is an escalation and notifies once more; an unknown after a
            # failure is not, and stays silent (issue #3781).
            notified = self._notifying.get(schedule.id)
            if notified is None or (
                notified is ScheduleRunOutcome.UNKNOWN
                and result.outcome is ScheduleRunOutcome.FAILURE
            ):
                self._notifying[schedule.id] = result.outcome
                title = (
                    f"Scheduled {schedule.action.value}: outcome unknown"
                    if result.outcome is ScheduleRunOutcome.UNKNOWN
                    else f"Scheduled {schedule.action.value} failed"
                )
                self.notifier.notify(
                    server_id=schedule.server_id,
                    kind=_NOTIFY_KIND,
                    title=title,
                    detail=result.detail or "",
                )
        if not recorded:
            self._forget(schedule.id)
        return recorded

    async def _advance(
        self,
        schedule: Schedule,
        *,
        after: dt.datetime,
        last_run_at: dt.datetime | None,
    ) -> None:
        # Staged inside _record's transaction, so the run row and the advance
        # commit together. ``after`` is when the run finished, not the tick
        # start: after a long execution the next occurrence must be strictly in
        # the future of the run, or a slow run could land next_run_at already in
        # the past. Not the commit instant either: a record owed since an
        # earlier tick (issue #4108) advances as if it had committed then, so an
        # occurrence that came due meanwhile is polled next, under the
        # missed-run rule (module docstring). The write goes through the narrow
        # bookkeeping UPDATE (guarded WHERE enabled AND next_run_at =
        # fired_occurrence) so a concurrent CRUD edit that changed the cadence
        # is never clobbered and a concurrently disabled schedule stays
        # disabled — 0 rows matched is the silent-skip contract.
        fired = schedule.next_run_at
        assert fired is not None  # list_due predicate guarantees non-None
        await self.uow.schedules.advance_run_state(
            schedule.id,
            fired_occurrence=fired,
            next_run_at=self._next_after(schedule, after),
            last_run_at=last_run_at,
        )

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
        self, schedule_id: ScheduleId, result: ActionResult, now: dt.datetime
    ) -> None:
        if result.outcome is ScheduleRunOutcome.FAILURE and result.retryable:
            self._backup_retry[schedule_id] = now + self.backup_retry_delay
        elif result.outcome is ScheduleRunOutcome.SUCCESS:
            self._backup_retry.pop(schedule_id, None)
        # SKIPPED, UNKNOWN, or a non-retryable (deterministic) FAILURE such as a
        # corrupt working set (#2250): leave any pending retry untouched — a skip is
        # not a fresh failure, an unknown outcome may still have run so a retry
        # could repeat it (#3660), and a deterministic failure's re-archive can
        # never succeed.

    def _is_stale(
        self,
        schedule: Schedule,
        now: dt.datetime,
        prev_tick_at: dt.datetime | None,
    ) -> bool:
        """Whether the due occurrence is too old to still execute (non-backup).

        An occurrence that became due after ``prev_tick_at`` is being listed at
        the earliest possible tick; its lateness is self-inflicted by a
        long-running sibling, not an outage, so it is never considered stale
        (#1947).
        """

        if schedule.next_run_at is None:
            return False
        if now - schedule.next_run_at <= LATE_RUN_GRACE:
            return False
        # If the occurrence became due after the previous tick began, its
        # lateness is entirely caused by this tick's own execution time — the
        # schedule had no earlier opportunity to run.
        if prev_tick_at is not None and schedule.next_run_at > prev_tick_at:
            return False
        return True

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
