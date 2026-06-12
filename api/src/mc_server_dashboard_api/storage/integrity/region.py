"""Structural validator for Minecraft ``.mca`` region containers (issue #738).

A region file is a flat array of 4 KiB *sectors*. The first two sectors are
header tables: a 1024-entry *location table* (sector 0) followed by a 1024-entry
*timestamp table* (sector 1). Each location-table entry is 4 bytes — a 3-byte
big-endian sector ``offset`` and a 1-byte ``sectorCount``; an all-zero entry
means the chunk is absent. A present chunk's payload begins at its sector with a
5-byte prefix: a 4-byte big-endian ``length`` and a 1-byte compression scheme.

A crash during a chunk save (the failure reproduced in #703) truncates the file:
a location entry points past EOF, a referenced chunk's declared length overruns
the real file size, or its prefix is severed mid-write. This module catches that
**structurally** — location-table sector bounds and per-present-chunk
length/compression sanity, with BYTE-PRECISE EOF bounds — reading only the two
header tables and each present chunk's 5-byte prefix (O(header) per region; region
payloads are never loaded). It does **not** decompress or NBT-decode.

One rule set, applied everywhere (issue #927). An earlier design (issue #923, #925)
split the rules by snapshot SOURCE: a STOPPED world was assumed 4096-padded (MC 26.x
pads region files to a sector boundary only on shutdown/close) and its non-4096 size
treated as a torn save, while a RUNNING world's legitimate UNPADDED tail — the last
chunk ends mid-sector and the file size is not a 4096 multiple — was accepted. That
``stopped => padded`` invariant does NOT hold: a sweep-stop timeout, SIGKILL, OOM,
crash, or host loss can leave a stopped world's regions unpadded, and the stop-leg
checkpoint then fails exactly when it is the last chance to capture the world. The
strict alignment check added detection power ONLY under that invalid invariant, so
the split is collapsed: the single rule set is the former LIVE rule.

* A non-4096-aligned size is NOT corruption: it is the normal on-disk shape of an
  unpadded tail (verified on a live 26.1.2 server: the trailing chunk is complete
  and decompresses cleanly). Alignment is retained as a signal only for the
  sub-header-size case below.
* A non-zero size below 8192 (two header sectors) is a torn save: a valid region
  carries both header tables, so anything shorter is structurally broken. The reason
  name ``not_4096_aligned`` is kept (it is the only alignment-derived verdict left).
* Per present chunk: the sector offset must clear both header sectors and start
  before EOF; the compression scheme must be known; the declared length must be
  >= 1 and fit its own sector allocation (``length <= sector_count*4096 - 4``, the
  capacity-consistency check); and it must fit byte-precisely within the real file
  (``offset*4096 + 4 + length <= size``). The byte-precise EOF bound — not the
  whole-file sector ceiling ``offset + sector_count <= size // 4096``, whose integer
  division drops the partial final sector — lets a valid unpadded tail pass while
  still catching a trailing chunk whose declared length overruns EOF.
* A short prefix read at a bounds-valid offset (the file is torn 1-4 bytes into the
  chunk's first sector) is ``truncated_chunk``, not a read fault.
* A 0-byte file is an empty region container (issue #905), structurally sound.

This rule-for-rule mirror matches the Go validator (regionfsck.go), the Worker's
source-side fail-fast.

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

    One rule set (issue #927): a non-4096-aligned size is the normal unpadded tail of
    a 26.x world (not a torn save) and the per-chunk EOF bound is byte-precise. See
    the module docstring.
    """
    # A 0-byte file is an empty region container — Minecraft legitimately writes
    # these (e.g. fresh poi/r.*.mca with no chunks yet) — so it is structurally
    # sound, not a torn save (issue #905).
    if size == 0:
        return None

    # A valid region carries both header tables (location + timestamp), so the
    # smallest sound non-empty file is two sectors. A non-zero size below that is a
    # torn save. A size that is not a 4096 multiple is NOT corruption (issue #927):
    # it is the normal unpadded tail of a 26.x world, so only the below-header-size
    # floor is enforced; the per-chunk byte-precise EOF bound below catches a real
    # overrun.
    if size < _HEADER_SECTORS * _SECTOR:
        return ReasonCode.NOT_4096_ALIGNED

    location_table = fh.read(_LOCATION_TABLE_SIZE)
    for index in range(_ENTRY_COUNT):
        entry = location_table[index * 4 : index * 4 + 4]
        offset = int.from_bytes(entry[0:3], "big")
        sector_count = entry[3]
        if offset == 0 and sector_count == 0:
            continue  # absent chunk.

        # Sector bounds: a present chunk must sit past both header tables and start
        # before EOF. The final sector may be partial (the unpadded tail), so the
        # bound is byte-precise against the real size — a whole-file sector ceiling
        # would drop that partial sector and wrongly reject a valid trailing chunk; the
        # byte-precise overrun is checked per chunk below.
        if offset < _HEADER_SECTORS or sector_count == 0:
            return ReasonCode.SECTOR_OUT_OF_BOUNDS
        if offset * _SECTOR >= size:
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

        # The length prefix counts the compression byte plus the compressed stream
        # and must be positive (length>=1). It must ALSO fit within the chunk's
        # declared sectors: MC always allocates
        # ``sector_count = ceil((4+length)/4096)``, so ``length <= sector_count*4096-4``
        # holds for every healthy chunk (len=346, cnt=1 is the live-server evidence) and
        # a declared length overrunning its own sector allocation into a neighbor chunk
        # is a torn-header signature — at zero false-reject cost.
        if length < 1:
            return ReasonCode.TRUNCATED_CHUNK
        available = sector_count * _SECTOR - 4
        if length > available:
            return ReasonCode.TRUNCATED_CHUNK
        # The trailing chunk may sit in a partial final sector, so its length must ALSO
        # fit byte-precisely within the real file (offset*4096 + 4 + length <= size):
        # this keeps a valid unpadded trailing chunk passing AND still catches a
        # trailing chunk whose declared length overruns the actual EOF (a genuine tear).
        if offset * _SECTOR + 4 + length > size:
            return ReasonCode.TRUNCATED_CHUNK

    return None


def check_region_file(path: Path) -> ReasonCode | None:
    """Structurally validate one ``.mca`` region file.

    Returns ``None`` if the file is structurally sound, or the first
    :class:`ReasonCode` that fails. Raises ``OSError`` only on a real I/O error.
    Reads at most the 8 KiB header plus a 5-byte prefix per present chunk. The single
    rule set (issue #927); see :func:`_check_region`.
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
    when corrupt, or ``None`` when sound. The single rule set (issue #927); see
    :func:`_check_region`.
    """
    reason = _check_region(len(data), io.BytesIO(data))
    if reason is None:
        return None
    return RegionFinding(path=Path(name), reason=reason)


@dataclass(frozen=True)
class MissingRegionFinding:
    """One region-bearing directory that LOST some-but-not-all of its files.

    ``directory`` is the region dir relative to the working-set root (e.g.
    ``region``, ``DIM-1/region``, ``dimensions/<ns>/<dim>/region``); ``lost`` is
    the sorted set of ``.mca`` file names present in the reference set but absent
    from the new one. A partial loss is the corruption signature this check
    catches (issue #854).
    """

    directory: Path
    lost: tuple[str, ...]


@dataclass(frozen=True)
class MissingRegionReport:
    """The aggregate of comparing a new working set against the prior one (#854).

    ``partial_loss`` lists one finding per region dir that lost SOME-but-not-ALL
    of its ``.mca`` files between the reference (prior ``current/``) set and the
    new (staged) set. A directory that lost ALL of its region files — a legitimate
    full-dimension/world delete — is deliberately NOT flagged; only a partial loss,
    which MC would silently regenerate as fresh empty chunks, is suspect.
    """

    partial_loss: list[MissingRegionFinding] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.partial_loss


def _region_sets_by_dir(root: Path) -> dict[Path, set[str]]:
    """Map each region-bearing directory (relative to ``root``) to its ``.mca``
    file-name set.

    A "region-bearing directory" is any directory that directly contains at least
    one ``*.mca`` file; this is the natural per-dimension grouping (``region/``,
    ``entities/``, ``poi/``, ``DIM*/region``, ``dimensions/**/region``) without
    hard-coding the dimension taxonomy. Returns an empty map when ``root`` is
    absent (an unpublished server has no prior set to compare against).
    """
    by_dir: dict[Path, set[str]] = {}
    if not root.is_dir():
        return by_dir
    for path in root.rglob("*.mca"):
        if not path.is_file():
            continue
        rel_dir = path.parent.relative_to(root)
        by_dir.setdefault(rel_dir, set()).add(path.name)
    return by_dir


def compare_region_name_sets(
    new: dict[Path, set[str]], reference: dict[Path, set[str]]
) -> MissingRegionReport:
    """Diff two region-dir -> ``.mca`` name-set maps for partial region loss (#854).

    The adapter-agnostic core behind :func:`check_missing_regions` (a local tree)
    and the object backend (object keys grouped by prefix). For each directory in
    ``reference`` that is still present in ``new`` (a NON-EMPTY new set), every name
    the reference had but the new set lacks is a lost region. A directory whose new
    set is EMPTY — all regions gone — is a legitimate full-dimension/world delete
    and is not flagged. Findings are sorted by directory for a stable report.
    """
    partial_loss: list[MissingRegionFinding] = []
    for rel_dir, prior_names in reference.items():
        new_names = new.get(rel_dir, set())
        # All gone -> full-dimension delete (legitimate); skip. Otherwise any name
        # the prior set had that the new set lacks is a partial loss.
        if not new_names:
            continue
        lost = prior_names - new_names
        if lost:
            partial_loss.append(
                MissingRegionFinding(directory=rel_dir, lost=tuple(sorted(lost)))
            )
    partial_loss.sort(key=lambda finding: finding.directory.as_posix())
    return MissingRegionReport(partial_loss=partial_loss)


def check_missing_regions(new_root: Path, reference_root: Path) -> MissingRegionReport:
    """Compare a new working set against the prior one for partial region loss (#854).

    Every other integrity gate validates only files that EXIST, so a region file
    that vanished is structurally valid absence: the snapshot publishes, the
    restore succeeds, and MC silently regenerates the chunks. This catches the
    corruption signature directly — for each region-bearing directory present in
    BOTH sets, a NEW ``.mca`` set that is a non-empty strict subset of the
    reference set means some regions were lost while the dimension still exists
    (partial loss). A directory whose region files are ALL gone is treated as a
    legitimate full-dimension/world delete and is NOT flagged (false-positive
    care). A directory new to ``new_root`` only adds regions and is never flagged.

    Read-only over both trees (reads directory entries, never file bodies).
    """
    return compare_region_name_sets(
        _region_sets_by_dir(new_root), _region_sets_by_dir(reference_root)
    )


def check_working_set(root: Path) -> WorkingSetReport:
    """Walk ``root`` recursively and validate every ``*.mca`` file beneath it.

    Region files live under several world subdirectories — ``region/``,
    ``entities/``, ``poi/``, per-dimension ``DIM*/…`` and ``dimensions/**`` — and
    all share the region-container format, so every ``.mca`` is validated
    regardless of where it sits. Returns a :class:`WorkingSetReport`; corruption
    is reported in the result, never raised.

    The single rule set (issue #927) applies to every region: a non-4096-aligned
    tail is the normal on-disk shape, not a tear, at every gate (the publish gate on
    either source, and the store/archive consumers). See the module docstring above.
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
