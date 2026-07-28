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

Its leniency is calibrated against the restore path rather than against the gzip
spec — see :class:`GzipReadProbe` — because condemning an archive restore would
accept is as harmful as passing one it would not.
"""

from __future__ import annotations

import zlib

from mc_server_dashboard_api.storage.domain.errors import ArchiveUnreadableError

# zlib's gzip-wrapper window setting: parse the gzip header and VERIFY the
# CRC32 + ISIZE trailer (the bit-rot check), rather than raw deflate.
_GZIP_WBITS = 16 + zlib.MAX_WBITS

# The two-byte gzip member signature. Used exactly the way CPython's ``gzip`` module
# uses it: to decide whether bytes following a terminated member are another member
# or trailing padding.
_GZIP_MAGIC = b"\x1f\x8b"

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

    **Trailing bytes are calibrated against what restore accepts.** A probe that
    condemns an archive the restore path would happily read is the same class of
    defect as the false "healthy" this exists to fix — it quarantines a good backup
    and emits a spurious audit entry. Restore opens the archive with
    ``tarfile.open(mode="r:gz")``, which stops at the tar end-of-archive marker
    *inside* the first member and never looks further. So once a member has
    terminated cleanly, what follows cannot make the archive unreadable:

    * another gzip member (concatenation is legal and the shell tools do it) is
      walked too, because bytes that announce themselves with the gzip magic must
      actually be a member — that is where the leniency stops;
    * anything else is trailing padding and ends the walk.

    **The trailer itself is deliberately STRICTER than restore.** Because tarfile
    stops early it never verifies the CRC32/ISIZE at all, so it will happily open an
    archive whose trailer has rotted (verified: flipping a CRC byte leaves
    ``tarfile`` reading the member back byte-identical). That is precisely the silent
    bit-rot this probe exists to surface — a stored object whose bytes have changed
    is damaged whether or not tarfile happens to stop before noticing, and the next
    flipped bit lands in the payload, which restore *does* reject. So a mismatched
    trailer is unreadable here even though restore would not complain today.

    What is caught, then: a stream that never reaches a trailer (truncation) and a
    trailer that no longer describes its payload (bit-rot).
    """

    def __init__(self) -> None:
        self._decompressor = zlib.decompressobj(_GZIP_WBITS)
        # Set once a terminated member is followed by non-member bytes: the walk is
        # done and later chunks are ignored (they are padding, see the class docs).
        self._padding = False
        # Bytes held back at a member boundary because there were not yet enough of
        # them to compare against the gzip magic.
        self._undecided = b""

    def feed(self, chunk: bytes) -> None:
        if self._padding:
            return
        if self._undecided:
            chunk = self._undecided + chunk
            self._undecided = b""
        while chunk:
            if self._decompressor.eof:
                if len(chunk) < len(_GZIP_MAGIC):
                    self._undecided = chunk  # decide once more bytes arrive
                    return
                if not chunk.startswith(_GZIP_MAGIC):
                    self._padding = True
                    return
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
        # ``_padding`` and ``_undecided`` are only ever set past a terminated member,
        # so either means the archive ended on a complete gzip stream plus trailing
        # bytes — which restore accepts.
        if self._padding or self._undecided:
            return
        if not self._decompressor.eof:
            raise ArchiveUnreadableError(
                "archive ended before its gzip stream reached a trailer"
            )
