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
// file: a location entry points past EOF, a referenced chunk's declared length
// overruns the real file size, or its prefix is severed mid-write. This package
// catches that *structurally* — location-table sector bounds and per-present-chunk
// length/compression sanity, with BYTE-PRECISE EOF bounds — reading only the two
// header tables and each present chunk's 5-byte prefix. It does not decompress or
// NBT-decode.
//
// ONE rule set, applied everywhere (issue #927). An earlier design (issue #923,
// #925) split the rules by snapshot SOURCE: a STOPPED world was assumed 4096-padded
// (MC 26.x pads region files to a sector boundary only on shutdown/close) and so its
// non-4096 size was treated as a torn save, while a RUNNING world's legitimate
// UNPADDED tail — the last chunk ends mid-sector and the file size is not a 4096
// multiple — was accepted. That `stopped => padded` invariant does NOT hold: a
// sweep-stop timeout, SIGKILL, OOM, crash, or host loss can leave a stopped world's
// regions unpadded, and the stop-leg checkpoint then fails exactly when it is the
// last chance to capture the world. The strict alignment check added detection power
// ONLY under that invalid invariant, so the split is collapsed: the single rule set
// is the former LIVE rule.
//   - A non-4096-aligned size is NOT corruption: it is the normal on-disk shape of
//     an unpadded tail (verified on a live 26.1.2 server: the trailing chunk is
//     complete and decompresses cleanly). Alignment is retained as a signal only for
//     the sub-header-size case below.
//   - A non-zero size below 8192 (two header sectors) is a torn save: a valid region
//     carries both header tables, so anything shorter is structurally broken. This
//     keeps the reason name not_4096_aligned (it is the only alignment-derived
//     verdict left).
//   - Per present chunk: the sector offset must clear both header sectors and start
//     before EOF; the compression scheme must be known; the declared length must be
//     >= 1 and fit its own sector allocation (length <= sectorCount*4096 - 4, the
//     capacity-consistency check); and it must fit byte-precisely within the real
//     file (offset*4096 + 4 + length <= size). The byte-precise EOF bound — not the
//     whole-file sector ceiling offset+sectorCount <= size/4096, whose integer
//     division drops the partial final sector — is what lets a valid unpadded tail
//     pass while still catching a trailing chunk whose declared length overruns EOF.
//   - A short prefix read at a bounds-valid offset (the file is torn 1-4 bytes into
//     the chunk's first sector) is truncated_chunk, not a read fault.
//   - A 0-byte file is an empty region container (issue #905), structurally sound.
//
// This rule-for-rule mirror matches the Python validator (region.py).
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
	// ReasonNotAligned marks a non-zero file shorter than the two header sectors
	// (a torn save). The reason name is retained from when it also covered a
	// non-4096-aligned size; that case is no longer corruption (issue #927).
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

// CheckRegionFile structurally validates one .mca region file (issue #927: ONE
// rule set, no source-keyed mode split). It returns ReasonNone if the file is
// structurally sound, or the first Reason that fails. A non-nil error is returned
// only on a real I/O failure. It reads at most the 8 KiB header plus a 5-byte prefix
// per present chunk; region payloads are never loaded. A non-4096-aligned size is
// the normal unpadded tail of a 26.x world, not corruption; the per-chunk EOF bound
// is byte-precise (offset*4096 + 4 + length <= size). See the package doc.
func CheckRegionFile(path string) (Reason, error) {
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
	// torn save. A size that is not a 4096 multiple is NOT corruption (issue #927):
	// it is the normal unpadded tail of a 26.x world, so only the below-header-size
	// floor is enforced; the per-chunk byte-precise EOF bound below catches a real
	// overrun.
	if size < headerSectors*sectorSize {
		return ReasonNotAligned, nil
	}

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

		// Sector bounds: a present chunk must sit past both header tables and start
		// before EOF. The file's final sector may be partial (the unpadded tail), so
		// the bound is byte-precise against the real size — a whole-file sector ceiling
		// would drop that partial sector and wrongly reject a valid trailing chunk. The
		// byte-precise per-chunk overrun is checked below.
		if offset < headerSectors || sectorCount == 0 {
			return ReasonSectorOutOfBounds, nil
		}
		if offset*sectorSize >= size {
			// The chunk's first sector starts at or past EOF: a real out-of-bounds
			// pointer even by byte-precise bounds.
			return ReasonSectorOutOfBounds, nil
		}

		if _, err := f.ReadAt(prefix, offset*sectorSize); err != nil {
			// The bounds check above only proved the chunk's FIRST byte is inside the
			// file (offset*4096 < size), not all 5 prefix bytes, so a tail torn 1-4 bytes
			// into the chunk's first sector ends the file mid-prefix: a short read there
			// is structural truncation, not an environmental fault, and is classified
			// ReasonTruncatedChunk to mirror the Python validator (region.py). Any other
			// I/O error is a real read fault.
			if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
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
		// sectors: MC always allocates sectorCount = ceil((4+length)/4096), so
		// length <= sectorCount*4096-4 holds for every healthy chunk (len=346, cnt=1 is
		// the live-server evidence) and a declared length overrunning its own sector
		// allocation into a neighbor chunk is a torn-header signature — at zero
		// false-reject cost.
		if length < 1 {
			return ReasonTruncatedChunk, nil
		}
		if length > sectorCount*sectorSize-4 {
			return ReasonTruncatedChunk, nil
		}
		// The trailing chunk may sit in a partial final sector, so its length must
		// ALSO fit byte-precisely within the real file (offset*4096 + 4 + length <=
		// size): this keeps a valid unpadded trailing chunk passing AND still catches a
		// trailing chunk whose declared length overruns the actual EOF (a genuine tear).
		if offset*sectorSize+4+length > size {
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
// while walking or reading. The single rule set (issue #927; see CheckRegionFile)
// applies to every region.
func CheckWorkingSet(root string) (Report, error) {
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
		reason, err := CheckRegionFile(path)
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
