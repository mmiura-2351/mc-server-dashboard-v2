"""Guards for streaming response bodies that declare a ``Content-Length``.

A download endpoint that streams stored bytes declares the size it read from the
store up front, so a client can show progress and refuse an over-cap transfer
(issues #2312, #2317). The declared length is correct for every non-racing case —
the storage port contract pins ``size(ref) == len(open(ref))``, and a stored
archive is immutable per storage ref.

The one way the two can disagree is a delete underneath an open stream (issue
#2318): ``DeleteBackup`` or the retention prune removes the object while the body
is on the wire. If the read then raises, the exception propagates out of the ASGI
app and the connection is torn down — the client sees a failed transfer, which is
the honest outcome. If instead the stream just ends, the response would complete
as a 200 whose body is shorter than the header promised: an incomplete archive
presented as a success.

:func:`counted` closes that gap by tallying the bytes that pass through and
failing at exhaustion when the total does not equal the declared length, so the
short body aborts the connection instead of completing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator


class ShortResponseBodyError(Exception):
    """A streamed body ended without delivering its declared ``Content-Length``."""


async def counted(source: AsyncIterator[bytes], declared: int) -> AsyncIterator[bytes]:
    """Yield ``source`` unchanged, failing if it ends below ``declared`` bytes.

    Raising after the last chunk aborts the response mid-body rather than letting
    it complete short, because the headers are long since on the wire by then.

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
