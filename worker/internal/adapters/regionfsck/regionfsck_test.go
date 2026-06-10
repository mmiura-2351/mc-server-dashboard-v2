package regionfsck

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"
)

const testSector = 4096

// buildRegion assembles a synthetic region image from the documented layout (no
// committed binaries). chunks maps a location-table entry index to its (offset,
// sectorCount); when nil, a single healthy chunk at index 0 occupying sector 2 is
// placed. length overrides the chunk's length prefix (-1 fills the sector), and
// compression sets its compression byte. sectors is the total file size in
// sectors (>= 2 header sectors).
func buildRegion(chunks map[int][2]int, sectors int, length int, compression byte) []byte {
	if chunks == nil {
		chunks = map[int][2]int{0: {2, 1}}
	}
	image := make([]byte, sectors*testSector)

	// Location table (sector 0): write each present entry as a 3-byte big-endian
	// offset + 1-byte sector count.
	for index, oc := range chunks {
		offset, count := oc[0], oc[1]
		image[index*4] = byte(offset >> 16)
		image[index*4+1] = byte(offset >> 8)
		image[index*4+2] = byte(offset)
		image[index*4+3] = byte(count)
	}

	// Each present chunk's 5-byte prefix at its sector start.
	for _, oc := range chunks {
		offset, count := oc[0], oc[1]
		if offset < 2 {
			continue // an out-of-bounds pointer has no payload to write.
		}
		start := offset * testSector
		if start+5 > len(image) {
			continue // pointer past EOF: nothing to write.
		}
		payloadLen := length
		if payloadLen < 0 {
			payloadLen = count*testSector - 4
		}
		binary.BigEndian.PutUint32(image[start:start+4], uint32(payloadLen))
		image[start+4] = compression
	}
	return image
}

func write(t *testing.T, path string, data []byte) string {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(path, data, 0o640); err != nil {
		t.Fatalf("write: %v", err)
	}
	return path
}

func TestHealthySingleChunkRegionIsClean(t *testing.T) {
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	reason, err := CheckRegionFile(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason != ReasonNone {
		t.Fatalf("reason = %v, want ReasonNone", reason)
	}
}

func TestHealthyEmptyRegionIsClean(t *testing.T) {
	// All-zero 8192-byte header: no present chunks, size two sectors.
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), make([]byte, 2*testSector))
	reason, err := CheckRegionFile(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason != ReasonNone {
		t.Fatalf("reason = %v, want ReasonNone", reason)
	}
}

func TestKnownCompressionSchemesAreAccepted(t *testing.T) {
	for _, compression := range []byte{1, 2, 3, 4, 0x82} {
		path := write(t, filepath.Join(t.TempDir(), "r.mca"), buildRegion(nil, 3, -1, compression))
		reason, err := CheckRegionFile(path)
		if err != nil {
			t.Fatalf("compression %#x: unexpected error: %v", compression, err)
		}
		if reason != ReasonNone {
			t.Fatalf("compression %#x: reason = %v, want ReasonNone", compression, reason)
		}
	}
}

func TestNon4096AlignedIsFlagged(t *testing.T) {
	image := buildRegion(nil, 3, -1, 2)
	path := write(t, filepath.Join(t.TempDir(), "r.mca"), image[:len(image)-10]) // torn mid-write.
	reason, _ := CheckRegionFile(path)
	if reason != ReasonNotAligned {
		t.Fatalf("reason = %v, want ReasonNotAligned", reason)
	}
}

func TestZeroSizeRegionIsFlagged(t *testing.T) {
	path := write(t, filepath.Join(t.TempDir(), "r.mca"), []byte{})
	reason, _ := CheckRegionFile(path)
	if reason != ReasonNotAligned {
		t.Fatalf("reason = %v, want ReasonNotAligned", reason)
	}
}

func TestSingleHeaderSectorRegionIsFlagged(t *testing.T) {
	// One 4096-byte sector is aligned but lacks the timestamp table (the second
	// required header sector), so it is structurally corrupt.
	path := write(t, filepath.Join(t.TempDir(), "r.mca"), make([]byte, testSector))
	reason, _ := CheckRegionFile(path)
	if reason != ReasonNotAligned {
		t.Fatalf("reason = %v, want ReasonNotAligned", reason)
	}
}

func TestLocationEntryPastEOFIsSectorOutOfBounds(t *testing.T) {
	// offset+count reaches sector 5 but the file is only 3 sectors long.
	path := write(t, filepath.Join(t.TempDir(), "r.mca"), buildRegion(map[int][2]int{0: {4, 1}}, 3, -1, 2))
	reason, _ := CheckRegionFile(path)
	if reason != ReasonSectorOutOfBounds {
		t.Fatalf("reason = %v, want ReasonSectorOutOfBounds", reason)
	}
}

func TestChunkInsideHeaderIsSectorOutOfBounds(t *testing.T) {
	// A present chunk pointing into a header sector (offset 1) is invalid.
	path := write(t, filepath.Join(t.TempDir(), "r.mca"), buildRegion(map[int][2]int{0: {1, 1}}, 3, -1, 2))
	reason, _ := CheckRegionFile(path)
	if reason != ReasonSectorOutOfBounds {
		t.Fatalf("reason = %v, want ReasonSectorOutOfBounds", reason)
	}
}

func TestUnknownCompressionByteIsBadCompression(t *testing.T) {
	path := write(t, filepath.Join(t.TempDir(), "r.mca"), buildRegion(nil, 3, -1, 9))
	reason, _ := CheckRegionFile(path)
	if reason != ReasonBadCompression {
		t.Fatalf("reason = %v, want ReasonBadCompression", reason)
	}
}

func TestChunkLengthExceedingSectorsIsTruncated(t *testing.T) {
	// Declared length overruns the single sector the entry reserves.
	path := write(t, filepath.Join(t.TempDir(), "r.mca"), buildRegion(map[int][2]int{0: {2, 1}}, 3, testSector*5, 2))
	reason, _ := CheckRegionFile(path)
	if reason != ReasonTruncatedChunk {
		t.Fatalf("reason = %v, want ReasonTruncatedChunk", reason)
	}
}

func TestZeroLengthChunkIsTruncated(t *testing.T) {
	path := write(t, filepath.Join(t.TempDir(), "r.mca"), buildRegion(nil, 3, 0, 2))
	reason, _ := CheckRegionFile(path)
	if reason != ReasonTruncatedChunk {
		t.Fatalf("reason = %v, want ReasonTruncatedChunk", reason)
	}
}

func TestCheckRegionFileMissingFileIsIOError(t *testing.T) {
	_, err := CheckRegionFile(filepath.Join(t.TempDir(), "does-not-exist.mca"))
	if err == nil {
		t.Fatal("missing file: want an I/O error, got nil")
	}
}

func TestWalkerOnCleanWorkingSetIsHealthy(t *testing.T) {
	root := t.TempDir()
	write(t, filepath.Join(root, "region", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	write(t, filepath.Join(root, "entities", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	write(t, filepath.Join(root, "poi", "r.0.0.mca"), make([]byte, 2*testSector))

	report, err := CheckWorkingSet(root)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if report.Scanned != 3 {
		t.Fatalf("scanned = %d, want 3", report.Scanned)
	}
	if !report.Healthy() {
		t.Fatalf("corrupt = %v, want healthy", report.Corrupt)
	}
}

func TestWalkerIgnoresNonMcaFiles(t *testing.T) {
	root := t.TempDir()
	write(t, filepath.Join(root, "region", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	write(t, filepath.Join(root, "level.dat"), []byte("not a region"))
	write(t, filepath.Join(root, "region", "r.0.0.mcc"), []byte("external chunk"))

	report, err := CheckWorkingSet(root)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if report.Scanned != 1 {
		t.Fatalf("scanned = %d, want 1", report.Scanned)
	}
	if !report.Healthy() {
		t.Fatalf("corrupt = %v, want healthy", report.Corrupt)
	}
}

func TestWalkerAggregatesMixedHealthAcrossDimensions(t *testing.T) {
	root := t.TempDir()
	healthy := buildRegion(nil, 3, -1, 2)
	alignedBad := buildRegion(nil, 3, -1, 9)                    // bad_compression.
	torn := buildRegion(nil, 3, -1, 2)                          // truncated below to not_aligned.
	torn = torn[:len(torn)-10]                                  //
	pastEOF := buildRegion(map[int][2]int{0: {4, 1}}, 3, -1, 2) // sector_out_of_bounds.

	layout := map[string][]byte{
		"region/r.0.0.mca":       healthy,
		"region/r.0.1.mca":       torn,
		"region/r.1.0.mca":       healthy,
		"entities/r.0.0.mca":     healthy,
		"entities/r.0.1.mca":     alignedBad,
		"poi/r.0.0.mca":          healthy,
		"DIM-1/region/r.0.0.mca": pastEOF,
		"DIM-1/region/r.0.1.mca": healthy,
	}
	for rel, data := range layout {
		write(t, filepath.Join(root, filepath.FromSlash(rel)), data)
	}

	report, err := CheckWorkingSet(root)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if report.Scanned != len(layout) {
		t.Fatalf("scanned = %d, want %d", report.Scanned, len(layout))
	}
	if report.Healthy() {
		t.Fatal("want unhealthy report")
	}
	if len(report.Corrupt) != 3 {
		t.Fatalf("corrupt count = %d, want 3 (%v)", len(report.Corrupt), report.Corrupt)
	}
	reasons := map[Reason]bool{}
	for _, f := range report.Corrupt {
		reasons[f.Reason] = true
	}
	for _, want := range []Reason{ReasonNotAligned, ReasonBadCompression, ReasonSectorOutOfBounds} {
		if !reasons[want] {
			t.Fatalf("missing reason %v in %v", want, report.Corrupt)
		}
	}
}

func TestWalkerFindsMcaInDimensionsSubtree(t *testing.T) {
	root := t.TempDir()
	write(t, filepath.Join(root, "dimensions", "namespace", "dim", "region", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	report, err := CheckWorkingSet(root)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if report.Scanned != 1 || !report.Healthy() {
		t.Fatalf("report = %+v, want 1 scanned & healthy", report)
	}
}

func TestWalkerMissingRootIsHealthyEmpty(t *testing.T) {
	// An absent working dir (a server with no published working set) is an empty
	// scan, not an error: snapshotting it packs an empty tar.
	report, err := CheckWorkingSet(filepath.Join(t.TempDir(), "absent"))
	if err != nil {
		t.Fatalf("missing root: unexpected error: %v", err)
	}
	if report.Scanned != 0 || !report.Healthy() {
		t.Fatalf("report = %+v, want 0 scanned & healthy", report)
	}
}
