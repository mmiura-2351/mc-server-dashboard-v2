"""The control-plane gRPC server's lifespan start/stop path (app.py).

With ``control.enabled`` and a Worker credential, entering the FastAPI lifespan
starts the grpc.aio server on the same event loop and exposes it on
``app.state``; exiting the lifespan stops it. Driven against a real server bound
to an ephemeral port (``grpc_port=0``) so the test is deterministic and fast and
needs no fixed port — the bound port is read back and dialed to prove the server
actually serves, then proven closed after lifespan exit.
"""

from __future__ import annotations

import grpc
import pytest
from grpc import aio
from grpc.aio._server import Server as ConcreteAioServer

from mc_server_dashboard_api.app import create_app
from mcsd.controlplane.v1.control_plane_pb2_grpc import WorkerServiceStub

_CREDENTIAL = "shared-worker-secret"

# Connection-level statuses that mean "the transport went away", not "the
# server made a per-call decision". Under full-suite load a channel can emit a
# GOAWAY (surfacing as INTERNAL/UNAVAILABLE) before the per-call abort's
# trailing status is read, masking the real rejection code (issue #187).
_GOAWAY_CODES = frozenset({grpc.StatusCode.INTERNAL, grpc.StatusCode.UNAVAILABLE})


async def _rejection_code(
    port: int, metadata: list[tuple[str, str]]
) -> grpc.StatusCode:
    """Return the terminal status of a rejected ``Session``, GOAWAY-tolerant.

    The server's per-call abort is authoritative, but a connection-level GOAWAY
    can race it and surface as INTERNAL/UNAVAILABLE instead (issue #187). That
    is a transport artifact, not the rejection: retry once on a *fresh* channel,
    which cannot inherit the prior connection's teardown. One retry is enough —
    two independent GOAWAYs back-to-back would itself be a real bug worth a
    failure.
    """

    async def _drive() -> grpc.StatusCode:
        async with aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            call = WorkerServiceStub(channel).Session(metadata=metadata)
            with pytest.raises(aio.AioRpcError) as exc:
                await call.read()
            return exc.value.code()

    code = await _drive()
    if code in _GOAWAY_CODES:
        code = await _drive()
    return code


async def test_lifespan_starts_and_stops_grpc_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", _CREDENTIAL)
    # Bind an ephemeral port; the real factory's add_insecure_port returns the
    # OS-assigned port, which we capture to dial the running server.
    monkeypatch.setenv("MCD_API_SERVER__GRPC_PORT", "0")
    monkeypatch.setenv("MCD_API_SERVER__HOST", "127.0.0.1")

    # The server has no port getter; capture the OS-assigned port from the real
    # add_insecure_port return value so we can dial the running server.
    bound_port: int | None = None
    real_add = ConcreteAioServer.add_insecure_port

    def _spy_add_insecure_port(self: aio.Server, address: str) -> int:
        nonlocal bound_port
        port = int(real_add(self, address))
        bound_port = port
        return port

    monkeypatch.setattr(ConcreteAioServer, "add_insecure_port", _spy_add_insecure_port)

    app = create_app()
    async with app.router.lifespan_context(app):
        # The server object is published on app.state once started.
        assert isinstance(app.state.grpc_server, aio.Server)
        assert bound_port is not None and bound_port > 0

        # It actually serves: dial the ephemeral port and exercise the auth gate.
        code = await _rejection_code(bound_port, [("authorization", "Bearer wrong")])
        assert code == grpc.StatusCode.UNAUTHENTICATED

    # After lifespan exit the server is stopped: the port no longer accepts.
    async with aio.insecure_channel(f"127.0.0.1:{bound_port}") as channel:
        stub = WorkerServiceStub(channel)
        call = stub.Session(metadata=[("authorization", f"Bearer {_CREDENTIAL}")])
        with pytest.raises(aio.AioRpcError) as exc:
            await call.read()
        assert exc.value.code() == grpc.StatusCode.UNAVAILABLE
