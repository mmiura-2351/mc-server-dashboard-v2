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
    async def list_game_ports(self) -> set[int]:
        """Return the game ports currently taken across all servers (issue #243).

        Deployment-wide (not community-scoped): a game port is a deployment
        resource, unique across every server. Used by create to pick the lowest
        free in-range port and by the availability endpoints. NULL ports (legacy
        rows) hold no port and are excluded.
        """

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
        *,
        unassign: bool = False,
    ) -> bool:
        """Cache the worker-reported observed state for ``server_id`` (FR-SRV-4).

        A no-op if the server is absent (it may have been deleted while the
        Worker still tracked it).

        ``unassign`` additionally clears ``assigned_worker_id`` in the same write.
        It is set only on a CONFIRMED stop (a graceful stop the Worker reports
        complete, or a SERVER_NOT_FOUND convergence) where no live instance can
        remain: clearing the assignment then lets a later start re-place under
        ``require_unassigned`` (issue #206). Crash/disconnect paths leave the
        assignment intact (the stickiness invariant), so they do not set it.

        Returns ``True`` when the write landed, ``False`` when the #216 monotonic
        guard dropped it (a same-instant or fresher write already stamped the row)
        or the server is absent. A convergence caller uses this to keep its
        returned entity honest (issue #292): it mutates the entity's observed
        fields only when ``True``, otherwise leaves them as-read so the return
        never claims a write that did not land.
        """

    @abc.abstractmethod
    async def mark_worker_servers_unknown(
        self, worker_id: WorkerId, observed_at: dt.datetime
    ) -> None:
        """Set observed=unknown for all servers assigned to ``worker_id`` (FR-WRK-4)."""

    @abc.abstractmethod
    async def reset_unverifiable_observed_states(self, observed_at: dt.datetime) -> int:
        """Invalidate the observed cache for assigned, in-flight servers (issue #224).

        Set ``observed=unknown`` (with a fresh ``observed_at``) for every server
        that has a non-null ``assigned_worker_id`` and a non-terminal observed
        state (``starting``, ``running``, ``stopping``, ``restarting``). The
        assignment is kept (the stickiness invariant): only the observed-state
        cache is cleared.

        Called once on API startup, before the reconciler loop begins. Observed
        state is a cache of worker reports; after a full-stack restart the API
        never observed the heartbeat lapse, so a row can persist as
        ``(desired=running, observed=running)`` with no live instance — phantom
        running forever, since the reconciler treats ``observed=running`` as
        converged. Resetting it to ``unknown`` makes that state unverifiable until
        a worker re-reports, so the reconciler converges truthfully.

        Terminal/cache-stable observed states (``stopped``, ``crashed``,
        ``unknown``) are already truthful across a restart and are left untouched,
        as are unassigned rows. Returns the number of rows updated.
        """

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
    async def list_all(self) -> list[Server]:
        """Return every server, spanning all communities (FR-BAK-3).

        The candidate set the periodic scheduled-backup scheduler iterates: unlike
        the snapshot scheduler (running-only), a scheduled backup applies to an
        at-rest server too (archived directly from Storage, no Worker), so the
        scheduler must see every server and branch on each one's state. The
        scheduler filters to those carrying a per-server schedule in config. A
        process-wide background task, not scoped to one community.
        """

    @abc.abstractmethod
    async def list_reconcilable(self) -> list[Server]:
        """Return servers whose desired/observed states may have diverged (issue #101).

        The candidate set the periodic divergence reconciler iterates: servers
        where the operator's intent and the last Worker-reported reality could be
        out of step and an intent re-dispatch may be owed. Three shapes qualify:

        - ``desired=running`` with an observed state that is neither ``starting``
          nor ``running`` (a start that was never delivered, or a crash that the
          Worker reported);
        - ``desired=running`` with no assigned Worker (a compensation-failure
          orphan: the intent committed but placement never stuck);
        - ``desired=stopped`` with ``observed=running`` (a stop that was never
          delivered).

        Aligned servers (``running``/``starting`` under a running intent, settled
        ``stopped`` under a stopped intent) are excluded so the reconciler's tick
        cost scales with divergence, not fleet size. The grace-window and
        Worker-connectivity decisions are applied by the reconciler use case on the
        returned candidates. Spans all communities — a process-wide background
        task, not scoped to one community.
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
