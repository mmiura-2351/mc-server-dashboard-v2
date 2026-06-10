"""Structural validator for Minecraft ``.mca`` region containers (issue #738).

A region file is a flat array of 4 KiB *sectors*. The first two sectors are
header tables: a 1024-entry *location table* (sector 0) followed by a 1024-entry
*timestamp table* (sector 1). Each location-table entry is 4 bytes — a 3-byte
big-endian sector ``offset`` and a 1-byte ``sectorCount``; an all-zero entry
means the chunk is absent. A present chunk's payload begins at its sector with a
5-byte prefix: a 4-byte big-endian ``length`` and a 1-byte compression scheme.

A crash during a chunk save (the failure reproduced in #703) truncates the file:
its size stops being a multiple of 4096, or a location entry points past EOF.
This module catches that **structurally** — 4096 alignment, location-table
sector bounds, and per-present-chunk length/compression sanity — reading only the
two header tables and each present chunk's 5-byte prefix (O(header) per region;
region payloads are never loaded). It does **not** decompress or NBT-decode.

Quiescence is the caller's responsibility. Run this **only against a quiesced
working set**: on a live world the read races the server's write and a healthy
region false-positives as corrupt. This validator does not — and cannot —
enforce quiescence; it faithfully reports whatever bytes are on disk.

Corruption is a normal return value, never an exception: :func:`check_region_file`
returns ``None`` for a healthy region or a :class:`ReasonCode` for a corrupt one,
and :func:`check_working_set` aggregates a :class:`WorkingSetReport`. Exceptions
are reserved for real I/O errors (``OSError``) raised while reading.
"""

from __future__ import annotations

import enum
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

# Region layout constants.
_SECTOR = 4096
_ENTRY_COUNT = 1024
_LOCATION_TABLE_SIZE = _ENTRY_COUNT * 4  # 4096: the first header sector.
_HEADER_SECTORS = 2  # location table + timestamp table.
_CHUNK_PREFIX = 5  # 4-byte big-endian length + 1-byte compression scheme.

# Known chunk compression schemes (the Anvil format): 1=gzip, 2=zlib, 3=none,
# 4=lz4. The 0x80 bit flags an external ``.mcc`` payload (the chunk is too large
# for the region file); the low bits still carry the scheme, so a present chunk
# may legitimately carry e.g. 0x82 (external + zlib).
_COMPRESSION_SCHEMES = frozenset({1, 2, 3, 4})
_EXTERNAL_FLAG = 0x80


class ReasonCode(enum.Enum):
    """A machine-readable reason a region file failed the structural check."""

    NOT_4096_ALIGNED = "not_4096_aligned"
    SECTOR_OUT_OF_BOUNDS = "sector_out_of_bounds"
    BAD_COMPRESSION = "bad_compression"
    TRUNCATED_CHUNK = "truncated_chunk"


@dataclass(frozen=True)
class RegionFinding:
    """One corrupt region file and the first structural reason it failed."""

    path: Path
    reason: ReasonCode


@dataclass(frozen=True)
class WorkingSetReport:
    """The aggregate result of walking a working set's ``.mca`` files.

    ``scanned`` counts every region file examined; ``corrupt`` lists one finding
    per corrupt file (the first failing reason). ``healthy`` is the overall
    verdict: ``True`` exactly when nothing was flagged.
    """

    scanned: int = 0
    corrupt: list[RegionFinding] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.corrupt


def _check_region(size: int, fh: BinaryIO) -> ReasonCode | None:
    """Structurally validate one region container given its byte ``size`` and a
    seekable binary reader ``fh`` positioned at its start.

    The shared core behind :func:`check_region_file` (a local path) and
    :func:`check_region_bytes` (an in-memory body). Returns ``None`` if sound or
    the first :class:`ReasonCode` that fails; reads at most the 8 KiB header plus a
    5-byte prefix per present chunk, so it never loads the region payload.
    """
    # A valid region carries both header tables (location + timestamp), so the
    # smallest sound file is two sectors. A non-zero size below that — or any
    # size that is not a 4096 multiple — is a torn save.
    if size < _HEADER_SECTORS * _SECTOR or size % _SECTOR != 0:
        return ReasonCode.NOT_4096_ALIGNED

    total_sectors = size // _SECTOR

    location_table = fh.read(_LOCATION_TABLE_SIZE)
    for index in range(_ENTRY_COUNT):
        entry = location_table[index * 4 : index * 4 + 4]
        offset = int.from_bytes(entry[0:3], "big")
        sector_count = entry[3]
        if offset == 0 and sector_count == 0:
            continue  # absent chunk.

        # Sector bounds: a present chunk must sit past both header tables and
        # stay wholly within the file.
        if offset < _HEADER_SECTORS or sector_count == 0:
            return ReasonCode.SECTOR_OUT_OF_BOUNDS
        if offset + sector_count > total_sectors:
            return ReasonCode.SECTOR_OUT_OF_BOUNDS

        fh.seek(offset * _SECTOR)
        prefix = fh.read(_CHUNK_PREFIX)
        if len(prefix) < _CHUNK_PREFIX:
            return ReasonCode.TRUNCATED_CHUNK
        length = int.from_bytes(prefix[0:4], "big")
        compression = prefix[4]

        if compression & _EXTERNAL_FLAG:
            scheme = compression & ~_EXTERNAL_FLAG
        else:
            scheme = compression
        if scheme not in _COMPRESSION_SCHEMES:
            return ReasonCode.BAD_COMPRESSION

        # The length prefix counts the compression byte plus the compressed
        # stream. It must be positive and fit within the declared sectors
        # (the 4-byte length itself is not counted by the length field).
        if length < 1:
            return ReasonCode.TRUNCATED_CHUNK
        available = sector_count * _SECTOR - 4
        if length > available:
            return ReasonCode.TRUNCATED_CHUNK

    return None


def check_region_file(path: Path) -> ReasonCode | None:
    """Structurally validate one ``.mca`` region file.

    Returns ``None`` if the file is structurally sound, or the first
    :class:`ReasonCode` that fails. Raises ``OSError`` only on a real I/O error.
    Reads at most the 8 KiB header plus a 5-byte prefix per present chunk.
    """
    size = path.stat().st_size
    with path.open("rb") as fh:
        return _check_region(size, fh)


def check_region_bytes(name: str, data: bytes) -> RegionFinding | None:
    """Structurally validate one ``.mca`` region held in memory (issue #750).

    The object-store adapter has no local working-set tree to walk: its region
    files arrive as object bodies. This runs the same structural check over a
    region's bytes (seeking within an in-memory buffer, never loading the payload
    beyond the header + per-chunk prefix the check reads) so the object backend can
    apply the same fail-closed gate the fs adapter applies via
    :func:`check_working_set`. Returns a :class:`RegionFinding` carrying ``name``
    when corrupt, or ``None`` when sound.
    """
    reason = _check_region(len(data), io.BytesIO(data))
    if reason is None:
        return None
    return RegionFinding(path=Path(name), reason=reason)


def check_working_set(root: Path) -> WorkingSetReport:
    """Walk ``root`` recursively and validate every ``*.mca`` file beneath it.

    Region files live under several world subdirectories — ``region/``,
    ``entities/``, ``poi/``, per-dimension ``DIM*/…`` and ``dimensions/**`` — and
    all share the region-container format, so every ``.mca`` is validated
    regardless of where it sits. Returns a :class:`WorkingSetReport`; corruption
    is reported in the result, never raised.
    """
    scanned = 0
    corrupt: list[RegionFinding] = []
    for path in sorted(root.rglob("*.mca")):
        if not path.is_file():
            continue
        scanned += 1
        reason = check_region_file(path)
        if reason is not None:
            corrupt.append(RegionFinding(path=path, reason=reason))
    return WorkingSetReport(scanned=scanned, corrupt=corrupt)
