"""Guards for streaming response bodies that declare a ``Content-Length``.

A download endpoint that streams stored bytes declares the size it read from the
store up front, so a client can show progress and refuse an over-cap transfer
(issues #2312, #2317). The declared length is correct for every non-racing case —
the storage port contract pins ``size(ref) == len(open(ref))``, and a stored
archive is immutable per storage ref.

The one way the two can disagree is a delete underneath an open stream (issue
#2318): ``DeleteBackup`` or the retention prune removes the object while the body
is on the wire. Either the read raises, or the stream simply ends early; both
deliver fewer bytes than the header promised.

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
"""

from __future__ import annotations

from collections.abc import AsyncIterator


class ShortResponseBodyError(Exception):
    """A streamed body ended without delivering its declared ``Content-Length``."""


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
        raise ShortResponseBodyError(
            f"streamed {total} bytes, declared Content-Length {declared}"
        )
