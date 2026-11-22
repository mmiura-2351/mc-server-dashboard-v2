"""Persistence Ports for the servers context.

The ``ServerRepository`` interface (ARCHITECTURE.md Section 5.1) the domain
depends on; a concrete async-SQLAlchemy adapter implements it. Lookups return
``None`` when absent rather than raising, so callers decide policy.
"""

from __future__ import annotations

import abc
import datetime as dt
from typing import Any

from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    CrashReason,
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
        """Stage a new server for persistence within the current transaction.

        Nothing is executed here, so nothing is enforced here: the row is staged
        and the create use case flushes nothing between this call and the commit
        (its two pre-reads -- the taken game ports and the taken slugs -- run
        *before* it), so the INSERT is emitted by the unit of work's ``commit``
        and every constraint the row carries is enforced there. The adapters
        translate five of them into typed domain errors, and four of those five
        are reachable from a create.

        A duplicate name raises :class:`ServerNameAlreadyExistsError`
        (``uq_server_community_name``) -- and on this path that constraint is the
        *only* enforcement of per-community name uniqueness rather than a
        backstop behind a pre-read: the create path never reads the name (only
        ``UpdateServer``'s rename does), so every duplicate reaches it. A racer
        that took the game port or the slug the caller read as free raises
        :class:`PortAlreadyTakenError` (``uq_server_game_port``, issue #243) or
        :class:`SlugAlreadyTakenError` (``uq_server_slug``, issue #955). A
        community deleted between the request's authorization gate and this
        INSERT raises :class:`CommunityNotFoundError`
        (``fk_server_community_id_community``, issue #2940) -- the same not-found
        that gate answers for a community that is gone.

        The fifth translated one, ``uq_server_bedrock_port``, shares
        :class:`PortAlreadyTakenError` (issue #1541) but is dormant on this
        path: a staged server carries no Bedrock port -- it is allocated by a
        later UPDATE, which the adapter translates at its own execute site --
        and NULL never collides.
        """

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
    async def list_bedrock_ports(self) -> set[int]:
        """Return the Bedrock UDP ports currently taken across all servers (#1541).

        Deployment-wide, like :meth:`list_game_ports`: a Bedrock port is a
        deployment resource (one public UDP port per Bedrock-enabled server on
        the relay). Used on Geyser detection to pick the lowest free in-window
        port. NULL ports (servers without Geyser) hold no port and are excluded.
        """

    @abc.abstractmethod
    async def list_slugs(self) -> set[str]:
        """Return all slugs currently taken across all servers (issue #955).

        Deployment-wide (not community-scoped): a slug is unique across every
        server. Used by create to generate a unique slug and by the rename path
        to check availability. Empty slugs (the entity default) are excluded.
        """

    @abc.abstractmethod
    async def get_by_slug(self, slug: str) -> "Server | None":
        """Return the server with the given slug, or ``None`` if absent (issue #955).

        Used by the rename path to detect uniqueness collisions before the DB
        constraint fires. Deployment-wide (slugs are globally unique).
        """

    @abc.abstractmethod
    async def list_ids_missing_game_port(self) -> list[ServerId]:
        """Return the ids of servers with no tracked game port (issue #310).

        Legacy/imported rows that predate port tracking (#243) carry
        ``game_port IS NULL``; they are excluded from the deployment-wide taken
        set (:meth:`list_game_ports`), so auto-assignment can hand their real
        (server.properties-bound) host port to a new server and collide. This
        read powers the startup WARN that lists those rows so an operator can
        backfill them (DEPLOYMENT.md Section 7). Spans all communities — a
        deployment-wide gap, not scoped to one community.
        """

    @abc.abstractmethod
    async def update(self, server: Server) -> None:
        """Persist the mutable fields of ``server`` (name, config, game port, ts).

        The game port is included so an at-rest re-port (issue #311) lands in the
        same write; a name/config-only edit leaves it unchanged.
        """

    @abc.abstractmethod
    async def update_backup_retention(
        self, server_id: ServerId, retention: dict[str, Any] | None
    ) -> None:
        """Set (or clear, with ``None``) a server's backup retention policy (#1841).

        A narrow single-column write — deliberately not folded into
        :meth:`update` so a concurrent name/config edit built from a stale
        entity can never clobber the policy. ``retention`` is the canonical
        jsonb shape produced by ``RetentionPolicy.to_json()``; the caller
        validates before writing. A missing id matches no row (a no-op — the
        caller has already loaded the community-checked server).
        """

    @abc.abstractmethod
    async def update_lifecycle(
        self,
        server: Server,
        *,
        expected_from: DesiredState,
        expected_assigned_worker_id: WorkerId | None,
        write_config: bool = False,
    ) -> bool:
        """Compare-and-set a server's lifecycle fields (desired state + Worker).

        Distinct from :meth:`update` (name/config edits): the lifecycle ops set
        ``desired_state``, ``assigned_worker_id`` and ``updated_at`` and must not
        touch the name. Observed state is written separately via
        :meth:`record_observed_state` from the control-plane event path.
        ``config`` is written only with ``write_config``, which a start sets to
        record the JAR it resolved in the same write (issue #118). Every other
        transition leaves it as the row holds it, so a config edit that committed
        after the caller read the row is kept (issue #3792).

        The write is guarded so two concurrent transitions that both passed the
        in-memory check cannot both apply: the UPDATE matches only while the
        row's ``desired_state`` still equals ``expected_from`` and its
        ``assigned_worker_id`` still equals ``expected_assigned_worker_id``
        (``None`` matches an unassigned row only). Both are the values the caller
        read, so the two columns are written back unchanged unless the transition
        changes them, and never from a stale read (issue #3792). The expected
        assignment has no default, so no caller can leave it out.

        Returns ``True`` when exactly one row was updated, ``False`` when the
        precondition no longer held (a lost race). On ``False`` the caller does
        **not** dispatch, bump the stop-intent count or adjust placement-load
        counts: a start, a stop or a restart raises
        :class:`LifecycleTransitionConflictError`, a drain skips the server, and
        a failed start's revert leaves the row to the write that moved it.

        A write that changes the desired state (issue #3428) or the assignment
        (issue #3818) is announced to live subscribers as a status event once
        the transaction commits, carrying the fields it changed.

        A write that changes the assignment also sets ``awaiting_hydrate`` (issue
        #3890): to ``True`` when it places the server on a Worker, which has not
        hydrated it yet, and to ``False`` when it releases the row. A write that
        keeps the assignment leaves the mark as the row holds it, so one built
        from a stale read cannot restore a mark a hydrate has cleared since.
        """

    @abc.abstractmethod
    async def record_observed_state(
        self,
        server_id: ServerId,
        observed_state: ObservedState,
        observed_at: dt.datetime,
        *,
        unassign: bool = False,
        expected_worker: WorkerId | None = None,
        detail: str = "",
        reason: CrashReason | None = None,
    ) -> bool:
        """Cache the worker-reported observed state for ``server_id`` (FR-SRV-4).

        A no-op if the server is absent (it may have been deleted while the
        Worker still tracked it).

        ``unassign`` additionally clears ``assigned_worker_id`` in the same write.
        It is set only on a CONFIRMED stop (a graceful stop the Worker reports
        complete, or a SERVER_NOT_FOUND convergence) where no live instance can
        remain: clearing the assignment then lets a later start re-place under
        the unassigned-only CAS (issue #206). Crash/disconnect paths leave the
        assignment intact (the stickiness invariant), so they do not set it.
        A write that unassigns is a release, so like every other release it lands
        only while the row is still ``desired_state=stopped`` (issue #3766).

        ``expected_worker`` makes the ownership check part of the guarded write
        (issue #1708): when set, the write lands only while the row is STILL
        assigned to that worker (an unassigned row never matches). The
        control-plane sink asserts the reporting worker here so a stop-unassign
        + re-place committing between its snapshot read and this write cannot
        let the stale worker's (freshest-stamped) report overwrite the new
        owner's state. API-driven convergence writers do not assert a worker:
        they converge their own dispatch outcome, not a worker's report.

        A write that lands with ``unassign``, or of ``running``, also clears
        ``awaiting_hydrate`` (issue #3890). A release owes no hydrate. A running
        instance's working set is the one its snapshots capture, whatever the
        store holds, so a hydrate is no longer what makes them safe.

        ``detail`` and ``reason`` are the report's free text and crash reason.
        A write of ``crashed`` stores them as the server's last crash
        (``crash_detail`` bounded, an empty one stored as ``None``), and a write
        of any other state clears both, as do the two bulk writes below (issue
        #3281). They are stored, kept or cleared together with the state,
        under the same guards.

        Returns ``True`` when the write landed, ``False`` when the #216 monotonic
        guard dropped it (a same-instant or fresher write already stamped the
        row), the ``expected_worker`` ownership condition failed, or the server
        is absent. A convergence caller uses this to keep its returned entity
        honest (issue #292): it mutates the entity's observed fields only when
        ``True``, otherwise leaves them as-read so the return never claims a
        write that did not land.

        A write that changes the state is also announced to live subscribers
        as a status event once the transaction commits — by the unit of work,
        not by the caller (issue #3212). The same holds for the two bulk
        writes below. Nothing is announced for a dropped write, a rolled-back
        transaction, or a row already at ``observed_state``, except that an
        ``unassign`` that releases the row announces the release (issue #3818).
        """

    @abc.abstractmethod
    async def clear_assignment_after_final_snapshot(
        self, server_id: ServerId, worker_id: WorkerId
    ) -> bool:
        """Clear ``assigned_worker_id`` after a confirmed-stop snapshot (issue #847).

        The graceful/redispatch stop records ``observed=stopped`` first but DELAYS
        the unassign until the final snapshot settles: holding the assignment across
        the snapshot keeps a racing start's unassigned-only compare-and-set
        failing, so an immediate start cannot re-place the server on a DIFFERENT
        worker whose hydrate would pull store generation N while the final (would-be
        N+1) snapshot is still uploading.

        Guarded so it only clears OUR held assignment: the UPDATE matches only when
        the row is still ``desired_state=stopped`` AND ``assigned_worker_id`` still
        equals ``worker_id``. A start cannot have re-placed during the snapshot
        window (its unassigned-only CAS saw the held assignment and 409'd), so
        in practice the guard always matches; it exists to make the late clear a
        no-op rather than a clobber if the row moved out from under us by any other
        path. Returns ``True`` when the assignment was cleared, ``False`` when the
        guard matched no row. A clear is announced to live subscribers as a status
        event once the transaction commits (issue #3818). It clears
        ``awaiting_hydrate`` with the assignment (issue #3890).
        """

    @abc.abstractmethod
    async def clear_stale_assignment(
        self,
        server_id: ServerId,
        worker_id: WorkerId,
        *,
        observed_at: dt.datetime | None,
    ) -> bool:
        """Clear the assignment of a wedged stop, as it was read (issue #3766).

        The release ``StopServer.clear_stale_assignment`` owes a row it read as
        ``desired_state=stopped``, still assigned, with an at-rest ``observed``.
        Guarded as :meth:`clear_assignment_after_final_snapshot` is (still
        stopped, still assigned to ``worker_id``), and also on ``observed_at``
        still being the one the caller read, ``None`` matching a row never
        observed only. Every observed-state write stamps a fresh ``observed_at``,
        so the row's ``observed_state`` has not moved either: no Worker report
        has landed since the read, a first one included. Returns ``True`` when
        the assignment was cleared, ``False`` when the guard matched no row. A
        clear is announced to live subscribers as a status event once the
        transaction commits (issue #3818). It clears ``awaiting_hydrate`` with
        the assignment (issue #3890).
        """

    @abc.abstractmethod
    async def clear_awaiting_hydrate(
        self, server_id: ServerId, worker_id: WorkerId, *, updated_at: dt.datetime
    ) -> bool:
        """Clear ``awaiting_hydrate`` for the placement the caller read (#3890).

        A start clears it once the Worker holds the store's working set. The
        write lands only while ``worker_id`` holds the row and its ``updated_at``
        is still the one the caller read when it checked that the placement is
        its own, so a newer placement on the same Worker keeps its mark (r2
        review). Returns ``True`` when the row changed. Not announced: the mark
        is the API's own bookkeeping, not a state a client reads.
        """

    @abc.abstractmethod
    async def mark_awaiting_hydrate(
        self, server_id: ServerId, worker_id: WorkerId
    ) -> bool:
        """Set ``awaiting_hydrate`` again while ``worker_id`` holds the row (#3890).

        A start sets it when the Worker proves it holds no working set for the
        server after a skipped hydrate (#2499). Guarded on the assignment only,
        not on ``updated_at``: an edit committed while the start was on the
        Worker must not leave a refused replay's scratch publishable. A newer
        placement on the same Worker cannot have cleared its own mark by then,
        because its hydrate runs on the Worker's per-server lane behind this
        start, whose answer the caller acts on with this one write.

        A re-sent start sets it too, before it hydrates the Worker it is assigned
        to (issue #4024). Nothing of that start is on the Worker's lane yet, so
        a newer placement's hydrate may have landed and cleared the mark since
        the caller's load: the caller re-reads the row and checks that the intent
        is still its own in the same unit of work. Returns ``True`` when the row
        changed. Not announced.
        """

    @abc.abstractmethod
    async def mark_worker_servers_unknown(
        self, worker_id: WorkerId, observed_at: dt.datetime
    ) -> None:
        """Set observed=unknown for the servers assigned to ``worker_id`` (FR-WRK-4).

        A ``crashed`` server keeps its state and stored crash (issue #3885): the
        Worker has already forgotten the crashed instance, so the disconnect
        tells nothing new about it. Under a running intent it keeps its
        ``observed_at`` too, which ``Server.crash_not_restarted`` compares;
        under a stopped intent only that column is set to ``observed_at``, so
        its release waits a full grace from the disconnect.
        """

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
    async def running_assignment_ids_for_worker(
        self, worker_id: WorkerId
    ) -> dict[str, int]:
        """Return ``worker_id``'s desired=running assignments, id -> declared memory.

        Maps each assigned server id to its declared ``memory_limit_mb`` (0 = unset,
        #843). The placement tally used to rebuild a reconnected Worker's assignments
        (epic #7 reconciliation obligation). Returns ids (not just a count) so the
        registry can reconcile the rebuild against reserved-but-uncommitted
        placements (#778) and restore committed memory; the count is ``len`` of this
        mapping.
        """

    @abc.abstractmethod
    async def list_desired_running_assigned(self) -> list[Server]:
        """Return every server with desired=running and an assigned Worker.

        DESIRED state only — there is deliberately no observed-state predicate, so
        a crashed (or otherwise not-yet-running) server is in the result as long as
        the operator wants it running and a Worker holds it. The name says
        ``desired`` for exactly that reason (issue #2480): the former
        ``list_running_assigned`` claimed an observed state it never filtered on.
        It spans all communities — every caller is a process-wide background task
        or a fleet-wide tally, not a request scoped to one community.

        All three callers REQUIRE the desired-only semantics:

        - The periodic snapshot scheduler's candidate set (FR-DATA-7). Dispatching
          to a crashed member is the decision, not an accident: under a running
          intent it is the only path that durably captures the crash-time world
          (see :class:`RunSnapshotCadenceTick`).
        - Worker drain (FR-WRK-5), which flips ``desired=stopped`` on everything
          assigned to the draining Worker. Skipping crashed rows would leave them
          ``desired=running`` on that Worker, and the reconciler would then restart
          them on the very host being drained.
        - Placement's committed-resource tally (#710), the declared CPU each host
          has promised. Dropping a crashed-but-assigned server would understate the
          host right before the reconciler restarts that server there.
        """

    @abc.abstractmethod
    async def list_all(self) -> list[Server]:
        """Return every server, spanning all communities (FR-BAK-3).

        The candidate set the periodic scheduled-backup scheduler iterates: unlike
        the snapshot scheduler (desired-running only), a scheduled backup applies
        to an at-rest server too (archived directly from Storage, no Worker), so
        the scheduler must see every server and branch on each one's state. The
        scheduler filters to those carrying a per-server schedule in config. A
        process-wide background task, not scoped to one community.
        """

    @abc.abstractmethod
    async def list_reconcilable(self) -> list[Server]:
        """Return servers whose desired/observed states may have diverged (issue #101).

        The candidate set the periodic divergence reconciler iterates: servers
        where the operator's intent and the last Worker-reported reality could be
        out of step and an intent re-dispatch may be owed. Seven shapes qualify:

        - ``desired=running`` with an observed state that is neither ``starting``
          nor ``running`` (a start that was never delivered, or a crash that the
          Worker reported);
        - ``desired=running`` with no assigned Worker (a compensation-failure
          orphan: the intent committed but placement never stuck);
        - ``desired=stopped`` with ``observed=running`` (a stop that was never
          delivered);
        - ``desired=stopped``, ``observed=stopped``, still assigned (issue #847
          bug 2: a stop wedged because its deferred unassign never ran);
        - ``desired=stopped``, ``observed=unknown``, still assigned (issue #1599:
          a stop interrupted mid-flight by an API restart or worker disconnect —
          and, since issue #2475, also a Worker actively reporting ``unknown``
          because it holds a failed-stop orphan whose backend daemon it cannot
          reach. Both mean the same thing for this arm: nobody can say whether
          the process is alive, so the stop is re-dispatched);
        - ``desired=stopped``, ``observed=crashed``, still assigned (issue #2439:
          the process died on its own under a stop intent);
        - ``desired=stopped``, ``observed=stopping``, still assigned (issue #2452:
          a stop whose dispatch failed after the Worker emitted ``stopping``).

        The remaining observed states under a stopped intent (``starting`` and
        ``restarting``) are deliberately NOT candidates, and the reasons are
        specific to each rather than the blanket "a transitional state cannot
        persist" this docstring used to assert. That premise was FALSE (issue
        #2452) and is what let the ``stopping`` wedge ship unnoticed when #2439 was
        fixed: the Worker's ``Stop`` emits ``stopping`` on entry, and BOTH of its
        failure paths (the kill call erroring, and the container surviving the
        kill) restore the pre-stop state WITHOUT emitting. That is still true of
        the driver; what changed with issue #2475 is that the Worker's instance
        manager now converges the failed stop above it — probing, retrying, and
        reporting ``stopped`` on confirmed death or ``unknown`` while it cannot
        tell — so a failed stop is no longer followed by silence. These arms
        remain the backstop for the cases no Worker can report at all (it is
        gone, or the stream is down). The actual reasons:

        - ``starting`` is a genuine transient here: the Worker's in-flight launch
          proceeds independently of the stop intent and settles into ``running``
          (or ``crashed``), whose report moves the row onto one of the arms above.
          Acting on it would race that launch.
        - ``restarting`` is never emitted at all — no Worker driver produces it
          (the ``SERVER_STATE_RESTARTING`` contract value has zero producers in
          ``worker/internal/``), so no row can reach it.

        ``reset_unverifiable_observed_states`` also rewrites every ASSIGNED row's
        non-terminal observed state to ``unknown`` on API startup, but that is an
        accidental escape, not a design: it needs an API restart, which is
        precisely why it did not save the ``stopping`` wedge. ``crashed`` has no
        escape — it is
        terminal, so no further report follows, and the startup reset leaves it
        alone as still-truthful — which is why it wedged until #2439.

        Aligned servers (``running``/``starting`` under a running intent, settled
        ``stopped`` under a stopped intent) are excluded so the reconciler's tick
        cost scales with divergence, not fleet size. The grace-window and
        Worker-connectivity decisions are applied by the reconciler use case on the
        returned candidates. Spans all communities — a process-wide background
        task, not scoped to one community.
        """

    @abc.abstractmethod
    async def existing_ids(self, server_ids: list[ServerId]) -> set[ServerId]:
        """Return the subset of ``server_ids`` that still exist in the store.

        Used by the control-plane RegisterAck to compute which held servers the
        Worker advertised are no longer known (deleted while the scratch was live,
        issue #924). The Worker reclaims the orphaned scratch for each unknown id.
        """

    @abc.abstractmethod
    async def delete(self, server_id: ServerId) -> None:
        """Delete the server row; its resource grants cascade (Section 10)."""
