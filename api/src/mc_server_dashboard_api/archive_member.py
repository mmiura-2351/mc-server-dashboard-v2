"""The one backup archive member rule (issues #3550, #3574, #3579).

Storage judges a backup member by this rule in restore and the health probe,
and its snapshot ingest takes only the names :func:`is_canonical_name` admits.
The servers upload gate judges each member by the same rule before an archive
is stored, so an upload is refused at once instead of failing later at restore
or sweep. It is a neutral root-level module, standard library only, so both
contexts' inward layers may import it. That is the ``rfc3339`` precedent in the
root-level import contract. Every caller first reads a member through
:func:`without_tar_root_prefix`.

How a member walk ends lives here too (issue #3778): :class:`EndCheckedReader`
checks that a walk stopped at the end-of-archive marker and reads the stream on
from there to its end. The servers upload gates read an uploaded ``tar.gz``
through it.

So does what a walk may decompress (issue #3810): :class:`CappedReader`, beneath
:class:`EndCheckedReader`, counts every byte the walk takes from the stream
toward a cap, and :class:`EndCheckedReader` holds the walk to
:class:`HeaderLimits`: the upload gates' (issue #3810) or the storage walks'
(issue #3859).

So does how gzip is read (issue #3835): :class:`GzipReader`, beneath both,
decompresses a ``tar.gz`` through :class:`GzipDecoder`, which the object
backend's health probe reads gzip through too, so every read of a ``tar.gz``
takes and refuses the same gzip streams (issue #3847).
"""

from __future__ import annotations

import gzip
import io
import sys
import tarfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

# The tar format's block size. The end-of-archive marker is a block of zeros.
_END_BLOCK = bytes(512)

# What a walk reads after its last member when it stops at the marker: the block,
# or first the one byte tarfile reads where it seeks to the next header.
_END_READS = (len(_END_BLOCK), len(_END_BLOCK) + 1)

# The header limits, for a walk held to them (issue #3810). How much the walk may
# read between handing out one member and the next: that member's header blocks
# and extended headers (PAX records, GNU long names and links, sparse maps).
# tarfile holds extended-header data whole, so this bounds what it holds of it at
# a time. Real extended headers are long names, xattrs and ACLs: kilobytes.
_BETWEEN_MEMBERS_BYTES = 1024 * 1024

# What the members a walk hands out may hold. tarfile gives most members a copy
# of the global PAX records dict, and the TarFile keeps the global records too,
# so a member may hold at most this many PAX records, global and its own...
_PAX_RECORDS_PER_MEMBER = 32
# ...and the members' names, link names, PAX keys and PAX values together at most
# this many characters, plus what HeaderLimits allows a member. The count
# over-states some of it, which errs safe: a global record is one string the
# members share but counts once a member, and a name or link name read from a PAX
# record counts twice. Real archives hold a dozen records and a few hundred
# characters a member. A TarFile opened with ``stream=True``, as every walk's is
# (issue #3931), keeps no member it has handed out, so the count bounds what the
# walk reads of them in all; opened without, tarfile keeps every member.
_HELD_CHARACTERS = 8 * 1024 * 1024

# How much of the stream past the end-of-archive marker to read at a time, so a
# long run of zeros cannot be materialized whole. Read through a CappedReader,
# each chunk counts toward the cap before the next one is read.
_TAIL_CHUNK_BYTES = 1024 * 1024

# How much a CappedReader reads at a time to skip forward. Each chunk counts
# toward the cap before the next one is read. A GzipReader skips the same way.
_SKIP_CHUNK_BYTES = 1024 * 1024

# zlib's gzip mode: it reads each member's header, refusing a header CRC that does
# not match and a reserved flag bit, and checks each member's CRC32 and ISIZE.
_GZIP_WBITS = 16 + zlib.MAX_WBITS

# The most members a gzip stream may hold (issue #3835). Each one costs a
# decompressor whatever it holds, and an empty one decompresses to nothing, so no
# decompressed-size cap sees it. The producers we know write one.
_MAX_GZIP_MEMBERS = 10_000

# How much of the compressed bytes zlib is handed at a time. zlib copies what it
# leaves of them, at a member's end and when a read is full, so this bounds that
# copy whatever the bytes fed at once.
_GZIP_PIECE_BYTES = 16 * 1024

# Zero padding a whole piece long, to recognize one in a single comparison.
_ZERO_PIECE = bytes(_GZIP_PIECE_BYTES)

# How much of its source a GzipReader reads at a time.
_GZIP_SOURCE_BYTES = 1024 * 1024


class HeaderLimitError(tarfile.ReadError):
    """A walk read or kept more header than its header limits allow.

    The limits hold only a walk whose :class:`EndCheckedReader` is given
    :class:`HeaderLimits`.

    More than 1 MiB of headers between two members, a member with more than 32
    PAX records, or more characters held by the members handed out than the
    limits allow (issue #3810). A ``ReadError``, so a caller that refuses
    unreadable archives refuses it too; its own class lets a caller name the
    refusal. The message is built from fixed text and the limits only.
    """


@dataclass(frozen=True)
class HeaderLimits:
    """The header limits a walk is held to (issues #3810, #3859).

    Every such walk reads at most 1 MiB of headers between two members, and each
    member it hands out holds at most 32 PAX records. The members may hold 8 Mi
    characters of names, link names and PAX records in all, plus
    ``characters_a_member`` for each member handed out: so a walk with no entry
    cap still takes what an archive of many ordinary members holds.
    """

    characters_a_member: int


# The upload gates' limits (issue #3810). Their entry cap is what keeps the
# 8 Mi characters out of a real archive's reach.
UPLOAD_HEADER_LIMITS = HeaderLimits(characters_a_member=0)

# The limits restore and the health check hold a backup's walk to (issue #3859),
# and the snapshot ingest a snapshot's (issue #3931). They have no entry cap: a
# backup the platform writes, and a snapshot the Worker packs, hold a member for
# every file of the working set, however many. Such a member holds its name
# twice, as the name and as its PAX ``path`` record, and a few short records
# (``mtime``, and ``size``, ``uid``, ``gid``, ``uname`` or ``gname`` when out of
# the ustar fields' range): under 9 Ki characters for a name of 4095 bytes,
# Linux's PATH_MAX, which bounds every name the fs writer reads from disk and the
# Worker packs. The Worker's largest is a file of 8 GiB or more at that length,
# a PAX ``path`` and ``size`` record and nothing else: 8,209 characters, in
# 5,632 bytes of headers. So 16 Ki a member takes every backup the fs writer
# writes and every snapshot the Worker packs, and the 8 Mi on top takes every
# archive the upload gates admit. The object writer's names are the snapshot's
# object keys, so its backups are taken alike only while every key is at most
# 4095 bytes, which nothing enforces yet (issue #3973).
STORAGE_HEADER_LIMITS = HeaderLimits(characters_a_member=16 * 1024)


def without_tar_root_prefix(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Read ``member`` without the conventional tar root prefix (issue #3574).

    ``tar -C dir -cf - .`` writes the root itself as a ``.`` (``./``) directory
    entry and every other member under a leading ``./``. The root entry returns
    ``None``: it is the directory everything restores into, not a member of it.
    Any other member comes back with exactly one leading ``./`` stripped, so
    ``./world/level.dat`` is judged, by :func:`unrestorable_reason`,
    :func:`is_canonical_name` and the reserved-name decision, as
    ``world/level.dat``. A second ``./`` stays and is refused as non-canonical.
    """

    if member.isdir() and member.name.removesuffix("/") == ".":
        return None
    if member.name.startswith("./"):
        return member.replace(name=member.name[2:], deep=False)
    return member


def is_canonical_name(name: str, *, is_dir: bool = False) -> bool:
    """Whether a slash-separated name has no empty or ``.`` segment (issue #3574).

    ``a//b``, ``a/./b`` and ``./a`` name the same place as a canonical spelling, but
    a store that keeps them verbatim lists an empty or ``.`` directory whose child
    path is its parent again. One trailing ``/`` on a directory member is allowed:
    it is how tar writes one. ``..`` is judged separately, as traversal. An
    archive member is judged after :func:`without_tar_root_prefix`, which strips
    the one leading ``./`` tar writes.
    """

    if is_dir:
        name = name.removesuffix("/")
    return all(part not in ("", ".") for part in name.split("/"))


def unrestorable_reason(member: tarfile.TarInfo) -> str | None:
    """Why no restore may take ``member``, or ``None`` when one may (issue #3550).

    A member must be a regular file or a directory, at a relative, canonical name
    with no ``..`` component. A hard link, symlink, device, FIFO or other special
    member, an absolute name, a ``..`` (even one that stays inside the tree) and
    an empty or ``.`` segment (:func:`is_canonical_name`) each return the reason,
    a phrase that follows the member's name. ``member`` is the one
    :func:`without_tar_root_prefix` returned, so a leading ``./`` is already gone.
    """

    if not (member.isfile() or member.isdir()):
        return "is not a regular file or directory"
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts:
        return "is absolute or has a '..' component"
    if not is_canonical_name(member.name, is_dir=member.isdir()):
        return "has an empty or '.' segment"
    return None


class EndCheckedReader(io.RawIOBase):
    """The stream a tar is walked from, checking that the walk reached its end (#3778).

    ``tarfile`` stops reading at the end-of-archive marker, so a ``tar.gz`` read
    through it never reaches the gzip trailer (CRC32 and ISIZE). It also takes a
    header it cannot read, past the first, or a stream that stops where a header
    should start, for the end of the archive, so the member list is silently cut
    short there.

    Pass it as ``tarfile.open(fileobj=reader, mode="r:")`` over the decompressed
    stream (:class:`GzipReader` for a ``tar.gz``), walk the archive through
    :meth:`members`, then iterate :meth:`read_past_end_marker`. Every walk opens
    ``tarfile.TarFile(fileobj=reader, mode="r", stream=True)``, the same read,
    so that ``tarfile`` keeps no member it hands out (issue #3931): typeshed's
    ``tarfile.open`` does not take ``stream``.

    The walk counts as having stopped at the marker only when everything it read
    after handing out its last member is one block of 512 zeros, after at most
    the one byte ``tarfile`` reads where it seeks to the next header. The
    caller's own reads of that member's data are not counted. Any other end is a
    ``ReadError``:

    - a header ``tarfile`` gave up on: read alone it is not zeros, and one it
      gave up on after reading more (an extension block, extended-header data,
      the header after it) makes the count too large;
    - a stream that ends where a header should start, or partway through one.

    The walk is forward only (issue #3798): a seek back, or one other than from
    the start of the stream, is a ``ReadError`` (see :meth:`seek`).

    One zero block is enough, though the format writes two: nothing is lost after
    it. The check relies on ``tarfile`` reading the block it stops at with one
    512-byte ``read``. Read any other way, the count or the last read differs and
    the archive is refused rather than accepted. Only ``r:`` is supported: in
    ``r|`` ``tarfile`` reads the file in whole records, so where its walk stopped
    cannot be seen from here.

    With ``header_limits`` the walk is also held to them (:class:`HeaderLimits`):
    the upload gates pass :data:`UPLOAD_HEADER_LIMITS` (issue #3810), restore,
    the health check and the snapshot ingest :data:`STORAGE_HEADER_LIMITS`
    (issues #3859, #3931).

    - The same count bounds what the walk reads between handing out one member
      and the next to 1 MiB: the next member's header blocks and extended
      headers, which ``tarfile`` holds whole. A read then asks the stream for at
      most one byte past that. The caller's reads of member data are not held to
      it.
    - Each member ``tarfile`` hands out holds its name, its link name and its
      PAX records, and the ``TarFile`` keeps the global ones
      (``TarFile.pax_headers``). It copies them into a member's own records for
      most member types, but not into an old-GNU sparse one, so each member
      counts as holding all of them. Unless the ``TarFile`` is opened with
      ``stream=True``, as every walk's is (issue #3931), ``tarfile`` keeps every
      member it hands out. :meth:`members` bounds what the members
      hold: at most 32 PAX records in a member, global and its own, and at most
      8 Mi characters of names, link names, PAX keys and PAX values in all, plus
      the limits' allowance for each member handed out. Neither bound sees a
      sparse member's map (issue #3869).

    Going past any of them is :class:`HeaderLimitError`.
    """

    def __init__(
        self, stream: io.BufferedIOBase, *, header_limits: HeaderLimits | None = None
    ) -> None:
        super().__init__()
        self._stream = stream
        self._header_limits = header_limits
        self._last_read = b""
        self._read_since_member = 0
        # Whether the walk, rather than the caller, is reading: from the start
        # until a member is handed out, and from each resume until the next.
        self._walking = True

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        """``True`` whatever the stream: ``tarfile`` seeks in ``r:``.

        Forward only, from the start of the stream (:meth:`seek`). Not a promise
        about the stream: :class:`GzipReader` seeks forward over any source by
        decompressing on.
        """

        return True

    def read(self, size: int = -1, /) -> bytes:
        budgeted = self._header_limits is not None and self._walking
        if budgeted:
            allowed = _BETWEEN_MEMBERS_BYTES + 1 - self._read_since_member
            size = allowed if size < 0 else min(size, allowed)
        self._last_read = self._stream.read(size)
        self._read_since_member += len(self._last_read)
        if budgeted and self._read_since_member > _BETWEEN_MEMBERS_BYTES:
            raise HeaderLimitError(
                f"more than {_BETWEEN_MEMBERS_BYTES} bytes of headers before a member"
            )
        return self._last_read

    def seek(self, offset: int, whence: int = io.SEEK_SET, /) -> int:
        """Forward only: a seek back is a ``ReadError`` (issue #3798).

        ``tarfile`` seeks back only when a member's data read ran on past where
        the next header starts, such as a GNU sparse map that takes more data than
        the member stores. It seeks with ``SEEK_SET`` only, so any other ``whence``
        is a ``ReadError`` too.
        """

        if whence != io.SEEK_SET:
            raise tarfile.ReadError("a seek other than from the start of the stream")
        if offset < self._stream.tell():
            raise tarfile.ReadError("a member's data runs past the member")
        return self._stream.seek(offset)

    def tell(self) -> int:
        return self._stream.tell()

    def members(self, tar: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
        """The members of ``tar``, the archive opened over this reader.

        The count of what the walk reads starts over each time the walk resumes
        after a member, which is after the caller has read that member's data.
        With header limits, only the walk's own reads are held to the budget
        between two members, and each member is held to what members may hold
        before it is handed out.
        """

        held = 0
        allowed = _HELD_CHARACTERS
        for member in tar:
            if self._header_limits is not None:
                records = {**tar.pax_headers, **member.pax_headers}
                if len(records) > _PAX_RECORDS_PER_MEMBER:
                    raise HeaderLimitError(
                        f"a member with more than {_PAX_RECORDS_PER_MEMBER} PAX records"
                    )
                held += (
                    len(member.name)
                    + len(member.linkname)
                    + sum(len(key) + len(value) for key, value in records.items())
                )
                allowed += self._header_limits.characters_a_member
                if held > allowed:
                    raise HeaderLimitError(_held_too_much(self._header_limits))
            self._walking = False
            yield member
            self._read_since_member = 0
            self._walking = True

    def read_past_end_marker(self) -> Iterator[bytes]:
        """Read on from the end-of-archive marker to the end of the stream.

        Iterate it once :meth:`members` is exhausted. The walk must have stopped
        at the marker (see the class), and what follows must be zeros: the
        marker's second block and the producer's record padding. Anything else,
        such as a second tar after the first, is a ``ReadError``. POSIX leaves
        the rest of the last record undefined; this is stricter on purpose, since
        restore never reads what lies there. Over :class:`GzipReader`, reading to
        the end is what checks the trailer, and what refuses bytes after the gzip
        stream that are neither another gzip member nor zero padding.

        The chunks are yielded as they are read. Read through a
        :class:`CappedReader`, each counts toward the cap.
        """

        if self._last_read != _END_BLOCK or self._read_since_member not in _END_READS:
            raise tarfile.ReadError(
                "the archive does not end at its end-of-archive marker"
            )
        while chunk := self._stream.read(_TAIL_CHUNK_BYTES):
            if chunk.count(0) != len(chunk):
                raise tarfile.ReadError("data past the end-of-archive marker")
            yield chunk


def _held_too_much(limits: HeaderLimits) -> str:
    message = (
        f"more than {_HELD_CHARACTERS} characters of names, link names and PAX records"
    )
    if limits.characters_a_member:
        message += f", plus {limits.characters_a_member} a member"
    return message


class CapExceededError(Exception):
    """A :class:`CappedReader` was asked for more than its cap (issue #3810).

    Deliberately none of the errors a reader raises for a damaged archive, so a
    caller can refuse it as too large rather than as unreadable.
    """


class CappedReader(io.BufferedIOBase):
    """The decompressed stream beneath :class:`EndCheckedReader`, bounded (#3810).

    Pass it as ``EndCheckedReader(CappedReader(stream, max_bytes))``. Every byte
    the walk takes from the stream counts toward ``max_bytes``: headers,
    extended-header data (PAX records, GNU long names), member data whether read
    or skipped, the stored data of a sparse member, padding and the read past
    the end-of-archive marker. A read past the cap raises
    :class:`CapExceededError`, having taken at most one byte more than the cap
    from the stream. :class:`GzipReader` decompresses no more than it is asked
    for.

    A seek forward reads on and drops what it read, so what ``tarfile`` skips is
    counted as it is decompressed. The stream itself is never seeked. A seek back
    is a ``ReadError``: it cannot take back what it counted. Under
    :class:`EndCheckedReader`, which refuses a seek back first (issue #3798), a
    walk never asks for one. ``tarfile`` in ``r:`` made none walking any of 43
    producers' archives (GNU tar, BusyBox, Go, ``git archive``, Python
    ``tarfile``), with member data read, skipped or partly read. A sparse
    member whose map reaches past its stored data makes one.

    A hole in a sparse member is not in the stream, so this does not count it.
    """

    def __init__(self, stream: io.BufferedIOBase, max_bytes: int) -> None:
        super().__init__()
        self._stream = stream
        self._max_bytes = max_bytes
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def read(self, size: int | None = -1, /) -> bytes:
        allowed = self._max_bytes + 1 - self._position
        data = self._stream.read(
            allowed if size is None or size < 0 else min(size, allowed)
        )
        self._position += len(data)
        if self._position > self._max_bytes:
            raise CapExceededError(f"more than {self._max_bytes} bytes")
        return data

    def seek(self, offset: int, whence: int = io.SEEK_SET, /) -> int:
        if whence != io.SEEK_SET:
            raise io.UnsupportedOperation("only a seek from the start")
        if offset < self._position:
            raise tarfile.ReadError("the archive seeks back in its stream")
        while self._position < offset and self.read(
            min(offset - self._position, _SKIP_CHUNK_BYTES)
        ):
            pass
        return self._position

    def tell(self) -> int:
        return self._position


class GzipDecoder:
    """A gzip stream decompressed from the bytes fed to it (issue #3835).

    Feed the compressed bytes with :meth:`feed`, take what they decompress to
    with :meth:`decompress`, and call :meth:`finish` once the stream has ended.
    :class:`GzipReader` reads a file through it, and the object backend's health
    probe the parts the store delivers, so both read gzip as one (issue #3847).

    The stream is one or more gzip members, and zlib's gzip mode reads each one:
    a header CRC that does not match, a reserved flag bit, damaged deflate data,
    a CRC32 or ISIZE that does not match, and bytes that do not start a member
    are ``zlib.error``. Zeros after a member are padding, skipped in bulk; zeros
    before the first member are not. A 10,001st member is ``gzip.BadGzipFile``
    as it starts, and a stream that does not end at a member's end is
    ``EOFError`` (:meth:`finish`).

    Only documented API is used (DEPENDENCIES.md Section 7). ``gzip.GzipFile``
    reads the same members, but it reads zero padding one byte at a time, pays
    for every member however many there are, and checks neither the header CRC
    nor the reserved flag bits.
    """

    def __init__(self) -> None:
        self._decompressor = zlib.decompressobj(_GZIP_WBITS)
        self._members = 1
        self._input = memoryview(b"")

    def feed(self, data: bytes) -> None:
        """Hand over the next compressed bytes, once :meth:`decompress` has taken
        all the ones fed before."""

        self._input = memoryview(data)

    def decompress(self, max_length: int) -> bytes:
        """Up to ``max_length`` decompressed bytes, at least one asked for.

        ``b""`` once every byte fed has been taken: feed the next ones, or
        :meth:`finish` if there are none.
        """

        while self._input:
            if self._decompressor.eof:
                if not self._input[0]:
                    head = bytes(self._input[:_GZIP_PIECE_BYTES])
                    padding = (
                        len(head)
                        if head == _ZERO_PIECE
                        else len(head) - len(head.lstrip(b"\0"))
                    )
                    self._input = self._input[padding:]
                    continue
                self._members += 1
                if self._members > _MAX_GZIP_MEMBERS:
                    raise gzip.BadGzipFile(
                        f"more than {_MAX_GZIP_MEMBERS} gzip members"
                    )
                self._decompressor = zlib.decompressobj(_GZIP_WBITS)
            piece = self._input[:_GZIP_PIECE_BYTES]
            data = self._decompressor.decompress(piece, max_length)
            # What zlib left of the piece. At a member's end that is the bytes
            # after it, and the tail can still hold an earlier call's.
            left = (
                self._decompressor.unused_data
                if self._decompressor.eof
                else self._decompressor.unconsumed_tail
            )
            self._input = self._input[len(piece) - len(left) :]
            if data:
                return data
        return b""

    def finish(self) -> None:
        """Refuse a stream that did not end at a member's end, as ``EOFError``."""

        if not self._decompressor.eof:
            raise EOFError("the gzip stream ended before the end of a member")


class GzipReader(io.BufferedIOBase):
    """The decompressed bytes of the gzip stream in ``source`` (issue #3835).

    The stream a ``tar.gz`` walk reads: pass it to :class:`EndCheckedReader`, or
    to the :class:`CappedReader` beneath one. It reads ``source`` 1 MiB at a time
    through a :class:`GzipDecoder`, so it takes and refuses what the decoder
    does, the stream's end included once a read reaches it. Closing it closes
    ``source``.

    A read returns all it asks for until the stream ends: ``tarfile`` takes a
    short read for the end of the data. A seek reads forward to its offset; a
    seek back, or one other than from the start, is ``io.UnsupportedOperation``.
    """

    def __init__(self, source: io.BufferedIOBase) -> None:
        super().__init__()
        self._source = source
        self._decoder = GzipDecoder()
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def read(self, size: int | None = -1, /) -> bytes:
        wanted = sys.maxsize if size is None or size < 0 else size
        parts = []
        while wanted:
            data = self._decoder.decompress(wanted)
            if not data:
                compressed = self._source.read(_GZIP_SOURCE_BYTES)
                if not compressed:
                    self._decoder.finish()
                    break
                self._decoder.feed(compressed)
                continue
            parts.append(data)
            wanted -= len(data)
        read = b"".join(parts)
        self._position += len(read)
        return read

    def seek(self, offset: int, whence: int = io.SEEK_SET, /) -> int:
        if whence != io.SEEK_SET or offset < self._position:
            raise io.UnsupportedOperation("a gzip stream seeks forward only")
        while self._position < offset and self.read(
            min(offset - self._position, _SKIP_CHUNK_BYTES)
        ):
            pass
        return self._position

    def tell(self) -> int:
        return self._position

    def close(self) -> None:
        self._source.close()
        super().close()
