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

The paths that extract an archive with ``tarfile`` instead — restore on both
backends, and the fs health check — reach the same verdict through
:func:`archive_read_errors_as_unreadable` (issue #3230).
"""

from __future__ import annotations

import contextlib
import gzip
import tarfile
import zlib
from collections.abc import Iterator

from mc_server_dashboard_api.storage.domain.errors import ArchiveUnreadableError

# zlib's gzip-wrapper window setting: parse the gzip header and VERIFY the
# CRC32 + ISIZE trailer (the bit-rot check), rather than raw deflate.
_GZIP_WBITS = 16 + zlib.MAX_WBITS

# The two-byte gzip member signature, used to decide whether bytes following a
# terminated member are another member or trailing padding.
#
# The authority for that decision is ``tarfile``, which is what restore actually
# reads with — NOT CPython's ``gzip`` module, which is stricter than this probe:
# ``gzip.decompress(archive + b"\x00" * 1024)`` succeeds but
# ``gzip.decompress(archive + b"not-a-member")`` raises ``BadGzipFile``, so gzip
# tolerates only zero padding. ``tarfile.open(mode="r:gz")`` accepts both, because
# it stops at the tar end-of-archive marker and never reads that far, and matching
# restore is what keeps the probe from quarantining a restorable backup.
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


# What ``tarfile`` raises when an archive's own bytes cannot be read back
# (issue #3230): a stream that ends before its gzip end-of-stream marker
# (``EOFError`` from ``r:gz``, ``ReadError("unexpected end of data")`` from
# ``r|gz``), damaged deflate data (``zlib.error``), bytes after a gzip member that
# are not another member (``BadGzipFile``), a compression method that is not
# deflate (``CompressionError``), or bytes that do not frame as a gzip tar at all
# (``ReadError``). Deliberately NOT ``tarfile.TarError`` as a whole: its
# ``FilterError`` branch refuses an unsafe member of a perfectly readable archive.
# A plain ``OSError`` (an I/O fault reading the bytes) is not here either: it says
# nothing about the archive's bytes, and each backend keeps its own I/O-fault
# policy for it.
_ARCHIVE_READ_ERRORS = (
    EOFError,
    zlib.error,
    gzip.BadGzipFile,
    tarfile.ReadError,
    tarfile.CompressionError,
)


@contextlib.contextmanager
def archive_read_errors_as_unreadable() -> Iterator[None]:
    """Report a failure to read an archive's bytes as :class:`ArchiveUnreadableError`.

    Wraps the ``tarfile`` read of a backup archive in restore and in the fs health
    check, so a truncated or bit-rotted archive reaches the caller as the modelled
    storage verdict rather than as a raw ``EOFError`` / ``tarfile`` error.
    """

    try:
        yield
    except _ARCHIVE_READ_ERRORS as exc:
        cause = exc.__cause__
        if isinstance(cause, OSError) and not isinstance(cause, gzip.BadGzipFile):
            # ``tarfile.open(mode="r:gz")`` reports ANY ``OSError`` raised while it
            # reads the first header as ``ReadError("not a gzip file")``. An I/O
            # fault there is no verdict about the bytes: surface it as the I/O
            # fault it is, for the backend's own policy to classify.
            raise cause from None
        raise ArchiveUnreadableError(f"archive could not be read back: {exc}") from exc
