"""Async-SQLAlchemy implementation of the ``ServerRepository`` Port.

Works on an ``AsyncSession`` owned by the enclosing ``UnitOfWork``; it stages
rows and runs reads but never commits — commit is the unit of work's job
(DATABASE.md Section 1). Rows are translated to/from the framework-free domain
entity here.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from sqlalchemy import CursorResult, and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.servers.adapters.models import ServerModel
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.memory_limit import memory_limit_from_config
from mc_server_dashboard_api.servers.domain.repositories import ServerRepository
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)


def _to_server(row: ServerModel) -> Server:
    return Server(
        id=ServerId(row.id),
        community_id=CommunityId(row.community_id),
        name=ServerName(row.name),
        mc_edition=row.mc_edition,
        mc_version=row.mc_version,
        server_type=ServerType(row.server_type),
        execution_backend=ExecutionBackend(row.execution_backend),
        config=dict(row.config),
        game_port=row.game_port,
        desired_state=DesiredState(row.desired_state),
        observed_state=ObservedState(row.observed_state),
        observed_at=row.observed_at,
        assigned_worker_id=(
            None if row.assigned_worker_id is None else WorkerId(row.assigned_worker_id)
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyServerRepository(ServerRepository):
    """:class:`ServerRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, server: Server) -> None:
        self._session.add(
            ServerModel(
                id=server.id.value,
                community_id=server.community_id.value,
                name=server.name.value,
                mc_edition=server.mc_edition,
                mc_version=server.mc_version,
                server_type=server.server_type.value,
                execution_backend=server.execution_backend.value,
                config=server.config,
                game_port=server.game_port,
                desired_state=server.desired_state.value,
                observed_state=server.observed_state.value,
                observed_at=server.observed_at,
                assigned_worker_id=(
                    None
                    if server.assigned_worker_id is None
                    else server.assigned_worker_id.value
                ),
                created_at=server.created_at,
                updated_at=server.updated_at,
            )
        )

    async def get_by_id(self, server_id: ServerId) -> Server | None:
        row = await self._session.get(ServerModel, server_id.value)
        return _to_server(row) if row is not None else None

    async def get_by_community_and_name(
        self, community_id: CommunityId, name: ServerName
    ) -> Server | None:
        stmt = select(ServerModel).where(
            ServerModel.community_id == community_id.value,
            ServerModel.name == name.value,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_server(row) if row is not None else None

    async def list_for_community(self, community_id: CommunityId) -> list[Server]:
        stmt = select(ServerModel).where(ServerModel.community_id == community_id.value)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_server(row) for row in rows]

    async def list_game_ports(self) -> set[int]:
        stmt = select(ServerModel.game_port).where(ServerModel.game_port.is_not(None))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {port for port in rows if port is not None}

    async def list_ids_missing_game_port(self) -> list[ServerId]:
        stmt = select(ServerModel.id).where(ServerModel.game_port.is_(None))
        rows = (await self._session.execute(stmt)).scalars().all()
        return [ServerId(row) for row in rows]

    async def update(self, server: Server) -> None:
        stmt = (
            update(ServerModel)
            .where(ServerModel.id == server.id.value)
            .values(
                name=server.name.value,
                config=server.config,
                # Persist the (possibly changed) game port (issue #311); a no-op
                # write for name/config-only edits that leave it unchanged. The
                # deployment-wide UNIQUE(game_port) backstops a concurrent racer.
                game_port=server.game_port,
                updated_at=server.updated_at,
            )
        )
        await self._session.execute(stmt)

    async def update_lifecycle(
        self,
        server: Server,
        *,
        expected_from: DesiredState,
        require_unassigned: bool = False,
    ) -> bool:
        # Compare-and-set: the WHERE clause carries the expected-from precondition
        # so a concurrent transition that already moved the row matches no row and
        # returns rowcount 0 (the lost-race signal). require_unassigned adds the
        # start precondition that nothing has placed the server yet.
        conditions = [
            ServerModel.id == server.id.value,
            ServerModel.desired_state == expected_from.value,
        ]
        if require_unassigned:
            conditions.append(ServerModel.assigned_worker_id.is_(None))
        stmt = (
            update(ServerModel)
            .where(*conditions)
            .values(
                desired_state=server.desired_state.value,
                assigned_worker_id=(
                    None
                    if server.assigned_worker_id is None
                    else server.assigned_worker_id.value
                ),
                # Persist config alongside the flip so StartServer's resolved-JAR
                # reference (issue #118) lands atomically with the desired-state
                # change. Other callers (stop/restart) pass an unchanged config, so
                # this is a no-op write for them.
                config=server.config,
                updated_at=server.updated_at,
            )
        )
        result = await self._session.execute(stmt)
        # An UPDATE returns a CursorResult, whose rowcount is the matched count;
        # the compare-and-set matched a row iff it equals 1 (the lost race is 0).
        return cast("CursorResult[Any]", result).rowcount == 1

    async def record_observed_state(
        self,
        server_id: ServerId,
        observed_state: ObservedState,
        observed_at: dt.datetime,
        *,
        unassign: bool = False,
    ) -> bool:
        values: dict[str, Any] = {
            "observed_state": observed_state.value,
            "observed_at": observed_at,
        }
        # On a CONFIRMED stop, clear the assignment in the same write so a later
        # start can re-place under require_unassigned (issue #206).
        if unassign:
            values["assigned_worker_id"] = None
        # Monotonic guard (issue #216): drop a write stamped no later than the row's
        # current observed_at, so a stale convergence write (stop SERVER_NOT_FOUND,
        # redispatch_start INVALID_STATE) cannot clobber a fresher StatusChange that
        # already landed. All observed-state writers stamp clock.now(). A never-
        # observed row has observed_at IS NULL and must still accept its first write.
        # Strict ``<`` (not ``<=``) is right here: observed_at is timestamptz with
        # microsecond resolution and every writer uses the same API clock, so equal
        # timestamps mean a genuine same-instant duplicate that is safe to drop. The
        # guard and the unassign live in one UPDATE, so a stale write's unassign
        # decision is dropped atomically with the state write.
        stmt = (
            update(ServerModel)
            .where(
                ServerModel.id == server_id.value,
                or_(
                    ServerModel.observed_at.is_(None),
                    ServerModel.observed_at < observed_at,
                ),
            )
            .values(**values)
        )
        result = await self._session.execute(stmt)
        # Report whether the guard accepted the write (rowcount == 1) so a
        # convergence caller can keep its returned entity honest (issue #292):
        # when the guard drops the write (0 rows; a same-instant or fresher write
        # already landed) the caller must not optimistically mutate the entity.
        return cast("CursorResult[Any]", result).rowcount == 1

    async def clear_assignment_after_final_snapshot(
        self, server_id: ServerId, worker_id: WorkerId
    ) -> bool:
        # Guarded so the late unassign only clears OUR held assignment (issue #847):
        # match only a still desired=stopped row still assigned to worker_id. A start
        # cannot have re-placed during the snapshot window (its require_unassigned CAS
        # saw the held assignment), so the guard normally matches; it makes the clear
        # a no-op rather than a clobber if the row moved by any other path.
        stmt = (
            update(ServerModel)
            .where(
                ServerModel.id == server_id.value,
                ServerModel.desired_state == DesiredState.STOPPED.value,
                ServerModel.assigned_worker_id == worker_id.value,
            )
            .values(assigned_worker_id=None)
        )
        result = await self._session.execute(stmt)
        return cast("CursorResult[Any]", result).rowcount == 1

    async def mark_worker_servers_unknown(
        self, worker_id: WorkerId, observed_at: dt.datetime
    ) -> None:
        # No monotonic guard (issue #216): this is a bulk cache-invalidation marking
        # state LESS certain (-> unknown) on a worker disconnect. It must always win,
        # even against a row with a newer observed_at — that fresher report came from
        # the now-disconnected worker and is exactly the untrustworthy data this
        # write exists to invalidate.
        stmt = (
            update(ServerModel)
            .where(ServerModel.assigned_worker_id == worker_id.value)
            .values(
                observed_state=ObservedState.UNKNOWN.value,
                observed_at=observed_at,
            )
        )
        await self._session.execute(stmt)

    async def reset_unverifiable_observed_states(self, observed_at: dt.datetime) -> int:
        # Assigned rows whose observed state is non-terminal (an in-flight cache
        # of a worker report). Terminal/cache-stable states (stopped, crashed,
        # unknown) stay truthful across a restart, so they are excluded.
        # No monotonic guard (issue #216): like mark_worker_servers_unknown this is a
        # bulk cache-invalidation marking state LESS certain (-> unknown) on API
        # restart, and must always win regardless of the row's observed_at.
        stmt = (
            update(ServerModel)
            .where(
                ServerModel.assigned_worker_id.is_not(None),
                ServerModel.observed_state.in_(
                    [
                        ObservedState.STARTING.value,
                        ObservedState.RUNNING.value,
                        ObservedState.STOPPING.value,
                        ObservedState.RESTARTING.value,
                    ]
                ),
            )
            .values(
                observed_state=ObservedState.UNKNOWN.value,
                observed_at=observed_at,
            )
        )
        result = await self._session.execute(stmt)
        return cast("CursorResult[Any]", result).rowcount

    async def running_assignment_ids_for_worker(
        self, worker_id: WorkerId
    ) -> dict[str, int]:
        # Also read each server's config blob so the rebuild restores committed
        # memory, not just the count (#843); the declared limit is derived with the
        # same validator the placement path uses (0 = unset).
        stmt = select(ServerModel.id, ServerModel.config).where(
            ServerModel.assigned_worker_id == worker_id.value,
            ServerModel.desired_state == DesiredState.RUNNING.value,
        )
        rows = (await self._session.execute(stmt)).all()
        return {
            str(row.id): memory_limit_from_config(dict(row.config)) or 0 for row in rows
        }

    async def list_running_assigned(self) -> list[Server]:
        stmt = select(ServerModel).where(
            ServerModel.desired_state == DesiredState.RUNNING.value,
            ServerModel.assigned_worker_id.is_not(None),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_server(row) for row in rows]

    async def list_all(self) -> list[Server]:
        stmt = select(ServerModel)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_server(row) for row in rows]

    async def list_reconcilable(self) -> list[Server]:
        running = DesiredState.RUNNING.value
        stopped = DesiredState.STOPPED.value
        stmt = select(ServerModel).where(
            or_(
                # desired=running but observed neither starting nor running
                # (start never delivered, or a Worker-reported crash).
                and_(
                    ServerModel.desired_state == running,
                    ServerModel.observed_state.notin_(
                        [ObservedState.STARTING.value, ObservedState.RUNNING.value]
                    ),
                ),
                # desired=running with no assigned Worker (compensation-failure
                # orphan); caught regardless of observed state.
                and_(
                    ServerModel.desired_state == running,
                    ServerModel.assigned_worker_id.is_(None),
                ),
                # desired=stopped but the Worker still reports it running.
                and_(
                    ServerModel.desired_state == stopped,
                    ServerModel.observed_state == ObservedState.RUNNING.value,
                ),
                # desired=stopped, observed=stopped, still assigned (issue #847,
                # bug 2): a stop wedged because its deferred unassign never ran (an
                # API crash or HTTP-task cancellation mid final-snapshot). No other
                # path converges this triple — StartServer's require_unassigned CAS
                # 409s before flipping desired, and the sink no longer unassigns —
                # so the reconciler's stale-stop arm releases the assignment.
                and_(
                    ServerModel.desired_state == stopped,
                    ServerModel.observed_state == ObservedState.STOPPED.value,
                    ServerModel.assigned_worker_id.is_not(None),
                ),
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_server(row) for row in rows]

    async def delete(self, server_id: ServerId) -> None:
        stmt = delete(ServerModel).where(ServerModel.id == server_id.value)
        await self._session.execute(stmt)
