"""The body-less response a ``HEAD`` probe of a download route gets (issue #2383).

A resumable-download client asks ``HEAD`` first, to learn the size and whether
``Accept-Ranges: bytes`` is offered, before deciding to issue a ranged ``GET``.
RFC 9110 Section 9.3.2 asks that response to carry the header fields the ``GET``
would have sent, and no content.

Starlette's plain :class:`~starlette.responses.Response` cannot express that: it
renders an empty body and then declares ``Content-Length: 0`` for it whenever the
caller did not name a length itself. On the two zip downloads — built
incrementally, so their ``GET`` declares no length at all — that would tell the
probe the representation is empty, which is exactly the answer it must not get. A
streaming response declares no length of its own, so the headers handed in are
the headers sent, and an empty stream is the ``HEAD`` body.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse


async def _no_body() -> AsyncIterator[bytes]:
    """No bytes at all — the body of a ``HEAD`` response."""

    return
    yield b""  # unreachable; the yield is what makes this an async generator


def head_response(*, media_type: str, headers: dict[str, str]) -> StreamingResponse:
    """A ``200`` carrying ``headers`` verbatim and no content."""

    return StreamingResponse(_no_body(), media_type=media_type, headers=headers)
