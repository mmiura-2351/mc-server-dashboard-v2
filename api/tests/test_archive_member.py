"""The end-of-archive check, :class:`EndCheckedReader` (issue #3778).

Each archive is walked as the upload gates walk one: ``tarfile.open(mode="r:")``
over the reader, the members through ``members()`` with their data read, then
the read past the marker. Every case runs over the plain tar and over
``gzip.GzipFile``.

This module imports only the standard library, pytest and ``archive_member``,
so it also runs outside the project environment under another interpreter:
``uv run --no-project --python 3.14 --with pytest python -m pytest --noconftest
-o addopts="" tests/test_archive_member.py`` from ``api/`` with ``PYTHONPATH=src``
(the ini's ``addopts`` name plugins a bare environment lacks). CPython changes
``tarfile`` internals in patch releases, and the check must not depend on them.
"""

from __future__ import annotations

import gzip
import io
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from mc_server_dashboard_api.archive_member import EndCheckedReader

_B_DATA = b"B" * 3000  # six records, the last one padded


def _tar(
    members: dict[str, bytes],
    *,
    fmt: int = tarfile.PAX_FORMAT,
    pax: dict[str, dict[str, str]] | None = None,
) -> bytes:
    """An uncompressed tar of ``members``, as ``tarfile`` writes one."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.pax_headers = (pax or {}).get(name, {})
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _flipped(data: bytes, at: int) -> bytes:
    damaged = bytearray(data)
    damaged[at] ^= 0x01
    return bytes(damaged)


# ``a.txt``'s header and data record, then ``b.txt``'s header at 1024 and its
# data up to _B_END, where the end-of-archive marker starts.
_AB = _tar({"a.txt": b"A", "b.txt": _B_DATA})
_B_END = 3 * 512 + 6 * 512


def _gnu_tar_on_path() -> bool:
    if shutil.which("tar") is None:
        return False
    version = subprocess.run(
        ["tar", "--version"], capture_output=True, text=True, timeout=60
    )
    return "GNU tar" in version.stdout


_needs_gnu_tar = pytest.mark.skipif(not _gnu_tar_on_path(), reason="no GNU tar")


def _by_gnu_tar(*options: str) -> Callable[[Path], bytes]:
    """GNU tar's uncompressed archive of ``a.txt`` and a sparse ``s.bin``.

    ``s.bin`` holds eight data regions, more than an old-GNU sparse header
    lists, so ``--sparse`` writes an extension block after its header.
    """

    def produce(tmp_path: Path) -> bytes:
        (tmp_path / "a.txt").write_bytes(b"A")
        with open(tmp_path / "s.bin", "wb") as sparse:
            for region in range(8):
                sparse.seek(region * 65536)
                sparse.write(b"D" * 100)
            sparse.truncate(8 * 65536)
        return subprocess.run(
            ["tar", "-cf", "-", *options, "-C", str(tmp_path), "a.txt", "s.bin"],
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout

    return produce


def _walk(
    archive: bytes, *, gzipped: bool, data: str = "read"
) -> tuple[list[str], int]:
    """The member names, and the bytes read past the end-of-archive marker.

    ``data`` is how much of each member's data the caller reads: all of it
    (``read``), none, so ``tarfile`` seeks over it (``skipped``), or one byte
    (``partly``).
    """

    body = io.BytesIO(gzip.compress(archive) if gzipped else archive)
    stream = EndCheckedReader(gzip.GzipFile(fileobj=body) if gzipped else body)
    names: list[str] = []
    with tarfile.open(fileobj=stream, mode="r:") as tar:
        for member in stream.members(tar):
            names.append(member.name)
            reader = None if data == "skipped" else tar.extractfile(member)
            if reader is not None:
                reader.read(-1 if data == "read" else 1)
    return names, sum(len(chunk) for chunk in stream.read_past_end_marker())


def _names(stream: EndCheckedReader, tar: tarfile.TarFile) -> list[str]:
    return [member.name for member in stream.members(tar)]


_both = pytest.mark.parametrize("gzipped", [False, True], ids=["tar", "tar.gz"])
_each_data_read = pytest.mark.parametrize("data", ["read", "skipped", "partly"])

_LONG = "d/" + "n" * 120  # over ustar's 100 characters

_ACCEPTED: list[object] = [
    pytest.param(lambda _: _AB, ["a.txt", "b.txt"], id="pax"),
    pytest.param(
        lambda _: _tar({_LONG: b"L"}, fmt=tarfile.GNU_FORMAT),
        [_LONG],
        id="gnu-longname",
    ),
    pytest.param(lambda _: _tar({_LONG: b"L"}), [_LONG], id="pax-longname"),
    pytest.param(
        lambda _: _tar({"a.txt": b"A"}, fmt=tarfile.USTAR_FORMAT), ["a.txt"], id="ustar"
    ),
    pytest.param(lambda _: _tar({}), [], id="no-members"),
    # One zero block is taken as the marker: nothing is lost after it, though
    # the format writes two.
    pytest.param(
        lambda _: _AB[: _B_END + 512], ["a.txt", "b.txt"], id="one-zero-block"
    ),
    pytest.param(
        lambda _: _AB[: _B_END + 1024], ["a.txt", "b.txt"], id="no-record-padding"
    ),
    pytest.param(
        lambda _: _tar({"z.bin": b"Z" + bytes(2047)}),
        ["z.bin"],
        id="data-ends-in-zeros",
    ),
    pytest.param(_by_gnu_tar(), ["a.txt", "s.bin"], id="gnu-tar", marks=_needs_gnu_tar),
    pytest.param(
        _by_gnu_tar("-b", "1"),
        ["a.txt", "s.bin"],
        id="gnu-tar-b1",
        marks=_needs_gnu_tar,
    ),
    pytest.param(
        _by_gnu_tar("-b", "64"),
        ["a.txt", "s.bin"],
        id="gnu-tar-b64",
        marks=_needs_gnu_tar,
    ),
    pytest.param(
        _by_gnu_tar("--format=gnu", "--sparse"),
        ["a.txt", "s.bin"],
        id="gnu-tar-sparse",
        marks=_needs_gnu_tar,
    ),
]


@_both
@_each_data_read
@pytest.mark.parametrize(("produce", "names"), _ACCEPTED)
def test_an_archive_that_ends_at_its_marker_reads_whole(
    produce: Callable[[Path], bytes],
    names: list[str],
    gzipped: bool,
    data: str,
    tmp_path: Path,
) -> None:
    walk = _walk(produce(tmp_path), gzipped=gzipped, data=data)

    assert walk[0] == names


def _with_a_malformed_pax_record() -> bytes:
    archive = _tar({"a.txt": b"A", "b.txt": b"B"}, pax={"b.txt": {"comment": "hi"}})
    record = archive.index(b" comment=hi")
    return archive[: record - 2] + b"xx" + archive[record:]


def _with_a_bad_sparse_extension(tmp_path: Path) -> bytes:
    archive = _by_gnu_tar("--format=gnu", "--sparse")(tmp_path)
    extension = 3 * 512  # after a.txt's two records and s.bin's header
    return archive[:extension] + b"z" * 24 + archive[extension + 24 :]


_PAX_B = _tar({"a.txt": b"A", _LONG: b"B"})
_GNU_B = _tar({"a.txt": b"A", _LONG: b"B"}, fmt=tarfile.GNU_FORMAT)


def _header(
    name: str,
    *,
    typ: bytes = b"0",
    size: int = 0,
    size_field: bytes | None = None,
    sparse: tuple[tuple[int, int], ...] = (),
    isextended: bool = False,
    realsize: int = 0,
) -> bytes:
    """One header block built by hand, its checksum valid.

    ``sparse``, ``isextended`` and ``realsize`` are an old-GNU sparse header's
    map (offset and size of each data region), extension flag and real size.
    """

    block = bytearray(512)
    block[: len(name)] = name.encode()
    block[124:136] = size_field or b"%011o\0" % size
    block[156:157] = typ
    block[257:265] = b"ustar  \0"
    for i, (offset, length) in enumerate(sparse):
        block[386 + 24 * i : 410 + 24 * i] = b"%011o\0%011o\0" % (offset, length)
    block[482] = isextended  # an old-GNU sparse extension block follows
    if realsize:
        block[483:495] = b"%011o\0" % realsize
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\0 " % sum(block)
    return bytes(block)


def _record(key: str, value: str) -> bytes:
    """One PAX record, ``<length> <key>=<value>\\n``, its length counting itself."""

    body = f" {key}={value}\n".encode()
    length = len(body) + 1
    while len(str(length)) + len(body) != length:
        length = len(str(length)) + len(body)
    return str(length).encode() + body


def _pax(records: dict[str, str]) -> bytes:
    data = b"".join(_record(key, value) for key, value in records.items())
    return _header("PaxHeader", typ=b"x", size=len(data)) + data.ljust(512, b"\0")


_A = _header("a.txt", size=1) + b"A".ljust(512, b"\0")
_ZEROS = bytes(4 * 512)
_MIB = 1024 * 1024
# A negative size, in base-256. tarfile reads the sparse extension blocks first
# and only then refuses it.
_SPARSE_NEGATIVE = _header(
    "s.bin", typ=b"S", size_field=b"\xff" * 11 + b"\0", isextended=True
)
_SPARSE = _header("s.bin", typ=b"S", isextended=True)
# One sparse region, and another extension block after this one.
_EXTENSION = (b"%011o\0%011o\0" % (512, 512)).ljust(504, b"\0") + b"\x01".ljust(
    8, b"\0"
)
# Over 1 MiB, which newer CPython reads in 1 MiB parts: a malformed first record,
# and a last part of 512 zeros.
_LARGE_PAX_DATA = b"xx comment=hi\n".ljust(_MIB, b"j") + bytes(512)

_REFUSED: list[object] = [
    # tarfile reads a header that does not parse, past the first, as the end.
    pytest.param(lambda _: _flipped(_AB, 1024), id="damaged-second-header"),
    pytest.param(
        lambda _: _flipped(_tar({"a.txt": b"A", "e.txt": b""}), 1024),
        id="damaged-last-header",  # only zeros follow it
    ),
    pytest.param(lambda _: _AB[:_B_END], id="no-end-marker"),
    pytest.param(
        lambda _: _tar({"z.bin": b"Z" + bytes(2047)})[: 512 + 2048],
        id="no-end-marker-after-zeros",
    ),
    pytest.param(lambda _: _AB[: _B_END + 100], id="cut-end-marker"),
    pytest.param(lambda _: _AB + _tar({"c.txt": b"C"}), id="second-tar-past-marker"),
    pytest.param(lambda _: _flipped(_AB, len(_AB) - 1), id="byte-in-record-padding"),
    pytest.param(lambda _: _with_a_malformed_pax_record(), id="malformed-pax-record"),
    pytest.param(lambda _: _flipped(_PAX_B, 1024), id="damaged-pax-header"),
    pytest.param(lambda _: _flipped(_PAX_B, 2048), id="damaged-header-after-pax"),
    pytest.param(lambda _: _flipped(_GNU_B, 2048), id="damaged-header-after-longname"),
    pytest.param(
        lambda _: _tar(
            {"a.txt": b"A", "s.bin": b"S"},
            pax={
                "s.bin": {
                    "GNU.sparse.size": "1",
                    "GNU.sparse.numblocks": "1",
                    "GNU.sparse.offset": "x",
                    "GNU.sparse.numbytes": "1",
                }
            },
        ),
        id="bad-sparse-offset",
    ),
    pytest.param(
        _with_a_bad_sparse_extension, id="bad-sparse-extension", marks=_needs_gnu_tar
    ),
    # No marker, after member data that ends in a block of zeros.
    pytest.param(lambda _: _tar({"z.bin": bytes(512)})[:1024], id="zero-block-data"),
    # A marker one byte short. The sparse member's data is read as 1 byte, then
    # 512 zeros; after it the walk reads 1 byte, then the 511 left: 512 in all,
    # so only the last read, 511 bytes, shows the marker is cut.
    pytest.param(
        lambda _: (
            _header(
                "s.bin",
                typ=b"S",
                size=513,
                sparse=((0, 1), (1000, 512)),
                realsize=4096,
            )
            + b"X".ljust(1024, b"\0")
            + bytes(511)
        ),
        id="cut-marker-after-sparse-data",
    ),
    # Headers built with a valid checksum that tarfile gives up on only after
    # reading a block of zeros: its last read alone looks like the marker.
    pytest.param(lambda _: _A + _SPARSE_NEGATIVE + _ZEROS, id="sparse-negative-size"),
    pytest.param(
        lambda _: _A + _SPARSE_NEGATIVE + _EXTENSION + _ZEROS,
        id="sparse-negative-size-after-extension",
    ),
    pytest.param(
        lambda _: (
            _A
            + _pax({"GNU.sparse.size": "1", "GNU.sparse.offset": "x"})
            + _SPARSE
            + _ZEROS
        ),
        id="pax-bad-sparse-offset-then-sparse",
    ),
    pytest.param(
        lambda _: _A + _pax({"size": "-5"}) + _SPARSE + _ZEROS,
        id="pax-negative-size-then-sparse",
    ),
    pytest.param(
        lambda _: (
            _A
            + _header("PaxHeader", typ=b"x", size=len(_LARGE_PAX_DATA))
            + _LARGE_PAX_DATA
            + _ZEROS
        ),
        id="large-pax-malformed",
    ),
    pytest.param(
        lambda _: (
            _A
            + _header("GlobalHead", typ=b"g", size=len(_LARGE_PAX_DATA))
            + _LARGE_PAX_DATA
            + _ZEROS
        ),
        id="large-global-pax-malformed",
    ),
]


@_both
@_each_data_read
@pytest.mark.parametrize("produce", _REFUSED)
def test_an_archive_that_does_not_end_at_its_marker_is_refused(
    produce: Callable[[Path], bytes], gzipped: bool, data: str, tmp_path: Path
) -> None:
    with pytest.raises(tarfile.ReadError):
        _walk(produce(tmp_path), gzipped=gzipped, data=data)


@_both
def test_a_walk_that_seeks_back_is_refused(gzipped: bool) -> None:
    # A GNU sparse map that takes more data than its member stores: reading the
    # data runs on into the next member, and tarfile then seeks back to that
    # member's header (issue #3798).
    sparse = {"GNU.sparse.size": "2048", "GNU.sparse.map": "0,2048"}
    archive = _tar({"s.bin": b"S", "b.txt": _B_DATA}, pax={"s.bin": sparse})

    with pytest.raises(tarfile.ReadError, match="runs past the member"):
        _walk(archive, gzipped=gzipped)


@_both
def test_the_zeros_past_the_marker_are_yielded_to_the_caller(gzipped: bool) -> None:
    # tarfile pads to a 20-record block; the walk stops after the marker's first
    # zero block, and every byte after it is read and yielded for the cap.
    archive = _tar({"a.txt": b"A"})

    assert _walk(archive, gzipped=gzipped) == (["a.txt"], len(archive) - 3 * 512)


_TAR_GZ = gzip.compress(_AB)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        pytest.param(_flipped(_TAR_GZ, len(_TAR_GZ) - 8), gzip.BadGzipFile, id="crc"),
        pytest.param(_flipped(_TAR_GZ, len(_TAR_GZ) - 4), gzip.BadGzipFile, id="isize"),
        pytest.param(_TAR_GZ[:-1], EOFError, id="cut-trailer"),
        pytest.param(_TAR_GZ + b"not gzip", gzip.BadGzipFile, id="garbage-after"),
    ],
)
def test_reading_past_the_marker_checks_the_gzip_trailer(
    body: bytes, error: type[Exception]
) -> None:
    stream = EndCheckedReader(gzip.GzipFile(fileobj=io.BytesIO(body)))
    with tarfile.open(fileobj=stream, mode="r:") as tar:
        assert _names(stream, tar) == ["a.txt", "b.txt"]
        with pytest.raises(error):
            for _chunk in stream.read_past_end_marker():
                pass


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_TAR_GZ + bytes(1024), id="zero-padding"),
        pytest.param(
            gzip.compress(_AB[:1024]) + gzip.compress(_AB[1024:]), id="two-members"
        ),
    ],
)
def test_a_gzip_stream_gzip_itself_reads_is_accepted(body: bytes) -> None:
    stream = EndCheckedReader(gzip.GzipFile(fileobj=io.BytesIO(body)))
    with tarfile.open(fileobj=stream, mode="r:") as tar:
        assert _names(stream, tar) == ["a.txt", "b.txt"]
        assert sum(len(chunk) for chunk in stream.read_past_end_marker()) > 0
