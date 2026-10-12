package regionfsck

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// These tests pin the region check against what a server can plant in its working
// set, or swap in while the check runs (issue #3335): the check also runs on a
// running server's working set, which that server's code shares with the Worker.

// tornRegion is a region the check reports as corrupt. Planted outside the working
// set, it makes a walk that strays there visible: its finding shows up.
func tornRegion() []byte { return make([]byte, 100) }

// checkWithin runs CheckWorkingSet under a deadline: a check that blocks (a FIFO
// without a writer) must fail the test, not hang the suite.
func checkWithin(t *testing.T, root string) (Report, error) {
	t.Helper()
	type result struct {
		report Report
		err    error
	}
	done := make(chan result, 1)
	go func() {
		report, err := CheckWorkingSet(root)
		done <- result{report, err}
	}()
	select {
	case r := <-done:
		return r.report, r.err
	case <-time.After(5 * time.Second):
		t.Fatal("CheckWorkingSet blocked")
		return Report{}, nil
	}
}

// assertScannedOnly fails unless the check succeeded having scanned exactly want
// region files, all of them healthy.
func assertScannedOnly(t *testing.T, report Report, err error, want int) {
	t.Helper()
	if err != nil {
		t.Fatalf("CheckWorkingSet: %v", err)
	}
	if report.Scanned != want || !report.Healthy() {
		t.Fatalf("report = %+v, want %d scanned & healthy", report, want)
	}
}

// A region file that is a link is left out, as the snapshot leaves it out: the
// check never reads what it points at.
func TestWalkerDoesNotFollowASymlinkedRegionFile(t *testing.T) {
	root := t.TempDir()
	outside := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), tornRegion())
	write(t, filepath.Join(root, "region", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	if err := os.Symlink(outside, filepath.Join(root, "region", "r.1.0.mca")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(root, "absent"), filepath.Join(root, "region", "r.2.0.mca")); err != nil {
		t.Fatal(err)
	}

	report, err := checkWithin(t, root)
	assertScannedOnly(t, report, err, 1)
}

func TestWalkerDoesNotFollowASymlinkedDirectory(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	write(t, filepath.Join(outside, "r.0.0.mca"), tornRegion())
	write(t, filepath.Join(root, "region", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	if err := os.Symlink(outside, filepath.Join(root, "entities")); err != nil {
		t.Fatal(err)
	}

	report, err := checkWithin(t, root)
	assertScannedOnly(t, report, err, 1)
}

// A FIFO named like a region file is left out without being opened: opening it
// would block until the server chose to write.
func TestWalkerDoesNotBlockOnAFIFONamedLikeARegion(t *testing.T) {
	root := t.TempDir()
	write(t, filepath.Join(root, "region", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	if err := unix.Mkfifo(filepath.Join(root, "region", "r.1.0.mca"), 0o600); err != nil {
		t.Fatal(err)
	}

	report, err := checkWithin(t, root)
	assertScannedOnly(t, report, err, 1)
}

// CheckRegionFile, given a path, refuses anything at it but a regular file.
func TestCheckRegionFileRefusesWhatIsNotARegularFile(t *testing.T) {
	outside := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), tornRegion())
	for name, plant := range map[string]func(path string) error{
		"symlink to a region outside": func(path string) error { return os.Symlink(outside, path) },
		"dangling symlink":            func(path string) error { return os.Symlink(outside+".absent", path) },
		"fifo":                        func(path string) error { return unix.Mkfifo(path, 0o600) },
		"directory":                   func(path string) error { return os.Mkdir(path, 0o700) },
	} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "r.0.0.mca")
			if err := plant(path); err != nil {
				t.Fatal(err)
			}
			type result struct {
				reason Reason
				err    error
			}
			done := make(chan result, 1)
			go func() {
				reason, err := CheckRegionFile(path)
				done <- result{reason, err}
			}()
			select {
			case r := <-done:
				if r.err == nil || r.reason != ReasonNone {
					t.Fatalf("CheckRegionFile = %v, %v; want an error and no verdict", r.reason, r.err)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("CheckRegionFile blocked")
			}
		})
	}
}

// A region file's size does not decide how much of it is read: the check reads
// the location table and one 5-byte prefix per present chunk, wherever in a
// sparse multi-gigabyte file those sit.
func TestSparseMultiGigabyteRegionIsJudgedFromItsHeaderAndPrefixes(t *testing.T) {
	const farSector = 3 << 20 // 12 GiB in.
	root := t.TempDir()
	path := filepath.Join(root, "region", "r.0.0.mca")
	write(t, path, nil)
	f, err := os.OpenFile(path, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()
	if err := f.Truncate((farSector + 1) * testSector); err != nil {
		t.Skipf("the filesystem cannot hold a sparse 12 GiB file: %v", err)
	}
	// Every one of the 1024 entries present, all pointing at the far sector.
	table := make([]byte, 1024*4)
	for i := 0; i < 1024; i++ {
		binary.BigEndian.PutUint32(table[i*4:], farSector<<8|1)
	}
	prefix := make([]byte, 5)
	binary.BigEndian.PutUint32(prefix, 100)
	prefix[4] = 2
	if _, err := f.WriteAt(table, 0); err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteAt(prefix, farSector*testSector); err != nil {
		t.Skipf("the filesystem cannot hold a sparse 12 GiB file: %v", err)
	}

	report, err := checkWithin(t, root)
	assertScannedOnly(t, report, err, 1)
}
