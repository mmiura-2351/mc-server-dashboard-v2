"""Server lifecycle use cases: start, stop, restart, RCON (Section 6.5, FR-SRV-2/5).

These run after the route's authorization dependency has admitted the caller, so
they assume an authorized member and only do the lifecycle work. They coordinate
the authoritative ``server`` record (desired state + assigned Worker) with the
fleet through the :class:`ControlPlane` seam (placement, command dispatch,
placement-load tracking).

Consistency model — *dispatch after commit, compensate on failure*. The DB write
(desired state, assignment) and the control-plane dispatch cannot be one atomic
unit: the dispatch crosses a network to a separate process. We commit the intent
first, then dispatch; if the dispatch fails (Worker refusal or no live session)
we honestly compensate the committed write back. The alternative — dispatch
inside the transaction — would hold the transaction open across a network round
trip and still could not roll back a command the Worker already applied. Choosing
commit-first keeps the desired state durable and the failure path explicit
(CONTROL_PLANE.md Section 4.2; the API's desired state is authoritative,
Section 4.4).

Stale-intent window — *honest about the gap, not papered over*. If the process
crashes (or the request is cancelled) between the commit and the dispatch, the
intent is durable but the command was never sent: a ``StartServer`` leaves
``desired_state=running`` with no Worker ever told to start, and a ``StopServer``
leaves ``desired_state=stopped`` with no Worker ever told to stop. This is not
silently corrected here — there is no in-line retry — but it is observable: the
divergence shows up as ``desired_state`` not matching the Worker-reported
``observed_state``. Closing the window (re-dispatching durable-but-unsent intent)
is a reconciler's job, tracked separately; the in-line compensation above covers
only the case where the dispatch *ran* and the Worker refused it.

Start dispatch is hydrate-then-start (FR-DATA-4). After the intent commits,
``StartServer`` first drives the Worker to pull the authoritative working set from
Storage (a server with no published working set yet hydrates to an empty dir, the
data-plane endpoint being 204), then dispatches the launch; either step failing
compensates the committed intent. The launch carries a conventional ``server.jar``
relpath and the server's recorded MC version (FR-EXE-5: the Worker picks the Java
runtime).

Assignment stickiness after dispatch (the orphan-placement invariant, issue #101).
ONCE A START COMMAND HAS BEEN SENT FOR A SERVER, THE RECONCILER NEVER PLACES IT ON
A DIFFERENT WORKER until the assignment is cleared by an authoritative path (a stop,
or worker-disconnect handling). The reconciler's ``place_and_start`` therefore
distinguishes WHERE a launch failed: a pre-dispatch failure (placement, jar
provisioning, a failed hydrate) is safe to ``_unassign`` for a later re-place, but a
post-dispatch failure — a failed start outcome, or a timeout/lost-response
``WorkerUnavailableError`` whose command MAY have been applied — KEEPS the
assignment so the next tick redispatches to the SAME Worker (where an
``INVALID_STATE`` resolves the lost-response case as already-running). The Worker's
double-start guard is per-process, so re-placing a started server elsewhere would
spawn a second live instance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.command_dispatch import (
    dispatch_failure as _dispatch_failure,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    ControlPlane,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidLifecycleTransitionError,
    LifecycleTransitionConflictError,
    NoEligibleWorkerError,
    ServerNotFoundError,
    ServerNotRunningError,
)
from mc_server_dashboard_api.servers.domain.jar_provisioner import JarProvisioner
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    JAR_KEY_CONFIG_FIELD,
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    WorkerId,
)

_LOG = logging.getLogger(__name__)

# The conventional JAR path inside a hydrated working set. The Worker launches
# against this relpath once the working set is hydrated (see __call__).
_DEFAULT_JAR_RELPATH = "server.jar"


@dataclass
class _Dispatch:
    """Mutable marker recording whether a start command was sent (issue #101).

    Threaded into :meth:`StartServer._launch` so the orphan-placement path can read
    the dispatch boundary on both the normal-return and exception paths: once the
    start command MAY have reached the Worker, the assignment must stick (the
    reconciler must never re-place a started server on a different Worker).
    """

    attempted: bool = False


async def _load(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> Server:
    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))
    return server


@dataclass(frozen=True)
class StartServer:
    """Place and start a server (server:start, FR-SRV-2).

    Ensure-on-start (FR-VER-3): before placement, the resolved server JAR is made
    present in the content-addressed pool (download + verify + store on first need)
    and its content key recorded on the server. The ensure runs *before* the
    placement/dispatch path and outside the lifecycle transaction (it crosses the
    network), so a download/verify failure fails the start cleanly with the server
    untouched — no Worker placed, no desired-state flip.
    """

    uow: UnitOfWork
    control_plane: ControlPlane
    clock: Clock
    jar_provisioner: JarProvisioner

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> Server:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            if server.desired_state is DesiredState.RUNNING:
                raise InvalidLifecycleTransitionError(str(server_id.value))
            # Ensure the resolved JAR is pooled BEFORE placement/dispatch (FR-VER-3):
            # a download/verify failure fails the start here, before a Worker is
            # placed or the desired state flipped. The ensure skips the download when
            # the recorded content key is still pooled, so the steady-state cost is a
            # presence check, not a fetch.
            jar_key = await self._ensure_jar(server)
            worker_id = await self.control_plane.place(backend=server.execution_backend)
            if worker_id is None:
                raise NoEligibleWorkerError(str(server_id.value))
            server.desired_state = DesiredState.RUNNING
            server.assigned_worker_id = worker_id
            server.config = {**server.config, JAR_KEY_CONFIG_FIELD: jar_key}
            server.updated_at = self.clock.now()
            applied = await self.uow.servers.update_lifecycle(
                server,
                expected_from=DesiredState.STOPPED,
                require_unassigned=True,
            )
            if not applied:
                # A concurrent start won the compare-and-set: the row is already
                # running/assigned. Abort before dispatch or any count change so
                # the lost race causes no double placement (FR-SRV-2).
                raise LifecycleTransitionConflictError(str(server_id.value))
            await self.uow.commit()

        self.control_plane.increment_assignment(worker_id=worker_id)
        try:
            outcome = await self._launch(server, community_id, server_id, worker_id)
        except WorkerUnavailableError as exc:
            await self._compensate(community_id, server_id, worker_id, original=exc)
            raise
        if not outcome.success:
            failure = _dispatch_failure(
                server_id=server_id, kind="StartServer", outcome=outcome
            )
            await self._compensate(community_id, server_id, worker_id, original=failure)
            raise failure
        return server

    async def place_and_start(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> Server:
        """Place a desired-running, unassigned server and dispatch its start.

        The reconciler's compensation-failure orphan path (issue #101): the start
        intent already committed (``desired_state=running``) but the server has no
        assigned Worker, so the normal placement+dispatch never completed. This
        reuses the placement, CAS-assignment, load-increment, and hydrate-then-start
        dispatch of :meth:`__call__`; only the entry guard differs (desired is
        already running, the server is unassigned) and the failure handling does NOT
        revert the desired state — the running intent is authoritative and was not
        created here. Raises the same typed errors as a normal start.

        Assignment stickiness after dispatch (the invariant this path guarantees):
        ONCE A START COMMAND HAS BEEN SENT FOR A SERVER, THE RECONCILER NEVER PLACES
        IT ON A DIFFERENT WORKER until the assignment is cleared by an authoritative
        path (a stop, or worker-disconnect handling). So the failure handling keys on
        WHERE the launch failed:

        - Before the start command was dispatched (placement, jar provisioning, a
          failed hydrate) -> safe: ``_unassign`` so a later tick re-places. No start
          ever reached a Worker, so re-placing elsewhere cannot double-start.
        - After the start was dispatched (a failed outcome, or a timeout/lost-response
          ``WorkerUnavailableError`` — the command MAY have been applied) -> KEEP the
          assignment. Subsequent ticks then take the ``redispatch_start`` path to the
          SAME Worker, where an ``INVALID_STATE`` (already running) resolves the
          lost-response case as converged. Re-placing elsewhere here is the bug: the
          Worker's double-start guard is per-process, so two Workers = two live
          instances of one server.
        """

        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            if (
                server.desired_state is not DesiredState.RUNNING
                or server.assigned_worker_id is not None
            ):
                # Not an orphan (already assigned, or no longer desired-running):
                # nothing for this path to reconcile.
                raise InvalidLifecycleTransitionError(str(server_id.value))
            jar_key = await self._ensure_jar(server)
            worker_id = await self.control_plane.place(backend=server.execution_backend)
            if worker_id is None:
                raise NoEligibleWorkerError(str(server_id.value))
            server.assigned_worker_id = worker_id
            server.config = {**server.config, JAR_KEY_CONFIG_FIELD: jar_key}
            server.updated_at = self.clock.now()
            applied = await self.uow.servers.update_lifecycle(
                server,
                expected_from=DesiredState.RUNNING,
                require_unassigned=True,
            )
            if not applied:
                # A concurrent assignment (a real start or another reconcile tick)
                # won the compare-and-set; abort before dispatch or any count
                # change so the lost race causes no double placement.
                raise LifecycleTransitionConflictError(str(server_id.value))
            await self.uow.commit()

        self.control_plane.increment_assignment(worker_id=worker_id)
        dispatch = _Dispatch()
        try:
            outcome = await self._launch(
                server, community_id, server_id, worker_id, dispatch
            )
        except WorkerUnavailableError as exc:
            # A timeout/lost-response AFTER the start was sent (dispatch.attempted)
            # MAY have been applied by the Worker: keep the assignment so the next
            # tick redispatches to the SAME Worker (stickiness invariant). Only a
            # pre-dispatch unavailable (a failed hydrate) is safe to unassign.
            if not dispatch.attempted:
                await self._unassign(community_id, server_id, worker_id, original=exc)
            raise
        if not outcome.success:
            failure = _dispatch_failure(
                server_id=server_id, kind="StartServer", outcome=outcome
            )
            # A failed START outcome may reflect a command the Worker partially
            # applied; keep the assignment for a same-Worker redispatch. A failed
            # HYDRATE outcome (not dispatched) never reached the start, so unassign.
            if not dispatch.attempted:
                await self._unassign(
                    community_id, server_id, worker_id, original=failure
                )
            raise failure
        return server

    async def redispatch_start(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> Server:
        """Re-send hydrate-then-start to an assigned, desired-running server.

        The reconciler's stale-intent path (issue #101): the start intent committed
        and a Worker is assigned, but the launch was never sent (crash between
        commit and dispatch) or the Worker is not actually running it. The intent
        and assignment stand; only the dispatch is replayed, so this changes no DB
        row — and on failure it reverts nothing (a later reconcile tick retries
        under backoff). The placement load was already counted on the original
        assignment, so it is not incremented again.

        An ``INVALID_STATE`` outcome means the Worker is already running the server
        (the hydrate/start guards reject a live instance): the divergence is in fact
        resolved, so we treat it as success rather than flipping the authoritative
        running intent to stopped. Any other failure raises for the next tick.

        On the ``INVALID_STATE`` convergence we MUST record observed=running (issue
        #213): the Worker performed no transition, so it will never emit a
        StatusChange to repair the cached observed state — without this write the
        row stays diverged (e.g. observed=unknown after an API restart) and the
        reconciler re-selects and redispatches it every tick forever. We do not
        unassign: the instance is live and keeps its Worker (mirror of the genuine
        success path, which the Worker's StatusChange(running) already covers, so it
        writes nothing here).
        """

        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            if (
                server.desired_state is not DesiredState.RUNNING
                or server.assigned_worker_id is None
            ):
                raise InvalidLifecycleTransitionError(str(server_id.value))
            worker_id = server.assigned_worker_id

        outcome = await self._launch(server, community_id, server_id, worker_id)
        if outcome.success:
            return server
        if outcome.status is CommandStatus.INVALID_STATE:
            observed_at = self.clock.now()
            async with self.uow:
                await self.uow.servers.record_observed_state(
                    server_id,
                    observed_state=ObservedState.RUNNING,
                    observed_at=observed_at,
                )
                await self.uow.commit()
            server.observed_state = ObservedState.RUNNING
            server.observed_at = observed_at
            return server
        raise _dispatch_failure(
            server_id=server_id, kind="StartServer", outcome=outcome
        )

    async def _launch(
        self,
        server: Server,
        community_id: CommunityId,
        server_id: ServerId,
        worker_id: WorkerId,
        dispatch: _Dispatch | None = None,
    ) -> CommandOutcome:
        """Hydrate the working set then dispatch the launch; return the outcome.

        ``dispatch`` is an optional marker the caller passes to learn whether the
        start command was actually *sent* to the Worker — the dispatch boundary the
        orphan-placement path keys its failure handling on (issue #101). It is
        flipped to ``attempted`` immediately before ``control_plane.start`` is
        called, so the caller can read it in BOTH the normal-return path and the
        ``except WorkerUnavailableError`` path: a timeout/lost-response on the start
        call means the command MAY have reached the Worker, and the marker reflects
        that even though the exception unwinds past this method.

        Hydrate the working set BEFORE the launch (FR-DATA-4): the API drives a pull
        of the authoritative working set from Storage to the Worker's scratch, then
        the Worker launches against it. A server with no published working set yet
        hydrates to an empty dir (the data-plane endpoint is 204 and the Worker
        starts fresh), so this is not an error. The failing step's outcome is
        returned (a failed hydrate short-circuits the launch); a
        :class:`WorkerUnavailableError` propagates. Compensation/cleanup policy is
        the caller's — this helper makes no DB write — so the normal start and the
        reconciler's re-dispatch paths can each react differently to a failure.
        """

        hydrate = await self.control_plane.hydrate(
            worker_id=worker_id,
            community_id=community_id,
            server_id=server_id,
        )
        if not hydrate.success:
            return hydrate
        if dispatch is not None:
            dispatch.attempted = True
        return await self.control_plane.start(
            worker_id=worker_id,
            server_id=server_id,
            backend=server.execution_backend,
            jar_relpath=_DEFAULT_JAR_RELPATH,
            minecraft_version=server.mc_version,
        )

    async def _ensure_jar(self, server: Server) -> str:
        """Ensure the resolved JAR is pooled; return its content key (FR-VER-3).

        Reuses the recorded content key (``config[JAR_KEY_CONFIG_FIELD]``) to skip a
        re-download when the JAR is still pooled. A provisioning failure surfaces
        before placement so the start fails cleanly.
        """

        known_key = server.config.get(JAR_KEY_CONFIG_FIELD)
        return await self.jar_provisioner.ensure(
            server_type=server.server_type.value,
            version=server.mc_version,
            known_key=known_key if isinstance(known_key, str) else None,
        )

    async def _compensate(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        worker_id: WorkerId,
        *,
        original: Exception,
    ) -> None:
        """Revert the committed start intent after a failed dispatch.

        The desired state and assignment were committed before the dispatch; on
        failure we honestly undo them so the record does not claim a server is
        running when the Worker rejected it.

        Placement-load decrement semantics (symmetric with StartServer's
        increment): decrement **iff** the revert compare-and-set actually
        reverted the row (rowcount true). If the CAS matched no row, a concurrent
        transition (e.g. a stop) already moved the state out of running and owns
        the decrement; decrementing here too would drive the count below the true
        running tally. If the compensation commit itself errors the DB state is
        unknown, so we do **not** decrement and log loudly — the reconnect
        assignment rebuild reconciles the count from the authoritative tally.

        If the compensation itself fails, the original dispatch failure
        (``original``) must not be masked: we log both errors explicitly and
        re-raise the compensation error chained from the original so neither is
        lost (the record is left diverged, which a reconciler later detects).
        """

        reverted = False
        try:
            async with self.uow:
                server = await self.uow.servers.get_by_id(server_id)
                if server is not None and server.community_id == community_id:
                    server.desired_state = DesiredState.STOPPED
                    server.assigned_worker_id = None
                    server.updated_at = self.clock.now()
                    reverted = await self.uow.servers.update_lifecycle(
                        server, expected_from=DesiredState.RUNNING
                    )
                    await self.uow.commit()
        except Exception as compensation_error:
            _LOG.error(
                "failed to compensate start intent after a failed dispatch; "
                "the server record is left with desired=running (original "
                "dispatch failure: %r)",
                original,
                exc_info=compensation_error,
            )
            raise compensation_error from original
        if reverted:
            self.control_plane.decrement_assignment(worker_id=worker_id)

    async def _unassign(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        worker_id: WorkerId,
        *,
        original: Exception,
    ) -> None:
        """Undo only the assignment after a failed PRE-DISPATCH orphan placement.

        Called by ``place_and_start`` only when the failure happened BEFORE the start
        command was sent (a failed hydrate): no start ever reached a Worker, so
        clearing the assignment to let a later tick re-place elsewhere cannot
        double-start (issue #101's stickiness invariant). A post-dispatch failure
        does NOT call this — the assignment must stick for a same-Worker redispatch.

        Unlike :meth:`_compensate`, the desired state is **left running**: the
        running intent is authoritative and was not created by this reconcile path,
        so a dispatch failure must not silently flip it to stopped — it only means
        the freshly-chosen Worker did not take the launch. We clear the assignment
        (and decrement its load iff the revert matched) so a later tick re-places on
        a fresh Worker.

        A failed unassign-commit is benign: it is logged (not masked) and the row is
        left assigned + desired=running, so the next tick sees an assigned orphan and
        takes the ``redispatch_start`` path to the SAME Worker — exactly the
        post-dispatch behavior, which is safe. We do not decrement the load in that
        case (the DB state is unknown; the reconnect rebuild reconciles it).
        """

        reverted = False
        try:
            async with self.uow:
                server = await self.uow.servers.get_by_id(server_id)
                if (
                    server is not None
                    and server.community_id == community_id
                    and server.assigned_worker_id == worker_id
                ):
                    server.assigned_worker_id = None
                    server.updated_at = self.clock.now()
                    reverted = await self.uow.servers.update_lifecycle(
                        server, expected_from=DesiredState.RUNNING
                    )
                    await self.uow.commit()
        except Exception as compensation_error:
            _LOG.error(
                "failed to undo orphan re-placement after a failed dispatch; the "
                "server record is left assigned (original dispatch failure: %r)",
                original,
                exc_info=compensation_error,
            )
            raise compensation_error from original
        if reverted:
            self.control_plane.decrement_assignment(worker_id=worker_id)


@dataclass(frozen=True)
class StopServer:
    """Stop a running server (server:stop, FR-SRV-2).

    Graceful by default; ``force`` skips the Worker's graceful (RCON) path and
    takes the immediate-kill path (issue #270). The rest of the flow — desired
    flip, placement-load decrement, final snapshot, unassign — is identical.
    """

    uow: UnitOfWork
    control_plane: ControlPlane
    clock: Clock

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId, force: bool = False
    ) -> Server:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            if server.desired_state is DesiredState.STOPPED:
                raise InvalidLifecycleTransitionError(str(server_id.value))
            if server.assigned_worker_id is None:
                # Desired-running with no assigned Worker is inconsistent; there
                # is nothing to command. Treat as a transition conflict.
                raise InvalidLifecycleTransitionError(str(server_id.value))
            worker_id = server.assigned_worker_id
            server.desired_state = DesiredState.STOPPED
            server.updated_at = self.clock.now()
            applied = await self.uow.servers.update_lifecycle(
                server, expected_from=DesiredState.RUNNING
            )
            if not applied:
                # A concurrent transition already moved the row out of running.
                # Abort before dispatch or the placement-load decrement so the
                # lost race does not double-decrement the count (FR-SRV-2).
                raise LifecycleTransitionConflictError(str(server_id.value))
            await self.uow.commit()

        # Decrement the placement load symmetrically with StartServer's
        # increment, right after desired flips to stopped. This keeps the
        # in-memory count consistent with the authoritative running-server tally
        # (count_running_for_worker, used to rebuild after a reconnect): both
        # define load as "servers assigned with desired=running". Deferring the
        # decrement to the StatusChange(stopped) event was considered but would
        # leave the count disagreeing with the desired-state tally during the
        # graceful-stop window and on a missed event.
        self.control_plane.decrement_assignment(worker_id=worker_id)
        outcome = await self.control_plane.stop(
            worker_id=worker_id, server_id=server_id, force=force
        )
        if outcome.status is CommandStatus.SERVER_NOT_FOUND:
            # The Worker has no live instance to stop (e.g. the process crashed on
            # the EULA, issue #197): stopping a not-running server is a no-op, not a
            # failure. The Worker's handleStop returns SERVER_NOT_FOUND (no live
            # instance on this Worker), never INVALID_STATE, for this case
            # (worker/internal/application/instancemanager/instancemanager.go:308-312).
            # The stop intent already landed (desired=stopped committed above);
            # converge the observed cache to stopped and report success. No final
            # snapshot: there is no live working set to capture. No live instance
            # remains, so clear the assignment too (issue #206) — otherwise a
            # later start's require_unassigned compare-and-set 409s forever.
            observed_at = self.clock.now()
            async with self.uow:
                await self.uow.servers.record_observed_state(
                    server_id,
                    observed_state=ObservedState.STOPPED,
                    observed_at=observed_at,
                    unassign=True,
                )
                await self.uow.commit()
            server.observed_state = ObservedState.STOPPED
            server.observed_at = observed_at
            server.assigned_worker_id = None
            return server
        if not outcome.success:
            raise _dispatch_failure(
                server_id=server_id, kind="StopServer", outcome=outcome
            )
        # The graceful stop returned only once the Worker reported the process
        # gone, so record observed=stopped and clear the assignment in one
        # transaction (issue #206). The unassign cannot wait on the later
        # StatusChange(stopped) event: a start blocked on require_unassigned must
        # be unblocked the moment the stop confirms. Recording observed=stopped
        # here is safe — a subsequent late StatusChange(stopped) from the
        # now-unassigned Worker is dropped by the sink's ownership guard
        # (server_state_sink.py:85, "dropping status report from non-owning
        # worker"), which is the acceptable outcome since this write already
        # converged the cache.
        observed_at = self.clock.now()
        async with self.uow:
            await self.uow.servers.record_observed_state(
                server_id,
                observed_state=ObservedState.STOPPED,
                observed_at=observed_at,
                unassign=True,
            )
            await self.uow.commit()
        server.observed_state = ObservedState.STOPPED
        server.observed_at = observed_at
        server.assigned_worker_id = None
        # Final snapshot AFTER the process has exited (the graceful stop above
        # only returns once the Worker reports the process gone), so the captured
        # working set is quiescent (FR-DATA-4, FR-DATA-7). A snapshot failure is
        # logged, not raised: the stop itself already succeeded and the server is
        # down; bounding the loss window is best-effort, and a reconciler/next
        # interval can re-snapshot. (The Worker self-addresses no Storage; the API
        # drives the snapshot because only it knows the (community, server) scope.)
        try:
            snapshot = await self.control_plane.snapshot(
                worker_id=worker_id,
                community_id=community_id,
                server_id=server_id,
            )
            if not snapshot.success:
                _LOG.warning(
                    "final snapshot on graceful stop failed for server %s: %s",
                    server_id.value,
                    snapshot.message or snapshot.status.value,
                )
        except WorkerUnavailableError:
            _LOG.warning(
                "final snapshot on graceful stop could not reach the Worker for "
                "server %s; the stop succeeded but the working set was not captured",
                server_id.value,
            )
        return server

    async def redispatch_stop(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> Server:
        """Re-send the stop command to an assigned, desired-stopped server.

        The reconciler's stale-intent path (issue #101): the stop intent committed
        (``desired_state=stopped``) and a Worker is still assigned, but the stop was
        never delivered (crash between commit and dispatch) so the Worker keeps the
        process running (observed=running). The intent stands; only the dispatch is
        replayed. The placement-load decrement is **not** repeated here: the
        original stop either already decremented it, or the in-memory count was lost
        to the crash and is rebuilt from the authoritative tally on reconnect — so
        decrementing again would drive the count below the true running total. A
        failed dispatch raises so the caller retries on a later tick.

        On a CONFIRMED stop (success, or SERVER_NOT_FOUND meaning no live instance
        remains) the assignment is cleared so a later start can re-place under
        require_unassigned (issue #206). A failed dispatch keeps the assignment for
        a same-Worker retry.
        """

        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            if (
                server.desired_state is not DesiredState.STOPPED
                or server.assigned_worker_id is None
            ):
                raise InvalidLifecycleTransitionError(str(server_id.value))
            worker_id = server.assigned_worker_id

        outcome = await self.control_plane.stop(
            worker_id=worker_id, server_id=server_id
        )
        if not outcome.success and outcome.status is not CommandStatus.SERVER_NOT_FOUND:
            raise _dispatch_failure(
                server_id=server_id, kind="StopServer", outcome=outcome
            )
        observed_at = self.clock.now()
        async with self.uow:
            await self.uow.servers.record_observed_state(
                server_id,
                observed_state=ObservedState.STOPPED,
                observed_at=observed_at,
                unassign=True,
            )
            await self.uow.commit()
        server.observed_state = ObservedState.STOPPED
        server.observed_at = observed_at
        server.assigned_worker_id = None
        return server


@dataclass(frozen=True)
class RestartServer:
    """Restart a running server in place (server:restart, FR-SRV-2)."""

    uow: UnitOfWork
    control_plane: ControlPlane
    clock: Clock

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> Server:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            if (
                server.desired_state is not DesiredState.RUNNING
                or server.assigned_worker_id is None
            ):
                raise InvalidLifecycleTransitionError(str(server_id.value))
            worker_id = server.assigned_worker_id
            # Restart keeps desired=running; the compare-and-set asserts the row
            # is still running before we dispatch, so a stop that won a concurrent
            # race turns this into a transition conflict rather than a restart of
            # a server already moving to stopped (FR-SRV-2).
            server.updated_at = self.clock.now()
            applied = await self.uow.servers.update_lifecycle(
                server, expected_from=DesiredState.RUNNING
            )
            if not applied:
                raise LifecycleTransitionConflictError(str(server_id.value))
            await self.uow.commit()

        outcome = await self.control_plane.restart(
            worker_id=worker_id, server_id=server_id
        )
        if not outcome.success:
            raise _dispatch_failure(
                server_id=server_id, kind="RestartServer", outcome=outcome
            )
        return server


@dataclass(frozen=True)
class SendServerCommand:
    """Forward an RCON/console command to a running server (FR-SRV-5).

    Permission ``server:command``.
    """

    uow: UnitOfWork
    control_plane: ControlPlane

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId, line: str
    ) -> str:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            if (
                server.observed_state is not ObservedState.RUNNING
                or server.assigned_worker_id is None
            ):
                raise ServerNotRunningError(str(server_id.value))
            worker_id = server.assigned_worker_id

        outcome = await self.control_plane.command(
            worker_id=worker_id, server_id=server_id, line=line
        )
        if outcome.status is CommandStatus.INVALID_STATE:
            raise ServerNotRunningError(str(server_id.value))
        if not outcome.success:
            raise _dispatch_failure(
                server_id=server_id, kind="ServerCommand", outcome=outcome
            )
        return outcome.output
