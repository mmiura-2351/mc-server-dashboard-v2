package instancemanager

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// A started server's spec carries the probe a graceful stop's wait polls (issue
// #3436): it fingerprints the working set's region files, so a chunk write — a
// new region file or a rewrite of one — changes the fingerprint, and nothing else
// does.
func TestStartedSpecCarriesTheWorldProgressProbe(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	startRunning(t, m)
	probe := d.started[0].WorldProgress
	if probe == nil {
		t.Fatal("the started spec carries no WorldProgress probe")
	}
	region := filepath.Join(m.scratchDir, s1, "world", "region", "r.0.0.mca")

	empty, err := probe()
	if err != nil {
		t.Fatalf("probe: %v", err)
	}
	if empty.Files != 0 {
		t.Fatalf("probe over a world with no region file = %+v, want no files", empty)
	}

	settleWrite(t, region, "chunk")
	first, err := probe()
	if err != nil {
		t.Fatalf("probe: %v", err)
	}
	if first.Files != 1 || first.Bytes != int64(len("chunk")) {
		t.Fatalf("probe after a region file was written = %+v, want 1 file of %d bytes", first, len("chunk"))
	}
	if again, _ := probe(); again != first {
		t.Fatalf("probe with nothing written = %+v, want it unchanged from %+v", again, first)
	}

	// A rewrite in place keeps the count and the size; only the mtime moves.
	later := first.Newest.Add(time.Second)
	if err := os.Chtimes(region, later, later); err != nil {
		t.Fatal(err)
	}
	rewritten, err := probe()
	if err != nil {
		t.Fatalf("probe: %v", err)
	}
	if rewritten == first || !rewritten.Newest.Equal(later) {
		t.Fatalf("probe after a rewrite = %+v, want the newest mtime %v", rewritten, later)
	}
}

// A scan that cannot finish is reported, not papered over with a fingerprint the
// driver would read as "nothing written".
func TestWorldProgressReportsAFailedScan(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	boom := errors.New("scan failed")
	m.scanRegion = func(string) (regionState, error) { return nil, boom }

	if _, err := m.worldProgress(t.TempDir())(); !errors.Is(err, boom) {
		t.Fatalf("probe error = %v, want %v", err, boom)
	}
}
