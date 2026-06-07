"""Guard that uvicorn has a usable WebSocket protocol implementation (#507).

Bare ``uvicorn`` ships no WS protocol library, so its auto-selector resolves
``AutoWebSocketsProtocol`` to ``None`` and silently downgrades every WS upgrade
to a plain HTTP GET — the live ``/events`` sockets never complete the handshake.
The TestClient-based WS tests do not catch this: Starlette's TestClient speaks
WS in-process without uvicorn's protocol layer. This test asserts the real
uvicorn runtime the e2e harness boots can serve WebSockets, and fails if the
``websockets`` runtime dependency is ever dropped.
"""

from uvicorn.protocols.websockets.auto import AutoWebSocketsProtocol


def test_uvicorn_websocket_protocol_is_available() -> None:
    """uvicorn resolves a concrete WS protocol (not ``None``)."""

    assert AutoWebSocketsProtocol is not None
