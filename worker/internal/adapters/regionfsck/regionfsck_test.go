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

func TestTornMidChunkIsTruncatedChunk(t *testing.T) {
	// A file truncated mid-write inside its trailing referenced chunk: the size is
	// no longer a 4096 multiple, but that alone is NOT corruption (issue #927) — the
	// byte-precise EOF bound catches the real tear (the chunk's declared length now
	// overruns the shorter file) and classifies it truncated_chunk.
	image := buildRegion(nil, 3, -1, 2)
	path := write(t, filepath.Join(t.TempDir(), "r.mca"), image[:len(image)-10]) // torn mid-write.
	reason, _ := CheckRegionFile(path)
	if reason != ReasonTruncatedChunk {
		t.Fatalf("reason = %v, want ReasonTruncatedChunk", reason)
	}
}

func TestZeroSizeRegionIsClean(t *testing.T) {
	// Minecraft legitimately writes 0-byte region containers (e.g. fresh poi
	// regions with no chunks yet); an empty file is an empty region, structurally
	// sound, not a torn save (issue #905).
	path := write(t, filepath.Join(t.TempDir(), "r.mca"), []byte{})
	reason, err := CheckRegionFile(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason != ReasonNone {
		t.Fatalf("reason = %v, want ReasonNone", reason)
	}
}

func TestShortNonZeroRegionIsFlagged(t *testing.T) {
	// A non-zero file below the two header sectors is a torn header, not an empty
	// region (issue #905): 100 bytes and a single full sector both stay flagged.
	for _, size := range []int{100, testSector} {
		path := write(t, filepath.Join(t.TempDir(), "r.mca"), make([]byte, size))
		reason, _ := CheckRegionFile(path)
		if reason != ReasonNotAligned {
			t.Fatalf("size %d: reason = %v, want ReasonNotAligned", size, reason)
		}
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

// unalignedTrailingChunk builds a region whose final chunk extends BYTE-PRECISELY
// to a non-4096 EOF — the unpadded tail of a live MC 26.x world (issue #923). The
// header is two sectors; a single chunk occupies sector 2 with a declared length
// that ends `tail` bytes into sector 2 (tail < sectorSize, so the file size is not
// a 4096 multiple). The chunk's payload fits exactly: offset*4096 + 4 + length == size.
func unalignedTrailingChunk(tail int) []byte {
	const offset = 2
	size := offset*testSector + tail
	image := make([]byte, size)
	// Location table entry 0: offset 2, sectorCount 1 (the partial final sector).
	image[0] = byte(offset >> 16)
	image[1] = byte(offset >> 8)
	image[2] = byte(offset)
	image[3] = 1
	// length = (payload through EOF) - 4 (the length field is not self-counted).
	length := size - offset*testSector - 4
	binary.BigEndian.PutUint32(image[offset*testSector:offset*testSector+4], uint32(length))
	image[offset*testSector+4] = 2 // zlib.
	return image
}

func TestUnalignedTrailingChunkIsHealthy(t *testing.T) {
	// A live 26.x world's region: non-4096 size, but the trailing chunk fits
	// byte-precisely. The single rule set (issue #927) treats it as healthy — an
	// unaligned tail is the on-disk format, not a tear. This is the case the old
	// strict mode wrongly refused on the stop-leg checkpoint.
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), unalignedTrailingChunk(459))

	reason, err := CheckRegionFile(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason != ReasonNone {
		t.Fatalf("reason = %v, want ReasonNone", reason)
	}
}

func TestUnalignedTrailingChunkOverrunningEOFIsTruncated(t *testing.T) {
	// An unpadded tail whose trailing chunk's declared length overruns the real EOF
	// (a genuine tear, not the legitimate unpadded tail): the byte-precise bound
	// catches it as a truncated chunk.
	image := unalignedTrailingChunk(459)
	offset := int64(2)
	binary.BigEndian.PutUint32(image[offset*testSector:offset*testSector+4], uint32(testSector*5))
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), image)

	if reason, _ := CheckRegionFile(path); reason != ReasonTruncatedChunk {
		t.Fatalf("reason = %v, want ReasonTruncatedChunk", reason)
	}
}

func TestAlignedChunkOverrunningEOFIsTruncated(t *testing.T) {
	// On an ALIGNED file, a chunk whose declared length overruns the sectors/EOF is
	// truncated, proving the byte-precise bound still catches a real overrun.
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"),
		buildRegion(map[int][2]int{0: {2, 1}}, 3, testSector*5, 2))
	if reason, _ := CheckRegionFile(path); reason != ReasonTruncatedChunk {
		t.Fatalf("reason = %v, want ReasonTruncatedChunk", reason)
	}
}

func TestAlignedHealthyRegionIsClean(t *testing.T) {
	// A normal aligned region is healthy: the single rule set relaxes only the tail,
	// never the rest of the structure.
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	reason, err := CheckRegionFile(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason != ReasonNone {
		t.Fatalf("reason = %v, want ReasonNone", reason)
	}
}

func TestCheckWorkingSetAcceptsUnalignedTail(t *testing.T) {
	// The #927 regression case at the working-set level: a set mixing an unaligned
	// (live-format) tail and a normal aligned region scans healthy — the stop-leg
	// snapshot of a crashed/non-gracefully-stopped server now PROCEEDS.
	root := t.TempDir()
	write(t, filepath.Join(root, "region", "r.0.0.mca"), unalignedTrailingChunk(459))
	write(t, filepath.Join(root, "region", "r.1.0.mca"), buildRegion(nil, 3, -1, 2))

	report, err := CheckWorkingSet(root)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if report.Scanned != 2 || !report.Healthy() {
		t.Fatalf("report = %+v, want 2 scanned & healthy", report)
	}
}

func TestInteriorChunkOverrunningSectorAllocationIsTruncated(t *testing.T) {
	// An interior chunk whose declared length overruns its OWN sector allocation
	// (sectorCount 1) into a neighbor, yet still fits byte-precisely inside the file
	// (so the EOF bound alone would pass). The retained length-vs-sectorCount
	// consistency check flags it as truncated. The file is aligned, so the size rule
	// does not short-circuit.
	image := buildRegion(map[int][2]int{0: {2, 1}}, 4, testSector*2-4, 2)
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), image)
	if reason, _ := CheckRegionFile(path); reason != ReasonTruncatedChunk {
		t.Fatalf("reason = %v, want ReasonTruncatedChunk", reason)
	}
}

func TestShortPrefixReadIsTruncatedChunk(t *testing.T) {
	// A tail torn 1-4 bytes into a referenced chunk's first sector: the bounds check
	// proves only the chunk's first byte is inside the file, so the 5-byte prefix
	// read ends mid-prefix (io.ErrUnexpectedEOF). The check classifies this as a
	// structural TRUNCATED_CHUNK (mirroring the Python validator), not an I/O error.
	const offset = 2
	// Two bytes into sector 2: offset*4096 < size (first byte in file) but the prefix
	// cannot be fully read.
	size := offset*testSector + 2
	image := make([]byte, size)
	image[0] = byte(offset >> 16)
	image[1] = byte(offset >> 8)
	image[2] = byte(offset)
	image[3] = 1
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), image)

	reason, err := CheckRegionFile(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
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

func TestWalkerCountsZeroByteRegionAsScannedHealthy(t *testing.T) {
	// The production reproduction (issue #905): a fully quiesced world whose only
	// "suspect" files are 0-byte poi regions must scan clean so its stop snapshot
	// is not refused.
	root := t.TempDir()
	write(t, filepath.Join(root, "region", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	write(t, filepath.Join(root, "poi", "r.-1.-1.mca"), []byte{})
	write(t, filepath.Join(root, "poi", "r.0.-1.mca"), []byte{})

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
	torn := make([]byte, testSector)                            // sub-header-size: not_4096_aligned.
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
