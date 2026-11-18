"""Archives whose bytes the upload caps did not see (issue #3810).

Both tar.gz upload doors, the file extract and the backup upload, are tested
against each shape. Each one is a ``tar.gz`` that decompresses past :data:`CAP`
while handing out next to no member data, holds more extended-header data than
the walk may read between two members (1 MiB), or whose walk seeks back.
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

# The shapes the 1 MiB budget refuses under the production caps.
OVER_THE_BUDGET = [
    pytest.param(functools.partial(pax_data, 2 * MIB), id="pax-data-2mib"),
    pytest.param(functools.partial(pax_data, 32 * MIB), id="pax-data-32mib"),
    pytest.param(functools.partial(gnu_longname_data, 2 * MIB), id="gnu-longname-2mib"),
    pytest.param(
        functools.partial(gnu_longname_data, 32 * MIB), id="gnu-longname-32mib"
    ),
]
