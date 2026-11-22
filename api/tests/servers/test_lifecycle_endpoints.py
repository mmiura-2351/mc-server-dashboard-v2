"""Endpoint tests for the lifecycle routes (start/stop/restart/command).

In-process via FastAPI's TestClient with the use cases and authorization Ports
faked (NFR-TEST-1, no DB / gRPC). Verifies the two-layer gate per route
(non-member 404, member-without-permission 403), the domain-error -> HTTP-code
mapping (invalid transition 409, no eligible worker / worker unavailable 503,
command failure 409, not-running 409), and the success serialisation.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
    PermissionChecker,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    ResourceRef,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_current_user,
    get_membership_visibility,
    get_permission_checker,
    get_release_held_server,
    get_restart_server,
    get_send_server_command,
    get_start_server,
    get_stop_server,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    SendServerCommand,
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    MAX_CONSOLE_LINE_BYTES,
    CommandOutcome,
    CommandStatus,
    UnavailableWorkerState,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    EulaNotAcceptedError,
    InvalidLifecycleTransitionError,
    LifecycleTransitionConflictError,
    NoEligibleWorkerError,
    ServerFileStorageUnavailableError,
    ServerNotFoundError,
    ServerNotRunningError,
    SnapshotUploadInFlightError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    DesiredState,
    FinalSnapshotRefusal,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.client_utils import enter_client
from tests.servers.contract_table import worker_status
from tests.servers.fakes import (
    DatabaseOutageError,
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeServerRepository,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
    RefusalWriteFailsRepository,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _FakeVisibility(MembershipVisibility):
    def __init__(self, *, member: bool) -> None:
        self._member = member

    async def is_member(self, *, user_id: UserId, community_id: CommunityId) -> bool:
        return self._member


class _FakeChecker(PermissionChecker):
    def __init__(self, *, allow: bool) -> None:
        self._allow = allow

    async def can(
        self, *, user: AuthUser, operation: Permission, resource: ResourceRef
    ) -> bool:
        return self._allow


class _FakeUseCase:
    def __init__(self, *, result: object = None, error: Exception | None = None):
        self._result = result
        self._error = error
        # The keyword args of the last call, so a test can assert what the route
        # forwarded (e.g. the ?force flag for stop; issue #270).
        self.kwargs: dict[str, object] = {}

    async def __call__(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._result


def _client(app: object) -> TestClient:
    return enter_client(TestClient(app))  # type: ignore[arg-type]


def _server(community_id: uuid.UUID) -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=ServersCommunityId(community_id),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.RUNNING,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _app(
    *,
    member: bool,
    allow: bool,
    start: _FakeUseCase | None = None,
    stop: _FakeUseCase | None = None,
    restart: _FakeUseCase | None = None,
    command: _FakeUseCase | None = None,
) -> object:
    from tests.identity.fakes import make_user

    app = _shared_app
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if start is not None:
        app.dependency_overrides[get_start_server] = lambda: start
    if stop is not None:
        app.dependency_overrides[get_stop_server] = lambda: stop
    if restart is not None:
        app.dependency_overrides[get_restart_server] = lambda: restart
    if command is not None:
        app.dependency_overrides[get_send_server_command] = lambda: command
    return app


def _url(community: uuid.UUID, server: uuid.UUID, action: str) -> str:
    return f"/api/communities/{community}/servers/{server}/{action}"


# --- two-layer gate --------------------------------------------------------


def test_non_member_gets_404_on_start() -> None:
    app = _app(member=False, allow=True, start=_FakeUseCase())
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_stop() -> None:
    app = _app(member=True, allow=False, stop=_FakeUseCase())
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "stop"))
    assert resp.status_code == 403


def test_member_without_permission_gets_403_on_command() -> None:
    app = _app(member=True, allow=False, command=_FakeUseCase())
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "command"), json={"line": "list"}
    )
    assert resp.status_code == 403


# --- success ---------------------------------------------------------------


def test_start_success_returns_server() -> None:
    community = uuid.uuid4()
    app = _app(member=True, allow=True, start=_FakeUseCase(result=_server(community)))
    client = _client(app)
    resp = client.post(_url(community, uuid.uuid4(), "start"))
    assert resp.status_code == 200
    assert resp.json()["desired_state"] == "running"


def test_command_success_returns_output() -> None:
    app = _app(member=True, allow=True, command=_FakeUseCase(result="players: 3"))
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "command"), json={"line": "list"}
    )
    assert resp.status_code == 200
    assert resp.json()["output"] == "players: 3"


# --- error mapping ---------------------------------------------------------


def test_start_invalid_transition_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        start=_FakeUseCase(error=InvalidLifecycleTransitionError("x")),
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "invalid_transition"


def test_start_transition_conflict_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        start=_FakeUseCase(error=LifecycleTransitionConflictError("x")),
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "transition_conflict"


def test_start_with_its_workers_upload_in_flight_is_409_upload_in_flight() -> None:
    # The start did not hydrate its Worker while that Worker's upload of the
    # server was in flight, and kept both for the reconciler (issue #3950); the
    # web UI reads the reason as a pending start.
    app = _app(
        member=True,
        allow=True,
        start=_FakeUseCase(error=SnapshotUploadInFlightError("x")),
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "upload_in_flight"


def test_start_no_eligible_worker_is_503() -> None:
    app = _app(
        member=True, allow=True, start=_FakeUseCase(error=NoEligibleWorkerError("x"))
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 503
    assert resp.json()["reason"] == "no_eligible_worker"


def test_start_owed_player_file_write_failure_is_503() -> None:
    # StartServer could not regenerate an ops.json / whitelist.json a group change
    # left stale (issue #3223); nothing was started, so the caller retries.
    app = _app(
        member=True,
        allow=True,
        start=_FakeUseCase(error=WorkingSetSeedFailedError("x")),
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 503
    assert resp.json()["reason"] == "seed_failed"


def test_start_store_outage_is_503() -> None:
    # The EULA read, or the ``accept_eula`` write, hit a store outage (issue
    # #3233). Both run before the desired-state flip, so nothing started.
    app = _app(
        member=True,
        allow=True,
        start=_FakeUseCase(error=ServerFileStorageUnavailableError("x")),
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 503
    assert resp.json()["reason"] == "storage_unavailable"


def test_start_worker_unavailable_is_503() -> None:
    app = _app(
        member=True, allow=True, start=_FakeUseCase(error=WorkerUnavailableError("x"))
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 503
    assert resp.json()["reason"] == "worker_unavailable"


def test_start_command_failure_is_409() -> None:
    app = _app(
        member=True, allow=True, start=_FakeUseCase(error=CommandDispatchError("x"))
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "command_failed"


def test_start_port_conflict_is_409_with_reason() -> None:
    # A sanitized port_conflict category (issue #225) renders as a 409 whose
    # reason names the category instead of the generic command_failed.
    app = _app(
        member=True,
        allow=True,
        start=_FakeUseCase(error=CommandDispatchError("x", reason="port_conflict")),
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "port_conflict"


def test_start_image_missing_is_409_with_reason() -> None:
    app = _app(
        member=True,
        allow=True,
        start=_FakeUseCase(error=CommandDispatchError("x", reason="image_missing")),
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "image_missing"


def test_start_worker_busy_is_409_with_reason() -> None:
    # A BUSY Worker rejection (issue #867) surfaces as worker_busy, not the
    # generic command_failed, so clients can distinguish a retryable in-flight
    # race from a settled failure.
    app = _app(
        member=True,
        allow=True,
        start=_FakeUseCase(error=CommandDispatchError("x", reason="worker_busy")),
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "worker_busy"


def test_start_eula_not_accepted_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        start=_FakeUseCase(error=EulaNotAcceptedError("x")),
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "eula_not_accepted"


def test_start_accept_eula_forwards_to_use_case() -> None:
    community = uuid.uuid4()
    start = _FakeUseCase(result=_server(community))
    app = _app(member=True, allow=True, start=start)
    client = _client(app)
    resp = client.post(
        _url(community, uuid.uuid4(), "start"), params={"accept_eula": "true"}
    )
    assert resp.status_code == 200
    assert start.kwargs["accept_eula"] is True


def test_start_defaults_accept_eula_false() -> None:
    community = uuid.uuid4()
    start = _FakeUseCase(result=_server(community))
    app = _app(member=True, allow=True, start=start)
    client = _client(app)
    resp = client.post(_url(community, uuid.uuid4(), "start"))
    assert resp.status_code == 200
    assert start.kwargs["accept_eula"] is False


def test_stop_missing_server_is_404() -> None:
    app = _app(
        member=True, allow=True, stop=_FakeUseCase(error=ServerNotFoundError("x"))
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "stop"))
    assert resp.status_code == 404


def test_stop_defaults_to_graceful_force_false() -> None:
    # No ?force query param: the route forwards force=False (today's behavior).
    community = uuid.uuid4()
    stop = _FakeUseCase(result=_server(community))
    app = _app(member=True, allow=True, stop=stop)
    client = _client(app)
    resp = client.post(_url(community, uuid.uuid4(), "stop"))
    assert resp.status_code == 200
    assert stop.kwargs["force"] is False


def test_stop_force_true_forwards_to_use_case() -> None:
    # ?force=true threads force=True into the StopServer use case (issue #270).
    community = uuid.uuid4()
    stop = _FakeUseCase(result=_server(community))
    app = _app(member=True, allow=True, stop=stop)
    client = _client(app)
    resp = client.post(_url(community, uuid.uuid4(), "stop"), params={"force": "true"})
    assert resp.status_code == 200
    assert stop.kwargs["force"] is True


# --- stop of a row already desired=stopped (issue #3491) ---------------------


def _desired_stopped(
    community: uuid.UUID, observed: ObservedState, *, assigned: bool
) -> Server:
    return dataclasses.replace(
        _server(community),
        desired_state=DesiredState.STOPPED,
        observed_state=observed,
        assigned_worker_id=WorkerId(uuid.uuid4()) if assigned else None,
    )


def _post_real_stop(
    server: Server,
    cp: FakeControlPlane,
    *,
    recorder: RecordingAuditRecorder | None = None,
    servers: FakeServerRepository | None = None,
) -> httpx2.Response:
    uow = FakeUnitOfWork(servers=servers)
    uow.servers.seed(server)
    use_case = StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))
    app = _app(member=True, allow=True)
    _shared_app.dependency_overrides[get_stop_server] = lambda: use_case
    if recorder is not None:
        _shared_app.dependency_overrides[get_audit_recorder] = lambda: recorder
    return _client(app).post(_url(server.community_id.value, server.id.value, "stop"))


def test_stop_on_a_drained_still_running_server_stops_it_now() -> None:
    # A drain left (stopped, running, assigned) for the reconciler; the operator's
    # stop no longer 409s but dispatches the stop immediately.
    server = _desired_stopped(uuid.uuid4(), ObservedState.RUNNING, assigned=True)
    cp = FakeControlPlane()

    resp = _post_real_stop(server, cp)

    assert resp.status_code == 200
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]
    assert resp.json()["observed_state"] == "stopped"
    assert resp.json()["assigned_worker_id"] is None


def test_stop_on_stopped_unknown_assigned_converges_like_the_reconciler() -> None:
    # The Worker answers SERVER_NOT_FOUND: final snapshot, then release.
    server = _desired_stopped(uuid.uuid4(), ObservedState.UNKNOWN, assigned=True)
    cp = FakeControlPlane(
        outcomes={
            "stop": CommandOutcome(
                status=worker_status("StopServer", "instance_stopped")
            )
        }
    )

    resp = _post_real_stop(server, cp)

    assert resp.status_code == 200
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]
    assert resp.json()["assigned_worker_id"] is None


@pytest.mark.parametrize("code", [CommandStatus.BUSY, CommandStatus.UNAVAILABLE])
def test_a_stop_whose_final_snapshot_is_refused_answers_the_server_held(
    code: CommandStatus,
) -> None:
    # Issue #3697: the stop the operator asked for is done, so it answers. The
    # server stays assigned to its Worker, which still owes the snapshot, and
    # the response says why.
    worker = WorkerId(uuid.uuid4())
    server = dataclasses.replace(_server(uuid.uuid4()), assigned_worker_id=worker)
    cp = FakeControlPlane(outcomes={"snapshot": CommandOutcome(status=code)})

    resp = _post_real_stop(server, cp)

    assert resp.status_code == 200
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]
    body = resp.json()
    assert body["observed_state"] == "stopped"
    assert body["assigned_worker_id"] == str(worker.value)
    assert body["final_snapshot_refusal"] == code.value


def test_a_stop_of_a_held_server_refused_again_is_409_and_audited_denied() -> None:
    # Issue #3697: an operator's stop of a held server retries the snapshot at
    # once; a refusal again says so rather than a generic worker failure.
    server = _desired_stopped(uuid.uuid4(), ObservedState.STOPPED, assigned=True)
    cp = FakeControlPlane(
        outcomes={"snapshot": CommandOutcome(status=CommandStatus.UNAVAILABLE)}
    )
    recorder = RecordingAuditRecorder()

    resp = _post_real_stop(server, cp, recorder=recorder)

    assert resp.status_code == 409
    assert resp.json()["reason"] == "final_snapshot_refused"
    assert [k for k, _, _ in cp.dispatched] == ["snapshot"]
    [event] = recorder.events
    assert event.operation == ops.SERVER_STOP
    assert event.outcome is Outcome.DENIED


def test_a_refused_re_stop_whose_refusal_write_fails_is_still_409() -> None:
    # Issue #4103: the failed write of the refusal is logged, and the refusal
    # still answers the stop, rather than a 500 for the database error.
    server = _desired_stopped(uuid.uuid4(), ObservedState.STOPPED, assigned=True)
    cp = FakeControlPlane(
        outcomes={"snapshot": CommandOutcome(status=CommandStatus.BUSY)}
    )
    repo = RefusalWriteFailsRepository(DatabaseOutageError("database unreachable"))

    resp = _post_real_stop(server, cp, servers=repo)

    assert resp.status_code == 409
    assert resp.json()["reason"] == "final_snapshot_refused"
    assert repo.refusal_writes == 1


def test_a_stop_of_a_held_server_whose_worker_is_gone_is_503_and_names_the_exits() -> (
    None
):
    # Issue #4111: the stop releases nothing, so it answers the stop route's
    # 503 for a Worker that is not connected, and says what does release it.
    server = dataclasses.replace(
        _desired_stopped(uuid.uuid4(), ObservedState.UNKNOWN, assigned=True),
        final_snapshot_refusal=FinalSnapshotRefusal.UNAVAILABLE,
    )
    assert server.assigned_worker_id is not None
    cp = FakeControlPlane(connected={server.assigned_worker_id: False})
    recorder = RecordingAuditRecorder()

    resp = _post_real_stop(server, cp, recorder=recorder)

    assert resp.status_code == 503
    body = resp.json()
    assert body["reason"] == "worker_unavailable"
    assert body["worker_state"] == "not_connected"
    assert body["detail"] == (
        "The server's Worker refused its final snapshot and is not connected, so "
        "the stop released nothing and the server stays held. It is released when "
        "the Worker returns and the snapshot is taken, when the Worker has stayed "
        "away for the reconciler's full grace, or when someone allowed to stop it "
        "releases it without the snapshot."
    )
    assert cp.dispatched == []
    [event] = recorder.events
    assert event.operation == ops.SERVER_STOP
    assert event.outcome is Outcome.ERROR


def test_stop_on_an_unassigned_desired_stopped_server_is_still_409() -> None:
    server = _desired_stopped(uuid.uuid4(), ObservedState.STOPPED, assigned=False)
    cp = FakeControlPlane()

    resp = _post_real_stop(server, cp)

    assert resp.status_code == 409
    assert resp.json()["reason"] == "invalid_transition"
    assert cp.dispatched == []


def test_stop_on_an_unplaced_running_intent_withdraws_it() -> None:
    # Issue #3815: no Worker holds the start intent yet, so the stop commits
    # desired=stopped and answers it as any stop, with nothing dispatched.
    server = dataclasses.replace(
        _server(uuid.uuid4()), observed_state=ObservedState.STOPPED
    )
    cp = FakeControlPlane()

    resp = _post_real_stop(server, cp)

    assert resp.status_code == 200
    assert resp.json()["desired_state"] == "stopped"
    assert resp.json()["assigned_worker_id"] is None
    assert cp.dispatched == []


def test_restart_not_running_is_409_with_reason() -> None:
    # Issue #2441: a restart the Worker refused because it holds no live instance
    # names server_not_running, not the unclassified command_failed catch-all.
    app = _app(
        member=True,
        allow=True,
        restart=_FakeUseCase(error=ServerNotRunningError("x")),
    )
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "restart"))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_not_running"


def test_command_not_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        command=_FakeUseCase(error=ServerNotRunningError("x")),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "command"), json={"line": "list"}
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_not_running"


# --- a refused server.properties (issue #3347) -------------------------------

# The planted file's content and where the Worker found it. The Worker's message
# names the path (never the content); neither may reach the HTTP response.
_SCRATCH_PATH = "/var/lib/mcsd/scratch/s1/server.properties"
_PLANTED_CONTENT = "rcon.password=the-other-servers-password"

# The Worker's reader message for the two refusals the issue names. A message
# that carried file content would be a Worker bug; the API must not echo even
# that, so one case embeds the content to prove the response drops it.
_REFUSED_PROPERTIES = {
    "symlink": f"serverproperties: {_SCRATCH_PATH} is a symbolic link; "
    "refusing to follow it",
    "oversized": f"serverproperties: {_SCRATCH_PATH} is larger than 4194304 "
    f"bytes; refusing to read it ({_PLANTED_CONTENT})",
}


def _assert_names_the_refusal_only(resp: httpx2.Response) -> None:
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "server_properties_unreadable"
    assert body["type"] == "urn:mcsd:error:server_properties_unreadable"
    # Nothing of the Worker's message: no path outside the working set, no content.
    assert _SCRATCH_PATH not in resp.text
    assert "scratch" not in resp.text
    assert _PLANTED_CONTENT not in resp.text
    assert "symbolic link" not in resp.text


@pytest.mark.parametrize("refusal", sorted(_REFUSED_PROPERTIES))
def test_start_with_a_refused_server_properties_is_409_naming_it(
    refusal: str,
) -> None:
    # The real StartServer use case behind the route, the Worker answering the
    # start with the code it emits for this precondition (the contract row).
    community = uuid.uuid4()
    server = dataclasses.replace(
        _server(community),
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
    )
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        place_to=WorkerId(uuid.uuid4()),
        outcomes={
            "start": CommandOutcome(
                status=worker_status("StartServer", "server_properties_unreadable"),
                message="instancemanager: start: containerdriver: "
                + _REFUSED_PROPERTIES[refusal],
            )
        },
    )
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        file_store=FakeFileStore(seed_eula=True),
    )
    app = _app(member=True, allow=True)
    _shared_app.dependency_overrides[get_start_server] = lambda: use_case

    resp = _client(app).post(_url(community, server.id.value, "start"))

    _assert_names_the_refusal_only(resp)


@pytest.mark.parametrize("refusal", sorted(_REFUSED_PROPERTIES))
def test_command_with_a_refused_server_properties_is_409_naming_it(
    refusal: str,
) -> None:
    community = uuid.uuid4()
    server = dataclasses.replace(
        _server(community), assigned_worker_id=WorkerId(uuid.uuid4())
    )
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(
            status=worker_status("ServerCommand", "server_properties_unreadable"),
            message="instancemanager: open rcon: rcon: read server.properties: "
            + _REFUSED_PROPERTIES[refusal],
        )
    )
    use_case = SendServerCommand(uow=uow, control_plane=cp)
    app = _app(member=True, allow=True)
    _shared_app.dependency_overrides[get_send_server_command] = lambda: use_case

    resp = _client(app).post(
        _url(community, server.id.value, "command"), json={"line": "list"}
    )

    _assert_names_the_refusal_only(resp)


# --- an expired command (issue #3305) ---------------------------------------


def _assert_names_the_expiry_only(resp: httpx2.Response) -> None:
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "command_expired"
    assert body["type"] == "urn:mcsd:error:command_expired"
    assert "waited" not in resp.text


# The Worker's EXPIRED message (worker/internal/domain/session/session.go): log
# only, like every Worker message.
_EXPIRED_MESSAGE = (
    "session: command expired after waiting 31s to start (budget 29s); not executed"
)


def test_an_expired_start_is_409_naming_it() -> None:
    community = uuid.uuid4()
    server = dataclasses.replace(
        _server(community),
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
    )
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        place_to=WorkerId(uuid.uuid4()),
        outcomes={
            "start": CommandOutcome(
                status=worker_status("StartServer", "command_expired"),
                message=_EXPIRED_MESSAGE,
            )
        },
    )
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        file_store=FakeFileStore(seed_eula=True),
    )
    app = _app(member=True, allow=True)
    _shared_app.dependency_overrides[get_start_server] = lambda: use_case

    resp = _client(app).post(_url(community, server.id.value, "start"))

    _assert_names_the_expiry_only(resp)


def test_an_expired_command_is_409_naming_it() -> None:
    community = uuid.uuid4()
    server = dataclasses.replace(
        _server(community), assigned_worker_id=WorkerId(uuid.uuid4())
    )
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(
            status=worker_status("ServerCommand", "command_expired"),
            message=_EXPIRED_MESSAGE,
        )
    )
    use_case = SendServerCommand(uow=uow, control_plane=cp)
    app = _app(member=True, allow=True)
    _shared_app.dependency_overrides[get_send_server_command] = lambda: use_case

    resp = _client(app).post(
        _url(community, server.id.value, "command"), json={"line": "list"}
    )

    _assert_names_the_expiry_only(resp)


# --- a start whose hydrate finds the server live (issue #3865) --------------


def test_a_start_whose_hydrate_finds_the_server_live_is_409_invalid_transition() -> (
    None
):
    # The placed Worker already runs the server, so it refused the hydrate. The
    # start keeps the server there and answers the start verb's "already running"
    # conflict, the reason a second start of a running server gets.
    community = uuid.uuid4()
    server = dataclasses.replace(
        _server(community),
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
    )
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        place_to=WorkerId(uuid.uuid4()),
        outcomes={
            "hydrate": CommandOutcome(
                status=worker_status("HydrateTrigger", "instance_running"),
                message="instancemanager: server already running",
            )
        },
    )
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        file_store=FakeFileStore(seed_eula=True),
    )
    app = _app(member=True, allow=True)
    _shared_app.dependency_overrides[get_start_server] = lambda: use_case

    resp = _client(app).post(_url(community, server.id.value, "start"))

    assert resp.status_code == 409
    assert resp.json()["reason"] == "invalid_transition"


# --- the start guard the Web UI gates Start on (issue #3860) ----------------

# The answer to a start in every (desired, observed, assigned) row, which the
# Web UI's ``startRefusal`` (serverState.ts) is tested against too: a guard that
# starts reading the observed state, or answers a row differently, fails here.
# An input the rows do not vary is not caught.
_START_GUARD: list[dict[str, Any]] = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "webui"
        / "src"
        / "pages"
        / "startGuard.json"
    ).read_text()
)["rows"]


def test_the_start_guard_table_lists_every_row_once() -> None:
    assert sorted(
        (row["desired"], row["observed"], row["assigned"]) for row in _START_GUARD
    ) == sorted(
        (desired.value, observed.value, assigned)
        for desired in DesiredState
        for observed in ObservedState
        for assigned in (False, True)
    )


@pytest.mark.parametrize(
    "row",
    _START_GUARD,
    ids=lambda row: "{desired}-{observed}-{held}".format(
        held="assigned" if row["assigned"] else "unassigned", **row
    ),
)
def test_start_answers_each_row_as_the_web_ui_expects(row: dict[str, Any]) -> None:
    community = uuid.uuid4()
    server = dataclasses.replace(
        _server(community),
        desired_state=DesiredState(row["desired"]),
        observed_state=ObservedState(row["observed"]),
        assigned_worker_id=WorkerId(uuid.uuid4()) if row["assigned"] else None,
    )
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    worker = WorkerId(uuid.uuid4())
    cp = FakeControlPlane(place_to=worker)
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        file_store=FakeFileStore(seed_eula=True),
    )
    app = _app(member=True, allow=True)
    _shared_app.dependency_overrides[get_start_server] = lambda: use_case

    resp = _client(app).post(_url(community, server.id.value, "start"))

    if row["reason"] is None:
        assert resp.status_code == 200
        assert ("start", worker, server.id) in cp.dispatched
    else:
        assert resp.status_code == 409
        assert resp.json()["reason"] == row["reason"]
        assert cp.dispatched == []


# --- a start whose hydrate may meet an instance (issue #3890) ---------------


@pytest.mark.parametrize(
    ("precondition", "status_code", "reason"),
    [
        ("command_in_flight", 409, "worker_busy"),
        ("orphan_pending", 409, "worker_busy"),
        ("untracked_container", 503, "worker_unavailable"),
        ("command_expired", 409, "command_expired"),
    ],
)
def test_a_start_whose_hydrate_may_meet_an_instance_keeps_its_worker(
    precondition: str, status_code: int, reason: str
) -> None:
    # The placed Worker refused the hydrate with an answer that leaves an instance
    # of the server there possible. The route answers that refusal as before, and
    # the server stays desired-running on that Worker for the reconciler.
    community = uuid.uuid4()
    worker = WorkerId(uuid.uuid4())
    server = dataclasses.replace(
        _server(community),
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
    )
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        place_to=worker,
        outcomes={
            "hydrate": CommandOutcome(
                status=worker_status("HydrateTrigger", precondition),
                message="refused",
            )
        },
    )
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        file_store=FakeFileStore(seed_eula=True),
    )
    app = _app(member=True, allow=True)
    _shared_app.dependency_overrides[get_start_server] = lambda: use_case

    resp = _client(app).post(_url(community, server.id.value, "start"))

    assert (resp.status_code, resp.json()["reason"]) == (status_code, reason)
    stored = uow.servers.by_id[server.id]
    assert (stored.desired_state, stored.assigned_worker_id) == (
        DesiredState.RUNNING,
        worker,
    )


# --- a Worker whose backend is unavailable (issues #3673, #3678) ------------


def test_an_unavailable_start_is_503_worker_unavailable() -> None:
    # The Worker answered, but its Docker daemon did not, or a leftover container
    # of the server could not be removed: a transient condition the client
    # retries, so 503 like a Worker that did not answer at all.
    community = uuid.uuid4()
    server = dataclasses.replace(
        _server(community),
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
    )
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        place_to=WorkerId(uuid.uuid4()),
        outcomes={
            "start": CommandOutcome(
                status=worker_status("StartServer", "backend_unavailable"),
                message="instancemanager: start: containerdriver: start container: "
                "execution: backend unavailable: container start timed out after "
                "10s: context deadline exceeded",
            )
        },
    )
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        file_store=FakeFileStore(seed_eula=True),
    )
    app = _app(member=True, allow=True)
    _shared_app.dependency_overrides[get_start_server] = lambda: use_case

    resp = _client(app).post(_url(community, server.id.value, "start"))

    assert resp.status_code == 503
    assert resp.json()["reason"] == "worker_unavailable"
    assert "timed out" not in resp.text


def test_an_unavailable_restart_is_503_worker_unavailable() -> None:
    app = _app(
        member=True,
        allow=True,
        restart=_FakeUseCase(
            error=CommandDispatchError("x", reason="worker_unavailable")
        ),
    )
    resp = _client(app).post(_url(uuid.uuid4(), uuid.uuid4(), "restart"))
    assert resp.status_code == 503
    assert resp.json()["reason"] == "worker_unavailable"


# --- the Worker state a lifecycle 503 names (issue #3820) -------------------

_LIFECYCLE_USE_CASE: dict[str, Callable[..., object]] = {
    "start": get_start_server,
    "stop": get_stop_server,
    "restart": get_restart_server,
}


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
@pytest.mark.parametrize(
    ("worker_state", "member"),
    [
        (UnavailableWorkerState.NOT_CONNECTED, "not_connected"),
        (UnavailableWorkerState.NO_ANSWER, "no_answer"),
    ],
)
def test_a_lifecycle_503_names_the_worker_state(
    action: str, worker_state: UnavailableWorkerState, member: str
) -> None:
    # A Worker that is not connected gets no automatic recovery, while one that
    # did not answer in time may still finish the command, so the WebUI words
    # the two apart from this member. The reason stays worker_unavailable.
    use_case = _FakeUseCase(
        error=WorkerUnavailableError("x", worker_state=worker_state)
    )
    app = _app(member=True, allow=True)
    _shared_app.dependency_overrides[_LIFECYCLE_USE_CASE[action]] = lambda: use_case

    resp = _client(app).post(_url(uuid.uuid4(), uuid.uuid4(), action))

    assert resp.status_code == 503
    assert resp.json()["reason"] == "worker_unavailable"
    assert resp.json()["worker_state"] == member


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
@pytest.mark.parametrize(
    "error",
    [
        # The Worker answered UNAVAILABLE: its container backend did not.
        CommandDispatchError("x", reason="worker_unavailable"),
        # A cause that says nothing about the Worker (missing data-plane config).
        WorkerUnavailableError("x"),
    ],
)
def test_a_lifecycle_503_without_a_worker_state_names_none(
    action: str, error: Exception
) -> None:
    use_case = _FakeUseCase(error=error)
    app = _app(member=True, allow=True)
    _shared_app.dependency_overrides[_LIFECYCLE_USE_CASE[action]] = lambda: use_case

    resp = _client(app).post(_url(uuid.uuid4(), uuid.uuid4(), action))

    assert resp.status_code == 503
    assert resp.json()["reason"] == "worker_unavailable"
    assert "worker_state" not in resp.json()


def test_the_console_503_names_no_worker_state() -> None:
    # Only the lifecycle routes name it; the console keeps its plain 503.
    app = _app(
        member=True,
        allow=True,
        command=_FakeUseCase(
            error=WorkerUnavailableError(
                "x", worker_state=UnavailableWorkerState.NOT_CONNECTED
            )
        ),
    )

    resp = _client(app).post(
        _url(uuid.uuid4(), uuid.uuid4(), "command"), json={"line": "list"}
    )

    assert resp.status_code == 503
    assert resp.json()["reason"] == "worker_unavailable"
    assert "worker_state" not in resp.json()


# --- console line bound (issue #3363) ---------------------------------------


def test_the_console_line_limit_is_the_one_the_worker_holds() -> None:
    limits = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "proto"
            / "contract"
            / "control_plane_limits.json"
        ).read_text()
    )

    assert MAX_CONSOLE_LINE_BYTES == limits["max_console_line_bytes"]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("a" * MAX_CONSOLE_LINE_BYTES, id="ascii"),
        # Three bytes per character: 482 characters are exactly the 1,446 bytes.
        pytest.param("あ" * (MAX_CONSOLE_LINE_BYTES // 3), id="multi-byte"),
    ],
)
def test_command_line_at_the_limit_is_forwarded_unchanged(line: str) -> None:
    assert len(line.encode()) == MAX_CONSOLE_LINE_BYTES
    command = _FakeUseCase(result="ok")
    app = _app(member=True, allow=True, command=command)
    client = _client(app)

    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "command"), json={"line": line})

    assert resp.status_code == 200
    assert resp.json() == {"output": "ok"}
    assert command.kwargs["line"] == line


@pytest.mark.parametrize(
    ("line", "message"),
    [
        pytest.param(
            "a" * (MAX_CONSOLE_LINE_BYTES + 1),
            "String should have at most 1446 characters",
            id="one-character-over",
        ),
        # 483 characters -- well under the character bound -- but 1,449 bytes.
        pytest.param(
            "あ" * (MAX_CONSOLE_LINE_BYTES // 3 + 1),
            "String should have at most 1446 bytes in UTF-8",
            id="under-in-characters-over-in-bytes",
        ),
    ],
)
def test_command_line_past_the_limit_is_422(line: str, message: str) -> None:
    command = _FakeUseCase(result="ok")
    app = _app(member=True, allow=True, command=command)
    client = _client(app)

    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "command"), json={"line": line})

    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json() == {
        "type": "urn:mcsd:error:validation_error",
        "title": "Unprocessable Content",
        "status": 422,
        "reason": "validation_error",
        "errors": [
            {"type": "string_too_long", "loc": ["body", "line"], "msg": message}
        ],
    }
    assert command.kwargs == {}


# --- the release of a held server without its final snapshot (issue #4055) --

_RELEASE = "release-without-snapshot"


class _GrantsOnly(PermissionChecker):
    """Grants exactly the listed permission codes."""

    def __init__(self, *granted: str) -> None:
        self._granted = set(granted)

    async def can(
        self, *, user: AuthUser, operation: Permission, resource: ResourceRef
    ) -> bool:
        return operation.value in self._granted


def _released(community_id: uuid.UUID) -> Server:
    return dataclasses.replace(
        _server(community_id),
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
    )


def _release_app(
    use_case: _FakeUseCase, *, granted: tuple[str, ...] = ("server:stop",)
) -> tuple[object, RecordingAuditRecorder]:
    app = _app(member=True, allow=True)
    recorder = RecordingAuditRecorder()
    _shared_app.dependency_overrides[get_permission_checker] = lambda: _GrantsOnly(
        *granted
    )
    _shared_app.dependency_overrides[get_release_held_server] = lambda: use_case
    _shared_app.dependency_overrides[get_audit_recorder] = lambda: recorder
    return app, recorder


def test_a_release_without_snapshot_answers_the_released_server() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = _FakeUseCase(result=_released(community))
    app, recorder = _release_app(use_case)

    resp = _client(app).post(_url(community, server_id, _RELEASE))

    assert resp.status_code == 200
    body = resp.json()
    assert body["desired_state"] == "stopped"
    assert body["assigned_worker_id"] is None
    assert body["final_snapshot_refusal"] is None
    assert use_case.kwargs == {
        "community_id": ServersCommunityId(community),
        "server_id": ServerId(server_id),
    }
    [event] = recorder.events
    assert event.operation == "server:release_without_snapshot"
    assert event.outcome is Outcome.SUCCESS
    assert event.target_type == ops.TARGET_SERVER
    assert event.target_id == uuid.UUID(body["id"])
    assert event.community_id == community


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (InvalidLifecycleTransitionError("x"), "invalid_transition"),
        (LifecycleTransitionConflictError("x"), "transition_conflict"),
    ],
)
def test_a_refused_release_without_snapshot_is_409_and_audited_denied(
    error: Exception, reason: str
) -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    app, recorder = _release_app(_FakeUseCase(error=error))

    resp = _client(app).post(_url(community, server_id, _RELEASE))

    assert resp.status_code == 409
    assert resp.json()["reason"] == reason
    [event] = recorder.events
    assert event.operation == "server:release_without_snapshot"
    assert event.outcome is Outcome.DENIED
    assert event.target_type == ops.TARGET_SERVER
    assert event.target_id == server_id
    assert event.community_id == community


def test_a_release_without_snapshot_of_a_missing_server_is_404() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = _FakeUseCase(error=ServerNotFoundError("x"))
    app, recorder = _release_app(use_case)

    resp = _client(app).post(_url(community, server_id, _RELEASE))

    assert resp.status_code == 404
    assert resp.json()["reason"] == "not_found"
    # The use case found no server, so a 404 records nothing.
    assert use_case.kwargs == {
        "community_id": ServersCommunityId(community),
        "server_id": ServerId(server_id),
    }
    assert recorder.events == []


@pytest.mark.parametrize(
    "granted",
    [
        "server:start",
        "server:restart",
        "server:update",
        "server:delete",
        "server:command",
        "server:read",
    ],
)
def test_only_server_stop_grants_the_release_without_snapshot(granted: str) -> None:
    use_case = _FakeUseCase(result=_released(uuid.uuid4()))
    app, _ = _release_app(use_case, granted=(granted,))

    resp = _client(app).post(_url(uuid.uuid4(), uuid.uuid4(), _RELEASE))

    assert resp.status_code == 403
    assert use_case.kwargs == {}
