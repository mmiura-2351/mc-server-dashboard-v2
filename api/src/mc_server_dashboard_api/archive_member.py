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

# How much of the stream past the end-of-archive marker to read at a time. The
# caller counts each chunk toward its decompressed-size cap before the next one
# is read, so a long run of zeros cannot be materialized whole.
_TAIL_CHUNK_BYTES = 1024 * 1024


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
    """

    def __init__(self, stream: io.BufferedIOBase) -> None:
        super().__init__()
        self._stream = stream
        self._last_read = b""
        self._read_since_member = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        """``True`` whatever the stream: ``tarfile`` seeks in ``r:``.

        Not a promise about the stream. ``gzip.GzipFile`` seeks forward over any
        source by decompressing on, and seeks back by starting over.
        """

        return True

    def read(self, size: int = -1, /) -> bytes:
        self._last_read = self._stream.read(size)
        self._read_since_member += len(self._last_read)
        return self._last_read

    def seek(self, offset: int, whence: int = io.SEEK_SET, /) -> int:
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()

    def members(self, tar: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
        """The members of ``tar``, the archive opened over this reader.

        The count of what the walk reads starts over each time the walk resumes
        after a member, which is after the caller has read that member's data.
        """

        for member in tar:
            yield member
            self._read_since_member = 0

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

        The chunks are yielded so the caller counts them toward its
        decompressed-size cap, under its own refusal.
        """

        if self._last_read != _END_BLOCK or self._read_since_member not in _END_READS:
            raise tarfile.ReadError(
                "the archive does not end at its end-of-archive marker"
            )
        while chunk := self._stream.read(_TAIL_CHUNK_BYTES):
            if chunk.count(0) != len(chunk):
                raise tarfile.ReadError("data past the end-of-archive marker")
            yield chunk
