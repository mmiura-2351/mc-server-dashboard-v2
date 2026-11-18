"""Shared helpers for the storage fs-adapter tests.

Build scopes, turn bytes into the async stream the Port expects, and tar up a
directory tree so a snapshot/backup can be staged from in-memory content.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import io
import os
import shutil
import subprocess
import tarfile
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    ServerId,
)


def new_scope() -> tuple[CommunityId, ServerId]:
    return CommunityId(uuid.uuid4()), ServerId(uuid.uuid4())


async def publish(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
    *,
    chunk: int = 7,
) -> None:
    """Stage ``files`` as a snapshot and publish it (the common test arrange step).

    ``chunk`` is the tar stream's write size; pass a coarse one (e.g. 1 MiB) for
    multi-MiB payloads, or the tiny default makes the spool write dominate.
    """

    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files, chunk=chunk))
    await storage.commit_snapshot(handle)


async def stream_of(data: bytes, *, chunk: int = 7) -> AsyncIterator[bytes]:
    """Yield ``data`` in small chunks, exercising the streaming Port contract."""

    for i in range(0, len(data), chunk):
        yield data[i : i + chunk]


def tar_bytes(files: dict[str, bytes]) -> bytes:
    """Build a tar stream whose members are ``{rel_path: content}``."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, content in files.items():
            info = tarfile.TarInfo(name=rel)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


async def tar_stream(
    files: dict[str, bytes], *, chunk: int = 7
) -> AsyncIterator[bytes]:
    async for piece in stream_of(tar_bytes(files), chunk=chunk):
        yield piece


def read_tar(blob: bytes) -> dict[str, bytes]:
    """Inverse of :func:`tar_bytes`: read a tar stream into ``{name: content}``."""

    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tar:
        for member in tar.getmembers():
            if member.isfile():
                handle = tar.extractfile(member)
                assert handle is not None
                out[member.name] = handle.read()
    return out


async def drain(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


def malicious_tar_with_escape() -> bytes:
    """A tar stream whose member tries to escape via ``../``.

    Used to prove the extractor sandboxes staging (``filter="data"`` refuses it).
    """

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        content = b"pwned"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def malicious_tar_with_symlink_escape() -> bytes:
    """A tar stream with a symlink member targeting an absolute path outside staging.

    Used to prove ``filter="data"`` rejects/neutralizes a symlink-based escape: it
    refuses a symlink whose target points outside the extraction root.
    """

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="escape_link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    return buf.getvalue()


def tar_with_link_member(
    link_type: bytes, *, mode: Literal["w", "w:gz"] = "w"
) -> bytes:
    """A tar with one regular file and one link member naming it, both contained.

    ``link_type`` is ``tarfile.LNKTYPE`` (hard link) or ``tarfile.SYMTYPE``. Every
    name stays inside the extraction root, so ``filter="data"`` alone admits the
    archive (issue #3371). ``mode="w:gz"`` gives the backup-archive form.
    """

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        content = b"level"
        info = tarfile.TarInfo(name="world/level.dat")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
        link = tarfile.TarInfo(name="world/alias.dat")
        link.type = link_type
        link.linkname = (
            "world/level.dat" if link_type == tarfile.LNKTYPE else "level.dat"
        )
        tar.addfile(link)
    return buf.getvalue()


# A non-numeric GNU sparse field, one per place CPython's ``tarfile`` converts it
# with a bare ``int()`` while it reads a header, and so raises ``ValueError``
# (issue #3790): the PAX header fields, and the format 1.0 map, which opens the
# member's data. Each maps to the member's PAX headers and data.
BAD_SPARSE_FIELDS: dict[str, tuple[dict[str, str], bytes]] = {
    "size": ({"GNU.sparse.size": "x"}, b"A"),
    "realsize": ({"GNU.sparse.realsize": "x"}, b"A"),
    "map": ({"GNU.sparse.map": "x"}, b"A"),
    "1.0-map": ({"GNU.sparse.major": "1", "GNU.sparse.minor": "0"}, b"x\n"),
}


def tar_with_a_bad_sparse_field(field: str, *, after: dict[str, bytes]) -> bytes:
    """A PAX tar of the ``after`` members, then one whose sparse ``field`` is bad.

    ``field`` is a :data:`BAD_SPARSE_FIELDS` key. With no ``after`` members the
    bad header is the first one, which ``tarfile.open`` itself reads.
    """

    pax_headers, data = BAD_SPARSE_FIELDS[field]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, content in after.items():
            info = tarfile.TarInfo(name=rel)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        info = tarfile.TarInfo(name="world/sparse.dat")
        info.size = len(data)
        info.pax_headers = pax_headers
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _padded(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % tarfile.BLOCKSIZE)


def _tar_of(*members: bytes) -> bytes:
    """Members as written by :func:`_member` and the sparse builders, then the
    end-of-archive marker."""

    return b"".join(members) + b"\0" * (2 * tarfile.BLOCKSIZE)


def _member(name: str, data: bytes, pax_headers: dict[str, str] | None = None) -> bytes:
    """One member's header blocks (a PAX header first when given) and data."""

    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.pax_headers = pax_headers or {}
    return info.tobuf(format=tarfile.PAX_FORMAT) + _padded(data)


def _sparse_1_0_member(
    name: str, sparse: list[tuple[int, int]], realsize: int, data: bytes
) -> bytes:
    """A GNU sparse format 1.0 member: the map opens its data, a block of its own."""

    lines = [len(sparse), *(n for entry in sparse for n in entry)]
    sparse_map = "".join(f"{n}\n" for n in lines).encode()
    headers = {
        "GNU.sparse.major": "1",
        "GNU.sparse.minor": "0",
        "GNU.sparse.realsize": str(realsize),
    }
    return _member(name, _padded(sparse_map) + data, headers)


def _pax_record(keyword: str, value: str) -> bytes:
    """One PAX record, ``"<length> <keyword>=<value>\\n"``; the length counts
    its own digits."""

    field = f" {keyword}={value}\n".encode()
    length = len(field) + 1
    while len(str(length)) + len(field) != length:
        length += 1
    return str(length).encode() + field


def _sparse_0_0_member(
    name: str, sparse: list[tuple[int, int]], realsize: int, data: bytes
) -> bytes:
    """A GNU sparse format 0.0 member: one ``GNU.sparse.offset`` and one
    ``GNU.sparse.numbytes`` record per map entry, in a PAX header written by hand,
    since a ``pax_headers`` dict holds a keyword once."""

    records = [("GNU.sparse.size", str(realsize))]
    records.append(("GNU.sparse.numblocks", str(len(sparse))))
    for offset, numbytes in sparse:
        records.append(("GNU.sparse.offset", str(offset)))
        records.append(("GNU.sparse.numbytes", str(numbytes)))
    pax = b"".join(_pax_record(keyword, value) for keyword, value in records)
    pax_info = tarfile.TarInfo(name="PaxHeaders/" + name.rsplit("/", 1)[-1])
    pax_info.type = tarfile.XHDTYPE
    pax_info.size = len(pax)
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    return (
        pax_info.tobuf(format=tarfile.USTAR_FORMAT)
        + _padded(pax)
        + info.tobuf(format=tarfile.USTAR_FORMAT)
        + _padded(data)
    )


def _gnu_number(n: int) -> bytes:
    """A 12-byte GNU tar number field: octal when it fits, else base-256."""

    if 0 <= n < 8**11:
        return b"%011o\0" % n
    if n >= 0:
        return b"\x80" + n.to_bytes(11, "big")
    return b"\xff" + (n + 256**11).to_bytes(11, "big")


def _gnu_sparse_slots(sparse: list[tuple[int, int]]) -> bytes:
    return b"".join(_gnu_number(offset) + _gnu_number(n) for offset, n in sparse)


def _old_gnu_sparse_member(
    name: str, sparse: list[tuple[int, int]], realsize: int, data: bytes
) -> bytes:
    """An old GNU sparse member (type ``S``): its map in the header block, and
    past four entries in extension blocks of 21 more each.

    GNU tar leaves the unused slots zero. ``tarfile`` reads those of the header
    block back as ``(0, 0)`` entries, and leaves out every entry of an extension
    block whose offset or length is zero.
    """

    info = tarfile.TarInfo(name=name)
    info.type = tarfile.GNUTYPE_SPARSE
    info.size = len(data)
    header = bytearray(info.tobuf(format=tarfile.GNU_FORMAT))
    header[386 : 386 + 24 * len(sparse[:4])] = _gnu_sparse_slots(sparse[:4])
    rest = sparse[4:]
    header[482] = 1 if rest else 0
    header[483:495] = _gnu_number(realsize)
    # The checksum counts its own field as spaces.
    header[148:156] = b" " * 8
    header[148:155] = b"%06o\0" % sum(header)
    extensions = b""
    while rest:
        block = bytearray(tarfile.BLOCKSIZE)
        slots, rest = rest[:21], rest[21:]
        block[: 24 * len(slots)] = _gnu_sparse_slots(slots)
        block[504] = 1 if rest else 0
        extensions += block
    return bytes(header) + extensions + _padded(data)


# A GNU sparse member whose numbers read but cannot describe a file, one per
# shape ``tarfile`` accepts without a check (issue #3802): a negative size or
# real size, a map entry with a negative offset or length, one that ends past the
# real size, and entries that overlap or are out of order, across the PAX formats
# 0.0, 0.1 and 1.0 and the old GNU type ``S`` header.
_SPARSE_NAME = "world/sparse.dat"
INVALID_SPARSE_LAYOUTS: dict[str, bytes] = {
    "0.1-negative-offset": _member(_SPARSE_NAME, b"A", {"GNU.sparse.map": "-5,1"}),
    "negative-realsize": _member(_SPARSE_NAME, b"A", {"GNU.sparse.realsize": "-1"}),
    "0.0-negative-size": _member(_SPARSE_NAME, b"A", {"GNU.sparse.size": "-1"}),
    "0.0-negative-offset": _member(
        _SPARSE_NAME,
        b"A",
        {"GNU.sparse.size": "4", "GNU.sparse.offset": "-5", "GNU.sparse.numbytes": "1"},
    ),
    "0.1-empty-region-at-a-negative-offset": _member(
        _SPARSE_NAME, b"", {"GNU.sparse.size": "4", "GNU.sparse.map": "-5,0"}
    ),
    "0.1-negative-numbytes": _member(
        _SPARSE_NAME, b"A", {"GNU.sparse.size": "4", "GNU.sparse.map": "0,-1"}
    ),
    "0.1-past-realsize": _member(
        _SPARSE_NAME, b"AB", {"GNU.sparse.size": "4", "GNU.sparse.map": "0,1,4,1"}
    ),
    "0.1-overlapping": _member(
        _SPARSE_NAME, b"ABCD", {"GNU.sparse.size": "4", "GNU.sparse.map": "0,2,1,2"}
    ),
    "0.1-unsorted": _member(
        _SPARSE_NAME, b"AB", {"GNU.sparse.size": "4", "GNU.sparse.map": "2,1,0,1"}
    ),
    "1.0-negative-offset": _sparse_1_0_member(_SPARSE_NAME, [(-5, 1)], 1, b"A"),
    "1.0-past-realsize": _sparse_1_0_member(_SPARSE_NAME, [(0, 1), (4, 1)], 4, b"AB"),
    "1.0-overlapping": _sparse_1_0_member(_SPARSE_NAME, [(0, 2), (1, 2)], 4, b"ABCD"),
    "S-negative-offset": _old_gnu_sparse_member(_SPARSE_NAME, [(-5, 1)], 4, b"A"),
    "S-negative-numbytes": _old_gnu_sparse_member(_SPARSE_NAME, [(0, -1)], 4, b"A"),
    "S-past-realsize": _old_gnu_sparse_member(_SPARSE_NAME, [(10, 1)], 4, b"A"),
    "S-far-past-realsize": _old_gnu_sparse_member(_SPARSE_NAME, [(2**40, 1)], 4, b"A"),
    "S-overlapping": _old_gnu_sparse_member(_SPARSE_NAME, [(0, 2), (1, 2)], 4, b"ABCD"),
    "S-unsorted": _old_gnu_sparse_member(_SPARSE_NAME, [(2, 1), (0, 1)], 4, b"AB"),
    "S-negative-realsize": _old_gnu_sparse_member(_SPARSE_NAME, [(0, 1)], -1, b"A"),
    "S-extension-block-negative-offset": _old_gnu_sparse_member(
        _SPARSE_NAME, [(0, 1), (2, 1), (4, 1), (6, 1), (-5, 1)], 8, b"ABCDE"
    ),
}


def tar_with_an_invalid_sparse_layout(layout: str, *, after: dict[str, bytes]) -> bytes:
    """A tar of the ``after`` members, then an :data:`INVALID_SPARSE_LAYOUTS` one.

    With no ``after`` members the bad header is the first one, which
    ``tarfile.open`` itself reads.
    """

    members = [_member(rel, content) for rel, content in after.items()]
    return _tar_of(*members, INVALID_SPARSE_LAYOUTS[layout])


# The files of :func:`tar_with_gnu_tar_sparse_members` (issue #3802). The maps of
# ``a.dat``, ``hole.dat`` and ``many.dat`` are the ones GNU tar 1.35 writes: data,
# a hole, data and a trailing hole, closed by a zero-length entry at the real
# size; a file that is all hole; and six data regions, which take the old ``S``
# header an extension block. ``touching.dat`` has two data regions that touch,
# which the map allows.
_MANY_REGIONS = [(8192 * i, 4096) for i in range(6)]
SPARSE_FILES = {
    "world/a.dat": b"A" * 512 + b"\0" * 512 + b"B" * 512 + b"\0" * 1536,
    "world/hole.dat": b"\0" * 1024,
    "world/touching.dat": b"C" * 512 + b"D" * 512 + b"\0" * 512,
    "world/many.dat": b"".join(bytes([c]) * 4096 + b"\0" * 4096 for c in b"MNOPQR")
    + b"\0" * 4096,
}
_SPARSE_MAPS: dict[str, tuple[list[tuple[int, int]], bytes]] = {
    "world/a.dat": ([(0, 512), (1024, 512), (3072, 0)], b"A" * 512 + b"B" * 512),
    "world/hole.dat": ([(1024, 0)], b""),
    "world/touching.dat": ([(0, 512), (512, 512), (1536, 0)], b"C" * 512 + b"D" * 512),
    "world/many.dat": (
        [*_MANY_REGIONS, (53248, 0)],
        b"".join(bytes([c]) * 4096 for c in b"MNOPQR"),
    ),
}
GNU_SPARSE_FORMATS = ["S", "0.0", "0.1", "1.0"]


def tar_with_gnu_tar_sparse_members(sparse_format: str) -> bytes:
    """A tar holding :data:`SPARSE_FILES` as GNU sparse members of one format.

    ``sparse_format`` is a :data:`GNU_SPARSE_FORMATS` entry. The old ``S``
    header pads its map with ``(0, 0)`` slots, as GNU tar's does.
    """

    members = []
    for name, (sparse, data) in _SPARSE_MAPS.items():
        realsize = len(SPARSE_FILES[name])
        if sparse_format == "S":
            members.append(_old_gnu_sparse_member(name, sparse, realsize, data))
        elif sparse_format == "0.0":
            members.append(_sparse_0_0_member(name, sparse, realsize, data))
        elif sparse_format == "1.0":
            members.append(_sparse_1_0_member(name, sparse, realsize, data))
        else:
            sparse_map = ",".join(str(n) for entry in sparse for n in entry)
            headers = {"GNU.sparse.size": str(realsize), "GNU.sparse.map": sparse_map}
            members.append(_member(name, data, headers))
    return _tar_of(*members)


# Where a tar ends inside an old GNU sparse member's extension blocks: ``tarfile``
# indexes the short block it read and raises ``IndexError`` (issue #3802). The
# member's 30 map entries fill the header block, a first extension block that
# announces a second, and that second block.
_EXTENDED_SPARSE_MEMBER = _old_gnu_sparse_member(
    _SPARSE_NAME, [(2 * i, 1) for i in range(30)], 60, b"A" * 30
)
SPARSE_EXTENSION_CUTS = {
    "at-the-block": 512,
    "100-bytes-into-the-block": 612,
    "504-bytes-into-the-block": 1016,
    "after-a-block-announcing-another": 1024,
}


def tar_cut_in_a_sparse_extension_block(cut: str, *, after: dict[str, bytes]) -> bytes:
    """A tar of the ``after`` members that ends at a :data:`SPARSE_EXTENSION_CUTS`
    point of an old GNU sparse member's extension blocks."""

    members = [_member(rel, content) for rel, content in after.items()]
    cut_member = _EXTENDED_SPARSE_MEMBER[: SPARSE_EXTENSION_CUTS[cut]]
    return b"".join(members) + cut_member


def gnu_tar_sparse_archive_cut_in_its_extension_block() -> bytes:
    """The ``S`` archive of :func:`tar_with_gnu_tar_sparse_members`, cut 100
    bytes into the extension block of ``world/many.dat``, as GNU tar lays it out."""

    archive = tar_with_gnu_tar_sparse_members("S")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        header = tar.getmember("world/many.dat").offset
    return archive[: header + tarfile.BLOCKSIZE + 100]


def _flipped(data: bytes, at: int) -> bytes:
    damaged = bytearray(data)
    damaged[at] ^= 0x01
    return bytes(damaged)


# The members of the end-of-archive shapes below (issue #3798): a good member,
# the one the damage follows, and one after the damage.
END_CHECK_FILES = {
    "server.properties": b"k=v",
    "world/level.dat": b"w" * 3000,
    "world/after.dat": b"after",
}
_PROPERTIES, _LEVEL, _AFTER = (
    _member(name, data) for name, data in END_CHECK_FILES.items()
)


def _bad_sparse_0_0(field: str) -> bytes:
    """A GNU sparse format 0.0 member whose ``field`` record is not a number.

    ``tarfile`` raises ``InvalidHeaderError`` for it, which ``TarFile.next``
    takes, past the first header, for the end of the archive.
    """

    numbers = {"GNU.sparse.offset": "0", "GNU.sparse.numbytes": "1"}
    numbers[field] = "x"
    headers = {"GNU.sparse.size": "4", "GNU.sparse.numblocks": "1", **numbers}
    return _member(_SPARSE_NAME, b"A", headers)


# A plain tar that does not end at its end-of-archive marker, or holds more than
# zeros past it (issue #3798). ``tarfile`` stops at the damage and takes it for
# the end, so a reader that did not check where it stopped kept the members
# before it and lost the rest.
TARS_NOT_ENDING_AT_THEIR_MARKER: dict[str, bytes] = {
    "bad-checksum-later-header": _tar_of(_PROPERTIES, _flipped(_LEVEL, 0), _AFTER),
    "non-numeric-sparse-offset": _tar_of(
        _PROPERTIES, _bad_sparse_0_0("GNU.sparse.offset"), _AFTER
    ),
    "non-numeric-sparse-numbytes": _tar_of(
        _PROPERTIES, _bad_sparse_0_0("GNU.sparse.numbytes"), _AFTER
    ),
    "no-end-marker": _PROPERTIES + _LEVEL,
    "cut-end-marker": _PROPERTIES + _LEVEL + bytes(100),
    "a-second-tar-past-the-marker": _tar_of(_PROPERTIES, _LEVEL) + _tar_of(_AFTER),
    "a-byte-past-the-marker": _tar_of(_PROPERTIES, _LEVEL) + b"\x01",
}

# A sound plain tar of :data:`END_CHECK_FILES`.
END_CHECK_TAR = _tar_of(_PROPERTIES, _LEVEL, _AFTER)

# A plain tar whose GNU sparse member takes 2048 bytes of data where it stores
# one, so a read of its data runs on into the next member, and ``tarfile`` then
# seeks back to that member's header (issue #3798). One shape per sparse format,
# and a real size with no map.
_OVER_READ = 2048
TARS_WHOSE_MEMBER_DATA_RUNS_PAST_IT: dict[str, bytes] = {
    name: _tar_of(_PROPERTIES, member, _LEVEL)
    for name, member in {
        "0.1-map": _member(
            _SPARSE_NAME,
            b"A",
            {"GNU.sparse.size": str(_OVER_READ), "GNU.sparse.map": f"0,{_OVER_READ}"},
        ),
        "S-map": _old_gnu_sparse_member(
            _SPARSE_NAME, [(0, _OVER_READ)], _OVER_READ, b"A"
        ),
        "1.0-map": _sparse_1_0_member(
            _SPARSE_NAME, [(0, _OVER_READ)], _OVER_READ, b"A"
        ),
        "realsize-without-a-map": _member(
            _SPARSE_NAME, b"A", {"GNU.sparse.realsize": str(_OVER_READ)}
        ),
    }.items()
}


# Files whose tar is over each header limit the upload doors hold a walk to, and
# which the storage walks take (issue #3810): the storage walks have no entry
# cap, so a large working set reaches the character limit (131,072 files with
# 64-character names do). Their PAX ``comment`` records are what is held.
_KIB = 1024
OVER_THE_UPLOAD_HEADER_LIMITS_FILES = {
    "server.properties": b"k=v",
    "world/level.dat": b"w",
    **{f"world/data/d{i}.dat": b"r" for i in range(9)},
}


@functools.cache
def over_the_upload_header_limits_tar() -> bytes:
    """The tar of :data:`OVER_THE_UPLOAD_HEADER_LIMITS_FILES`, 10.5 MiB, built on
    first use."""

    return _tar_of(
        # 2 MiB of extended headers before one member, over the 1 MiB budget.
        _member("server.properties", b"k=v", {"comment": "c" * (2048 * _KIB)}),
        # 33 PAX records in one member, over the 32 a member may hold.
        _member(
            "world/level.dat",
            b"w",
            {f"SCHILY.xattr.user.a{i:02}": "v" for i in range(33)},
        ),
        # 9 x 960 KiB of PAX values, over the 8 Mi characters the members may hold.
        *(
            _member(f"world/data/d{i}.dat", b"r", {"comment": "c" * (960 * _KIB)})
            for i in range(9)
        ),
    )


def _stored_byte_flipped() -> bytes:
    """:data:`END_CHECK_TAR` gzipped uncompressed, one stored data byte flipped:
    only the CRC32 tells."""

    archive = gzip.compress(END_CHECK_TAR, compresslevel=0, mtime=0)
    return _flipped(archive, archive.index(b"k=v"))


_END_CHECK_TAR_GZ = gzip.compress(END_CHECK_TAR, mtime=0)

# A ``tar.gz`` of :data:`END_CHECK_TAR` whose gzip stream does not read to a
# sound end (issue #3798): restore stopped at the end-of-archive marker and
# never read the trailer.
DAMAGED_GZIP_STREAMS: dict[str, bytes] = {
    "bad-crc": _flipped(_END_CHECK_TAR_GZ, len(_END_CHECK_TAR_GZ) - 8),
    "bad-isize": _flipped(_END_CHECK_TAR_GZ, len(_END_CHECK_TAR_GZ) - 4),
    "flipped-stored-byte": _stored_byte_flipped(),
    "cut-trailer": _END_CHECK_TAR_GZ[:-1],
    "garbage-after-the-stream": _END_CHECK_TAR_GZ + b"not a gzip member",
}

# Where :data:`END_CHECK_TAR` splits into two gzip members: after the first tar
# member's data record, and 1000 bytes into the second member's data.
GZIP_MEMBER_SPLITS = {"at-a-tar-member": 1024, "inside-a-tar-member": 2048}


def two_gzip_members(split: str) -> bytes:
    """:data:`END_CHECK_TAR` as two gzip members split at ``split``."""

    at = GZIP_MEMBER_SPLITS[split]
    return gzip.compress(END_CHECK_TAR[:at]) + gzip.compress(END_CHECK_TAR[at:])


def reserved_member_over(size: int) -> bytes:
    """A tar whose reserved root member holds ``size`` zero bytes (issue #3798)."""

    return _tar_of(_PROPERTIES, _member(".mcsd_generation-cache", bytes(size)))


def reserved_member_with_unreadable_data() -> bytes:
    """A tar whose reserved root member's data does not read (issue #3798).

    Its old GNU sparse map takes 2001 bytes of data where the member stores one
    block, so a read of its data runs past the end of the archive. A walk that
    skips the data reads the archive whole.
    """

    reserved = _old_gnu_sparse_member(
        ".mcsd_generation", [(0, 1), (100, 2000)], 4096, b"X"
    )
    return _tar_of(_PROPERTIES, reserved)


def _gnu_tar_on_path() -> bool:
    if shutil.which("tar") is None:
        return False
    version = subprocess.run(
        ["tar", "--version"], capture_output=True, text=True, timeout=60
    )
    return "GNU tar" in version.stdout


needs_gnu_tar = pytest.mark.skipif(
    not _gnu_tar_on_path(),
    reason="GNU tar is not on PATH: its archives are not checked here",
)
needs_pigz = pytest.mark.skipif(
    shutil.which("pigz") is None,
    reason="pigz is not on PATH: its archives are not checked here",
)
needs_go = pytest.mark.skipif(
    shutil.which("go") is None,
    reason="go is not on PATH: Go archive/tar snapshots are not checked here",
)


def _end_check_tree(root: Path) -> Path:
    for name, data in END_CHECK_FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


def gnu_tar(tmp_path: Path, *options: str) -> bytes:
    """GNU tar's archive of :data:`END_CHECK_FILES`, ``world/`` as a directory."""

    tree = _end_check_tree(tmp_path / "tree")
    return subprocess.run(
        ["tar", "-c", *options, "-f", "-", "-C", str(tree), "server.properties"]
        + ["world"],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout


def pigz(data: bytes) -> bytes:
    return subprocess.run(
        ["pigz", "-c"], input=data, capture_output=True, check=True, timeout=60
    ).stdout


# The Worker's pack (``worker/internal/adapters/datatransfer``): ``archive/tar``
# with a directory header, regular files, and a name too long for ustar, which
# Go writes as PAX. ``Close`` writes the two zero blocks and no record padding.
_GO_PACK = """package main

import (
	"archive/tar"
	"os"
	"strings"
	"time"
)

func main() {
	tw := tar.NewWriter(os.Stdout)
	put := func(name string, typ byte, data string) {
		hdr := &tar.Header{Name: name, Mode: 0o644, Size: int64(len(data)),
			ModTime: time.Unix(1700000000, 0), Typeflag: typ}
		if err := tw.WriteHeader(hdr); err != nil {
			panic(err)
		}
		if _, err := tw.Write([]byte(data)); err != nil {
			panic(err)
		}
	}
	put("world/", tar.TypeDir, "")
	put("server.properties", tar.TypeReg, "k=v")
	put("world/level.dat", tar.TypeReg, strings.Repeat("w", 3000))
	put("world/after.dat", tar.TypeReg, "after")
	put("world/"+strings.Repeat("n", 120), tar.TypeReg, "long")
	if err := tw.Close(); err != nil {
		panic(err)
	}
}
"""

# The files :func:`go_archive_tar` writes.
GO_PACK_FILES = {**END_CHECK_FILES, "world/" + "n" * 120: b"long"}


def go_archive_tar(tmp_path: Path) -> bytes:
    """A plain tar as Go's ``archive/tar`` writes the Worker's snapshot."""

    (tmp_path / "pack.go").write_text(_GO_PACK)
    env = {**os.environ, "GOWORK": "off", "GOFLAGS": "", "GOTOOLCHAIN": "local"}
    return subprocess.run(
        ["go", "run", "pack.go"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=True,
        timeout=300,
    ).stdout


def bomb_targz(*, decompressed: int = 4096) -> bytes:
    """A self-contained ``tar.gz`` whose single member inflates to ``decompressed``.

    The compressed bytes are tiny (highly compressible zero fill); the decompressed
    member is ``decompressed`` bytes, modeling a gzip bomb so a restore-cap test can
    trip the decompressed-bytes bound with a small fixture (#287).
    """

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"\x00" * decompressed
        info = tarfile.TarInfo(name="world/region.mca")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def healthy_region_bytes() -> bytes:
    """A structurally valid empty ``.mca`` region: two all-zero header sectors.

    The integrity walker (issue #738) accepts a 4096-aligned region whose location
    table lists no present chunks, so this is a clean region file the publish/backup
    gates pass.
    """

    return bytes(2 * 4096)


def corrupt_region_bytes() -> bytes:
    """A structurally corrupt ``.mca`` region: a non-zero size below the two header
    sectors.

    Models the crash-during-save truncation reproduced in #703 — a torn region too
    short to hold both header tables, which the integrity walker (#738) flags as
    ``not_4096_aligned`` (the only alignment-derived verdict left after #927: a
    merely non-4096-aligned size is the normal unpadded tail, no longer corruption).
    The publish/backup integrity gate must refuse a working set containing it.
    """

    return bytes(4096 + 17)


def mode_invariant_corrupt_region_bytes() -> bytes:
    """A region the single rule set still flags as corrupt at every gate (#927).

    A merely non-4096-aligned size is the normal unpadded tail (accepted), so a
    gate-corruption test needs a real tear: a 3-sector aligned region whose location
    entry points to sector 4 — past EOF — which is ``sector_out_of_bounds``. The name
    is retained for the tests that already reference it; "mode invariant" now means
    "corrupt under the one rule set everywhere", not "corrupt in both former modes".
    """

    image = bytearray(3 * 4096)
    image[0:4] = (4).to_bytes(3, "big") + bytes([1])  # offset 4, count 1: past EOF.
    return bytes(image)


def unaligned_live_region_bytes(tail: int = 459) -> bytes:
    """A region with the legitimate UNPADDED tail of a 26.x world (#923/#927).

    Two header sectors plus one chunk in sector 2 whose data ends ``tail`` bytes into
    sector 2 (``tail`` < 4096), so the file size is NOT a multiple of 4096 but the
    trailing chunk fits byte-precisely. The single region rule set (#927) accepts it
    at every gate. Used to commit such a snapshot into the store and assert the
    publish gate and the at-rest consumers (backup/restore/sweep) tolerate it.
    """

    offset = 2
    size = offset * 4096 + tail
    image = bytearray(size)
    image[0:4] = offset.to_bytes(3, "big") + bytes([1])
    length = size - offset * 4096 - 4
    start = offset * 4096
    image[start : start + 4] = length.to_bytes(4, "big")
    image[start + 4] = 2  # zlib.
    return bytes(image)


def region_targz(files: dict[str, bytes]) -> bytes:
    """A self-contained ``tar.gz`` of ``{rel_path: content}`` (a backup archive).

    Mirrors the adapter-internal backup codec (gzip tar, STORAGE.md Section 2) so a
    restore-gate test can stage a backup carrying a chosen — healthy or corrupt —
    region file without going through the gated create path.
    """

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, content in files.items():
            info = tarfile.TarInfo(name=rel)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def snapshot_dir(root: Path, community: CommunityId, server: ServerId) -> Path:
    """The directory ``current`` resolves to (the live snapshot), for assertions."""

    import os

    link = (
        root
        / "communities"
        / str(community.value)
        / "servers"
        / str(server.value)
        / "current"
    )
    target = os.readlink(link)
    return link.parent / target


# --- observing the per-server publish lock (issue #3552) -------------------


class _ObservedThreadLock:
    """The fs server lock, reporting an acquire that finds it held."""

    def __init__(self, on_contended: Callable[[], None]) -> None:
        self._lock = threading.Lock()
        self._on_contended = on_contended

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(blocking=False):
            return True
        self._on_contended()
        if not blocking:
            return False
        return self._lock.acquire(timeout=timeout)

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


class _ObservedAsyncLock:
    """The object server lock, reporting an acquire that finds it held."""

    def __init__(self, on_contended: Callable[[], None]) -> None:
        self._lock = asyncio.Lock()
        self._on_contended = on_contended

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> None:
        if self._lock.locked():
            self._on_contended()
        await self._lock.acquire()

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


def watch_lock_contention(
    storage: Storage, community: CommunityId, server: ServerId
) -> asyncio.Event:
    """Observe the server's publish lock: the event marks an acquire that finds it held.

    Call it before anything takes the lock. The first acquire then holds it
    uncontended, and the event marks the first caller that has to wait for it.
    """

    loop = asyncio.get_running_loop()
    contended = asyncio.Event()

    def _signal() -> None:
        loop.call_soon_threadsafe(contended.set)

    locks: dict[Any, Any]
    if isinstance(storage, FsStorage):
        locks = cast(dict[Any, Any], storage._server_locks)
        locks[storage._server_root(community, server)] = _ObservedThreadLock(_signal)
    else:
        object_storage = cast(ObjectStorage, storage)
        locks = cast(dict[Any, Any], object_storage._server_locks)
        key = object_storage._server_prefix(community, server)
        locks[key] = _ObservedAsyncLock(_signal)
    return contended


async def wait_until_contended(
    waiter: asyncio.Task[Any], contended: asyncio.Event, *, timeout: float = 10
) -> None:
    """Return once ``waiter`` is waiting for the held lock.

    Fails if ``waiter`` finishes first, which means it never waited for the
    lock, or if neither happens within ``timeout``.
    """

    signal = asyncio.ensure_future(contended.wait())
    try:
        done, _ = await asyncio.wait(
            {waiter, signal}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        signal.cancel()
    assert signal in done, (
        "finished without waiting for the held lock"
        if waiter in done
        else "timed out before waiting for the held lock"
    )
    assert not waiter.done(), "finished while the lock was still held"
