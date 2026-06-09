"""Structural ``.mca`` region validator (issue #738).

Table-driven coverage of the single-file structural check and the working-set
walker. All fixtures are synthesised in-test from the documented region layout
(no committed binaries): a region is a flat array of 4096-byte sectors whose
first sector is a 1024-entry location table (3-byte big-endian offset + 1-byte
sector count per entry), and each present chunk's sector opens with a 4-byte
big-endian length + 1-byte compression scheme.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.integrity.region import (
    ReasonCode,
    check_region_file,
    check_working_set,
)

_SECTOR = 4096


def _build_region(
    chunks: dict[int, tuple[int, int]] | None = None,
    *,
    sectors: int = 3,
    length: int | None = None,
    compression: int = 2,
) -> bytes:
    """Assemble a region image.

    ``chunks`` maps a location-table entry index to ``(offset, sector_count)``;
    when omitted, a single healthy chunk at index 0 occupying sector 2 is placed.
    ``length`` overrides the chunk's length prefix (default: fill the sector),
    and ``compression`` sets its compression byte. ``sectors`` is the total file
    size in sectors (>= 2 header sectors).
    """
    if chunks is None:
        chunks = {0: (2, 1)}

    image = bytearray(sectors * _SECTOR)

    # Location table (sector 0): write each present entry.
    for index, (offset, sector_count) in chunks.items():
        entry = offset.to_bytes(3, "big") + bytes([sector_count])
        image[index * 4 : index * 4 + 4] = entry

    # Each present chunk's 5-byte prefix at its sector start.
    for offset, sector_count in chunks.values():
        if offset < 2:
            continue  # an out-of-bounds pointer has no payload to write.
        start = offset * _SECTOR
        if start + 5 > len(image):
            continue  # pointer past EOF: nothing to write.
        payload_len = length if length is not None else sector_count * _SECTOR - 4
        image[start : start + 4] = payload_len.to_bytes(4, "big")
        image[start + 4] = compression

    return bytes(image)


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_healthy_single_chunk_region_is_clean(tmp_path: Path) -> None:
    path = _write(tmp_path / "r.0.0.mca", _build_region())
    assert check_region_file(path) is None


def test_healthy_empty_region_is_clean(tmp_path: Path) -> None:
    # All-zero 8192-byte header: no present chunks, size two sectors.
    path = _write(tmp_path / "r.0.0.mca", bytes(2 * _SECTOR))
    assert check_region_file(path) is None


@pytest.mark.parametrize("compression", [1, 2, 3, 4, 0x82])
def test_known_compression_schemes_are_accepted(
    tmp_path: Path, compression: int
) -> None:
    path = _write(tmp_path / "r.mca", _build_region(compression=compression))
    assert check_region_file(path) is None


def test_non_4096_aligned_is_flagged(tmp_path: Path) -> None:
    image = _build_region()
    path = _write(tmp_path / "r.mca", image[:-10])  # torn mid-write.
    assert check_region_file(path) is ReasonCode.NOT_4096_ALIGNED


def test_zero_size_region_is_flagged(tmp_path: Path) -> None:
    path = _write(tmp_path / "r.mca", b"")
    assert check_region_file(path) is ReasonCode.NOT_4096_ALIGNED


def test_location_entry_past_eof_is_sector_out_of_bounds(tmp_path: Path) -> None:
    # offset+count reaches sector 5 but the file is only 3 sectors long.
    image = _build_region(chunks={0: (4, 1)}, sectors=3)
    path = _write(tmp_path / "r.mca", image)
    assert check_region_file(path) is ReasonCode.SECTOR_OUT_OF_BOUNDS


def test_chunk_inside_header_is_sector_out_of_bounds(tmp_path: Path) -> None:
    # A present chunk pointing into a header sector (offset 1) is invalid.
    image = _build_region(chunks={0: (1, 1)}, sectors=3)
    path = _write(tmp_path / "r.mca", image)
    assert check_region_file(path) is ReasonCode.SECTOR_OUT_OF_BOUNDS


def test_unknown_compression_byte_is_bad_compression(tmp_path: Path) -> None:
    path = _write(tmp_path / "r.mca", _build_region(compression=9))
    assert check_region_file(path) is ReasonCode.BAD_COMPRESSION


def test_chunk_length_exceeding_sectors_is_truncated(tmp_path: Path) -> None:
    # Declared length overruns the single sector the entry reserves.
    image = _build_region(chunks={0: (2, 1)}, length=_SECTOR * 5)
    path = _write(tmp_path / "r.mca", image)
    assert check_region_file(path) is ReasonCode.TRUNCATED_CHUNK


def test_zero_length_chunk_is_truncated(tmp_path: Path) -> None:
    image = _build_region(length=0)
    path = _write(tmp_path / "r.mca", image)
    assert check_region_file(path) is ReasonCode.TRUNCATED_CHUNK


def test_walker_on_clean_working_set_is_healthy(tmp_path: Path) -> None:
    _write(tmp_path / "region" / "r.0.0.mca", _build_region())
    _write(tmp_path / "entities" / "r.0.0.mca", _build_region())
    _write(tmp_path / "poi" / "r.0.0.mca", bytes(2 * _SECTOR))

    report = check_working_set(tmp_path)

    assert report.scanned == 3
    assert report.corrupt == []
    assert report.healthy is True


def test_walker_ignores_non_mca_files(tmp_path: Path) -> None:
    _write(tmp_path / "region" / "r.0.0.mca", _build_region())
    _write(tmp_path / "level.dat", b"not a region")
    _write(tmp_path / "region" / "r.0.0.mcc", b"external chunk")

    report = check_working_set(tmp_path)

    assert report.scanned == 1
    assert report.healthy is True


def test_walker_aggregates_mixed_health_across_dimensions(tmp_path: Path) -> None:
    # Mirror the 13/36 reproduction shape across the four region-bearing dirs.
    healthy = _build_region()
    aligned_bad = _build_region(compression=9)  # bad_compression.
    torn = _build_region()[:-10]  # not_4096_aligned.
    past_eof = _build_region(chunks={0: (4, 1)}, sectors=3)  # sector_out_of_bounds.

    layout: dict[str, bytes] = {
        "region/r.0.0.mca": healthy,
        "region/r.0.1.mca": torn,
        "region/r.1.0.mca": healthy,
        "entities/r.0.0.mca": healthy,
        "entities/r.0.1.mca": aligned_bad,
        "poi/r.0.0.mca": healthy,
        "DIM-1/region/r.0.0.mca": past_eof,
        "DIM-1/region/r.0.1.mca": healthy,
    }
    for rel, data in layout.items():
        _write(tmp_path / rel, data)

    report = check_working_set(tmp_path)

    assert report.scanned == len(layout)
    assert report.healthy is False
    assert len(report.corrupt) == 3
    reasons = {(f.path.name, f.reason) for f in report.corrupt}
    assert ("r.0.1.mca", ReasonCode.NOT_4096_ALIGNED) in reasons
    assert ("r.0.1.mca", ReasonCode.BAD_COMPRESSION) in reasons
    assert ("r.0.0.mca", ReasonCode.SECTOR_OUT_OF_BOUNDS) in reasons


def test_walker_finds_mca_in_dimensions_subtree(tmp_path: Path) -> None:
    # world/dimensions/<ns>/<dim>/region/*.mca must be discovered too.
    _write(
        tmp_path / "dimensions" / "namespace" / "dim" / "region" / "r.0.0.mca",
        _build_region(),
    )
    report = check_working_set(tmp_path)
    assert report.scanned == 1
    assert report.healthy is True
