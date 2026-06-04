"""Persistence Ports for the servers context.

The ``ServerRepository`` interface (ARCHITECTURE.md Section 5.1) the domain
depends on; a concrete async-SQLAlchemy adapter implements it. Lookups return
``None`` when absent rather than raising, so callers decide policy.

:class:`ResourceGrantSweeper` is a narrow Port for the server-delete grant sweep
(DATABASE.md Section 10): ``resource_grant`` rows carry no FK on ``resource_id``,
so deleting a server does not cascade to its grants. The sweep is owned by the
community context, but the servers use case must not import another context's
domain (import-linter). This Port is the clean seam: the wiring binds it to the
community resource-grant adapter on the *same* session as the server delete, so
both run in one transaction.
"""

from __future__ import annotations

import abc
import datetime as dt
import uuid

from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    WorkerId,
)


class ServerRepository(abc.ABC):
    """Port: persistence for :class:`Server` aggregates."""

    @abc.abstractmethod
    async def add(self, server: Server) -> None:
        """Stage a new server for persistence within the current transaction."""

    @abc.abstractmethod
    async def get_by_id(self, server_id: ServerId) -> Server | None:
        """Return the server with ``server_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def get_by_community_and_name(
        self, community_id: CommunityId, name: ServerName
    ) -> Server | None:
        """Return the server named ``name`` in ``community_id``, or ``None``."""

    @abc.abstractmethod
    async def list_for_community(self, community_id: CommunityId) -> list[Server]:
        """Return all servers in ``community_id`` (the ``server:read`` listing)."""

    @abc.abstractmethod
    async def update(self, server: Server) -> None:
        """Persist the mutable fields of ``server`` (name, config, timestamps)."""

    @abc.abstractmethod
    async def update_lifecycle(
        self,
        server: Server,
        *,
        expected_from: DesiredState,
        require_unassigned: bool = False,
    ) -> bool:
        """Compare-and-set a server's lifecycle fields (desired state + Worker).

        Distinct from :meth:`update` (name/config edits): the lifecycle ops set
        ``desired_state``, ``assigned_worker_id`` and ``updated_at`` and must not
        touch name/config. Observed state is written separately via
        :meth:`record_observed_state` from the control-plane event path.

        The write is guarded so two concurrent transitions that both passed the
        in-memory check cannot both apply: the UPDATE matches only when the row's
        ``desired_state`` still equals ``expected_from`` (and, when
        ``require_unassigned`` is set for a start, ``assigned_worker_id IS NULL``).
        Returns ``True`` when exactly one row was updated, ``False`` when the
        precondition no longer held (a lost race); the caller turns ``False`` into
        a :class:`LifecycleTransitionConflictError` and does **not** dispatch or
        adjust placement-load counts.
        """

    @abc.abstractmethod
    async def record_observed_state(
        self,
        server_id: ServerId,
        observed_state: ObservedState,
        observed_at: dt.datetime,
    ) -> None:
        """Cache the worker-reported observed state for ``server_id`` (FR-SRV-4).

        A no-op if the server is absent (it may have been deleted while the
        Worker still tracked it).
        """

    @abc.abstractmethod
    async def mark_worker_servers_unknown(
        self, worker_id: WorkerId, observed_at: dt.datetime
    ) -> None:
        """Set observed=unknown for all servers assigned to ``worker_id`` (FR-WRK-4)."""

    @abc.abstractmethod
    async def count_running_for_worker(self, worker_id: WorkerId) -> int:
        """Count servers assigned to ``worker_id`` with desired=running.

        The placement-load tally used to rebuild a reconnected Worker's
        assignment count (epic #7 reconciliation obligation).
        """

    @abc.abstractmethod
    async def list_running_assigned(self) -> list[Server]:
        """Return every server with desired=running and an assigned Worker.

        The candidate set the periodic snapshot scheduler iterates (FR-DATA-7):
        servers the operator wants running that have a Worker to snapshot. It
        spans all communities — the scheduler is a process-wide background task,
        not a request scoped to one community.
        """

    @abc.abstractmethod
    async def delete(self, server_id: ServerId) -> None:
        """Delete the server row (its grants are swept separately, Section 10)."""


class ResourceGrantSweeper(abc.ABC):
    """Port: delete all resource grants on a specific resource (Section 10)."""

    @abc.abstractmethod
    async def delete_for_resource(
        self, resource_type: str, resource_id: uuid.UUID
    ) -> None:
        """Delete all grants on ``(resource_type, resource_id)``.

        Called by the server-delete use case in the same transaction as the
        server-row delete, since grants FK nothing on ``resource_id``.
        """
