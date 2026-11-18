"""Archives whose bytes the upload caps did not see (issue #3810).

Both tar.gz upload doors, the file extract and the backup upload, are tested
against each shape. Each one is a ``tar.gz`` that decompresses past :data:`CAP`
while handing out next to no member data, holds more extended-header data than
the walk may read between two members (1 MiB), has its members hold more PAX
records or characters than the walk keeps, or whose walk seeks back. So is a
chain of extended headers too long for ``tarfile`` to read.
Building one costs up to a fraction of a second, so each is built once, on
first use.
"""

from __future__ import annotations

import functools
import gzip
import io
import tarfile

import pytest

MIB = 1024 * 1024

# The cap the over-cap shapes are refused under.
CAP = 512 * 1024

# How much the sparse over-cap shape decompresses outside member data.
_UNSEEN = 32 * MIB

# Extended-header data over CAP but under the 1 MiB budget on what the walk
# reads between two members, so the cap is what refuses it.
_UNDER_THE_BUDGET = 768 * 1024


def old_gnu_sparse_header(
    name: str, *, stored: int, regions: tuple[tuple[int, int], ...], realsize: int
) -> bytes:
    """An old-GNU sparse header: ``stored`` bytes follow it, ``regions`` map them.

    ``tarfile`` reads sparse members but cannot write them, so the sparse fields
    are patched into a header it built.
    """

    info = tarfile.TarInfo(name=name)
    info.type = tarfile.GNUTYPE_SPARSE
    info.size = stored
    header = bytearray(info.tobuf(format=tarfile.GNU_FORMAT))
    for i, (offset, length) in enumerate(regions):
        header[386 + 24 * i : 410 + 24 * i] = b"%011o\0%011o\0" % (offset, length)
    header[483:495] = b"%011o\0" % realsize
    checksum = sum(header[:148]) + 8 * ord(" ") + sum(header[156:])
    header[148:156] = b"%06o\0 " % checksum
    return bytes(header)


@functools.cache
def sparse_data_not_mapped() -> bytes:
    """One old-GNU sparse member: 32 MiB stored, no data region, a real size of 0.

    Its data is empty, so ``tarfile`` seeks over the 32 MiB to the next header.
    """

    header = old_gnu_sparse_header("s.bin", stored=_UNSEEN, regions=(), realsize=0)
    return gzip.compress(header + bytes(_UNSEEN) + bytes(1024), compresslevel=1)


def _tar_gz(info: tarfile.TarInfo, *, fmt: int) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt) as tar:
        tar.addfile(info, io.BytesIO(b"A"))
    return gzip.compress(buf.getvalue(), compresslevel=1)


@functools.cache
def pax_data(size: int) -> bytes:
    """A one-byte member after ``size`` bytes of PAX extended-header data."""

    info = tarfile.TarInfo(name="a.txt")
    info.size = 1
    info.pax_headers = {"comment": "c" * size}
    return _tar_gz(info, fmt=tarfile.PAX_FORMAT)


@functools.cache
def gnu_longname_data(size: int) -> bytes:
    """A one-byte member whose GNU long name is ``size`` bytes."""

    info = tarfile.TarInfo(name="d/" + "n" * size)
    info.size = 1
    return _tar_gz(info, fmt=tarfile.GNU_FORMAT)


@functools.cache
def sparse_maps_past_stored_data() -> bytes:
    """A 4 MiB member, then 1000 old-GNU sparse members that store nothing.

    Each maps 512 bytes of data, so reading it reads on into the next header,
    which ``tarfile`` then seeks back to. Over ``gzip.GzipFile`` a seek back
    decompresses the stream again from its start.
    """

    big = tarfile.TarInfo(name="big.bin")
    big.size = 4 * MIB
    parts = [big.tobuf(format=tarfile.GNU_FORMAT), bytes(big.size)]
    parts += [
        old_gnu_sparse_header(
            f"s{i:04}.bin", stored=0, regions=((0, 512),), realsize=512
        )
        for i in range(1000)
    ]
    return gzip.compress(b"".join(parts) + bytes(1024))


def _global_pax(records: dict[str, str], members: int) -> bytes:
    """``members`` one-byte members after a global PAX header of ``records``."""

    buf = io.BytesIO()
    with tarfile.open(
        fileobj=buf, mode="w", format=tarfile.PAX_FORMAT, pax_headers=records
    ) as tar:
        for i in range(members):
            info = tarfile.TarInfo(name=f"m{i:05}")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    return gzip.compress(buf.getvalue(), compresslevel=1)


@functools.cache
def global_pax_records() -> bytes:
    """A global PAX header of 33 records: every member holds a copy of each."""

    return _global_pax({f"k{i:02}": "" for i in range(33)}, members=2)


@functools.cache
def global_pax_held_by_many_members() -> bytes:
    """A global record of 768 KiB, under the 1 MiB budget, then 11 members.

    Each member's PAX records are a copy of the global ones, so the walk counts
    the record once a member, more than the 8 Mi characters it allows in all.
    ``tarfile`` shares the record's string among the members, so this
    over-states what they hold, which keeps the bound on the safe side.
    """

    return _global_pax({"comment": "c" * (768 * 1024)}, members=11)


@functools.cache
def gnu_long_links_held_by_many_members() -> bytes:
    """11 regular members, each with a GNU long link of 768 KiB.

    ``tarfile`` holds a long link as the member's ``linkname`` whatever its
    type, and neither door looks at it on a regular member. Each link is under
    the 1 MiB budget; together they are more than the 8 Mi characters the walk
    allows.
    """

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for i in range(11):
            info = tarfile.TarInfo(name=f"m{i:02}")
            info.size = 1
            info.linkname = "l" * (768 * 1024)
            tar.addfile(info, io.BytesIO(b"x"))
    return gzip.compress(buf.getvalue(), compresslevel=1)


# How many extended headers make ``tarfile`` recurse past Python's limit while
# reading one member: about 400 do, 100 do not. 500 of them are 500 KiB, under
# the 1 MiB budget.
_CHAINED_HEADERS = 500


def _block(info: tarfile.TarInfo, data: bytes) -> bytes:
    info.size = len(data)
    return info.tobuf(format=tarfile.GNU_FORMAT) + data.ljust(512, b"\0")


def extended_header_chain(kind: bytes, *, after_a_member: bool) -> bytes:
    """500 extended headers of ``kind`` before ``a.txt``, ``b.txt`` first or not.

    ``kind`` is a GNU long name, or a PAX ``x`` or ``g`` header of one record.
    """

    if kind == tarfile.GNUTYPE_LONGNAME:
        data = b"a.txt\0"
    else:
        data = b"16 comment=abcd\n"
    header = tarfile.TarInfo(name="././@LongLink")
    header.type = kind
    parts = [_block(tarfile.TarInfo(name="b.txt"), b"B")] if after_a_member else []
    parts += [_block(header, data)] * _CHAINED_HEADERS
    parts += [_block(tarfile.TarInfo(name="a.txt"), b"A")]
    return gzip.compress(b"".join(parts) + bytes(1024))


# A chain of each kind of extended header, before the first member and before a
# later one.
EXTENDED_HEADER_CHAINS = [
    pytest.param(
        functools.partial(extended_header_chain, kind, after_a_member=later),
        id=f"{name}-{'later' if later else 'first'}",
    )
    for kind, name in (
        (tarfile.GNUTYPE_LONGNAME, "gnu-longname"),
        (tarfile.XHDTYPE, "pax-x"),
        (tarfile.XGLTYPE, "pax-g"),
    )
    for later in (False, True)
]


def directories(count: int) -> bytes:
    """``count`` directory members, then one file."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for i in range(count):
            info = tarfile.TarInfo(name=f"d{i}")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        info = tarfile.TarInfo(name="a.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"A"))
    return buf.getvalue()


# The shapes CAP refuses: each decompresses past it outside member data.
OVER_CAP = [
    pytest.param(sparse_data_not_mapped, id="sparse-data-not-mapped"),
    pytest.param(functools.partial(pax_data, _UNDER_THE_BUDGET), id="pax-data"),
    pytest.param(
        functools.partial(gnu_longname_data, _UNDER_THE_BUDGET), id="gnu-longname"
    ),
]

_HELD_TOO_MUCH = "more than 8388608 characters of names, link names and PAX records"

# The shapes whose members hold more than the walk keeps, under the production
# caps: more than 32 PAX records in a member, or more than 8 Mi characters of
# names, link names and PAX records in all. Each with the detail both doors
# give it.
OVER_WHAT_MEMBERS_HOLD = [
    pytest.param(
        global_pax_records,
        "a member with more than 32 PAX records",
        id="33-global-pax-records",
    ),
    pytest.param(
        global_pax_held_by_many_members,
        _HELD_TOO_MUCH,
        id="global-record-held-11-times",
    ),
    pytest.param(
        gnu_long_links_held_by_many_members, _HELD_TOO_MUCH, id="gnu-long-links"
    ),
]

# The detail both doors give a shape the 1 MiB budget refuses.
OVER_THE_BUDGET_DETAIL = "more than 1048576 bytes of headers before a member"

# The shapes the 1 MiB budget refuses under the production caps.
OVER_THE_BUDGET = [
    pytest.param(functools.partial(pax_data, 2 * MIB), id="pax-data-2mib"),
    pytest.param(functools.partial(pax_data, 32 * MIB), id="pax-data-32mib"),
    pytest.param(functools.partial(gnu_longname_data, 2 * MIB), id="gnu-longname-2mib"),
    pytest.param(
        functools.partial(gnu_longname_data, 32 * MIB), id="gnu-longname-32mib"
    ),
]
