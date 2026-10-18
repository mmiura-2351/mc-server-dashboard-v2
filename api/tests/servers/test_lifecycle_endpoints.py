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
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    get_current_user,
    get_membership_visibility,
    get_permission_checker,
    get_restart_server,
    get_send_server_command,
    get_start_server,
    get_stop_server,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    SendServerCommand,
    StartServer,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    MAX_CONSOLE_LINE_BYTES,
    CommandOutcome,
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
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.client_utils import enter_client
from tests.servers.contract_table import worker_status
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
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
