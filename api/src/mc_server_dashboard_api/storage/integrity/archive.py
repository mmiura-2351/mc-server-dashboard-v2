"""Streaming readability probe for a stored backup archive (issue #2371).

The sibling of :mod:`.region`, one level up: where that walks a working set's
``.mca`` containers for structural corruption, this proves only that an archive's
bytes can still be *produced* — the precondition every restore depends on and
that the object backend's health check never tested.

:class:`GzipReadProbe` decompresses a gzip stream incrementally and throws the
output away as it is produced, so an arbitrarily large archive costs bounded
memory and no disk. It answers one question: does this byte stream decompress to
a well-formed end, trailer included? A truncated body never reaches the trailer;
a bit-rotted one reaches a trailer whose CRC32/ISIZE no longer describes the
payload. Both are :class:`ArchiveUnreadableError`.
"""

from __future__ import annotations

import zlib

from mc_server_dashboard_api.storage.domain.errors import ArchiveUnreadableError

# zlib's gzip-wrapper window setting: parse the gzip header and VERIFY the
# CRC32 + ISIZE trailer (the bit-rot check), rather than raw deflate.
_GZIP_WBITS = 16 + zlib.MAX_WBITS

# Cap the decompressed bytes materialized per ``decompress`` call. The output is
# discarded immediately, but a gzip member can inflate ~1000x, so an unbounded
# call would let an uploaded archive (issue #281 admits arbitrary bodies) balloon
# peak memory before we could drop it.
_OUT_CHUNK = 8 * 1024 * 1024


class GzipReadProbe:
    """Decompress a gzip byte stream chunk by chunk, discarding the output.

    Feed the compressed bytes with :meth:`feed` and call :meth:`finish` once the
    stream is exhausted. Either raises :class:`ArchiveUnreadableError` as soon as
    the bytes stop being a well-formed gzip stream.

    Concatenated members are accepted: a ``tar.gz`` written by one pass is a single
    member, but gzip permits members to be appended, and the shell tools do it — so
    a terminated member followed by more bytes starts a new member rather than
    being rejected as trailing garbage.
    """

    def __init__(self) -> None:
        self._decompressor = zlib.decompressobj(_GZIP_WBITS)

    def feed(self, chunk: bytes) -> None:
        while chunk:
            if self._decompressor.eof:
                # The previous member terminated cleanly; whatever follows must
                # itself be a gzip member.
                self._decompressor = zlib.decompressobj(_GZIP_WBITS)
            try:
                self._decompressor.decompress(chunk, _OUT_CHUNK)
            except zlib.error as exc:
                raise ArchiveUnreadableError(
                    f"archive is not a readable gzip stream: {exc}"
                ) from exc
            tail = self._decompressor.unconsumed_tail
            if tail:
                chunk = tail
            elif self._decompressor.eof:
                chunk = self._decompressor.unused_data
            else:
                chunk = b""

    def finish(self) -> None:
        if not self._decompressor.eof:
            raise ArchiveUnreadableError(
                "archive ended before its gzip stream reached a trailer"
            )
