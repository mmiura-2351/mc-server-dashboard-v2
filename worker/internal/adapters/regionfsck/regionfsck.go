// Package regionfsck checks region headers and chunk prefixes without decoding payloads.
// It mirrors the API validator; callers must quiesce writers before scanning.
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

const (
	sectorSize        = 4096
	entryCount        = 1024
	locationTableSize = entryCount * 4 // 4096: the first header sector.
	headerSectors     = 2              // location table + timestamp table.
	chunkPrefix       = 5              // 4-byte big-endian length + 1-byte compression scheme.
)

// The compression byte's high bit marks an external .mcc payload; low bits still identify the scheme.
const externalFlag = 0x80

// knownCompressionSchemes are the Anvil compression schemes: 1=gzip, 2=zlib,
// 3=none, 4=lz4.
var knownCompressionSchemes = map[byte]bool{1: true, 2: true, 3: true, 4: true}

// Reason is a machine-readable reason a region file failed the structural check.
type Reason int

const (
	// ReasonNone marks a structurally sound region file.
	ReasonNone Reason = iota
	// ReasonNotAligned means a non-empty file lacks both header sectors; an unpadded tail is valid.
	ReasonNotAligned
	// ReasonSectorOutOfBounds marks a chunk starting in a header or at/past EOF, or with zero allocated sectors.
	ReasonSectorOutOfBounds
	// ReasonBadCompression marks a present chunk with an unknown compression scheme.
	ReasonBadCompression
	// ReasonTruncatedChunk marks an incomplete prefix or a length that is zero, exceeds allocation, or overruns EOF.
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

// Report counts all scanned regions and records the first failing check per corrupt file.
type Report struct {
	Scanned int
	Corrupt []Finding
}

// Healthy reports whether nothing was flagged.
func (r Report) Healthy() bool { return len(r.Corrupt) == 0 }

// CheckRegionFile reads the 8 KiB header and each present chunk's five-byte prefix.
// Corruption returns a Reason; only I/O failures return an error. Partial final sectors are valid.
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

	// Minecraft may create empty region files; zero bytes is valid.
	if size == 0 {
		return ReasonNone, nil
	}

	// Require both headers for non-empty files; a partial final sector is valid.
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

		// Use byte bounds so a valid partial final sector is not rejected.
		if offset < headerSectors || sectorCount == 0 {
			return ReasonSectorOutOfBounds, nil
		}
		if offset*sectorSize >= size {
			// The chunk's first sector starts at or past EOF: a real out-of-bounds
			// pointer even by byte-precise bounds.
			return ReasonSectorOutOfBounds, nil
		}

		if _, err := f.ReadAt(prefix, offset*sectorSize); err != nil {
			// A prefix cut short by EOF is structural truncation, not an I/O fault.
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

		// Length includes the compression byte and must fit the chunk's allocated sectors.
		if length < 1 {
			return ReasonTruncatedChunk, nil
		}
		if length > sectorCount*sectorSize-4 {
			return ReasonTruncatedChunk, nil
		}
		// Check actual bytes at EOF; the final sector need not be padded.
		if offset*sectorSize+4+length > size {
			return ReasonTruncatedChunk, nil
		}
	}

	return ReasonNone, nil
}

// CheckWorkingSet validates all .mca files, including every dimension and region type.
// An absent root is healthy; corruption goes in Report and I/O failures return errors.
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
