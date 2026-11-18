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
toward a cap and refuses a walk that seeks back, and :class:`EndCheckedReader`
bounds what the walk reads between two members.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Iterator
from pathlib import PurePosixPath

# The tar format's block size. The end-of-archive marker is a block of zeros.
_END_BLOCK = bytes(512)

# What a walk reads after its last member when it stops at the marker: the block,
# or first the one byte tarfile reads where it seeks to the next header.
_END_READS = (len(_END_BLOCK), len(_END_BLOCK) + 1)

# How much the walk may read between handing out one member and the next: that
# member's header blocks and extended headers (PAX records, GNU long names and
# links, sparse maps). tarfile holds extended-header data whole, so this bounds
# what it holds of it at a time (issue #3810). Real extended headers are long
# names, xattrs and ACLs: kilobytes.
_BETWEEN_MEMBERS_BYTES = 1024 * 1024

# What the members a walk hands out may hold (issue #3810). tarfile keeps every
# member, each with its own copy of the global PAX records, so a global header
# under the budget above is held once per member. A member may hold at most this
# many PAX records, global and its own...
_PAX_RECORDS_PER_MEMBER = 32
# ...and the members' names, PAX keys and PAX values together at most this many
# characters, a name read from a PAX record counted twice. Real archives hold
# dozens of records and a few hundred characters a member.
_HELD_CHARACTERS = 8 * 1024 * 1024

# How much of the stream past the end-of-archive marker to read at a time, so a
# long run of zeros cannot be materialized whole. Read through a CappedReader,
# each chunk counts toward the cap before the next one is read.
_TAIL_CHUNK_BYTES = 1024 * 1024

# How much a CappedReader reads at a time to skip forward. Each chunk counts
# toward the cap before the next one is read.
_SKIP_CHUNK_BYTES = 1024 * 1024


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
    stream (``gzip.GzipFile`` for a ``tar.gz``), walk the archive through
    :meth:`members`, then iterate :meth:`read_past_end_marker`.

    The walk counts as having stopped at the marker only when everything it read
    after handing out its last member is one block of 512 zeros, after at most
    the one byte ``tarfile`` reads where it seeks to the next header. The
    caller's own reads of that member's data are not counted. Any other end is a
    ``ReadError``:

    - a header ``tarfile`` gave up on: read alone it is not zeros, and one it
      gave up on after reading more (an extension block, extended-header data,
      the header after it) makes the count too large;
    - a stream that ends where a header should start, or partway through one.

    One zero block is enough, though the format writes two: nothing is lost after
    it. The check relies on ``tarfile`` reading the block it stops at with one
    512-byte ``read``. Read any other way, the count or the last read differs and
    the archive is refused rather than accepted. Only ``r:`` is supported: in
    ``r|`` ``tarfile`` reads the file in whole records, so where its walk stopped
    cannot be seen from here.

    The same count bounds what the walk reads between handing out one member and
    the next to 1 MiB (issue #3810): the next member's header blocks and extended
    headers, which ``tarfile`` holds whole. A read then asks the stream for at
    most one byte past that, and going past it is a ``ReadError``. The caller's
    reads of member data are not held to it.

    ``tarfile`` also keeps every member it hands out, each with its own copy of
    the global PAX records, so :meth:`members` bounds what they hold, also as a
    ``ReadError``: at most 32 PAX records in a member, global and its own, and at
    most 8 Mi characters of names, PAX keys and PAX values in all.
    """

    def __init__(self, stream: io.BufferedIOBase) -> None:
        super().__init__()
        self._stream = stream
        self._last_read = b""
        self._read_since_member = 0
        # Whether the walk, rather than the caller, is reading: from the start
        # until a member is handed out, and from each resume until the next.
        self._walking = True

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        """``True`` whatever the stream: ``tarfile`` seeks in ``r:``.

        Not a promise about the stream. ``gzip.GzipFile`` seeks forward over any
        source by decompressing on, and seeks back by starting over.
        """

        return True

    def read(self, size: int = -1, /) -> bytes:
        if self._walking:
            allowed = _BETWEEN_MEMBERS_BYTES + 1 - self._read_since_member
            size = allowed if size < 0 else min(size, allowed)
        self._last_read = self._stream.read(size)
        self._read_since_member += len(self._last_read)
        if self._walking and self._read_since_member > _BETWEEN_MEMBERS_BYTES:
            raise tarfile.ReadError(
                f"more than {_BETWEEN_MEMBERS_BYTES} bytes of headers before a member"
            )
        return self._last_read

    def seek(self, offset: int, whence: int = io.SEEK_SET, /) -> int:
        """Forward only: a seek back is a ``ReadError`` (issue #3798).

        ``tarfile`` seeks back only when a member's data read ran on past where
        the next header starts, such as a GNU sparse map that takes more data than
        the member stores. It seeks with ``SEEK_SET`` only.
        """

        if whence != io.SEEK_SET or offset < self._stream.tell():
            raise tarfile.ReadError("a member's data runs past the member")
        return self._stream.seek(offset)

    def tell(self) -> int:
        return self._stream.tell()

    def members(self, tar: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
        """The members of ``tar``, the archive opened over this reader.

        The count of what the walk reads starts over each time the walk resumes
        after a member, which is after the caller has read that member's data.
        Only the walk's own reads are held to the budget between two members.
        Each member is held to what members may hold before it is handed out.
        """

        held = 0
        for member in tar:
            if len(member.pax_headers) > _PAX_RECORDS_PER_MEMBER:
                raise tarfile.ReadError(
                    f"a member with more than {_PAX_RECORDS_PER_MEMBER} PAX records"
                )
            held += len(member.name) + sum(
                len(key) + len(value) for key, value in member.pax_headers.items()
            )
            if held > _HELD_CHARACTERS:
                raise tarfile.ReadError(
                    f"more than {_HELD_CHARACTERS} characters of names and PAX records"
                )
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
        restore never reads what lies there. Over ``gzip.GzipFile``, reading to
        the end is what checks the trailer: a mismatch is ``BadGzipFile``, and so
        are bytes after the gzip stream that are neither another gzip member nor
        zero padding.

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
    from the stream. The stream may decompress more than it hands over:
    ``gzip.GzipFile`` reads ahead by ``io.DEFAULT_BUFFER_SIZE``, 8 KiB on 3.13
    and 128 KiB on 3.14.

    A seek forward reads on and drops what it read, so what ``tarfile`` skips is
    counted as it is decompressed. The stream itself is never seeked. A seek back
    is a ``ReadError``: over ``gzip.GzipFile`` it would decompress the stream
    again from its start. ``tarfile`` in ``r:`` made none walking any of 43
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
