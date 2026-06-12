// Package regionfsck structurally validates Minecraft .mca region containers
// on the Worker, before a snapshot is packed and uploaded (issue #741, parent
// #703). It is the Go mirror of the API-side Python validator
// (api/.../storage/integrity/region.py): the same structural rules, used as a
// source-side fail-fast so a corrupt working set is caught at the Worker — clear
// signal, no wasted tar+upload — rather than only being refused by the API gate
// after a full transfer. The API gate remains the correctness guarantee.
//
// A region file is a flat array of 4 KiB *sectors*. The first two sectors are
// header tables: a 1024-entry *location table* (sector 0) followed by a 1024-entry
// *timestamp table* (sector 1). Each location-table entry is 4 bytes — a 3-byte
// big-endian sector offset and a 1-byte sectorCount; an all-zero entry means the
// chunk is absent. A present chunk's payload begins at its sector with a 5-byte
// prefix: a 4-byte big-endian length and a 1-byte compression scheme.
//
// A crash during a chunk save (the failure reproduced in #703) truncates the
// file: its size stops being a multiple of 4096, or a location entry points past
// EOF. This package catches that *structurally* — 4096 alignment, location-table
// sector bounds, and per-present-chunk length/compression sanity — reading only
// the two header tables and each present chunk's 5-byte prefix. It does not
// decompress or NBT-decode.
//
// Two modes, keyed by snapshot SOURCE (issue #923). MC 26.x pads region files to
// a sector boundary only on shutdown/close: a STOPPED world's regions are all
// 4096-aligned, but a RUNNING (even quiesced) world legitimately keeps an UNPADDED
// tail — the last chunk's data ends mid-sector and the file size is not a multiple
// of 4096. Verified on a live 26.1.2 server: the trailing chunk in such a file is
// complete and decompresses cleanly; it is the on-disk format, not a tear.
//   - STRICT mode (a stopped/at-rest set): a non-4096 size IS a torn save, and the
//     per-chunk bound is whole-sector. Unchanged from the original behavior.
//   - LIVE mode (a running server's periodic snapshot): a non-4096 size is NOT
//     corruption, and the per-chunk EOF bound is BYTE-PRECISE — a present chunk
//     passes when offset*4096 + 4 + length <= size. (The strict whole-file ceiling
//     offset+sectorCount <= size/4096 would wrongly reject a valid trailing chunk
//     because integer division drops the partial final sector.) The
//     length-vs-sectorCount consistency check (length <= sectorCount*4096-4) is KEPT
//     in live mode too — MC always allocates sectorCount for the chunk's own length,
//     so it costs no false reject and still catches an interior chunk whose declared
//     length overruns its sector allocation into a neighbor. All other rules —
//     header presence (size==0 fine per #905; a non-zero size below 8192 still
//     corrupt), sector offsets inside the header, compression scheme, length>=1 —
//     are identical. A trailing chunk whose declared length overruns the real EOF
//     is still corrupt in BOTH modes.
//
// The mode is chosen from the snapshot SOURCE, not the consumer: STRICT holds only
// where alignment is guaranteed at the source — the Worker's stopped-path pre-pack
// fsck and the API publish gate for X-Snapshot-Source: stopped/absent. LIVE is used
// everywhere the store or archives derived from it are consumed (the publish gate
// for running, plus the API's backup create/restore and integrity-sweep fsck),
// because a running-source publish may have committed a legitimate unpadded set into
// the store and every at-rest consumer must tolerate it. This split mirrors the
// Python validator (region.py).
//
// Quiescence is the caller's responsibility. Run this *only against a quiesced
// working set*: on a live world the read races the server's write and a healthy
// region false-positives as corrupt. On the snapshot path the #694 RCON
// save-off/save-on bracket (instancemanager.quiesceRunning) provides that
// quiescence — this check must run inside it, between save-off and the transfer.
// It does not — and cannot — enforce quiescence; it faithfully reports whatever
// bytes are on disk.
//
// Corruption is a normal return value, never an error: CheckRegionFile returns
// ReasonNone for a healthy region or a Reason for a corrupt one, and
// CheckWorkingSet aggregates a Report. Errors are reserved for real I/O failures
// hit while reading.
package regionfsck

import (
	"encoding/binary"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// Region layout constants.
const (
	sectorSize        = 4096
	entryCount        = 1024
	locationTableSize = entryCount * 4 // 4096: the first header sector.
	headerSectors     = 2              // location table + timestamp table.
	chunkPrefix       = 5              // 4-byte big-endian length + 1-byte compression scheme.
)

// externalFlag is the 0x80 bit on a chunk's compression byte: the payload lives
// in an external .mcc file (the chunk is too large for the region file). The low
// bits still carry the scheme, so a present chunk may legitimately carry e.g.
// 0x82 (external + zlib).
const externalFlag = 0x80

// knownCompressionSchemes are the Anvil compression schemes: 1=gzip, 2=zlib,
// 3=none, 4=lz4.
var knownCompressionSchemes = map[byte]bool{1: true, 2: true, 3: true, 4: true}

// Reason is a machine-readable reason a region file failed the structural check.
type Reason int

const (
	// ReasonNone marks a structurally sound region file.
	ReasonNone Reason = iota
	// ReasonNotAligned marks a file whose size is not a positive multiple of 4096
	// of at least two header sectors (a torn save).
	ReasonNotAligned
	// ReasonSectorOutOfBounds marks a present chunk whose sector offset/count sits
	// inside a header sector or reaches past EOF.
	ReasonSectorOutOfBounds
	// ReasonBadCompression marks a present chunk with an unknown compression scheme.
	ReasonBadCompression
	// ReasonTruncatedChunk marks a present chunk whose 5-byte prefix is missing or
	// whose declared length is empty or overruns its reserved sectors.
	ReasonTruncatedChunk
)

// String renders the reason as a stable name (matching the Python validator's
// reason codes) for log/error messages.
func (r Reason) String() string {
	switch r {
	case ReasonNone:
		return "none"
	case ReasonNotAligned:
		return "not_4096_aligned"
	case ReasonSectorOutOfBounds:
		return "sector_out_of_bounds"
	case ReasonBadCompression:
		return "bad_compression"
	case ReasonTruncatedChunk:
		return "truncated_chunk"
	default:
		return "unknown"
	}
}

// Finding is one corrupt region file and the first structural reason it failed.
type Finding struct {
	Path   string
	Reason Reason
}

// Report is the aggregate result of walking a working set's .mca files. Scanned
// counts every region file examined; Corrupt lists one finding per corrupt file
// (the first failing reason).
type Report struct {
	Scanned int
	Corrupt []Finding
}

// Healthy reports whether nothing was flagged.
func (r Report) Healthy() bool { return len(r.Corrupt) == 0 }

// CheckRegionFile structurally validates one .mca region file in STRICT mode (a
// stopped/at-rest set). It returns ReasonNone if the file is structurally sound,
// or the first Reason that fails. A non-nil error is returned only on a real I/O
// failure. It reads at most the 8 KiB header plus a 5-byte prefix per present
// chunk; region payloads are never loaded.
func CheckRegionFile(path string) (Reason, error) {
	return CheckRegionFileMode(path, false)
}

// CheckRegionFileMode is CheckRegionFile with an explicit mode (issue #923). When
// live is false it is the strict at-rest check (unchanged). When live is true it
// validates a RUNNING server's working set, where MC 26.x leaves a legitimate
// unpadded tail: a non-4096-aligned size is NOT corruption and the whole-file sector
// ceiling is replaced by a byte-precise per-chunk EOF bound (offset*4096 + 4 +
// length <= size). The length-vs-sectorCount consistency check is kept in both
// modes; all other structural rules are identical.
func CheckRegionFileMode(path string, live bool) (Reason, error) {
	f, err := os.Open(path)
	if err != nil {
		return ReasonNone, err
	}
	defer func() { _ = f.Close() }()

	info, err := f.Stat()
	if err != nil {
		return ReasonNone, err
	}
	size := info.Size()

	// A 0-byte file is an empty region container — Minecraft legitimately writes
	// these (e.g. fresh poi/r.*.mca with no chunks yet) — so it is structurally
	// sound, not a torn save (issue #905).
	if size == 0 {
		return ReasonNone, nil
	}

	// A valid region carries both header tables (location + timestamp), so the
	// smallest sound non-empty file is two sectors. A non-zero size below that is a
	// torn save in both modes. A size that is not a 4096 multiple is a torn save in
	// strict mode, but in live mode it is the normal unpadded tail of a running
	// 26.x world (issue #923), so only the below-header-size floor is enforced there.
	if size < headerSectors*sectorSize {
		return ReasonNotAligned, nil
	}
	if !live && size%sectorSize != 0 {
		return ReasonNotAligned, nil
	}
	totalSectors := size / sectorSize

	table := make([]byte, locationTableSize)
	if _, err := f.ReadAt(table, 0); err != nil {
		return ReasonNone, err
	}

	prefix := make([]byte, chunkPrefix)
	for index := 0; index < entryCount; index++ {
		entry := table[index*4 : index*4+4]
		offset := int64(entry[0])<<16 | int64(entry[1])<<8 | int64(entry[2])
		sectorCount := int64(entry[3])
		if offset == 0 && sectorCount == 0 {
			continue // absent chunk.
		}

		// Sector bounds: a present chunk must sit past both header tables and stay
		// wholly within the file. In live mode the file's final sector may be partial
		// (the unpadded tail), so the whole-sector ceiling is computed against the real
		// byte size — totalSectors drops that partial sector and would wrongly reject a
		// valid trailing chunk. The byte-precise per-chunk overrun is checked below.
		if offset < headerSectors || sectorCount == 0 {
			return ReasonSectorOutOfBounds, nil
		}
		if !live && offset+sectorCount > totalSectors {
			return ReasonSectorOutOfBounds, nil
		}
		if live && offset*sectorSize >= size {
			// The chunk's first sector starts at or past EOF: a real out-of-bounds
			// pointer even by byte-precise bounds.
			return ReasonSectorOutOfBounds, nil
		}

		if _, err := f.ReadAt(prefix, offset*sectorSize); err != nil {
			// In live mode the bounds check above only proved the chunk's FIRST byte is
			// inside the file (offset*4096 < size), not all 5 prefix bytes, so a tail torn
			// 1-4 bytes into the chunk's first sector ends the file mid-prefix: a short
			// read there is structural truncation, not an environmental fault, and is
			// classified ReasonTruncatedChunk to mirror the Python validator (region.py).
			// In strict mode the whole-sector bound already proved the full prefix is
			// within the file, so a short read is a real read fault. Any other I/O error
			// is a read fault in both modes.
			if live && (errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF)) {
				return ReasonTruncatedChunk, nil
			}
			return ReasonNone, err
		}
		length := int64(binary.BigEndian.Uint32(prefix[0:4]))
		compression := prefix[4]

		scheme := compression
		if compression&externalFlag != 0 {
			scheme = compression &^ externalFlag
		}
		if !knownCompressionSchemes[scheme] {
			return ReasonBadCompression, nil
		}

		// The length prefix counts the compression byte plus the compressed stream and
		// must be positive (length>=1). It must ALSO fit within the chunk's declared
		// sectors in BOTH modes: MC always allocates sectorCount = ceil((4+length)/4096),
		// so length <= sectorCount*4096-4 holds for every healthy chunk (issue #923's own
		// evidence: len=346, cnt=1) and a declared length overrunning its own sector
		// allocation into a neighbor chunk is a torn-header signature worth catching even
		// in live mode — at zero false-reject cost.
		if length < 1 {
			return ReasonTruncatedChunk, nil
		}
		if length > sectorCount*sectorSize-4 {
			return ReasonTruncatedChunk, nil
		}
		// In live mode the trailing chunk may sit in a partial final sector, so its
		// length must ALSO fit byte-precisely within the real file (offset*4096 + 4 +
		// length <= size): this keeps a valid unpadded trailing chunk passing AND still
		// catches a trailing chunk whose declared length overruns the actual EOF (a
		// genuine tear).
		if live && offset*sectorSize+4+length > size {
			return ReasonTruncatedChunk, nil
		}
	}

	return ReasonNone, nil
}

// CheckWorkingSet walks root recursively and validates every *.mca file beneath
// it. Region files live under several world subdirectories — region/, entities/,
// poi/, per-dimension DIM*/… and dimensions/** — and all share the
// region-container format, so every .mca is validated regardless of where it
// sits. An absent root is an empty, healthy scan (a server with no published
// working set snapshots to an empty tar); corruption is reported in the Report,
// never returned as an error. A non-nil error is reserved for a real I/O failure
// while walking or reading.
func CheckWorkingSet(root string) (Report, error) {
	return CheckWorkingSetMode(root, false)
}

// CheckWorkingSetMode is CheckWorkingSet with an explicit mode (issue #923): live
// is true for a RUNNING server's periodic snapshot (the unpadded-tail relaxation,
// see CheckRegionFileMode) and false for a stopped/at-rest set (strict). Every
// other rule, and the walk itself, is identical.
func CheckWorkingSetMode(root string, live bool) (Report, error) {
	var report Report
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) && path == root {
				// An absent working dir: nothing to scan.
				return filepath.SkipAll
			}
			return err
		}
		if d.IsDir() || !strings.HasSuffix(d.Name(), ".mca") {
			return nil
		}
		report.Scanned++
		reason, err := CheckRegionFileMode(path, live)
		if err != nil {
			return err
		}
		if reason != ReasonNone {
			report.Corrupt = append(report.Corrupt, Finding{Path: path, Reason: reason})
		}
		return nil
	})
	if err != nil {
		return Report{}, err
	}
	return report, nil
}
