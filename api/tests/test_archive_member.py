"""The end-of-archive check, :class:`EndCheckedReader` (issue #3778), the cap
beneath it, :class:`CappedReader` (issue #3810), and the gzip stream beneath
both, :class:`GzipReader` (issue #3835).

Each archive is walked as the upload gates walk one: ``tarfile.open(mode="r:")``
over the reader, the members through ``members()`` with their data read, then
the read past the marker. Every case runs over the plain tar and over
:class:`GzipReader`.

This module imports only the standard library, pytest, ``archive_member`` and
``tests.gzip_shapes`` (standard library only too), so it also runs outside the
project environment under another interpreter:
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
import zlib
from collections.abc import Callable
from pathlib import Path

import pytest

from mc_server_dashboard_api.archive_member import (
    STORAGE_HEADER_LIMITS,
    UPLOAD_HEADER_LIMITS,
    CapExceededError,
    CappedReader,
    EndCheckedReader,
    GzipReader,
    HeaderLimitError,
    HeaderLimits,
)
from tests import gzip_shapes

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
    archive: bytes,
    *,
    gzipped: bool,
    data: str = "read",
    cap: int | None = None,
    header_limits: HeaderLimits | None = None,
) -> tuple[list[str], int]:
    """The member names, and the bytes read past the end-of-archive marker.

    ``data`` is how much of each member's data the caller reads: all of it
    (``read``), none, so ``tarfile`` seeks over it (``skipped``), or one byte
    (``partly``). With a ``cap``, the stream is read through a
    :class:`CappedReader`, and with ``header_limits`` the walk is held to them,
    as the upload gates and the storage walks read it.
    """

    body = io.BytesIO(gzip.compress(archive) if gzipped else archive)
    source = GzipReader(body) if gzipped else body
    stream = EndCheckedReader(
        source if cap is None else CappedReader(source, cap),
        header_limits=header_limits,
    )
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
    # Member data is read on the caller's turn, so it is not held to the budget
    # on what the walk reads between two members (issue #3810).
    pytest.param(
        lambda _: _tar({"big.bin": b"B" * (2 * _MIB)}),
        ["big.bin"],
        id="member-over-the-header-budget",
    ),
]


# The walks with each set of header limits: the snapshot ingest's (none), the
# storage walks' and the upload gates'.
_each_walk = pytest.mark.parametrize(
    "header_limits",
    [None, STORAGE_HEADER_LIMITS, UPLOAD_HEADER_LIMITS],
    ids=["ingest", "storage", "upload"],
)
# The two sets of header limits, which share the budget and the record cap.
_each_limits = pytest.mark.parametrize(
    "limits", [STORAGE_HEADER_LIMITS, UPLOAD_HEADER_LIMITS], ids=["storage", "upload"]
)


@_both
@_each_data_read
@_each_walk
@pytest.mark.parametrize(("produce", "names"), _ACCEPTED)
def test_an_archive_that_ends_at_its_marker_reads_whole(
    produce: Callable[[Path], bytes],
    names: list[str],
    gzipped: bool,
    data: str,
    header_limits: HeaderLimits | None,
    tmp_path: Path,
) -> None:
    # The snapshot ingest, the storage walks and the upload gates alike.
    walk = _walk(
        produce(tmp_path), gzipped=gzipped, data=data, header_limits=header_limits
    )

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
@pytest.mark.parametrize("capped", [False, True], ids=["uncapped", "capped"])
@pytest.mark.parametrize("produce", _REFUSED)
def test_an_archive_that_does_not_end_at_its_marker_is_refused(
    produce: Callable[[Path], bytes],
    capped: bool,
    gzipped: bool,
    data: str,
    tmp_path: Path,
) -> None:
    # Capped, the stream is read as the upload gates read it, through a
    # CappedReader whose cap the archive stays within.
    archive = produce(tmp_path)
    with pytest.raises(tarfile.ReadError):
        _walk(archive, gzipped=gzipped, data=data, cap=len(archive) if capped else None)


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
        pytest.param(_flipped(_TAR_GZ, len(_TAR_GZ) - 8), zlib.error, id="crc"),
        pytest.param(_flipped(_TAR_GZ, len(_TAR_GZ) - 4), zlib.error, id="isize"),
        pytest.param(_TAR_GZ[:-1], EOFError, id="cut-trailer"),
        pytest.param(_TAR_GZ + b"not gzip", zlib.error, id="garbage-after"),
    ],
)
def test_reading_past_the_marker_checks_the_gzip_trailer(
    body: bytes, error: type[Exception]
) -> None:
    stream = EndCheckedReader(GzipReader(io.BytesIO(body)))
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
    stream = EndCheckedReader(GzipReader(io.BytesIO(body)))
    with tarfile.open(fileobj=stream, mode="r:") as tar:
        assert _names(stream, tar) == ["a.txt", "b.txt"]
        assert sum(len(chunk) for chunk in stream.read_past_end_marker()) > 0


# --- the cap beneath the walk, CappedReader (issue #3810) -------------------
#
# Shapes whose bytes ``tarfile`` reads or skips without handing them out as
# member data: an old-GNU sparse member's stored data when its map takes none of
# it, and extended-header data.
_UNMAPPED = 64 * 1024
_SPARSE_UNMAPPED = _header("s.bin", typ=b"S", size=_UNMAPPED) + bytes(_UNMAPPED)
_LARGE_PAX_COMMENT = {"a.txt": {"comment": "c" * _UNMAPPED}}
_LARGE_LONGNAME = "d/" + "n" * _UNMAPPED

_COUNTED: list[object] = [
    *_ACCEPTED,
    pytest.param(
        lambda _: _SPARSE_UNMAPPED + _ZEROS, ["s.bin"], id="sparse-data-not-mapped"
    ),
    pytest.param(
        lambda _: _tar({"a.txt": b"A"}, pax=_LARGE_PAX_COMMENT),
        ["a.txt"],
        id="large-pax-data",
    ),
    pytest.param(
        lambda _: _tar({_LARGE_LONGNAME: b"L"}, fmt=tarfile.GNU_FORMAT),
        [_LARGE_LONGNAME],
        id="large-gnu-longname",
    ),
]


@_both
@_each_data_read
@pytest.mark.parametrize(("produce", "names"), _COUNTED)
def test_every_byte_of_the_stream_counts_toward_the_cap(
    produce: Callable[[Path], bytes],
    names: list[str],
    gzipped: bool,
    data: str,
    tmp_path: Path,
) -> None:
    # The stream is the uncompressed tar. Each of its bytes counts, whether the
    # walk reads it, skips it or reads it past the marker, so a cap of its length
    # takes the archive and one byte less refuses it.
    archive = produce(tmp_path)

    def walk(cap: int) -> list[str]:
        walked, _ = _walk(
            archive,
            gzipped=gzipped,
            data=data,
            cap=cap,
            header_limits=UPLOAD_HEADER_LIMITS,
        )
        return walked

    assert walk(len(archive)) == names
    with pytest.raises(CapExceededError):
        walk(len(archive) - 1)


def test_a_capped_reader_refuses_a_seek_back() -> None:
    # It cannot take back what it counted. Under EndCheckedReader, which refuses a
    # seek back first (issue #3798), a walk never asks it to.
    reader = CappedReader(io.BytesIO(bytes(4096)), 4096)
    reader.read(1024)

    with pytest.raises(tarfile.ReadError, match="seeks back"):
        reader.seek(512)


def test_a_seek_other_than_from_the_start_is_refused() -> None:
    # tarfile seeks with SEEK_SET only (issue #3798).
    stream = EndCheckedReader(io.BytesIO(bytes(4096)))

    with pytest.raises(tarfile.ReadError, match="other than from the start"):
        stream.seek(0, io.SEEK_CUR)


@pytest.mark.parametrize("size", [-1, 4096])
def test_a_read_takes_one_byte_past_the_cap_at_most(size: int) -> None:
    source = io.BytesIO(bytes(4096))
    reader = CappedReader(source, 100)

    with pytest.raises(CapExceededError):
        reader.read(size)
    assert source.tell() == 101


def test_a_skip_takes_one_byte_past_the_cap_at_most() -> None:
    source = io.BytesIO(bytes(4096))
    reader = CappedReader(source, 100)

    with pytest.raises(CapExceededError):
        reader.seek(4096)
    assert source.tell() == 101


def test_a_skip_past_the_end_stops_at_the_end() -> None:
    reader = CappedReader(io.BytesIO(bytes(10)), 100)

    assert reader.seek(50) == 10
    assert reader.read(1) == b""


class _Recording(io.BytesIO):
    """A source that records the size of every read asked of it."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.sizes: list[int] = []

    def read(self, size: int | None = -1, /) -> bytes:
        self.sizes.append(-1 if size is None else size)
        return super().read(size)


def test_a_skip_is_read_in_chunks() -> None:
    source = _Recording(bytes(8 * _MIB))
    reader = CappedReader(source, 8 * _MIB)

    assert reader.seek(4 * _MIB) == 4 * _MIB
    assert 0 < max(source.sizes) <= _MIB


# --- the budget on what the walk reads between two members (issue #3810) ----
#
# ``tarfile`` holds an extended header's data whole, so EndCheckedReader bounds
# what the walk may read between handing out one member and the next: 1 MiB.


def _pax_of(length: int) -> bytes:
    """A PAX extended header whose data is one record of exactly ``length`` bytes."""

    value = "c" * length
    while len(record := _record("comment", value)) != length:
        value = value[: len(value) - (len(record) - length)]
    return _header("PaxHeader", typ=b"x", size=length) + record + bytes(-length % 512)


def _global_pax_of(length: int) -> bytes:
    block = bytearray(_pax_of(length))
    block[:512] = _header("GlobalHead", typ=b"g", size=length)
    return bytes(block)


_OVER_BUDGET = 2 * _MIB

_OVER_THE_BUDGET: list[object] = [
    pytest.param(lambda: _pax_of(_OVER_BUDGET) + _A, id="pax-data"),
    pytest.param(lambda: _A + _pax_of(_OVER_BUDGET) + _A, id="pax-data-after-a-member"),
    pytest.param(lambda: _A + _global_pax_of(_OVER_BUDGET) + _A, id="global-pax-data"),
    pytest.param(
        lambda: _A + _tar({"d/" + "n" * _OVER_BUDGET: b"L"}, fmt=tarfile.GNU_FORMAT),
        id="gnu-longname-after-a-member",
    ),
]


@_both
@_each_data_read
@_each_limits
@pytest.mark.parametrize("build", _OVER_THE_BUDGET)
def test_what_the_walk_reads_between_two_members_is_bounded(
    build: Callable[[], bytes], limits: HeaderLimits, gzipped: bool, data: str
) -> None:
    archive = build() + _ZEROS

    # The budget's own refusal, not tarfile's reaction to the reads it clamped.
    with pytest.raises(HeaderLimitError, match="headers before a member"):
        _walk(
            archive, gzipped=gzipped, data=data, cap=len(archive), header_limits=limits
        )
    # Without the header limits, as the snapshot ingest reads it, it is taken.
    assert _walk(archive, gzipped=gzipped, data=data, cap=len(archive))[0]


@_both
def test_the_budget_takes_exactly_one_mib_between_two_members(gzipped: bool) -> None:
    # The first member's reads: the PAX header block, its data and a.txt's header.
    at_the_budget = _pax_of(_MIB - 1024) + _A + _ZEROS
    # One byte more PAX data is one more block.
    over_the_budget = _pax_of(_MIB - 1023) + _A + _ZEROS

    limits = UPLOAD_HEADER_LIMITS
    assert _walk(at_the_budget, gzipped=gzipped, header_limits=limits)[0] == ["a.txt"]
    with pytest.raises(HeaderLimitError, match="headers before a member"):
        _walk(over_the_budget, gzipped=gzipped, header_limits=limits)


def test_an_extended_header_is_read_one_byte_past_the_budget_at_most() -> None:
    # The header claims 64 MiB of PAX data: tarfile asks for it in one read
    # before 3.13.15 and in 1 MiB parts after.
    source = _Recording(_pax_of(64 * _MIB) + _A + _ZEROS)
    stream = EndCheckedReader(source, header_limits=UPLOAD_HEADER_LIMITS)

    with pytest.raises(HeaderLimitError, match="headers before a member"):
        with tarfile.open(fileobj=stream, mode="r:") as tar:
            _names(stream, tar)
    assert source.tell() == _MIB + 1


# --- what the members handed out hold (issue #3810) -------------------------
#
# ``tarfile`` keeps every member it hands out, with its name, its link name and
# a copy of the global PAX records dict. So a member may hold at most 32 PAX
# records, global and its own, and the members' names, link names, PAX keys and
# PAX values together at most 8 Mi characters.
_HELD = 8 * _MIB


def _pax_tar(
    members: dict[str, dict[str, str]], *, global_records: dict[str, str] | None = None
) -> bytes:
    """A PAX tar of one-byte members, each with its own PAX records."""

    buf = io.BytesIO()
    with tarfile.open(
        fileobj=buf, mode="w", format=tarfile.PAX_FORMAT, pax_headers=global_records
    ) as tar:
        for name, records in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = 1
            info.pax_headers = records
            tar.addfile(info, io.BytesIO(b"x"))
    return buf.getvalue()


def _records(count: int, prefix: str = "k") -> dict[str, str]:
    return {f"{prefix}{i:02}": "" for i in range(count)}


@_both
@_each_limits
@pytest.mark.parametrize(
    ("archive", "refused"),
    [
        pytest.param(_pax_tar({"a": _records(32)}), False, id="32-own"),
        pytest.param(_pax_tar({"a": _records(33)}), True, id="33-own"),
        pytest.param(
            _pax_tar({"a": {}, "b": {}}, global_records=_records(32)),
            False,
            id="32-global",
        ),
        pytest.param(
            _pax_tar({"a": {}, "b": {}}, global_records=_records(33)),
            True,
            id="33-global",
        ),
        # The global records are in every later member's copy of them.
        pytest.param(
            _pax_tar({"a": _records(17, "m")}, global_records=_records(16)),
            True,
            id="16-global-and-17-own",
        ),
    ],
)
def test_a_member_holds_at_most_32_pax_records(
    archive: bytes, refused: bool, limits: HeaderLimits, gzipped: bool
) -> None:
    if refused:
        with pytest.raises(HeaderLimitError, match="PAX records"):
            _walk(archive, gzipped=gzipped, header_limits=limits)
    else:
        assert _walk(archive, gzipped=gzipped, header_limits=limits)[0]
    # Without the header limits, as the snapshot ingest reads it, it is taken.
    assert _walk(archive, gzipped=gzipped)[0]


def _held_by_values(extra: int) -> bytes:
    """16 members named ``m00`` to ``m15``, each holding 1/16 of 8 Mi characters.

    A member holds its name, 3 characters, and one ``comment`` record. ``extra``
    more characters go into the last value.
    """

    value = _HELD // 16 - len("m00") - len("comment")
    return _pax_tar(
        {
            f"m{i:02}": {"comment": "c" * (value + (extra if i == 15 else 0))}
            for i in range(16)
        }
    )


def _held_by_a_global_record(members: int) -> bytes:
    """A global record of 1/16 of 8 Mi characters, held by each of ``members``."""

    value = _HELD // 16 - len("m00") - len("comment")
    return _pax_tar(
        {f"m{i:02}": {} for i in range(members)},
        global_records={"comment": "c" * value},
    )


def _held_by_link_names(extra: int) -> bytes:
    """16 regular members ``00`` to ``15`` whose GNU long links take the rest of
    1/16 of 8 Mi characters each; ``tarfile`` holds a long link as the member's
    ``linkname`` whatever its type."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for i in range(16):
            info = tarfile.TarInfo(name=f"{i:02}")
            info.size = 1
            info.linkname = "l" * (_HELD // 16 - 2 + (extra if i == 15 else 0))
            tar.addfile(info, io.BytesIO(b"x"))
    return buf.getvalue()


def _held_by_names(extra: int) -> bytes:
    """16 members whose GNU long names are 1/16 of 8 Mi characters each."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for i in range(16):
            info = tarfile.TarInfo(
                name=f"{i:02}".ljust(_HELD // 16 + (extra if i == 15 else 0), "n")
            )
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    return buf.getvalue()


@_both
@pytest.mark.parametrize(
    ("build", "refused"),
    [
        pytest.param(lambda: _held_by_values(0), False, id="values-at-the-bound"),
        pytest.param(lambda: _held_by_values(1), True, id="values-one-over"),
        pytest.param(lambda: _held_by_a_global_record(16), False, id="global-16-times"),
        pytest.param(lambda: _held_by_a_global_record(17), True, id="global-17-times"),
        pytest.param(lambda: _held_by_names(0), False, id="names-at-the-bound"),
        pytest.param(lambda: _held_by_names(1), True, id="names-one-over"),
        pytest.param(lambda: _held_by_link_names(0), False, id="links-at-the-bound"),
        pytest.param(lambda: _held_by_link_names(1), True, id="links-one-over"),
    ],
)
def test_the_members_hold_at_most_8_mi_characters_of_names_and_pax_records(
    build: Callable[[], bytes], refused: bool, gzipped: bool
) -> None:
    archive = build()
    if refused:
        with pytest.raises(HeaderLimitError, match="characters of names"):
            _walk(archive, gzipped=gzipped, header_limits=UPLOAD_HEADER_LIMITS)
    else:
        walked = _walk(archive, gzipped=gzipped, header_limits=UPLOAD_HEADER_LIMITS)
        assert len(walked[0]) >= 16
    # Without the header limits, as the snapshot ingest reads it, it is taken.
    assert len(_walk(archive, gzipped=gzipped)[0]) >= 16


def _held_by_many_values(extra: int) -> bytes:
    """16 members ``m00`` to ``m15`` holding together exactly what the storage
    walks allow 16 members: 8 Mi characters, plus 16 Ki a member (issue #3859).

    ``extra`` more characters go into the last value.
    """

    members = 16
    total = _HELD + STORAGE_HEADER_LIMITS.characters_a_member * members
    own = len("m00") + len("comment")
    value, rest = divmod(total - members * own, members)
    return _pax_tar(
        {
            f"m{i:02}": {
                "comment": "c" * (value + (rest + extra if i == members - 1 else 0))
            }
            for i in range(members)
        }
    )


@_both
@pytest.mark.parametrize("extra", [0, 1], ids=["at-the-bound", "one-over"])
def test_the_storage_walks_allow_16_ki_characters_more_a_member(
    extra: int, gzipped: bool
) -> None:
    archive = _held_by_many_values(extra)

    if extra:
        with pytest.raises(HeaderLimitError, match="plus 16384 a member"):
            _walk(archive, gzipped=gzipped, header_limits=STORAGE_HEADER_LIMITS)
    else:
        walked = _walk(archive, gzipped=gzipped, header_limits=STORAGE_HEADER_LIMITS)
        assert len(walked[0]) == 16
    # Past the upload gates' 8 Mi characters either way.
    with pytest.raises(HeaderLimitError, match="characters of names"):
        _walk(archive, gzipped=gzipped, header_limits=UPLOAD_HEADER_LIMITS)


def _named_at_path_max(members: int) -> bytes:
    """``members`` one-byte members whose names are 4095 characters, the longest
    a Linux path is, each with the ``mtime`` record ``tarfile`` writes for a file
    it adds from disk, whose time is a float."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for i in range(members):
            info = tarfile.TarInfo(name=f"{i:04}/".ljust(4095, "n"))
            info.size = 1
            info.mtime = 1_795_019_864.1156616
            tar.addfile(info, io.BytesIO(b"x"))
    return buf.getvalue()


@_both
def test_the_storage_walks_take_many_members_named_at_path_max(gzipped: bool) -> None:
    # A backup writer's member holds its name twice, as the name and as its PAX
    # path record, and a few short records: under 9 Ki characters at Linux's
    # PATH_MAX. Restore and the health check have no entry cap, so the storage
    # walks take any number of them (issue #3859); 1,100 are past the upload
    # gates' 8 Mi characters.
    archive = _named_at_path_max(1100)

    walked = _walk(archive, gzipped=gzipped, header_limits=STORAGE_HEADER_LIMITS)

    assert len(walked[0]) == 1100
    with pytest.raises(HeaderLimitError, match="characters of names"):
        _walk(archive, gzipped=gzipped, header_limits=UPLOAD_HEADER_LIMITS)


def _sparse_after_global_records(windows: list[dict[str, str]]) -> bytes:
    """Each window: a global PAX header of its records, then an old-GNU sparse
    member that stores and maps nothing.

    ``tarfile`` copies the global records into a member's own for most member
    types, but not into an old-GNU sparse (``S``) one: its own stay empty, while
    ``TarFile.pax_headers`` keeps every global record.
    """

    parts = []
    for i, records in enumerate(windows):
        data = b"".join(_record(key, value) for key, value in records.items())
        parts += [
            _header("GlobalHead", typ=b"g", size=len(data)),
            data + bytes(-len(data) % 512),
            _header(f"s{i}.bin", typ=b"S"),
        ]
    return b"".join(parts) + _ZEROS


@_both
@_each_limits
@pytest.mark.parametrize(
    ("windows", "match"),
    [
        # 60,000 records a window, each window under the budget.
        pytest.param(
            [{f"k{w}_{i:05}": "v" for i in range(60_000)} for w in range(2)],
            "PAX records",
            id="records",
        ),
        # One record of about 1 Mi characters a window, a new key each time, so
        # the record count stays low.
        pytest.param(
            [{f"k{w}": "\U0001f600" + "a" * (1000 * 1024)} for w in range(4)],
            "characters of names",
            id="characters",
        ),
    ],
)
def test_each_member_holds_the_global_records_whatever_its_type(
    windows: list[dict[str, str]], match: str, limits: HeaderLimits, gzipped: bool
) -> None:
    archive = _sparse_after_global_records(windows)

    with pytest.raises(HeaderLimitError, match=match):
        _walk(archive, gzipped=gzipped, header_limits=limits)
    # Without the header limits, as the snapshot ingest reads it, it is taken.
    assert len(_walk(archive, gzipped=gzipped)[0]) == len(windows)


@pytest.mark.parametrize(
    ("build", "handed_out"),
    [
        pytest.param(
            lambda: _pax_tar({"a": {}, "b": _records(33)}), ["a"], id="records"
        ),
        pytest.param(
            lambda: _held_by_values(1),
            [f"m{i:02}" for i in range(15)],
            id="characters",
        ),
    ],
)
def test_a_member_is_checked_before_it_is_handed_out(
    build: Callable[[], bytes], handed_out: list[str]
) -> None:
    # The member over a bound is refused before the caller gets it, so the
    # caller never reads its data.
    stream = EndCheckedReader(io.BytesIO(build()), header_limits=UPLOAD_HEADER_LIMITS)
    names: list[str] = []

    with pytest.raises(HeaderLimitError):
        with tarfile.open(fileobj=stream, mode="r:") as tar:
            for member in stream.members(tar):
                names.append(member.name)
    assert names == handed_out


# --- the gzip stream beneath both, GzipReader (issue #3835) -----------------
#
# zlib's gzip mode reads each member's header and checks its trailer. The
# reader walks the members, skips zero padding after each, and holds a stream
# to 10,000 members. ``tarfile`` takes a short read for the end of the data, so
# a read returns all it asks for until the stream ends.

# What zlib or the reader raises for a stream that is not one or more gzip
# members with only zero padding after each.
_NOT_GZIP = (zlib.error, EOFError, gzip.BadGzipFile)


def _decompressed(body: bytes, size: int = _MIB) -> bytes:
    reader = GzipReader(io.BytesIO(body))
    return b"".join(iter(lambda: reader.read(size), b""))


_MEMBER_CUTS = {
    "one-member": [],
    "cut-inside-a-header-block": [1024 + 100],
    "cut-inside-member-data": [1536 + 1000],
    "cut-inside-the-end-marker": [_B_END + 100],
    "a-member-a-block": list(range(512, len(_AB), 512)),
}


@pytest.mark.parametrize("padding", [0, 700], ids=["unpadded", "zero-padded"])
@pytest.mark.parametrize("size", [1, 511, 512, 513, 65536])
@pytest.mark.parametrize("cuts", list(_MEMBER_CUTS.values()), ids=list(_MEMBER_CUTS))
def test_a_read_returns_all_it_asks_for_until_the_stream_ends(
    cuts: list[int], size: int, padding: int
) -> None:
    # The members' data adds up to the tar, 20 blocks: with a read of 512, the
    # last read that returns data ends where the last member ends.
    members = gzip_shapes.in_members(_AB, cuts)
    reader = GzipReader(io.BytesIO(bytes(padding).join(members) + bytes(padding)))

    reads = list(iter(lambda: reader.read(size), b""))

    assert b"".join(reads) == _AB
    assert {len(read) for read in reads[:-1]} <= {size}


def test_a_seek_reads_forward_to_its_offset() -> None:
    reader = GzipReader(io.BytesIO(b"".join(gzip_shapes.in_members(_AB, [1000]))))

    assert reader.seek(1536) == 1536
    assert reader.read(512) == _AB[1536:2048]
    assert reader.tell() == 2048


def test_a_seek_back_is_refused() -> None:
    reader = GzipReader(io.BytesIO(_TAR_GZ))
    reader.read(1024)

    with pytest.raises(io.UnsupportedOperation):
        reader.seek(512)


@pytest.mark.parametrize(
    ("members", "refused"),
    [(10_000, False), (10_001, True)],
    ids=["10000-members", "10001-members"],
)
def test_a_stream_holds_at_most_10000_gzip_members(members: int, refused: bool) -> None:
    body = _TAR_GZ + gzip_shapes.EMPTY_MEMBER * (members - 1)

    if refused:
        with pytest.raises(gzip.BadGzipFile, match="more than 10000 gzip members"):
            _decompressed(body)
    else:
        assert _decompressed(body) == _AB


def test_zero_padding_is_read_in_bulk() -> None:
    # gzip.GzipFile reads zero padding after a member one byte at a time.
    source = _Recording(_TAR_GZ + bytes(4 * _MIB))
    reader = GzipReader(source)

    assert b"".join(iter(lambda: reader.read(_MIB), b"")) == _AB
    assert len(source.sizes) <= 64


_FIRST, _SECOND = gzip_shapes.in_members(_AB, [_B_END])


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_TAR_GZ + b"\x01", id="a-non-zero-byte-after"),
        pytest.param(_TAR_GZ + b"not gzip", id="junk-after"),
        pytest.param(_TAR_GZ + bytes(4096) + b"\x01", id="zeros-then-a-non-zero-byte"),
        pytest.param(_FIRST + b"\x01" + _SECOND, id="a-non-zero-byte-between-members"),
        pytest.param(
            _FIRST + bytes(512) + b"\x01" + _SECOND,
            id="zeros-then-junk-between-members",
        ),
        pytest.param(_TAR_GZ + b"\x1f", id="a-lone-magic-byte-after"),
        pytest.param(_TAR_GZ + bytes(512) + b"\x1f\x8b", id="zeros-then-a-cut-member"),
        pytest.param(bytes(512) + _TAR_GZ, id="zeros-before-the-first-member"),
        pytest.param(bytes(1024), id="only-zeros"),
        pytest.param(b"", id="empty"),
    ],
)
def test_a_stream_of_more_than_members_and_zero_padding_is_refused(
    body: bytes,
) -> None:
    with pytest.raises(_NOT_GZIP):
        _decompressed(body)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            gzip_shapes.with_a_header_crc(_AB, matches=False), id="a-bad-header-crc"
        ),
        *(
            pytest.param(gzip_shapes.with_a_reserved_flag(_AB, flag), id=f"{flag:#x}")
            for flag in (0x20, 0x40, 0x80)
        ),
    ],
)
def test_a_member_header_that_zlib_refuses_is_refused(body: bytes) -> None:
    # gzip.GzipFile skips the header CRC unchecked and ignores the reserved flag
    # bits; zlib's gzip mode refuses both (issue #3847).
    with pytest.raises(zlib.error):
        _decompressed(body)


def _with_every_optional_field(data: bytes) -> bytes:
    """``data`` as one gzip member whose header has an extra field, a name, a
    comment and a CRC that matches."""

    header = (
        b"\x1f\x8b\x08\x1e"
        + bytes(6)
        + len(b"BC\x02\x00\x00\x00").to_bytes(2, "little")
        + b"BC\x02\x00\x00\x00"
        + b"a.tar\0a comment\0"
    )
    crc = zlib.crc32(header) & 0xFFFF
    return header + crc.to_bytes(2, "little") + gzip_shapes.gzipped(data)[10:]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            gzip_shapes.with_a_header_crc(_AB, matches=True), id="a-header-crc"
        ),
        pytest.param(_with_every_optional_field(_AB), id="every-optional-field"),
    ],
)
def test_a_member_header_with_optional_fields_is_accepted(body: bytes) -> None:
    assert _decompressed(body) == _AB


def _on_path(*tools: str) -> pytest.MarkDecorator:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    return pytest.mark.skipif(bool(missing), reason=f"not on PATH: {missing}")


def _gnu_tar_through(*compressor: str) -> Callable[[Path], bytes]:
    """GNU tar's archive of ``a.txt`` and a sparse ``s.bin``, compressed by
    ``compressor`` from a file, so a compressor that names it does."""

    def produce(tmp_path: Path) -> bytes:
        plain = tmp_path / "archive.tar"
        plain.write_bytes(_by_gnu_tar()(tmp_path))
        return subprocess.run(
            [*compressor, str(plain)], capture_output=True, check=True, timeout=60
        ).stdout

    return produce


def _by_python_tarfile(tmp_path: Path) -> bytes:
    """The backup writers' way: ``tarfile`` in ``w:gz`` to a named file."""

    archive = tmp_path / "backup.tar.gz"
    with tarfile.open(archive, mode="w:gz") as tar:
        for name, data in (("a.txt", b"A"), ("b.txt", _B_DATA)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return archive.read_bytes()


_GZIP_PRODUCERS = [
    pytest.param(_by_python_tarfile, id="python-tarfile-w-gz"),
    pytest.param(_by_gnu_tar("-z"), id="gnu-tar-z", marks=_needs_gnu_tar),
    pytest.param(
        _gnu_tar_through("gzip", "-9", "-c"),
        id="gzip-9",
        marks=[_needs_gnu_tar, _on_path("gzip")],
    ),
    *(
        pytest.param(
            _gnu_tar_through("pigz", "-c", *options),
            id="-".join(["pigz", *options]),
            marks=[_needs_gnu_tar, _on_path("pigz")],
        )
        for options in [(), ("-i",), ("--rsyncable",), ("-p", "4", "-b", "128")]
    ),
]


@pytest.mark.parametrize("produce", _GZIP_PRODUCERS)
def test_a_tar_gz_from_each_producer_reads_as_gzip_reads_it(
    produce: Callable[[Path], bytes], tmp_path: Path
) -> None:
    body = produce(tmp_path)

    assert _decompressed(body) == gzip.decompress(body)
