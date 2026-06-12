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
    check_missing_regions,
    check_region_bytes,
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


def test_zero_size_region_is_clean(tmp_path: Path) -> None:
    # Minecraft legitimately writes 0-byte region containers (e.g. fresh poi
    # regions with no chunks yet); an empty file is structurally sound, not a
    # torn save (issue #905).
    path = _write(tmp_path / "r.mca", b"")
    assert check_region_file(path) is None


def test_short_non_zero_region_is_flagged(tmp_path: Path) -> None:
    # A non-zero file below the two header sectors is a torn header, not an empty
    # region (issue #905).
    path = _write(tmp_path / "r.mca", bytes(100))
    assert check_region_file(path) is ReasonCode.NOT_4096_ALIGNED


def test_single_header_sector_region_is_flagged(tmp_path: Path) -> None:
    # One 4096-byte sector is aligned but lacks the timestamp table (the second
    # required header sector), so it is structurally corrupt.
    path = _write(tmp_path / "r.mca", bytes(_SECTOR))
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


def _unaligned_live_region(tail: int = 459) -> bytes:
    """Build a region with the legitimate UNPADDED tail of a live MC 26.x world.

    An 8 KiB header plus one chunk in sector 2 whose data ends ``tail`` bytes into
    sector 2 (``tail`` < 4096), so the file size is NOT a multiple of 4096 but the
    trailing chunk fits byte-precisely: ``offset*4096 + 4 + length == size`` (issue
    #923). Live mode accepts it; strict mode flags ``not_4096_aligned``.
    """
    offset = 2
    size = offset * _SECTOR + tail
    image = bytearray(size)
    image[0:4] = offset.to_bytes(3, "big") + bytes([1])
    length = size - offset * _SECTOR - 4
    start = offset * _SECTOR
    image[start : start + 4] = length.to_bytes(4, "big")
    image[start + 4] = 2  # zlib.
    return bytes(image)


def test_unaligned_tail_is_live_healthy_but_strict_not_aligned(tmp_path: Path) -> None:
    # A live 26.x world's region: non-4096 size, but the trailing chunk fits
    # byte-precisely. Live mode is healthy; strict mode flags the size (issue #923).
    path = _write(tmp_path / "r.0.0.mca", _unaligned_live_region())
    assert check_region_file(path, live=True) is None
    assert check_region_file(path, live=False) is ReasonCode.NOT_4096_ALIGNED


def test_unaligned_tail_overrunning_eof_is_corrupt_in_both_modes(
    tmp_path: Path,
) -> None:
    # Same unpadded tail but the trailing chunk's declared length overruns the real
    # EOF (a genuine tear): live mode catches it as a truncated chunk via the
    # byte-precise bound; strict mode rejects the same bytes even earlier on the
    # non-4096 size. Both modes refuse it (issue #923).
    image = bytearray(_unaligned_live_region())
    start = 2 * _SECTOR
    image[start : start + 4] = (_SECTOR * 5).to_bytes(4, "big")
    path = _write(tmp_path / "r.0.0.mca", bytes(image))
    assert check_region_file(path, live=True) is ReasonCode.TRUNCATED_CHUNK
    assert check_region_file(path, live=False) is ReasonCode.NOT_4096_ALIGNED


def test_aligned_chunk_overrunning_eof_is_truncated_in_both_modes(
    tmp_path: Path,
) -> None:
    # On an ALIGNED file the byte-precise live bound still catches a chunk whose
    # declared length overruns its sectors/EOF: truncated in BOTH modes (issue #923).
    image = _build_region(chunks={0: (2, 1)}, length=_SECTOR * 5)
    path = _write(tmp_path / "r.0.0.mca", image)
    assert check_region_file(path, live=True) is ReasonCode.TRUNCATED_CHUNK
    assert check_region_file(path, live=False) is ReasonCode.TRUNCATED_CHUNK


def test_aligned_and_zero_files_behave_the_same_in_both_modes(tmp_path: Path) -> None:
    # The live relaxation only loosens the unpadded tail: a normal aligned region is
    # healthy in both modes, a 0-byte file is healthy in both (issue #905), and a
    # non-zero file below the header floor is corrupt in both (issue #923).
    aligned = _write(tmp_path / "aligned.mca", _build_region())
    empty = _write(tmp_path / "empty.mca", b"")
    short = _write(tmp_path / "short.mca", bytes(100))
    for live in (True, False):
        assert check_region_file(aligned, live=live) is None
        assert check_region_file(empty, live=live) is None
        assert check_region_file(short, live=live) is ReasonCode.NOT_4096_ALIGNED


def test_working_set_live_mode_accepts_unaligned_tail(tmp_path: Path) -> None:
    _write(tmp_path / "region" / "r.0.0.mca", _unaligned_live_region())
    _write(tmp_path / "region" / "r.1.0.mca", _build_region())

    live = check_working_set(tmp_path, live=True)
    assert live.scanned == 2
    assert live.healthy is True

    strict = check_working_set(tmp_path, live=False)
    assert strict.healthy is False


def test_check_region_bytes_live_mode_accepts_unaligned_tail() -> None:
    data = _unaligned_live_region()
    assert check_region_bytes("region/r.0.0.mca", data, live=True) is None
    finding = check_region_bytes("region/r.0.0.mca", data, live=False)
    assert finding is not None
    assert finding.reason is ReasonCode.NOT_4096_ALIGNED


def test_chunk_length_exceeding_sectors_is_truncated_in_live_mode(
    tmp_path: Path,
) -> None:
    # An interior chunk whose declared length overruns its OWN sector allocation
    # (sector_count 1) into a neighbor, yet still fits byte-precisely inside the file
    # (the live EOF bound alone would pass). The retained length-vs-sector_count
    # consistency check (issue #923 review) flags it as truncated in BOTH modes. The
    # file is aligned so the size rule does not short-circuit strict mode.
    image = _build_region(chunks={0: (2, 1)}, sectors=4, length=_SECTOR * 2 - 4)
    path = _write(tmp_path / "r.0.0.mca", image)
    assert check_region_file(path, live=True) is ReasonCode.TRUNCATED_CHUNK
    assert check_region_file(path, live=False) is ReasonCode.TRUNCATED_CHUNK


def test_short_prefix_read_is_truncated_chunk_in_live_mode(tmp_path: Path) -> None:
    # A tail torn 1-4 bytes into a referenced chunk's first sector: the live bounds
    # check proves only the chunk's first byte is inside the file, so the 5-byte
    # prefix read ends mid-prefix. Live mode classifies this structural truncation as
    # TRUNCATED_CHUNK, mirroring the Go validator (issue #923 review).
    offset = 2
    image = bytearray(offset * _SECTOR + 2)  # two bytes into sector 2.
    image[0:4] = offset.to_bytes(3, "big") + bytes([1])
    path = _write(tmp_path / "r.0.0.mca", bytes(image))
    assert check_region_file(path, live=True) is ReasonCode.TRUNCATED_CHUNK


def test_walker_on_clean_working_set_is_healthy(tmp_path: Path) -> None:
    _write(tmp_path / "region" / "r.0.0.mca", _build_region())
    _write(tmp_path / "entities" / "r.0.0.mca", _build_region())
    _write(tmp_path / "poi" / "r.0.0.mca", bytes(2 * _SECTOR))

    report = check_working_set(tmp_path)

    assert report.scanned == 3
    assert report.corrupt == []
    assert report.healthy is True


def test_walker_counts_zero_byte_region_as_scanned_healthy(tmp_path: Path) -> None:
    # The production reproduction (issue #905): a fully quiesced world whose only
    # "suspect" files are 0-byte poi regions must scan clean so its stop snapshot
    # is not refused.
    _write(tmp_path / "region" / "r.0.0.mca", _build_region())
    _write(tmp_path / "poi" / "r.-1.-1.mca", b"")
    _write(tmp_path / "poi" / "r.0.-1.mca", b"")

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


# --- in-memory body check (the object-store gate's entry point, issue #750) --


def test_check_region_bytes_healthy_returns_none() -> None:
    assert check_region_bytes("r.0.0.mca", _build_region()) is None


def test_check_region_bytes_empty_returns_none() -> None:
    # A 0-byte region body is an empty container, structurally sound (issue #905).
    assert check_region_bytes("poi/r.-1.-1.mca", b"") is None


def test_check_region_bytes_corrupt_returns_finding_with_name() -> None:
    image = _build_region()
    finding = check_region_bytes("world/region/r.0.0.mca", image[:-10])
    assert finding is not None
    assert finding.reason is ReasonCode.NOT_4096_ALIGNED
    assert finding.path == Path("world/region/r.0.0.mca")


def test_check_region_bytes_matches_check_region_file(tmp_path: Path) -> None:
    # The body check and the path check share one core: a region failing on disk
    # fails identically in memory (the object/fs gate parity, #750).
    image = _build_region(compression=99)  # an unknown compression scheme.
    path = _write(tmp_path / "r.mca", image)
    finding = check_region_bytes("r.mca", image)
    assert finding is not None
    assert finding.reason is check_region_file(path)


# --- missing-region detection (partial loss vs. full delete, issue #854) -----


def _seed(root: Path, names: list[str]) -> Path:
    for name in names:
        _write(root / name, _build_region())
    return root


def test_missing_regions_clean_when_sets_match(tmp_path: Path) -> None:
    layout = ["region/r.0.0.mca", "region/r.0.1.mca", "DIM-1/region/r.0.0.mca"]
    new = _seed(tmp_path / "new", layout)
    ref = _seed(tmp_path / "ref", layout)

    report = check_missing_regions(new, ref)

    assert report.complete is True
    assert report.partial_loss == []


def test_missing_regions_clean_when_set_grows(tmp_path: Path) -> None:
    # Adding regions (new chunks generated) is never a loss.
    new = _seed(tmp_path / "new", ["region/r.0.0.mca", "region/r.0.1.mca"])
    ref = _seed(tmp_path / "ref", ["region/r.0.0.mca"])

    assert check_missing_regions(new, ref).complete is True


def test_missing_regions_clean_on_first_publish(tmp_path: Path) -> None:
    # No prior current/ to compare against -> nothing flagged.
    new = _seed(tmp_path / "new", ["region/r.0.0.mca"])
    ref = tmp_path / "ref"  # never created.

    assert check_missing_regions(new, ref).complete is True


def test_missing_regions_flags_partial_loss_in_a_dimension(tmp_path: Path) -> None:
    # The same dimension still has regions but lost one — the corruption signature.
    new = _seed(tmp_path / "new", ["region/r.0.0.mca"])
    ref = _seed(tmp_path / "ref", ["region/r.0.0.mca", "region/r.0.1.mca"])

    report = check_missing_regions(new, ref)

    assert report.complete is False
    assert len(report.partial_loss) == 1
    finding = report.partial_loss[0]
    assert finding.directory == Path("region")
    assert finding.lost == ("r.0.1.mca",)


def test_missing_regions_allows_full_dimension_delete(tmp_path: Path) -> None:
    # A whole dimension's regions are gone (legitimate delete) — not flagged, while
    # a partial loss in another dimension still IS.
    new = _seed(tmp_path / "new", ["region/r.0.0.mca"])
    ref = _seed(
        tmp_path / "ref",
        ["region/r.0.0.mca", "DIM-1/region/r.0.0.mca", "DIM-1/region/r.0.1.mca"],
    )

    report = check_missing_regions(new, ref)

    assert report.complete is True


def test_missing_regions_full_delete_and_partial_loss_together(tmp_path: Path) -> None:
    new = _seed(tmp_path / "new", ["region/r.0.0.mca"])
    ref = _seed(
        tmp_path / "ref",
        [
            "region/r.0.0.mca",
            "region/r.0.1.mca",  # partial loss in region/.
            "DIM1/region/r.0.0.mca",  # full delete of DIM1 -> allowed.
        ],
    )

    report = check_missing_regions(new, ref)

    assert report.complete is False
    assert [f.directory for f in report.partial_loss] == [Path("region")]
