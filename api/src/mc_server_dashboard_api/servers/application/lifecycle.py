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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandStatus,
    ControlPlane,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
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
        # Hydrate the working set BEFORE the launch (FR-DATA-4): the API drives a
        # pull of the authoritative working set from Storage to the Worker's
        # scratch, then the Worker launches against it. A server with no published
        # working set yet hydrates to an empty dir (the data-plane endpoint is 204
        # and the Worker starts fresh), so this is not an error.
        try:
            hydrate = await self.control_plane.hydrate(
                worker_id=worker_id,
                community_id=community_id,
                server_id=server_id,
            )
        except WorkerUnavailableError as exc:
            await self._compensate(community_id, server_id, worker_id, original=exc)
            raise
        if not hydrate.success:
            failure = CommandDispatchError(hydrate.message or hydrate.status.value)
            await self._compensate(community_id, server_id, worker_id, original=failure)
            raise failure
        try:
            outcome = await self.control_plane.start(
                worker_id=worker_id,
                server_id=server_id,
                backend=server.execution_backend,
                jar_relpath=_DEFAULT_JAR_RELPATH,
                minecraft_version=server.mc_version,
            )
        except WorkerUnavailableError as exc:
            await self._compensate(community_id, server_id, worker_id, original=exc)
            raise
        if not outcome.success:
            failure = CommandDispatchError(outcome.message or outcome.status.value)
            await self._compensate(community_id, server_id, worker_id, original=failure)
            raise failure
        return server

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

        If the compensation itself fails, the original dispatch failure
        (``original``) must not be masked: we log both errors explicitly and
        re-raise the compensation error chained from the original so neither is
        lost (the record is left diverged, which a reconciler later detects).
        """

        try:
            async with self.uow:
                server = await self.uow.servers.get_by_id(server_id)
                if server is not None and server.community_id == community_id:
                    server.desired_state = DesiredState.STOPPED
                    server.assigned_worker_id = None
                    server.updated_at = self.clock.now()
                    await self.uow.servers.update_lifecycle(
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
        self.control_plane.decrement_assignment(worker_id=worker_id)


@dataclass(frozen=True)
class StopServer:
    """Stop a running server gracefully (server:stop, FR-SRV-2)."""

    uow: UnitOfWork
    control_plane: ControlPlane
    clock: Clock

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
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
            worker_id=worker_id, server_id=server_id
        )
        if not outcome.success:
            raise CommandDispatchError(outcome.message or outcome.status.value)
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
            raise CommandDispatchError(outcome.message or outcome.status.value)
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
            raise CommandDispatchError(outcome.message or outcome.status.value)
        return outcome.output
