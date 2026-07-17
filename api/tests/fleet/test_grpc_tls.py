"""Server-side TLS for the control-plane gRPC listener (NFR-SEC-1).

The control channel must be authenticated AND encrypted. These tests prove the
``make_grpc_server`` TLS posture end to end against a real grpc.aio server on an
ephemeral localhost port:

- with a cert/key pair, the listener serves over TLS and a TLS client that
  verifies against the cert completes a ``Session`` (register -> RegisterAck);
- with ``insecure=True``, the listener binds plaintext and logs a loud WARNING.

The cert/key are committed test-only fixtures (``tls_fixtures/`` README); Python
stdlib cannot mint X.509 certs, so a committed pair keeps the test deterministic
without a certificate-minting dependency.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import grpc
import pytest
from grpc import aio
from grpc.aio._server import Server as ConcreteAioServer

from mc_server_dashboard_api.fleet.adapters.control_plane import ControlPlaneState
from mc_server_dashboard_api.fleet.adapters.grpc_server import make_grpc_server
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from mcsd.controlplane.v1.control_plane_pb2_grpc import WorkerServiceStub
from tests.fleet.fakes import (
    FakeClock,
    FakeServerStateSink,
    RecordingRealTimeEvents,
)

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_TIMEOUT = dt.timedelta(seconds=30)
_CREDENTIAL = "shared-worker-secret"
_WORKER_ID = "22222222-2222-2222-2222-222222222222"

_FIXTURES = Path(__file__).parent / "tls_fixtures"
_CERT_FILE = _FIXTURES / "server.crt"
_KEY_FILE = _FIXTURES / "server.key"


def _register_message() -> pb.WorkerMessage:
    caps = pb.WorkerCapabilities(
        drivers=[pb.EXECUTION_DRIVER_KIND_CONTAINER],
        max_servers=4,
        resources=pb.HostResources(cpu_cores=8, memory_bytes=16_000_000_000),
    )
    return pb.WorkerMessage(
        correlation_id="reg-1",
        register=pb.Register(
            worker_id=_WORKER_ID, worker_version="1.0.0", capabilities=caps
        ),
    )


def _make_server(
    *,
    cert_file: str | None = None,
    key_file: str | None = None,
    insecure: bool = False,
) -> aio.Server:
    return make_grpc_server(
        registry=InMemoryWorkerRegistry(
            clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT
        ),
        clock=FakeClock(_T0),
        worker_credential=_CREDENTIAL,
        heartbeat_timeout=_TIMEOUT,
        transfer_deadline=dt.timedelta(seconds=660),
        control_plane=ControlPlaneState(),
        state_sink=FakeServerStateSink(),
        real_time_events=RecordingRealTimeEvents(),
        host="127.0.0.1",
        port=0,
        cert_file=cert_file,
        key_file=key_file,
        insecure=insecure,
    )


async def test_tls_server_accepts_tls_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The TLS listener serves; a client verifying against the server cert
    # completes a Session (register -> RegisterAck), proving the channel is
    # encrypted end to end (NFR-SEC-1). The server exposes no port getter, so
    # capture the OS-assigned port from add_secure_port's return value.
    bound_port: int | None = None
    real_add = ConcreteAioServer.add_secure_port

    def _spy_add_secure_port(
        self: aio.Server, address: str, credentials: grpc.ServerCredentials
    ) -> int:
        nonlocal bound_port
        port = int(real_add(self, address, credentials))
        bound_port = port
        return port

    monkeypatch.setattr(ConcreteAioServer, "add_secure_port", _spy_add_secure_port)

    server = _make_server(cert_file=str(_CERT_FILE), key_file=str(_KEY_FILE))
    await server.start()
    try:
        assert bound_port is not None and bound_port > 0
        creds = grpc.ssl_channel_credentials(root_certificates=_CERT_FILE.read_bytes())
        async with aio.secure_channel(
            f"localhost:{bound_port}",
            creds,
            options=(("grpc.ssl_target_name_override", "localhost"),),
        ) as channel:
            stub = WorkerServiceStub(channel)
            call = stub.Session(metadata=[("authorization", f"Bearer {_CREDENTIAL}")])
            await call.write(_register_message())
            response = await call.read()
            assert response.WhichOneof("payload") == "register_ack"
            await call.done_writing()
    finally:
        await server.stop(grace=None)


async def test_insecure_listener_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The plaintext opt-out is loud: binding an insecure listener emits a WARN
    # so a misconfigured production deploy is visible in the logs (NFR-SEC-1).
    with caplog.at_level(logging.WARNING):
        server = _make_server(insecure=True)
    await server.stop(grace=None)
    assert any(
        "WITHOUT TLS" in record.message and record.levelno == logging.WARNING
        for record in caplog.records
    )
