"""Shared test doubles for the core context's tests."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import socketserver
import threading
import time
from collections.abc import Awaitable, Callable, Iterator

import asyncpg
import sqlalchemy.exc
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi


class RecordingSleep:
    """Fake ``asyncio.sleep``: records each delay, returns at once for the first
    ``returns`` calls, then parks until the task is cancelled."""

    def __init__(self, *, returns: int) -> None:
        self.delays: list[float] = []
        self.parked = asyncio.Event()
        self._returns = returns

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) > self._returns:
            self.parked.set()
            await asyncio.Event().wait()


def server_error(
    error: asyncpg.PostgresError, *, invalidated: bool = False
) -> sqlalchemy.exc.DBAPIError:
    """What SQLAlchemy over asyncpg raises when the server answers with ``error``.

    Built the way the 2.1 dialect builds it (#3995): asyncpg's error becomes the
    dialect's own ``Error``, which carries its SQLSTATE and has asyncpg's error as
    its cause, and SQLAlchemy wraps that in the generic ``DBAPIError``. A real
    server refusing a connect gives this shape, the connection not invalidated
    (tests/integration/test_transient_database_error.py). A connection lost
    mid-query is the same shape with ``invalidated``.
    """

    # The dialect's ``Error.__init__`` is untyped.
    orig = AsyncAdapt_asyncpg_dbapi.Error(error.args[0], error)  # type: ignore[no-untyped-call]
    orig.__cause__ = error
    wrapped = sqlalchemy.exc.DBAPIError.instance(
        None,
        None,
        orig,
        AsyncAdapt_asyncpg_dbapi.Error,
        connection_invalidated=invalidated,
    )
    assert type(wrapped) is sqlalchemy.exc.DBAPIError
    return wrapped


def interface_error(
    error: asyncpg.InterfaceError, *, invalidated: bool = False
) -> sqlalchemy.exc.InterfaceError:
    """What SQLAlchemy over asyncpg raises when the driver itself refuses ``error``.

    The 2.1 dialect maps asyncpg's ``InterfaceError`` to its own, which has no
    SQLSTATE, and SQLAlchemy wraps that in ``sqlalchemy.exc.InterfaceError``
    (#4010). "another operation is in progress" on a live connection is a
    concurrency bug; "connection is closed" comes with ``invalidated``.
    """

    # The dialect's ``InterfaceError.__init__`` is untyped.
    orig = AsyncAdapt_asyncpg_dbapi.InterfaceError(error.args[0], error)  # type: ignore[no-untyped-call]
    orig.__cause__ = error
    wrapped = sqlalchemy.exc.DBAPIError.instance(
        None,
        None,
        orig,
        AsyncAdapt_asyncpg_dbapi.Error,
        connection_invalidated=invalidated,
    )
    assert type(wrapped) is sqlalchemy.exc.InterfaceError
    return wrapped


# How long a stalled-server test waits before it gives up (#4042): far below the
# driver's own 60 s connect timeout, so a bound that is not applied fails the test
# in seconds rather than hanging it for a minute.
_STALL_GUARD_SECONDS = 3.0


async def first_failure(
    query: Callable[[], Awaitable[object]],
) -> tuple[Exception, float]:
    """What ``query`` raised against a stalled server, and how long it took.

    The guard's own expiry is not returned: it escapes as the ``TimeoutError`` of
    ``asyncio.timeout``, which the driver's bound also raises, so a test cannot
    mistake the guard for the bound it checks.
    """

    started = time.monotonic()
    async with asyncio.timeout(_STALL_GUARD_SECONDS):
        try:
            await query()
        except Exception as exc:
            return exc, time.monotonic() - started
    raise AssertionError("the stalled server answered")


@contextlib.contextmanager
def black_hole_database() -> Iterator[str]:
    """The URL of a server that accepts a connection and never answers it (#4042).

    The socket listens but never accepts: the kernel completes the TCP handshake
    from the backlog, and the driver then waits for an answer to its startup that
    never comes, as it does when the database host stops responding.
    """

    with socket.create_server(("127.0.0.1", 0)) as listener:
        yield f"postgresql+asyncpg://u:p@127.0.0.1:{listener.getsockname()[1]}/none"


_SSL_REQUEST = 80877103
_CANCEL_REQUEST = 80877102


def _backend_message(kind: bytes, body: bytes) -> bytes:
    return kind + (len(body) + 4).to_bytes(4, "big") + body


# AuthenticationOk, the parameters asyncpg reads at connect, a cancel key, then
# ReadyForQuery: a startup PostgreSQL completes without asking for a password.
_STARTUP_ANSWER = (
    _backend_message(b"R", (0).to_bytes(4, "big"))
    + b"".join(
        _backend_message(b"S", name + b"\0" + value + b"\0")
        for name, value in (
            (b"server_version", b"18.0"),
            (b"server_encoding", b"UTF8"),
            (b"client_encoding", b"UTF8"),
            (b"DateStyle", b"ISO, MDY"),
            (b"integer_datetimes", b"on"),
            (b"standard_conforming_strings", b"on"),
            (b"TimeZone", b"UTC"),
        )
    )
    + _backend_message(b"K", (1).to_bytes(4, "big") + (2).to_bytes(4, "big"))
    + _backend_message(b"Z", b"I")
)


class _SilentHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock: socket.socket = self.request
        with sock.makefile("rb") as stream:
            while len(head := stream.read(4)) == 4:
                body = stream.read(int.from_bytes(head, "big") - 4)
                code = int.from_bytes(body[:4], "big")
                if code == _SSL_REQUEST:
                    sock.sendall(b"N")
                elif code == _CANCEL_REQUEST:
                    return
                else:
                    sock.sendall(_STARTUP_ANSWER)
                    # Read every query, answer none, until the client hangs up.
                    while stream.read1(65536):
                        pass
                    return


@contextlib.contextmanager
def silent_database() -> Iterator[str]:
    """The URL of a server that completes the startup, then answers no query (#4042).

    A connection that went silent, as one to a frozen server does: the startup
    succeeds, and every query after it waits for an answer that never comes. A
    cancel request is read and ignored. The server runs on threads, so it serves
    any event loop, a ``TestClient``'s included.
    """

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SilentHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"postgresql+asyncpg://u:p@127.0.0.1:{server.server_address[1]}/none"
    finally:
        server.shutdown()
        server.server_close()
