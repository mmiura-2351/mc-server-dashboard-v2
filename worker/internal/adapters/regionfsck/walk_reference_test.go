package regionfsck

import (
	"encoding/binary"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// referenceCheckRegionFile and referenceCheckWorkingSet are the path-based region
// check as it stood before issue #3335 (filepath.WalkDir, then os.Open by path),
// retained here — in the test only, comments included — as the definition of the
// verdict. The descriptor-relative walk that replaced it must return the same
// Report for a working set nobody is changing.

func referenceCheckRegionFile(path string) (Reason, error) {
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
func referenceCheckWorkingSet(root string) (Report, error) {
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
		reason, err := referenceCheckRegionFile(path)
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

// representativeWorkingSet builds a working set shaped like a real server's — a
// world with its region/, entities/ and poi/ containers, the legacy DIM* and the
// dimensions/** layouts, and the files around it — holding every verdict the check
// can return, and returns its root. It holds only directories and regular files:
// what a legitimate working set is made of.
func representativeWorkingSet(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	healthy := buildRegion(nil, 3, -1, 2)

	// A region with many chunks, one of them external (0x80 | zlib), in the order
	// Minecraft allocates them.
	manyChunks := map[int][2]int{}
	for i := 0; i < 40; i++ {
		manyChunks[i*25] = [2]int{2 + i, 1}
	}
	many := buildRegion(manyChunks, 42, 100, 2)
	many[2*testSector+4] = 0x82

	overrun := unalignedTrailingChunk(459)
	binary.BigEndian.PutUint32(overrun[2*testSector:2*testSector+4], uint32(testSector*5))

	tornPrefix := make([]byte, 2*testSector+2)
	tornPrefix[2], tornPrefix[3] = 2, 1

	for rel, data := range map[string][]byte{
		// Healthy shapes.
		"world/region/r.0.0.mca":                                healthy,
		"world/region/r.0.1.mca":                                many,
		"world/region/r.-1.0.mca":                               {},
		"world/region/r.-1.-1.mca":                              make([]byte, 2*testSector),
		"world/entities/r.0.0.mca":                              healthy,
		"world/poi/r.0.0.mca":                                   {},
		"world/dimensions/minecraft/the_end/region/r.0.0.mca":   unalignedTrailingChunk(459),
		"world/dimensions/minecraft/the_end/entities/r.0.0.mca": buildRegion(nil, 3, -1, 4),
		"world_nether/DIM-1/region/r.0.0.mca":                   buildRegion(nil, 3, -1, 1),
		"world_the_end/DIM1/region/r.0.0.mca":                   buildRegion(nil, 3, -1, 3),
		// One of each corrupt verdict, spread over the layouts.
		"world/region/r.1.0.mca":                        buildRegion(nil, 2, -1, 2),
		"world/region/r.1.1.mca":                        make([]byte, 100),
		"world/entities/r.1.0.mca":                      buildRegion(nil, 3, -1, 9),
		"world/poi/r.1.0.mca":                           buildRegion(map[int][2]int{0: {1, 1}}, 3, -1, 2),
		"world/dimensions/custom/deep/region/r.0.0.mca": overrun,
		"world_nether/DIM-1/region/r.1.0.mca":           buildRegion(nil, 3, 0, 2),
		"world_nether/DIM-1/entities/r.0.0.mca":         tornPrefix,
		"world_the_end/DIM1/region/r.1.0.mca":           buildRegion(map[int][2]int{0: {2, 1}}, 4, testSector*2-4, 2),
		"world_the_end/DIM1/region/r.2.0.mca":           make([]byte, testSector),
		"plugins/dynmap/a region backup/r.0.0.mca":      make([]byte, 5000),
		"plugins/dynmap/ワールド/r.0.0.mca":                 buildRegion(nil, 3, -1, 2),
		// Names that sort around the ones above, and names the suffix match must
		// take or leave exactly as before.
		"world/region-old/r.0.0.mca":        make([]byte, 1),
		"world/region.bak/r.0.0.mca":        healthy,
		"world/region/r.0.0.mca.bak":        make([]byte, 1),
		"world/region/r.0.0.MCA":            make([]byte, 1),
		"world/region/.mca":                 make([]byte, 7),
		"world/region/r.0.0.mcc":            make([]byte, 1),
		"looks-like-a-region.mca/r.0.0.mca": make([]byte, 3),
		"looks-like-a-region.mca/notes.txt": []byte("a directory named like a region"),
		"a.mca":                             make([]byte, 9),
		// Everything else a server keeps.
		"world/level.dat":        []byte("level"),
		"world/session.lock":     {},
		"server.properties":      []byte("server-port=25565\n"),
		"logs/latest.log":        []byte("log"),
		"plugins/Essentials.jar": make([]byte, 10000),
	} {
		write(t, filepath.Join(root, filepath.FromSlash(rel)), data)
	}
	if err := os.MkdirAll(filepath.Join(root, "world", "datapacks", "empty"), 0o750); err != nil {
		t.Fatal(err)
	}
	return root
}

// The verdict on a working set at rest is the path-based walk's, field for field:
// the same files scanned, the same findings with the same paths and reasons, in
// the same order.
func TestVerdictOnAWorkingSetAtRestMatchesThePathBasedWalk(t *testing.T) {
	root := representativeWorkingSet(t)

	want, err := referenceCheckWorkingSet(root)
	if err != nil {
		t.Fatalf("reference walk: %v", err)
	}
	// The fixture must exercise the check, or the comparison proves nothing.
	reasons := map[Reason]int{}
	for _, f := range want.Corrupt {
		reasons[f.Reason]++
	}
	for _, r := range []Reason{ReasonNotAligned, ReasonSectorOutOfBounds, ReasonBadCompression, ReasonTruncatedChunk} {
		if reasons[r] == 0 {
			t.Fatalf("the fixture holds no %v region; reference report = %+v", r, want)
		}
	}
	if healthy := want.Scanned - len(want.Corrupt); healthy < 10 {
		t.Fatalf("the fixture holds only %d healthy regions; reference report = %+v", healthy, want)
	}

	t.Logf("fixture: %d region files scanned, %d corrupt %v", want.Scanned, len(want.Corrupt), reasons)

	got, err := CheckWorkingSet(root)
	if err != nil {
		t.Fatalf("CheckWorkingSet: %v", err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("CheckWorkingSet differs from the path-based walk\n got: %+v\nwant: %+v", got, want)
	}

	// Each region file alone, too.
	err = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		wantReason, wantErr := referenceCheckRegionFile(path)
		gotReason, gotErr := CheckRegionFile(path)
		if gotReason != wantReason || (gotErr == nil) != (wantErr == nil) {
			t.Errorf("CheckRegionFile(%s) = %v, %v; the path-based check gives %v, %v",
				path, gotReason, gotErr, wantReason, wantErr)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}
