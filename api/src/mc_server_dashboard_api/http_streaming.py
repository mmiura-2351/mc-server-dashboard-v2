"""Guards for streaming response bodies that declare a ``Content-Length``.

A download endpoint that streams stored bytes declares the size it read from the
store up front, so a client can show progress and refuse an over-cap transfer
(issues #2312, #2317). The declared length is correct for every non-racing case —
the storage port contract pins ``size(ref) == len(open(ref))``, and a stored
archive is immutable per storage ref.

The one way the two can disagree is a delete underneath an open stream: a
``DeleteBackup`` or the retention prune removes the archive (issue #2318), or a
delete removes a resource pack blob (issue #2337), while the body is on the wire.
Either the read raises, or the stream simply ends early; both deliver fewer bytes
than the header promised.

The client-observable outcome is already correct in both cases, and this module
does not change it: an exception propagates out of the ASGI app and tears the
connection down, and a body that ends below its declared ``Content-Length`` is
rejected by the HTTP layer itself (measured on uvicorn + h11: ``curl`` reports
``transfer closed with N bytes remaining``, exit 18, with and without this
guard).

What :func:`counted` adds is an *attributable* server-side failure in place of a
generic protocol error raised by whichever HTTP implementation happens to be
underneath: it tallies the bytes that pass through and fails at exhaustion when
the total falls below the declared length, naming both numbers. That also makes
the invariant hold on its own terms rather than depending on the ASGI server and
protocol version to notice.

:func:`started` guards the same invariant from the other end, before the header
is written (issues #2415, #2455). A store's read stream locates what it reads on
its FIRST iteration — the open lives inside the generator so a stream that is
never consumed holds no descriptor (issues #2341, #2390) — and Starlette sends
``http.response.start`` before it touches the body iterator. A delete landing
between the size probe and the body therefore surfaced as a ``200`` carrying a
``Content-Length`` the body could never deliver. Beginning the stream while the
status is still choosable turns that into the miss it is. Both downloads named
above now do so, so neither delete case reaches the wire as a short body any
more: it is a clean ``404`` instead. It also takes the backup download's
filesystem body out of the delete case entirely — past that first read the bytes
come from an open descriptor, which an unlink cannot shorten.

The object backend is not covered by that descriptor argument, and is what
:func:`counted` still earns its place for: its length comes from a ``HEAD`` and
its bytes from a separate ``GET``, so a store serving fewer bytes than it
reported ends the body cleanly short, with no exception to notice it by. The
resource pack blob store is object storage only, so on those two routes that is
the whole of what remains to guard.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import anyio
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

_logger = logging.getLogger(__name__)


class ShortResponseBodyError(Exception):
    """A streamed body ended without delivering its declared ``Content-Length``."""


async def started(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Begin ``source`` now, returning an equivalent stream over the same bytes.

    Pulls the first chunk, so everything the source does to locate what it reads
    happens here — while the caller can still choose a status — rather than after
    the response has started. The returned stream replays that chunk and then
    yields the rest, so the body is unchanged and the declared length still
    matches it.

    Nothing is resolved twice: this is the source's own single resolution, moved
    ahead of the headers. What it buys beyond the status is that the resolution
    now outlives them — a filesystem source is holding an open descriptor a later
    unlink cannot shorten, and an object source has its ``GET`` in flight — so the
    length declared from the probe and the body served after it can no longer be
    separated by a delete.

    The started source is handed straight to the returned generator, which closes
    it exactly as before. Nothing is held by a stream that was never begun: the
    only stream that now holds a descriptor is one that has, by definition,
    started — and asyncio finalizes an abandoned started async generator, which is
    not true of one that never ran.
    """

    try:
        first: bytes | None = await anext(source)
    except StopAsyncIteration:
        # An empty source is already exhausted and has closed itself; the stream
        # below then yields nothing, which is the body a zero-length declaration
        # asks for.
        first = None
    return _Replaying(first, source)


class _Replaying(AsyncIterator[bytes]):
    """The already-pulled ``first`` chunk, then the remainder of the source.

    A class rather than an async generator because of what closing it has to do
    (issue #3234). The source is ALREADY begun when this is built, so it holds
    what it opened from this moment on — and closing a generator that has never
    been iterated does not enter its body, so a ``finally`` in one would be
    skipped in exactly the case that matters: a response that fails or is
    disconnected before its first body chunk. :meth:`aclose` here closes the
    source whether or not a chunk was ever taken.
    """

    def __init__(self, first: bytes | None, rest: AsyncIterator[bytes]) -> None:
        self._first = first
        self._rest = rest

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if self._first is not None:
            first, self._first = self._first, None
            return first
        return await anext(self._rest)

    async def aclose(self) -> None:
        await aclose_stream(self._rest)


async def aclose_stream(stream: object) -> None:
    """Close ``stream`` now if it can be closed; a no-op for ``None``.

    For a begun stream that nothing is going to consume. A begun store stream
    holds what it opened — a descriptor, a client, a snapshot's reader lease —
    and releases it only when it is exhausted or closed.

    Shielded: this runs on the way out of a response, which may be a response
    being cancelled, and a cancel scope that is already cancelled re-cancels
    every further ``await`` inside it — the close would be abandoned halfway.
    """

    aclose = getattr(stream, "aclose", None)
    if aclose is not None:
        with anyio.CancelScope(shield=True):
            await aclose()


class ClosingStreamingResponse(StreamingResponse):
    """A :class:`StreamingResponse` that closes its body stream when it is done.

    Starlette iterates the body and, when the client goes away mid-transfer,
    simply stops: the generator is left suspended at its ``yield``, and whatever
    it holds stays held until the interpreter finalizes it. Closing the iterator
    on every way out of the response — completion, disconnect, a failure in the
    body, a failure before the first chunk — releases it at once instead (issue
    #3234).

    This close covers a body that was left SUSPENDED. A disconnect that lands
    while the body is mid-read is a different case: the cancellation is thrown
    into the read, the generators unwind right there, and nothing is left for
    this close to do. What they release on that path is made cancellation-safe
    where it is released (the seams' and the store's shielded cleanup), not here.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await aclose_stream(self.body_iterator)


async def counted(source: AsyncIterator[bytes], declared: int) -> AsyncIterator[bytes]:
    """Yield ``source`` unchanged, failing if it ends below ``declared`` bytes.

    The failure can only be raised after the last chunk, since the headers are
    long since on the wire by then; it fails the response rather than reporting
    anything to the client.

    Only the short direction is guarded: a body that runs *past* its declared
    length is already a protocol error the ASGI server rejects, and nothing here
    could stop it after the excess chunk has been yielded.
    """

    total = 0
    async for chunk in source:
        total += len(chunk)
        yield chunk
    if total < declared:
        # Starlette's BaseHTTPMiddleware captures this exception into app_exc and
        # then sends the terminal empty-body message, on which h11 raises its own
        # generic protocol error before ``raise app_exc`` is reached — so the
        # exception is discarded and never reaches a log handler. Emit the
        # attributable diagnosis here, at the point of failure, so both byte
        # counts survive regardless of that propagation (issue #2385).
        _logger.error(
            "streaming response body short: streamed %d bytes, "
            "declared Content-Length %d",
            total,
            declared,
        )
        raise ShortResponseBodyError(
            f"streamed {total} bytes, declared Content-Length {declared}"
        )
