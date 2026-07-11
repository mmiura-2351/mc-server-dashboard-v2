"""HTTP edge for the general scheduler's CRUD surface (epic #649, issue #1837).

Routes live under ``/communities/{community_id}/servers/{server_id}/schedules``
(+ ``/{schedule_id}`` and ``/{schedule_id}/runs``). A schedule is a per-server
resource, so the gate is per-server (``resource_type='server'``): a resource grant
on one server opens exactly that server's schedules (FR-AUTHZ-2).

Reads require ``schedule:read``. Writes are **two-layer**: ``schedule:manage`` and
the permission for the action the schedule performs (``command`` ->
``server:command``, ``start`` -> ``server:start``, ``stop`` -> ``server:stop``,
``restart`` -> ``server:restart``, ``backup`` -> ``backup:schedule``). So
``schedule:manage`` alone cannot schedule an action the caller could not run
directly. Layer-1 membership is enforced at the edge (non-member -> 404, no
existence signal); the write routes defer the two-layer Layer-2 decision to the
use case (which knows the action) via a resource-scoped ``authorize`` callable,
and a denied permission is a 403 carrying the missing code in the ``permission``
member.

**Authorization is write-time only (deliberate).** The gate is checked when a
schedule is created or edited; the runner (#1838) later executes each occurrence
as the *system*. Revoking a member's permission does not stop schedules they
already created — a ``schedule:manage`` holder must disable or delete them.

The router is thin: it resolves use cases via dependency injection, runs them,
serialises the result, translates domain errors to HTTP codes, and audits the
mutations (FR-AUD-1).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.community.domain.value_objects import AuthUser, Permission
from mc_server_dashboard_api.dependencies import (
    ScheduleWriteAuthz,
    get_audit_recorder,
    get_create_schedule,
    get_delete_schedule,
    get_list_schedule_runs,
    get_list_schedules,
    get_read_schedule,
    get_update_schedule,
    require_permission,
    require_schedule_write_authz,
)
from mc_server_dashboard_api.http_datetime import UtcDatetime
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.servers.application.schedules import (
    CreateSchedule,
    DeleteSchedule,
    ListScheduleRuns,
    ListSchedules,
    ReadSchedule,
    UpdateSchedule,
    WarningStepInput,
)
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidCronExpressionError,
    InvalidScheduleCadenceError,
    InvalidScheduleError,
    InvalidScheduleNameError,
    InvalidSchedulePayloadError,
    InvalidScheduleTimezoneError,
    PermissionDeniedError,
    ScheduleNameAlreadyExistsError,
    ScheduleNotFoundError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.schedule import (
    DEFAULT_TIMEZONE,
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

router = APIRouter()

_SERVER_RESOURCE_TYPE = "server"


class WarningStepBody(BaseModel):
    """A pre-action player-warning step for a stop/restart schedule (#1839)."""

    offset_minutes: int
    message: str = Field(min_length=1)


class CreateScheduleRequest(BaseModel):
    name: str = Field(min_length=1)
    action: ScheduleAction
    cron: str | None = None
    interval_seconds: int | None = None
    timezone: str = DEFAULT_TIMEZONE
    enabled: bool = True
    command: str | None = None
    warning_steps: list[WarningStepBody] | None = None


class UpdateScheduleRequest(BaseModel):
    """A partial edit. An omitted field keeps its value; ``warning_steps: []``
    clears the steps (distinct from omitting it). The action is immutable."""

    name: str | None = Field(default=None, min_length=1)
    cron: str | None = None
    interval_seconds: int | None = None
    timezone: str | None = None
    enabled: bool | None = None
    command: str | None = None
    warning_steps: list[WarningStepBody] | None = None


class WarningStepResponse(BaseModel):
    offset_minutes: int
    message: str


class ScheduleResponse(BaseModel):
    """Public view of a schedule (issue #1837)."""

    id: str
    server_id: str
    name: str
    action: str
    cron: str | None
    interval_seconds: int | None
    timezone: str
    enabled: bool
    command: str | None
    warning_steps: list[WarningStepResponse]
    next_run_at: UtcDatetime | None
    last_run_at: UtcDatetime | None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    created_by: str | None

    @classmethod
    def from_entity(cls, schedule: Schedule) -> "ScheduleResponse":
        return cls(
            id=str(schedule.id.value),
            server_id=str(schedule.server_id.value),
            name=schedule.name,
            action=schedule.action.value,
            cron=schedule.cadence.cron,
            interval_seconds=schedule.cadence.interval_seconds,
            timezone=schedule.timezone,
            enabled=schedule.enabled,
            command=schedule.command,
            warning_steps=[
                WarningStepResponse(
                    offset_minutes=step.offset_minutes, message=step.message
                )
                for step in schedule.warning_steps
            ],
            next_run_at=schedule.next_run_at,
            last_run_at=schedule.last_run_at,
            created_at=schedule.created_at,
            updated_at=schedule.updated_at,
            created_by=(
                None if schedule.created_by is None else str(schedule.created_by)
            ),
        )


class ScheduleRunResponse(BaseModel):
    """Public view of one recorded schedule execution (issue #1837)."""

    id: str
    schedule_id: str
    started_at: UtcDatetime
    finished_at: UtcDatetime
    outcome: str
    detail: str | None

    @classmethod
    def from_entity(cls, run: ScheduleRun) -> "ScheduleRunResponse":
        return cls(
            id=str(run.id.value),
            schedule_id=str(run.schedule_id.value),
            started_at=run.started_at,
            finished_at=run.finished_at,
            outcome=run.outcome.value,
            detail=run.detail,
        )


def _warning_inputs(
    steps: list[WarningStepBody] | None,
) -> list[WarningStepInput] | None:
    if steps is None:
        return None
    return [(step.offset_minutes, step.message) for step in steps]


@router.post(
    "/communities/{community_id}/servers/{server_id}/schedules",
    status_code=status.HTTP_201_CREATED,
)
async def create_schedule(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    body: CreateScheduleRequest,
    authz: Annotated[
        ScheduleWriteAuthz,
        Depends(
            require_schedule_write_authz(
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[CreateSchedule, Depends(get_create_schedule)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> ScheduleResponse:
    authorized = authz.auth_user
    try:
        schedule = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            authorize=authz.authorize,
            name=body.name,
            action=body.action,
            cron=body.cron,
            interval_seconds=body.interval_seconds,
            timezone=body.timezone,
            enabled=body.enabled,
            command=body.command,
            warning_steps=_warning_inputs(body.warning_steps),
            created_by=authorized.user_id.value,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except PermissionDeniedError as exc:
        raise _forbidden(exc.permission) from exc
    except ScheduleNameAlreadyExistsError as exc:
        raise _conflict("schedule_name_exists") from exc
    except InvalidScheduleError as exc:
        raise _schedule_unprocessable(exc) from exc
    await _record(
        recorder, ops.SCHEDULE_CREATE, authorized, community_id, schedule.id.value
    )
    return ScheduleResponse.from_entity(schedule)


@router.get("/communities/{community_id}/servers/{server_id}/schedules")
async def list_schedules(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    _authorized: Annotated[
        object,
        Depends(
            require_permission(
                Permission("schedule:read"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[ListSchedules, Depends(get_list_schedules)],
) -> list[ScheduleResponse]:
    try:
        schedules = await use_case(
            community_id=CommunityId(community_id), server_id=ServerId(server_id)
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    return [ScheduleResponse.from_entity(schedule) for schedule in schedules]


@router.get("/communities/{community_id}/servers/{server_id}/schedules/{schedule_id}")
async def read_schedule(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    schedule_id: uuid.UUID,
    _authorized: Annotated[
        object,
        Depends(
            require_permission(
                Permission("schedule:read"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[ReadSchedule, Depends(get_read_schedule)],
) -> ScheduleResponse:
    try:
        schedule = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            schedule_id=ScheduleId(schedule_id),
        )
    except (ServerNotFoundError, ScheduleNotFoundError) as exc:
        raise _not_found() from exc
    return ScheduleResponse.from_entity(schedule)


@router.patch("/communities/{community_id}/servers/{server_id}/schedules/{schedule_id}")
async def update_schedule(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    schedule_id: uuid.UUID,
    body: UpdateScheduleRequest,
    authz: Annotated[
        ScheduleWriteAuthz,
        Depends(
            require_schedule_write_authz(
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[UpdateSchedule, Depends(get_update_schedule)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> ScheduleResponse:
    authorized = authz.auth_user
    try:
        schedule = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            schedule_id=ScheduleId(schedule_id),
            authorize=authz.authorize,
            name=body.name,
            cron=body.cron,
            interval_seconds=body.interval_seconds,
            timezone=body.timezone,
            enabled=body.enabled,
            command=body.command,
            warning_steps=_warning_inputs(body.warning_steps),
        )
    except (ServerNotFoundError, ScheduleNotFoundError) as exc:
        raise _not_found() from exc
    except PermissionDeniedError as exc:
        raise _forbidden(exc.permission) from exc
    except ScheduleNameAlreadyExistsError as exc:
        raise _conflict("schedule_name_exists") from exc
    except InvalidScheduleError as exc:
        raise _schedule_unprocessable(exc) from exc
    await _record(
        recorder, ops.SCHEDULE_UPDATE, authorized, community_id, schedule.id.value
    )
    return ScheduleResponse.from_entity(schedule)


@router.delete(
    "/communities/{community_id}/servers/{server_id}/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_schedule(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    schedule_id: uuid.UUID,
    authz: Annotated[
        ScheduleWriteAuthz,
        Depends(
            require_schedule_write_authz(
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[DeleteSchedule, Depends(get_delete_schedule)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    authorized = authz.auth_user
    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            schedule_id=ScheduleId(schedule_id),
            authorize=authz.authorize,
        )
    except (ServerNotFoundError, ScheduleNotFoundError) as exc:
        raise _not_found() from exc
    except PermissionDeniedError as exc:
        raise _forbidden(exc.permission) from exc
    await _record(recorder, ops.SCHEDULE_DELETE, authorized, community_id, schedule_id)


@router.get(
    "/communities/{community_id}/servers/{server_id}/schedules/{schedule_id}/runs"
)
async def list_schedule_runs(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    schedule_id: uuid.UUID,
    _authorized: Annotated[
        object,
        Depends(
            require_permission(
                Permission("schedule:read"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[ListScheduleRuns, Depends(get_list_schedule_runs)],
) -> list[ScheduleRunResponse]:
    try:
        runs = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            schedule_id=ScheduleId(schedule_id),
        )
    except (ServerNotFoundError, ScheduleNotFoundError) as exc:
        raise _not_found() from exc
    return [ScheduleRunResponse.from_entity(run) for run in runs]


async def _record(
    recorder: AuditRecorder,
    operation: str,
    authorized: AuthUser,
    community_id: uuid.UUID,
    target_id: uuid.UUID,
) -> None:
    """Record a successful schedule mutation (FR-AUD-1), fire-after-commit."""

    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_SCHEDULE,
            target_id=target_id,
        )
    )


# Each validation family maps to a distinct 422 reason (issue #1837). The
# subclasses are checked most-specific first; the base ``InvalidScheduleError`` is
# the catch-all for any entity-state invariant not covered above.
_SCHEDULE_422_REASONS: tuple[tuple[type[InvalidScheduleError], str], ...] = (
    (InvalidCronExpressionError, "invalid_cron"),
    (InvalidScheduleCadenceError, "invalid_cadence"),
    (InvalidScheduleTimezoneError, "invalid_timezone"),
    (InvalidScheduleNameError, "invalid_schedule_name"),
    (InvalidSchedulePayloadError, "invalid_payload"),
)


def _schedule_unprocessable(exc: InvalidScheduleError) -> ProblemException:
    for error_type, reason in _SCHEDULE_422_REASONS:
        if isinstance(exc, error_type):
            return _unprocessable(reason)
    return _unprocessable("invalid_schedule")


def _unprocessable(reason: str) -> ProblemException:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, reason)


def _conflict(reason: str) -> ProblemException:
    return problem(status.HTTP_409_CONFLICT, reason)


def _forbidden(permission: str) -> ProblemException:
    # The two-layer write gate denied a specific permission; carry its code in the
    # ``permission`` member so the Web UI can name what is missing (#425/#555).
    return problem(
        status.HTTP_403_FORBIDDEN, "forbidden", extensions={"permission": permission}
    )


def _not_found() -> ProblemException:
    # No-existence-signal posture (Section 6.4): a server/schedule outside this
    # community (or on another server) 404s the same as a wholly unknown one.
    return problem(status.HTTP_404_NOT_FOUND, "not_found")
