"""Schedule CRUD use cases for the general scheduler (epic #649, issue #1837).

These run *after* the route's authorization dependency has admitted the caller
(Layer-1 membership: non-member -> 404, Section 6.4), so they assume a member and
do the data work plus the Layer-2 permission decision the write gate defers to
them.

**Write gate is two-layer (anti-escalation).** A create/update/delete of a
schedule requires ``schedule:manage`` *and* the permission for the action the
schedule performs — ``command`` -> ``server:command``, ``start`` ->
``server:start``, ``stop`` -> ``server:stop``, ``restart`` -> ``server:restart``,
``backup`` -> ``backup:schedule``. So ``schedule:manage`` alone cannot be used to
make the system run an action the caller could not run directly (e.g. schedule a
console command without ``server:command``). The write routes hand the use case a
resource-scoped ``authorize(code)`` callable (Layer-1 already checked at the edge)
and the use case denies on the first required code the caller lacks. The
per-action warning steps on a stop/restart schedule are a fixed ``say`` broadcast
the runner sends — not arbitrary console commands — so they need only the
stop/restart permission, never ``server:command``.

**Authorization is write-time only (deliberate).** The gate is evaluated when a
schedule is created or edited; the runner later executes each occurrence as the
*system*, not as the creator. Revoking a member's permission (or deleting the
member) does not stop schedules they already created — those keep firing until a
caller holding ``schedule:manage`` *plus the schedule's action permission*
disables or deletes them (disable and delete are writes, so the same two-layer
gate applies). ``created_by`` is a soft actor reference for the audit trail, not
a live authorization anchor.

Cross-scope safety mirrors the other servers use cases: a server outside the path
community, or a schedule on a different server, is reported not-found
(:class:`ServerNotFoundError` / :class:`ScheduleNotFoundError`), leaking no
existence signal (FR-COMM-3). ``next_run_at`` is recomputed from the cadence on
create/update whenever the schedule is enabled, and is ``None`` exactly while it
is disabled (the entity invariant).
"""

from __future__ import annotations

import datetime as dt
import uuid
import zoneinfo
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidSchedulePayloadError,
    InvalidScheduleTimezoneError,
    PermissionDeniedError,
    ScheduleNameAlreadyExistsError,
    ScheduleNotFoundError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.next_run_calculator import (
    NextRunCalculator,
)
from mc_server_dashboard_api.servers.domain.schedule import (
    DEFAULT_TIMEZONE,
    Cadence,
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
    WarningStep,
    next_interval_run,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)

_SCHEDULE_MANAGE_PERMISSION = "schedule:manage"

# The action's own permission the write gate also requires (anti-escalation):
# a schedule may only be created/edited to perform an action the caller is itself
# authorized to perform.
_ACTION_PERMISSION: dict[ScheduleAction, str] = {
    ScheduleAction.COMMAND: "server:command",
    ScheduleAction.START: "server:start",
    ScheduleAction.STOP: "server:stop",
    ScheduleAction.RESTART: "server:restart",
    ScheduleAction.BACKUP: "backup:schedule",
}

# A warning-step input is a raw ``(offset_minutes, message)`` pair; the use case
# builds the validated :class:`WarningStep` so a domain validation failure is
# raised inside the call and mapped to 422 at the edge.
WarningStepInput = tuple[int, str]

# The Layer-2 permission decision the write gate defers to the use case, bound to
# the schedule's server resource at the edge (FR-AUTHZ-2).
Authorize = Callable[[str], Awaitable[bool]]


async def _authorize_write(authorize: Authorize, action: ScheduleAction) -> None:
    """Enforce the two-layer write gate, denying on the first missing code.

    ``schedule:manage`` is checked first (a wholly unauthorized caller is denied
    on it), then the action's own permission (anti-escalation). The missing code
    is carried on :class:`PermissionDeniedError` so the edge names it in the 403.
    """

    for code in (_SCHEDULE_MANAGE_PERMISSION, _ACTION_PERMISSION[action]):
        if not await authorize(code):
            raise PermissionDeniedError(code)


async def _require_server(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> None:
    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))


async def _load_schedule(
    uow: UnitOfWork,
    community_id: CommunityId,
    server_id: ServerId,
    schedule_id: ScheduleId,
) -> Schedule:
    await _require_server(uow, community_id, server_id)
    schedule = await uow.schedules.get_by_id(schedule_id)
    if schedule is None or schedule.server_id != server_id:
        raise ScheduleNotFoundError(str(schedule_id.value))
    return schedule


async def _ensure_unique_name(
    uow: UnitOfWork,
    server_id: ServerId,
    name: str,
    *,
    exclude_id: ScheduleId | None = None,
) -> None:
    for existing in await uow.schedules.list_for_server(server_id):
        if existing.name == name and existing.id != exclude_id:
            raise ScheduleNameAlreadyExistsError(name)


def _warning_steps(steps: Sequence[WarningStepInput] | None) -> tuple[WarningStep, ...]:
    if steps is None:
        return ()
    return tuple(
        WarningStep(offset_minutes=offset, message=message) for offset, message in steps
    )


def _validate_warning_offsets(schedule: Schedule) -> None:
    """Reject an interval schedule whose warning cannot precede its own cadence.

    A warning ``offset_minutes`` before the action can only be sent if that
    offset is shorter than the interval; at ``offset_minutes * 60 >=
    interval_seconds`` the warning instant falls on or before the previous
    occurrence, so the runner can never send it on time and logs a missed
    warning on every occurrence (issue #1852). Cron cadences have no fixed
    period, so the check does not apply to them. This is enforced here, at
    create/update, rather than as a :class:`Schedule` invariant, so the runner
    can still load — and keep logging (#1850) — any pre-existing row that
    violates it (the domain entity is reconstructed from every persisted row).
    """

    interval = schedule.cadence.interval_seconds
    if interval is None:
        return
    if any(step.offset_minutes * 60 >= interval for step in schedule.warning_steps):
        raise InvalidSchedulePayloadError(
            "warning offset must be shorter than the interval cadence"
        )


def _next_run_at(
    schedule: Schedule, now: dt.datetime, calculator: NextRunCalculator
) -> dt.datetime:
    """Return the next due instant for an enabled schedule from its cadence.

    Cron cadences are evaluated in the schedule's timezone via the calculator
    Port; interval cadences use the pure epoch-anchored, per-schedule-jittered
    grid. ``now`` is the clock instant the recompute is anchored to.
    """

    cadence = schedule.cadence
    if cadence.cron is not None:
        return calculator.next_after(cadence.cron, schedule.timezone, now)
    assert cadence.interval_seconds is not None  # guaranteed by the cadence XOR
    return next_interval_run(
        schedule.id, interval_seconds=cadence.interval_seconds, after=now
    )


@dataclass(frozen=True)
class CreateSchedule:
    """Create a per-server schedule (schedule:manage + the action's permission)."""

    uow: UnitOfWork
    clock: Clock
    calculator: NextRunCalculator

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        authorize: Authorize,
        name: str,
        action: ScheduleAction,
        cron: str | None = None,
        interval_seconds: int | None = None,
        timezone: str = DEFAULT_TIMEZONE,
        enabled: bool = True,
        command: str | None = None,
        warning_steps: Sequence[WarningStepInput] | None = None,
        created_by: uuid.UUID | None = None,
    ) -> Schedule:
        async with self.uow:
            await _require_server(self.uow, community_id, server_id)
            await _authorize_write(authorize, action)
            cadence = Cadence(cron=cron, interval_seconds=interval_seconds)
            if cadence.cron is not None:
                self.calculator.validate(cadence.cron)
            now = self.clock.now()
            schedule = Schedule(
                id=ScheduleId.new(),
                server_id=server_id,
                name=name,
                action=action,
                cadence=cadence,
                enabled=enabled,
                created_at=now,
                updated_at=now,
                timezone=timezone,
                command=command,
                warning_steps=_warning_steps(warning_steps),
                next_run_at=None,
                last_run_at=None,
                created_by=created_by,
            )
            _validate_warning_offsets(schedule)
            await _ensure_unique_name(self.uow, server_id, schedule.name)
            if enabled:
                schedule = replace(
                    schedule, next_run_at=_next_run_at(schedule, now, self.calculator)
                )
            await self.uow.schedules.add(schedule)
            await self.uow.commit()
        return schedule


@dataclass(frozen=True)
class ListSchedules:
    """List a server's schedules ordered by name (schedule:read)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[Schedule]:
        async with self.uow:
            await _require_server(self.uow, community_id, server_id)
            return await self.uow.schedules.list_for_server(server_id)


@dataclass(frozen=True)
class ReadSchedule:
    """Read one schedule (schedule:read)."""

    uow: UnitOfWork

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        schedule_id: ScheduleId,
    ) -> Schedule:
        async with self.uow:
            return await _load_schedule(self.uow, community_id, server_id, schedule_id)


@dataclass(frozen=True)
class UpdateSchedule:
    """Edit a schedule (schedule:manage + the action's permission).

    A partial (PATCH) edit: a field left unset (``None``) keeps its current value;
    ``warning_steps=[]`` clears the steps (distinct from omitting the field). The
    action itself is immutable — to run a different action, delete and recreate
    (each gated) — so the write gate always checks the *existing* action's
    permission. The entity is rebuilt via :func:`dataclasses.replace` so its
    invariants re-run, and ``next_run_at`` is recomputed when the result is
    enabled (``None`` when disabled).
    """

    uow: UnitOfWork
    clock: Clock
    calculator: NextRunCalculator

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        schedule_id: ScheduleId,
        authorize: Authorize,
        name: str | None = None,
        cron: str | None = None,
        interval_seconds: int | None = None,
        timezone: str | None = None,
        enabled: bool | None = None,
        command: str | None = None,
        warning_steps: Sequence[WarningStepInput] | None = None,
    ) -> Schedule:
        async with self.uow:
            existing = await _load_schedule(
                self.uow, community_id, server_id, schedule_id
            )
            await _authorize_write(authorize, existing.action)
            cadence = existing.cadence
            if cron is not None or interval_seconds is not None:
                cadence = Cadence(cron=cron, interval_seconds=interval_seconds)
                if cadence.cron is not None:
                    self.calculator.validate(cadence.cron)
            new_enabled = existing.enabled if enabled is None else enabled
            now = self.clock.now()
            updated = replace(
                existing,
                name=existing.name if name is None else name,
                cadence=cadence,
                timezone=existing.timezone if timezone is None else timezone,
                enabled=new_enabled,
                command=existing.command if command is None else command,
                warning_steps=(
                    existing.warning_steps
                    if warning_steps is None
                    else _warning_steps(warning_steps)
                ),
                next_run_at=None,
                updated_at=now,
            )
            _validate_warning_offsets(updated)
            if updated.name != existing.name:
                await _ensure_unique_name(
                    self.uow, server_id, updated.name, exclude_id=schedule_id
                )
            if new_enabled:
                updated = replace(
                    updated, next_run_at=_next_run_at(updated, now, self.calculator)
                )
            await self.uow.schedules.update(updated)
            await self.uow.commit()
        return updated


@dataclass(frozen=True)
class DeleteSchedule:
    """Delete a schedule (schedule:manage + the action's permission)."""

    uow: UnitOfWork

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        schedule_id: ScheduleId,
        authorize: Authorize,
    ) -> None:
        async with self.uow:
            schedule = await _load_schedule(
                self.uow, community_id, server_id, schedule_id
            )
            await _authorize_write(authorize, schedule.action)
            await self.uow.schedules.delete(schedule_id)
            await self.uow.commit()


@dataclass(frozen=True)
class ListScheduleRuns:
    """List a schedule's run history newest-first (schedule:read)."""

    uow: UnitOfWork

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        schedule_id: ScheduleId,
    ) -> list[ScheduleRun]:
        async with self.uow:
            await _load_schedule(self.uow, community_id, server_id, schedule_id)
            return await self.uow.schedule_runs.list_for_schedule(schedule_id)


_PREVIEW_COUNT = 5


@dataclass(frozen=True)
class PreviewSchedule:
    """Compute the next N occurrences for a cadence without persisting (schedule:read).

    Validates the cadence and timezone using the same domain rules as create,
    so its 422 reasons match exactly. Returns UTC datetimes.
    """

    clock: Clock
    calculator: NextRunCalculator

    def __call__(
        self,
        *,
        cron: str | None = None,
        interval_seconds: int | None = None,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> list[dt.datetime]:
        cadence = Cadence(cron=cron, interval_seconds=interval_seconds)
        if cadence.cron is not None:
            self.calculator.validate(cadence.cron)
        try:
            zoneinfo.ZoneInfo(timezone)
        except (ValueError, zoneinfo.ZoneInfoNotFoundError):
            raise InvalidScheduleTimezoneError(timezone)
        now = self.clock.now()
        runs: list[dt.datetime] = []
        after = now
        for _ in range(_PREVIEW_COUNT):
            if cadence.cron is not None:
                nxt = self.calculator.next_after(cadence.cron, timezone, after)
            else:
                assert cadence.interval_seconds is not None
                # For preview, use simple arithmetic (no jitter — no schedule id).
                nxt = after + dt.timedelta(seconds=cadence.interval_seconds)
            runs.append(nxt)
            after = nxt
        return runs
