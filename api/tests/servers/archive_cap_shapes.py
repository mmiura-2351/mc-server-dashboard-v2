"""Archives whose bytes the upload caps did not see (issue #3810).

Both tar.gz upload doors, the file extract and the backup upload, are tested
against each shape. Each one is a ``tar.gz`` that decompresses past a 1 MiB cap
while handing out next to no member data, or whose walk seeks back. Building
one costs a fraction of a second, so each is built once, on first use.
"""

from __future__ import annotations

import functools
import gzip
import io
import tarfile

MIB = 1024 * 1024

# How much each over-cap shape decompresses outside member data.
_UNSEEN = 32 * MIB


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
def pax_data() -> bytes:
    """A one-byte member after 32 MiB of PAX extended-header data."""

    info = tarfile.TarInfo(name="a.txt")
    info.size = 1
    info.pax_headers = {"comment": "c" * _UNSEEN}
    return _tar_gz(info, fmt=tarfile.PAX_FORMAT)


@functools.cache
def gnu_longname_data() -> bytes:
    """A one-byte member whose GNU long name is 32 MiB."""

    info = tarfile.TarInfo(name="d/" + "n" * _UNSEEN)
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


# The shapes a 1 MiB cap refuses: each decompresses 32 MiB outside member data.
OVER_CAP = [sparse_data_not_mapped, pax_data, gnu_longname_data]
