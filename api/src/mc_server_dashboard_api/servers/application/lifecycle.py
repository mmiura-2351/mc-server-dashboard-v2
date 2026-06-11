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
only pre-dispatch failures — failures where the start command demonstrably never
reached the Worker (placement, jar provisioning, a failed hydrate, or a
pre-dispatch ``WorkerUnavailableError``). A timeout/lost-response AFTER the start
was sent, and an ``INVALID_STATE`` outcome (the Worker refusing because an
instance is already live there), both do NOT compensate — see the
assignment-stickiness note below.

Start dispatch is hydrate-then-start (FR-DATA-4). After the intent commits,
``StartServer`` first drives the Worker to pull the authoritative working set from
Storage (a server with no published working set yet hydrates to an empty dir, the
data-plane endpoint being 204), then dispatches the launch; a failure before the
start command is sent (a failed hydrate, or a pre-dispatch
``WorkerUnavailableError``) compensates the committed intent. The launch carries a
conventional ``server.jar`` relpath and the server's recorded MC version
(FR-EXE-5: the Worker picks the Java runtime).

Assignment stickiness after dispatch (the orphan-placement invariant, issue #101).
ONCE A START COMMAND HAS BEEN SENT FOR A SERVER, THE RECONCILER NEVER PLACES IT ON
A DIFFERENT WORKER until the assignment is cleared by an authoritative path (a stop,
or worker-disconnect handling). Both the normal start (``__call__``) and the
reconciler's ``place_and_start`` therefore distinguish WHERE a launch failed: a
pre-dispatch failure (placement, jar provisioning, a failed hydrate) is safe to
revert, but a post-dispatch timeout/lost-response ``WorkerUnavailableError`` whose
command MAY have been applied KEEPS the assignment and ``desired=running`` so the
next reconcile tick redispatches to the SAME Worker (where an ``INVALID_STATE``
resolves the lost-response case as already-running). An ``INVALID_STATE`` outcome
returned straight to ``__call__`` is the same case observed synchronously: the
instance is DEFINITELY live on the assigned Worker (already running, or a pending
failed-stop orphan), so it likewise keeps the assignment and ``desired=running``
and records observed=running rather than compensating. The Worker's double-start
guard is per-process, so reverting a possibly-started or definitely-running server
— and letting a later start place it elsewhere — would spawn a second live
instance (issue #773/#774).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.command_dispatch import (
    dispatch_failure as _dispatch_failure,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.committed_resources import (
    committed_resources_by_worker,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    ControlPlane,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.cpu_allocation import (
    cpu_allocation_from_config,
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
from mc_server_dashboard_api.servers.domain.memory_limit import (
    memory_limit_from_config,
)
from mc_server_dashboard_api.servers.domain.store_generation import (
    StoreGenerationReader,
)
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
    store_generation: StoreGenerationReader

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
            worker_id = await self._place(server)
            if worker_id is None:
                raise NoEligibleWorkerError(str(server_id.value))
            server.desired_state = DesiredState.RUNNING
            server.assigned_worker_id = worker_id
            server.config = {**server.config, JAR_KEY_CONFIG_FIELD: jar_key}
            server.updated_at = self.clock.now()
            # Any failure across the CAS+commit window — a raising update_lifecycle or
            # commit, the lost-race abort, or this request task being cancelled at an
            # await (a client disconnect cancels the HTTP task) — must release the
            # placement reservation (the slot _place tentatively took, #778), or it
            # leaks permanently: a never-committed server id never appears in the
            # authoritative tally, so no reconnect rebuild ever reclaims it. Releasing
            # on an ambiguous commit failure is safe: if the commit actually landed,
            # the committed count undercounts by one until the reconnect rebuild,
            # exactly the recovery _compensate's docstring already relies on.
            try:
                applied = await self.uow.servers.update_lifecycle(
                    server,
                    expected_from=DesiredState.STOPPED,
                    require_unassigned=True,
                )
                if not applied:
                    # A concurrent start won the compare-and-set: the row is already
                    # running/assigned. Abort before dispatch or any committed count
                    # change so the lost race causes no double placement (FR-SRV-2).
                    raise LifecycleTransitionConflictError(str(server_id.value))
                await self.uow.commit()
                # Confirm the placement reservation as a committed assignment now the
                # intent is durable (#778); a no-op if a reconnect rebuild already
                # counted this row. Confirm HERE — inside the transaction, immediately
                # after the commit returns and with no await in between (a sync
                # registry call) — not after the ``async with`` block: the UoW
                # ``__aexit__`` awaits rollback()/close(), suspension points outside
                # this try/except where a client-disconnect CancelledError (or a
                # post-commit teardown error) would leave the committed reservation
                # neither confirmed nor released, leaking it permanently (#840).
                self.control_plane.increment_assignment(
                    worker_id=worker_id, server_id=server_id
                )
            except (Exception, asyncio.CancelledError):
                self.control_plane.release_reservation(
                    worker_id=worker_id, server_id=server_id
                )
                raise

        dispatch = _Dispatch()
        try:
            outcome = await self._launch(
                server, community_id, server_id, worker_id, dispatch
            )
        except WorkerUnavailableError as exc:
            # A timeout/lost-response AFTER the start was sent (dispatch.attempted)
            # MAY have been applied by the Worker (issue #773, mirroring #101's fix
            # to place_and_start): keep desired=running and the assignment so the
            # reconciler redispatches to the SAME Worker, where an INVALID_STATE
            # resolves it as already-running. Compensating here would orphan a live
            # instance and let a later start place a second one on a different
            # Worker. Only a PRE-dispatch unavailable (e.g. a failed hydrate) never
            # reached the Worker, so the committed intent is safe to compensate.
            if not dispatch.attempted:
                await self._compensate(community_id, server_id, worker_id, original=exc)
            raise
        if outcome.success:
            return server
        if dispatch.attempted and outcome.status is CommandStatus.INVALID_STATE:
            # The Worker refused the START because an instance for this server is
            # ALREADY live on the assigned Worker — INVALID_STATE on a start is only
            # "already running" or a pending failed-stop orphan, never a "nothing
            # started" refusal (instancemanager handleStart). (Gate on
            # ``dispatch.attempted`` so a PRE-dispatch INVALID_STATE — a failed
            # hydrate — still compensates: that one never reached the start.)
            # Compensating here (desired=stopped + unassign) would orphan that live
            # instance: its StatusChange(running) is dropped at the ownership guard
            # once unassigned, the row wedges at
            # desired=stopped/observed=running/unassigned (which the reconciler skips
            # forever), and a later start would place a SECOND instance on a different
            # Worker (issue #773/#774). So converge exactly as redispatch_start does
            # (#213): record observed=running and KEEP desired=running + the
            # assignment — the user's start IS satisfied.
            observed_at = self.clock.now()
            async with self.uow:
                applied = await self.uow.servers.record_observed_state(
                    server_id,
                    observed_state=ObservedState.RUNNING,
                    observed_at=observed_at,
                )
                await self.uow.commit()
            # Keep the return honest (issue #292): reflect the observed state on the
            # entity only when the guarded write landed; if a same-instant/fresher
            # write won the #216 guard, leave the as-read fields rather than claim a
            # write that did not happen.
            if applied:
                server.observed_state = ObservedState.RUNNING
                server.observed_at = observed_at
            return server
        failure = _dispatch_failure(
            server_id=server_id, kind="StartServer", outcome=outcome
        )
        if dispatch.attempted and outcome.status is CommandStatus.BUSY:
            # The Worker refused the START with BUSY: another mutating lifecycle
            # command for this id is already in flight and its outcome is UNKNOWN
            # (issue #824). Unlike INVALID_STATE (a settled "already running"), we
            # MUST NOT converge observed=running here — the raced original may still
            # FAIL and leave the server down. So neither record-running nor
            # compensate: KEEP desired=running + the assignment, and raise so the
            # caller sees a retryable conflict. A later reconcile tick takes the
            # redispatch_start path to the SAME Worker once the in-flight command
            # settles (a genuine success then emits a StatusChange; a failure leaves
            # the row diverged for the next retry). Gate on ``dispatch.attempted`` so
            # a PRE-dispatch BUSY — a hydrate refused for the same race — still
            # compensates below: that one never reached the start.
            raise failure
        await self._compensate(community_id, server_id, worker_id, original=failure)
        raise failure

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
            worker_id = await self._place(server)
            if worker_id is None:
                raise NoEligibleWorkerError(str(server_id.value))
            server.assigned_worker_id = worker_id
            server.config = {**server.config, JAR_KEY_CONFIG_FIELD: jar_key}
            server.updated_at = self.clock.now()
            # Release the placement reservation on any failure across the CAS+commit
            # window — a raising update_lifecycle or commit, the lost-race abort, or a
            # task cancellation at an await — so the tentatively-taken slot (#778) does
            # not leak permanently (a never-committed server id never enters the
            # authoritative tally, so no reconnect rebuild reclaims it). Releasing on
            # an ambiguous commit failure is safe: a landed commit undercounts by one
            # until the reconnect rebuild, the recovery this path already relies on.
            try:
                applied = await self.uow.servers.update_lifecycle(
                    server,
                    expected_from=DesiredState.RUNNING,
                    require_unassigned=True,
                )
                if not applied:
                    # A concurrent assignment (a real start or another reconcile tick)
                    # won the compare-and-set; abort before dispatch or any committed
                    # count change so the lost race causes no double placement.
                    raise LifecycleTransitionConflictError(str(server_id.value))
                await self.uow.commit()
                # Confirm the placement reservation as a committed assignment now the
                # intent is durable (#778); a no-op if a reconnect rebuild already
                # counted this row. Confirm HERE — inside the transaction, immediately
                # after the commit returns and with no await in between (a sync
                # registry call) — not after the ``async with`` block: the UoW
                # ``__aexit__`` awaits rollback()/close(), suspension points outside
                # this try/except where a client-disconnect CancelledError (or a
                # post-commit teardown error) would leave the committed reservation
                # neither confirmed nor released, leaking it permanently (#840).
                self.control_plane.increment_assignment(
                    worker_id=worker_id, server_id=server_id
                )
            except (Exception, asyncio.CancelledError):
                self.control_plane.release_reservation(
                    worker_id=worker_id, server_id=server_id
                )
                raise

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
        """Re-send the start to an assigned, desired-running server.

        Presence-gated skip-hydrate (issue #696): a same-worker restart starts on
        the Worker's EXISTING working set when the Worker reports it still holds it
        (its persistent scratch is the live, newer copy), and only hydrates first
        when the Worker does NOT report holding it (a fresh/wiped scratch). The
        unconditional hydrate this path used to do rolled the world back to the last
        snapshot on every restart by clobbering the newer scratch.

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

        # Generation-gated skip-hydrate (issue #763, generalizing #698): a
        # same-worker restart must NOT hydrate-clobber a live, newer working set, but
        # MUST hydrate a stale one. Compare the generation the assigned Worker reports
        # holding against the authoritative store generation: skip the destructive
        # hydrate only when the Worker holds a generation at least as fresh as the
        # store (its scratch is at least as new as the last snapshot). Hydrate when
        # the Worker reports NOTHING held (None — a fresh/wiped/GC'd scratch, or a
        # Worker too old to report) OR holds a STALE generation (presence at an older
        # generation, e.g. an A->B->A leftover scratch B has since advanced past) —
        # never silently boot an empty/absent/stale working set.
        #
        # The threshold is Storage's authoritative current_generation, read straight
        # from Storage (the single source of truth). The generation advances
        # atomically with the working set it names, so there is no lag window in which
        # a Worker holding the prior generation could satisfy held >= store and
        # WRONGLY skip a hydrate it needs — a #696-class world rollback.
        held_generation = self.control_plane.held_generation(
            worker_id=worker_id, server_id=server_id
        )
        store_generation = await self.store_generation.current_generation(
            community_id=community_id, server_id=server_id
        )
        skip_hydrate = (
            held_generation is not None and held_generation >= store_generation
        )
        outcome = await self._launch(
            server, community_id, server_id, worker_id, skip_hydrate=skip_hydrate
        )
        if outcome.success:
            return server
        if outcome.status is CommandStatus.INVALID_STATE:
            observed_at = self.clock.now()
            async with self.uow:
                applied = await self.uow.servers.record_observed_state(
                    server_id,
                    observed_state=ObservedState.RUNNING,
                    observed_at=observed_at,
                )
                await self.uow.commit()
            # Keep the return honest (issue #292): mutate the entity only when the
            # write landed. If the #216 guard dropped it, a same-instant/fresher
            # observed write already won, so leave the entity's observed fields
            # as-read rather than claim a write that did not happen.
            if applied:
                server.observed_state = ObservedState.RUNNING
                server.observed_at = observed_at
            return server
        if outcome.status is CommandStatus.BUSY:
            # A BUSY start means another mutating lifecycle command for this id is
            # already in flight on the Worker and its outcome is UNKNOWN (issue
            # #824) — NOT the settled "already running" INVALID_STATE above. So we
            # must NOT record observed=running: if the raced original later FAILS,
            # the server is down and a speculative observed=running would stick
            # (the reconciler never re-selects it). Raise instead, changing no row;
            # the assignment and running intent stand, so a later reconcile tick
            # retries the redispatch once the in-flight command settles.
            raise _dispatch_failure(
                server_id=server_id, kind="StartServer", outcome=outcome
            )
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
        *,
        skip_hydrate: bool = False,
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

        ``skip_hydrate`` SKIPS the hydrate and goes straight to start (issue #696).
        Only a same-worker restart (``redispatch_start``) sets it, and only when the
        assigned Worker reports it still HOLDS the live working set: hydrating there
        would unpack the last authoritative snapshot over the Worker's newer scratch
        and roll the world back. A fresh placement (``place_and_start``, the normal
        ``__call__``) always hydrates — the Worker may have an empty/absent scratch,
        and a server that moved A->B->A returns via ``place_and_start``, whose
        leftover scratch on A is stale (B advanced + snapshotted it), so it MUST
        hydrate.
        """

        if not skip_hydrate:
            hydrate = await self.control_plane.hydrate(
                worker_id=worker_id,
                community_id=community_id,
                server_id=server_id,
            )
            if not hydrate.success:
                return hydrate
        if dispatch is not None:
            dispatch.attempted = True
        # Source the per-server memory limit from the config blob (#705 helper) and
        # convert MiB -> bytes for the wire (#706). Unset -> 0, so the Worker driver
        # keeps picking a default heap (pre-#706 behavior).
        memory_limit_mb = memory_limit_from_config(server.config)
        memory_limit_bytes = (
            memory_limit_mb * 1024 * 1024 if memory_limit_mb is not None else 0
        )
        # Source the per-server CPU allocation from the config blob (#722 helper) and
        # carry it as-is on the wire (#723). Unset -> 0, so the Worker driver applies
        # its default weight. No derivation (unlike the memory -> -Xmx path).
        cpu_millis = cpu_allocation_from_config(server.config) or 0
        return await self.control_plane.start(
            worker_id=worker_id,
            server_id=server_id,
            backend=server.execution_backend,
            server_type=server.server_type,
            jar_relpath=_DEFAULT_JAR_RELPATH,
            minecraft_version=server.mc_version,
            memory_limit_bytes=memory_limit_bytes,
            cpu_millis=cpu_millis,
        )

    async def _place(self, server: Server) -> WorkerId | None:
        """Place ``server`` with commit-based resource awareness (#710).

        Sums the declared resources of the servers already running on each Worker
        (the committed accounting) and passes them, with this server's own memory
        request, through the control-plane seam so placement can avoid grossly
        oversubscribing a host's advertised memory. The DB read lives here at the
        application boundary; the pure placement filter stays free of I/O.
        """

        committed = committed_resources_by_worker(
            await self.uow.servers.list_running_assigned()
        )
        return await self.control_plane.place(
            server_id=server.id,
            backend=server.execution_backend,
            memory_limit_mb=memory_limit_from_config(server.config),
            committed_by_worker=committed,
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
            self.control_plane.decrement_assignment(
                worker_id=worker_id, server_id=server_id
            )

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
            self.control_plane.decrement_assignment(
                worker_id=worker_id, server_id=server_id
            )


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
        # (running_assignment_ids_for_worker, used to rebuild after a reconnect): both
        # define load as "servers assigned with desired=running". Deferring the
        # decrement to the StatusChange(stopped) event was considered but would
        # leave the count disagreeing with the desired-state tally during the
        # graceful-stop window and on a missed event.
        self.control_plane.decrement_assignment(
            worker_id=worker_id, server_id=server_id
        )
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
                applied = await self.uow.servers.record_observed_state(
                    server_id,
                    observed_state=ObservedState.STOPPED,
                    observed_at=observed_at,
                    unassign=True,
                )
                await self.uow.commit()
            # Keep the return honest (issue #292): mutate the entity only when the
            # write landed. The guard and the unassign share one UPDATE, so a
            # dropped write also dropped the unassign — leave both observed fields
            # and the assignment as-read rather than claim a write that did not
            # happen.
            if applied:
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
            applied = await self.uow.servers.record_observed_state(
                server_id,
                observed_state=ObservedState.STOPPED,
                observed_at=observed_at,
                unassign=True,
            )
            await self.uow.commit()
        # Keep the return honest (issue #292): mutate the entity only when the
        # write landed; if the #216 guard dropped it, a same-instant/fresher write
        # already won and dropped the unassign atomically, so leave the observed
        # fields and assignment as-read.
        if applied:
            server.observed_state = ObservedState.STOPPED
            server.observed_at = observed_at
            server.assigned_worker_id = None
        # Final snapshot AFTER the process has exited (the graceful stop above
        # only returns once the Worker reports the process gone), so the captured
        # working set is quiescent (FR-DATA-4, FR-DATA-7).
        await self._final_snapshot(
            worker_id=worker_id, community_id=community_id, server_id=server_id
        )
        return server

    async def _final_snapshot(
        self,
        *,
        worker_id: WorkerId,
        community_id: CommunityId,
        server_id: ServerId,
    ) -> None:
        """Capture the quiescent working set after a confirmed graceful stop.

        Shared by ``__call__`` and ``redispatch_stop`` (issue #846) so reconciler-
        and drain-driven stops take the same final snapshot as a direct
        ``server:stop`` — otherwise post-#845 the Worker retains the stop scratch
        waiting on a snapshot that never comes, and progression since the last
        periodic snapshot is lost (FR-DATA-7).

        A snapshot failure is logged, not raised: the stop itself already succeeded
        and the server is down. But the failure is logged at ERROR, not warning
        (issue #841): this is the ONLY final-snapshot path for a graceful stop, and
        the server is now stopped+unassigned, so there is no periodic-snapshot or
        reconciler retry to recover it — a failure here means the world progressed
        since the last periodic snapshot is permanently lost. A silent warning is
        exactly what hid the #841 regression (a worker-side empty_snapshot 400
        swallowed here), so it must be loud. (The Worker self-addresses no Storage;
        the API drives the snapshot because only it knows the (community, server)
        scope.)
        """

        try:
            snapshot = await self.control_plane.snapshot(
                worker_id=worker_id,
                community_id=community_id,
                server_id=server_id,
            )
            if not snapshot.success:
                _LOG.error(
                    "final snapshot on graceful stop FAILED for server %s: %s; "
                    "the working set was NOT captured and progression since the last "
                    "periodic snapshot is lost (no retry exists for a stopped server)",
                    server_id.value,
                    snapshot.message or snapshot.status.value,
                )
        except WorkerUnavailableError:
            _LOG.error(
                "final snapshot on graceful stop could not reach the Worker for "
                "server %s; the stop succeeded but the working set was NOT captured "
                "and progression since the last periodic snapshot is lost",
                server_id.value,
            )

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
            applied = await self.uow.servers.record_observed_state(
                server_id,
                observed_state=ObservedState.STOPPED,
                observed_at=observed_at,
                unassign=True,
            )
            await self.uow.commit()
        # Keep the return honest (issue #292): mutate the entity only when the
        # write landed; if the #216 guard dropped it, leave the observed fields and
        # assignment as-read rather than claim a write that did not happen.
        if applied:
            server.observed_state = ObservedState.STOPPED
            server.observed_at = observed_at
            server.assigned_worker_id = None
        # Final snapshot, mirroring __call__ (issue #846): only on a confirmed live
        # stop. SERVER_NOT_FOUND means no live instance remained on the Worker, so
        # there is no working set to capture — skip it.
        if outcome.status is not CommandStatus.SERVER_NOT_FOUND:
            await self._final_snapshot(
                worker_id=worker_id,
                community_id=community_id,
                server_id=server_id,
            )
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
        if outcome.status is CommandStatus.SERVER_NOT_FOUND:
            # The server stopped between the observed-running check above and this
            # dispatch: the Worker's handleServerCommand returns SERVER_NOT_FOUND
            # (no live instance), never INVALID_STATE, for a not-running target
            # (worker/internal/application/instancemanager/instancemanager.go:412-419,
            # pinned by the #204 contract guard). Surface it as not-running.
            raise ServerNotRunningError(str(server_id.value))
        if not outcome.success:
            raise _dispatch_failure(
                server_id=server_id, kind="ServerCommand", outcome=outcome
            )
        return outcome.output
