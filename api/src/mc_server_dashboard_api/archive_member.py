"""The one backup archive member rule (issues #3550, #3574, #3579).

Storage judges a backup member by this rule in restore and the health probe,
and its snapshot ingest takes only the names :func:`is_canonical_name` admits.
The servers upload gate judges each member by the same rule before an archive
is stored, so an upload is refused at once instead of failing later at restore
or sweep. It is a neutral root-level module, standard library only, so both
contexts' inward layers may import it. That is the ``rfc3339`` precedent in the
root-level import contract. Every caller first reads a member through
:func:`without_tar_root_prefix`.

How a member walk ends lives here too (issue #3778): :class:`EndCheckedTarInfo`
ends it only at the end-of-archive marker, and :func:`read_past_end_marker`
reads the stream on from that marker to its end. The servers upload gates read
an uploaded ``tar.gz`` through both.
"""

from __future__ import annotations

import tarfile
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Self

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


class EndCheckedTarInfo(tarfile.TarInfo):
    """A ``TarInfo`` read that ends a tar only at its end-of-archive marker (#3778).

    ``tarfile`` reads a header that fails to parse past the archive's first, and
    a stream that stops where a header should start, as the end of the archive,
    so a damaged header silently cuts the member list short. Opened with
    ``tarinfo=EndCheckedTarInfo``, only an all-zero block (the marker) ends it;
    any other header failure is a ``ReadError``.
    """

    @classmethod
    def frombuf(cls, buf: bytes | bytearray, encoding: str, errors: str) -> Self:
        try:
            return super().frombuf(buf, encoding, errors)
        except tarfile.HeaderError as exc:
            if buf.count(0) == tarfile.BLOCKSIZE:
                raise
            raise tarfile.ReadError(f"bad tar header: {exc}") from exc


def read_past_end_marker(tar: tarfile.TarFile) -> Iterator[bytes]:
    """Read a tar on from its end-of-archive marker to the end of the stream (#3778).

    Call it once iteration over ``tar`` has ended. What the stream holds past
    the marker must be zeros: the marker's second block and the producer's
    record padding. Anything else, such as a second tar after the first, is a
    ``ReadError``. POSIX leaves the rest of the last record undefined; this is
    stricter on purpose, since restore never reads what lies there.

    Over a ``tar.gz`` opened ``r:gz``, reading to the end is also what makes the
    gzip reader check the trailer (CRC32 and ISIZE), which ``tarfile`` never
    reaches when it stops at the marker. The gzip reader then refuses whatever
    follows the gzip stream unless it is another gzip member or zero padding.

    The chunks are yielded so the caller counts them toward its decompressed-size
    cap, under its own refusal.
    """

    while chunk := tar.fileobj.read(_TAIL_CHUNK_BYTES):
        if chunk.count(0) != len(chunk):
            raise tarfile.ReadError("data past the end-of-archive marker")
        yield chunk
