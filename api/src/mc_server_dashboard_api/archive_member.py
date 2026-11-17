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
proves a walk stopped at the end-of-archive marker and reads the stream on from
there to its end. The servers upload gates read an uploaded ``tar.gz`` through
it.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Iterator
from pathlib import PurePosixPath

# The tar format's block size. The end-of-archive marker is a block of zeros.
_END_BLOCK = bytes(512)

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
    """The stream a tar is walked from, proving the walk reached its end (#3778).

    ``tarfile`` stops reading at the end-of-archive marker, so a ``tar.gz`` read
    through it never reaches the gzip trailer (CRC32 and ISIZE). It also reads a
    header that does not parse, past the first, or a stream that stops where a
    header should start, as the end of the archive, so damage there silently cuts
    the member list short.

    Pass it as ``tarfile.open(fileobj=reader, mode="r:")`` over the decompressed
    stream (``gzip.GzipFile`` for a ``tar.gz``), walk every member, then iterate
    :meth:`read_past_end_marker`. The walk must have stopped on a block of 512
    zeros: the read the walk ended on is judged, so a damaged header, a missing
    marker or a cut one is a ``ReadError``. One zero block is enough, though the
    format writes two: nothing is lost after it. Only the ``r:`` mode is
    supported; in ``r|`` ``tarfile`` reads the file in whole records, so where
    its walk stopped cannot be seen from here.
    """

    def __init__(self, stream: io.BufferedIOBase) -> None:
        super().__init__()
        self._stream = stream
        self._last_read = b""

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def read(self, size: int = -1, /) -> bytes:
        self._last_read = self._stream.read(size)
        return self._last_read

    def seek(self, offset: int, whence: int = io.SEEK_SET, /) -> int:
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()

    def read_past_end_marker(self) -> Iterator[bytes]:
        """Read on from the end-of-archive marker to the end of the stream.

        Iterate it once the walk has ended. The walk must have stopped on the
        marker (see the class), and what follows must be zeros: the marker's
        second block and the producer's record padding. Anything else, such as a
        second tar after the first, is a ``ReadError``. POSIX leaves the rest of
        the last record undefined; this is stricter on purpose, since restore
        never reads what lies there. Over ``gzip.GzipFile``, reading to the end
        is what checks the trailer: a mismatch is ``BadGzipFile``, and so are
        bytes after the gzip stream that are neither another gzip member nor
        zero padding.

        The chunks are yielded so the caller counts them toward its
        decompressed-size cap, under its own refusal.
        """

        if self._last_read != _END_BLOCK:
            raise tarfile.ReadError(
                "the archive does not end at its end-of-archive marker"
            )
        while chunk := self._stream.read(_TAIL_CHUNK_BYTES):
            if chunk.count(0) != len(chunk):
                raise tarfile.ReadError("data past the end-of-archive marker")
            yield chunk
