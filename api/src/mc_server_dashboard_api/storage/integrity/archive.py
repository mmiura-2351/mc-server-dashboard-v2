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

What it accepts after a complete gzip member is calibrated against restore — see
:class:`GzipReadProbe` — because condemning a restorable backup is as harmful as
passing one restore would refuse. It is not the health verdict on its own: the
object backend's health check walks the archive as restore does once the probe
has read it.

The paths that extract an archive with ``tarfile`` instead — restore on both
backends, and both backends' health checks — reach their verdict through
:func:`archive_read_errors_as_unreadable` (issue #3230). Every member walk of
both backends, snapshot ingest included, reads the headers through
:class:`HeaderCheckedTarInfo` (issue #3790) and must end at the end-of-archive
marker, with only zeros after it (``archive_member.EndCheckedReader``, issue
#3798); restore and health decompress with our own ``gzip.GzipFile`` and read it
to its end, which checks every gzip member's CRC32 and ISIZE. The restore and
health paths of both backends judge each member by
:func:`refuse_unrestorable_member` (issue #3550), and both backends' snapshot
ingest takes only the canonical names ``is_canonical_name`` admits (issue
#3574). Every one of those paths first reads a member through
``without_tar_root_prefix``. The member rule itself lives in
:mod:`mc_server_dashboard_api.archive_member`, so the servers upload gate judges
an uploaded archive by the same code (issue #3579).
"""

from __future__ import annotations

import contextlib
import gzip
import tarfile
import zlib
from collections.abc import Iterator
from typing import Self, cast

from mc_server_dashboard_api.archive_member import unrestorable_reason
from mc_server_dashboard_api.storage.domain.errors import ArchiveUnreadableError

# zlib's gzip-wrapper window setting: parse the gzip header and VERIFY the
# CRC32 + ISIZE trailer (the bit-rot check), rather than raw deflate.
_GZIP_WBITS = 16 + zlib.MAX_WBITS

# The two-byte gzip member signature, used to decide whether bytes following a
# terminated member are another member or trailing bytes the probe leaves to the
# member walk that follows it.
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

    **Past a terminated member it refuses nothing restore accepts.** A probe that
    condemns an archive the restore path would happily read is the same class of
    defect as the false "healthy" this exists to fix — it quarantines a good
    backup and emits a spurious audit entry. Restore reads the archive with
    ``gzip.GzipFile`` to its end (issue #3798), so it checks every member's
    trailer, as the probe does, and once a member has terminated cleanly:

    * another gzip member (concatenation is legal and the shell tools do it) is
      walked too, because bytes that announce themselves with the gzip magic must
      actually be a member, as they must for ``GzipFile``;
    * anything else ends the probe's walk. ``GzipFile`` takes zero bytes there
      for padding and refuses any other byte, but the probe leaves that verdict
      to the member walk the health check runs after it, which reads the archive
      as restore does (issue #3236).

    What the probe catches, then: a stream that never reaches a trailer
    (truncation) and a trailer that no longer describes its payload (bit-rot),
    as soon as the store delivers the bytes, before the short-body re-read.
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
        # bytes, which the member walk after the probe judges.
        if self._padding or self._undecided:
            return
        if not self._decompressor.eof:
            raise ArchiveUnreadableError(
                "archive ended before its gzip stream reached a trailer"
            )


# What reading a backup archive raises when its own bytes cannot be read back
# (issue #3230). Restore and health decompress with ``gzip.GzipFile`` and walk
# the tar with ``tarfile`` in ``r:`` through ``EndCheckedReader`` (issue #3798):
# a stream that ends before its gzip end-of-stream marker (``EOFError``), damaged
# deflate data (``zlib.error``, or ``ReadError`` where ``tarfile`` reads a
# header), bytes that are not gzip, a compression method that is not deflate, a
# CRC32 or ISIZE that does not match, or bytes after a gzip member that are
# neither another member nor zero padding (``BadGzipFile``), and a tar that does
# not frame, does not end at its end-of-archive marker or holds more than zeros
# past it (``ReadError``). Deliberately NOT ``tarfile.TarError`` as a whole: its
# ``FilterError`` branch refuses an unsafe member of a perfectly readable archive.
# A plain ``OSError`` (an I/O fault reading the bytes) is not here either: it says
# nothing about the archive's bytes, reaches the caller as itself, and each
# backend keeps its own I/O-fault policy for it. Nor is ``ValueError``, though
# ``tarfile`` raises it for a header with a bad number (issue #3790), nor
# ``IndexError``, which it raises for an old GNU sparse extension block cut short
# (issue #3802): the fs restore's wrap spans its own code too.
# :class:`HeaderCheckedTarInfo` turns both into a ``ReadError`` where they arise.
_ARCHIVE_READ_ERRORS = (
    EOFError,
    zlib.error,
    gzip.BadGzipFile,
    tarfile.ReadError,
)


class HeaderCheckedTarInfo(tarfile.TarInfo):
    """A ``TarInfo`` whose header read reports a ``ValueError`` or an
    ``IndexError`` as ``ReadError``.

    CPython's ``tarfile`` converts the PAX ``GNU.sparse.size``,
    ``GNU.sparse.realsize`` and ``GNU.sparse.map`` fields, and the sparse format
    1.0 map, with a bare ``int()``, so a non-numeric one raises ``ValueError``
    (issue #3790). Opening an archive with ``tarinfo=HeaderCheckedTarInfo``
    reports it as the ``ReadError`` that the restore walk and the snapshot edge
    already refuse. Both hooks are public: the library reference documents the
    ``tarinfo`` argument as replacing the default ``TarInfo`` class, and
    ``TarInfo.fromtarfile`` as the classmethod that reads the next member.

    ``tarfile`` also reads an old GNU sparse header's extension blocks without
    checking their length, so a tar that ends inside one raises ``IndexError``
    (issue #3802), and the read reports that as the same ``ReadError``. Only the
    header read is covered, so neither error from the caller's own code is
    mistaken for one. Not ``InvalidHeaderError``: ``TarFile.next`` takes that,
    past the first header, for the end of the archive.

    A header whose numbers read can still not describe a file (issue #3802): a
    negative size, or a GNU sparse map with a negative offset or length, an entry
    past the member's real size, or entries that overlap or are out of order.
    ``tarfile`` takes those numbers as they are, so the read refuses them as the
    same ``ReadError``, by :func:`_refuse_invalid_layout`.
    """

    @classmethod
    def fromtarfile(cls, archive: tarfile.TarFile) -> Self:
        try:
            member = super().fromtarfile(archive)
        except (ValueError, IndexError) as exc:
            raise tarfile.ReadError(f"unreadable member header: {exc}") from exc
        _refuse_invalid_layout(member)
        return member


def _refuse_invalid_layout(member: tarfile.TarInfo) -> None:
    """Raise ``ReadError`` for a member whose size or sparse map cannot be a file.

    The library reference documents ``TarInfo.sparse`` only as "sparse member
    information". CPython sets it to ``None``, or to a list of ``(offset,
    numbytes)`` data regions of a file of ``size`` bytes, its real size (typeshed's
    ``bytes | None`` does not match). The regions must lie inside the file, in
    order and apart. GNU tar ends a map with a zero-length region at the real size,
    and the old ``S`` header pads it with ``(0, 0)`` slots, so a zero-length region
    is held to the bounds but not to the order.
    """

    if member.size < 0:
        raise tarfile.ReadError(
            f"unreadable member header: {member.name!r} has a negative size"
        )
    # ``TarInfo.sparse`` is documented by name only: its value's shape is relied
    # on here. Whether DEPENDENCIES.md Section 7 records that is an owner
    # decision, issue #3829.
    sparse = cast("list[tuple[int, int]] | None", member.sparse)
    if sparse is None:
        return
    data_end = 0
    for offset, numbytes in sparse:
        if offset < 0 or numbytes < 0 or offset + numbytes > member.size:
            raise tarfile.ReadError(
                f"unreadable member header: {member.name!r} has a sparse region "
                f"({offset}, {numbytes}) outside its {member.size} bytes"
            )
        if numbytes:
            if offset < data_end:
                raise tarfile.ReadError(
                    f"unreadable member header: {member.name!r} has sparse "
                    "regions that overlap or are out of order"
                )
            data_end = offset + numbytes


@contextlib.contextmanager
def archive_read_errors_as_unreadable() -> Iterator[None]:
    """Report a failure to read an archive's bytes as :class:`ArchiveUnreadableError`.

    Wraps the read of a backup archive in restore and in both backends' health
    checks, so a truncated or bit-rotted archive reaches the caller as the
    modelled storage verdict rather than as a raw ``EOFError`` / ``tarfile`` /
    ``gzip`` error.
    """

    try:
        yield
    except _ARCHIVE_READ_ERRORS as exc:
        raise ArchiveUnreadableError(f"archive could not be read back: {exc}") from exc


def refuse_unrestorable_member(member: tarfile.TarInfo) -> None:
    """Refuse a backup archive member that no restore may take (issue #3550).

    The one member rule of both backends, for restore and the health probe alike:
    a member must be a regular file or a directory, at a relative, canonical name
    with no ``..`` component. A hard link, symlink, device, FIFO or other special
    member, an absolute name, a ``..`` — even one that stays inside the tree — and
    an empty or ``.`` segment raise :class:`ArchiveUnreadableError`: the archive
    reads, but nothing can be restored from it, override or not. The rule is
    :func:`~mc_server_dashboard_api.archive_member.unrestorable_reason`, shared
    with the servers upload gate (issue #3579). ``member`` is the one
    ``without_tar_root_prefix`` returned, so a leading ``./`` is already gone.
    The platform's own producers emit none of these: the Worker's pack and the fs
    writer name members after real directory entries, and the object backend's
    archive writer leaves out the legacy keys a store can still hold — the
    ``//.dir`` key of issue #1944, and a non-canonical key ingest accepted before
    issue #3574 (issue #3574).
    """

    reason = unrestorable_reason(member)
    if reason is not None:
        raise ArchiveUnreadableError(
            f"archive holds a member that cannot be restored: {member.name!r} {reason}"
        )
