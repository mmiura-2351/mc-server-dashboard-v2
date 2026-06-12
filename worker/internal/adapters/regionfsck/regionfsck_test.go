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

func TestUnalignedTrailingChunkIsLiveHealthyButStrictNotAligned(t *testing.T) {
	// A live 26.x world's region: non-4096 size, but the trailing chunk fits
	// byte-precisely. Live mode treats it as healthy; strict mode flags the size.
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), unalignedTrailingChunk(459))

	reason, err := CheckRegionFileMode(path, true)
	if err != nil {
		t.Fatalf("live: unexpected error: %v", err)
	}
	if reason != ReasonNone {
		t.Fatalf("live: reason = %v, want ReasonNone", reason)
	}

	reason, err = CheckRegionFileMode(path, false)
	if err != nil {
		t.Fatalf("strict: unexpected error: %v", err)
	}
	if reason != ReasonNotAligned {
		t.Fatalf("strict: reason = %v, want ReasonNotAligned", reason)
	}
}

func TestTrailingChunkOverrunningEOFIsTruncatedInLiveMode(t *testing.T) {
	// An unpadded tail whose trailing chunk's declared length overruns the real EOF
	// (a genuine tear, not the legitimate unpadded tail): live mode catches it as a
	// truncated chunk via the byte-precise bound. Strict mode rejects the same bytes
	// even earlier on the non-4096 size (not_4096_aligned) — still corrupt, just by
	// the alignment rule it always had — so both modes refuse it.
	image := unalignedTrailingChunk(459)
	offset := int64(2)
	binary.BigEndian.PutUint32(image[offset*testSector:offset*testSector+4], uint32(testSector*5))
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), image)

	if reason, _ := CheckRegionFileMode(path, true); reason != ReasonTruncatedChunk {
		t.Fatalf("live: reason = %v, want ReasonTruncatedChunk", reason)
	}
	if reason, _ := CheckRegionFileMode(path, false); reason != ReasonNotAligned {
		t.Fatalf("strict: reason = %v, want ReasonNotAligned", reason)
	}
}

func TestAlignedChunkOverrunningEOFIsTruncatedInBothModes(t *testing.T) {
	// On an ALIGNED file (so the alignment rule does not short-circuit), a chunk
	// whose declared length overruns the sectors/EOF is truncated in BOTH modes,
	// proving the byte-precise live bound still catches a real overrun.
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"),
		buildRegion(map[int][2]int{0: {2, 1}}, 3, testSector*5, 2))
	for _, live := range []bool{true, false} {
		reason, _ := CheckRegionFileMode(path, live)
		if reason != ReasonTruncatedChunk {
			t.Fatalf("live=%v: reason = %v, want ReasonTruncatedChunk", live, reason)
		}
	}
}

func TestAlignedFilesBehaveTheSameInBothModes(t *testing.T) {
	// A normal aligned region is healthy in both modes (the live relaxation only
	// loosens the tail, never the rest of the structure).
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	for _, live := range []bool{true, false} {
		reason, err := CheckRegionFileMode(path, live)
		if err != nil {
			t.Fatalf("live=%v: unexpected error: %v", live, err)
		}
		if reason != ReasonNone {
			t.Fatalf("live=%v: reason = %v, want ReasonNone", live, reason)
		}
	}
}

func TestZeroAndShortFilesBehaveTheSameInBothModes(t *testing.T) {
	// A 0-byte file is healthy in both modes (issue #905); a non-zero file below the
	// two header sectors is corrupt in both (the live relaxation does not loosen the
	// header floor).
	zero := write(t, filepath.Join(t.TempDir(), "empty.mca"), []byte{})
	short := write(t, filepath.Join(t.TempDir(), "short.mca"), make([]byte, 100))
	for _, live := range []bool{true, false} {
		if reason, err := CheckRegionFileMode(zero, live); err != nil || reason != ReasonNone {
			t.Fatalf("live=%v zero: reason=%v err=%v, want ReasonNone", live, reason, err)
		}
		if reason, _ := CheckRegionFileMode(short, live); reason != ReasonNotAligned {
			t.Fatalf("live=%v short: reason=%v, want ReasonNotAligned", live, reason)
		}
	}
}

func TestCheckWorkingSetModeLiveAcceptsUnalignedTail(t *testing.T) {
	root := t.TempDir()
	write(t, filepath.Join(root, "region", "r.0.0.mca"), unalignedTrailingChunk(459))
	write(t, filepath.Join(root, "region", "r.1.0.mca"), buildRegion(nil, 3, -1, 2))

	live, err := CheckWorkingSetMode(root, true)
	if err != nil {
		t.Fatalf("live: unexpected error: %v", err)
	}
	if live.Scanned != 2 || !live.Healthy() {
		t.Fatalf("live: report = %+v, want 2 scanned & healthy", live)
	}

	strict, err := CheckWorkingSetMode(root, false)
	if err != nil {
		t.Fatalf("strict: unexpected error: %v", err)
	}
	if strict.Healthy() {
		t.Fatal("strict: want the unaligned tail flagged")
	}
}

func TestChunkLengthExceedingSectorsIsTruncatedInLiveMode(t *testing.T) {
	// An interior chunk whose declared length overruns its OWN sector allocation
	// (sectorCount 1) into a neighbor, yet still fits byte-precisely inside the file
	// (so the live EOF bound alone would pass). The retained length-vs-sectorCount
	// consistency check (issue #923 review) must flag it as truncated in BOTH modes.
	// The file is aligned so the size rule does not short-circuit strict mode.
	image := buildRegion(map[int][2]int{0: {2, 1}}, 4, testSector*2-4, 2)
	path := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), image)
	for _, live := range []bool{true, false} {
		reason, _ := CheckRegionFileMode(path, live)
		if reason != ReasonTruncatedChunk {
			t.Fatalf("live=%v: reason = %v, want ReasonTruncatedChunk", live, reason)
		}
	}
}

func TestShortPrefixReadIsTruncatedChunkInLiveMode(t *testing.T) {
	// A tail torn 1-4 bytes into a referenced chunk's first sector: the live bounds
	// check proves only the chunk's first byte is inside the file, so the 5-byte
	// prefix read ends mid-prefix (io.ErrUnexpectedEOF). Live mode classifies this as
	// a structural TRUNCATED_CHUNK (mirroring the Python validator), not an I/O error.
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

	reason, err := CheckRegionFileMode(path, true)
	if err != nil {
		t.Fatalf("live: unexpected error: %v", err)
	}
	if reason != ReasonTruncatedChunk {
		t.Fatalf("live: reason = %v, want ReasonTruncatedChunk", reason)
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
