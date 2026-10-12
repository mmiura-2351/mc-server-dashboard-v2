package regionfsck

import (
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

// These tests replace an entry between the moment the walk decides what it is and
// the moment it opens it — the window a running server has (issue #3335).

// beforeOpening runs swap once, immediately before the walk opens the entry called
// name — after the walk has already decided what kind of entry it is. The hook
// makes the ordering deterministic.
func beforeOpening(t *testing.T, name string, swap func()) {
	t.Helper()
	orig := openAt
	t.Cleanup(func() { openAt = orig })
	done := false
	openAt = func(dirFd int, entry string, flags int) (int, error) {
		if !done && entry == name {
			done = true
			swap()
		}
		return orig(dirFd, entry, flags)
	}
}

// assertFailedWithoutAVerdict fails unless the check returned an error and no
// report: an entry replaced under the walk yields no verdict at all, least of all
// one about whatever replaced it.
func assertFailedWithoutAVerdict(t *testing.T, report Report, err error) {
	t.Helper()
	if err == nil {
		t.Fatalf("CheckWorkingSet = %+v, nil; want an error", report)
	}
	if report.Scanned != 0 || len(report.Corrupt) != 0 {
		t.Errorf("CheckWorkingSet returned a report alongside the error: %+v", report)
	}
}

// replace moves the entry at path aside and puts whatever plant creates in its
// place.
func replace(t *testing.T, path string, plant func(path string) error) {
	t.Helper()
	if err := os.Rename(path, path+".moved"); err != nil {
		t.Fatal(err)
	}
	if err := plant(path); err != nil {
		t.Fatal(err)
	}
}

// A region file replaced after the walk has seen it as a regular file is not
// followed, read or blocked on, whatever replaced it.
func TestWalkerRefusesARegionFileSwappedMidWalk(t *testing.T) {
	outside := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), tornRegion())
	for name, plant := range map[string]func(path string) error{
		"for a symlink to a region outside": func(path string) error { return os.Symlink(outside, path) },
		"for a dangling symlink":            func(path string) error { return os.Symlink(outside+".absent", path) },
		"for a fifo":                        func(path string) error { return unix.Mkfifo(path, 0o600) },
		"for a directory":                   func(path string) error { return os.Mkdir(path, 0o700) },
		"for nothing":                       func(string) error { return nil },
	} {
		t.Run(name, func(t *testing.T) {
			root := t.TempDir()
			path := write(t, filepath.Join(root, "region", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
			beforeOpening(t, "r.0.0.mca", func() { replace(t, path, plant) })

			report, err := checkWithin(t, root)
			assertFailedWithoutAVerdict(t, report, err)
		})
	}
}

// A directory replaced after the walk has seen it as a directory does not lead the
// walk out of the working set.
func TestWalkerRefusesADirectorySwappedMidWalk(t *testing.T) {
	outside := t.TempDir()
	write(t, filepath.Join(outside, "r.0.0.mca"), tornRegion())
	for name, plant := range map[string]func(path string) error{
		"for a symlink to a directory outside": func(path string) error { return os.Symlink(outside, path) },
		"for a file":                           func(path string) error { return os.WriteFile(path, nil, 0o600) },
		"for nothing":                          func(string) error { return nil },
	} {
		t.Run(name, func(t *testing.T) {
			root := t.TempDir()
			write(t, filepath.Join(root, "region", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
			beforeOpening(t, "region", func() { replace(t, filepath.Join(root, "region"), plant) })

			report, err := checkWithin(t, root)
			assertFailedWithoutAVerdict(t, report, err)
		})
	}
}

// A directory replaced by a link once the walk is inside it changes nothing: the
// walk reads the directory it opened, never the path again.
func TestWalkerStaysInTheDirectoryItOpened(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	write(t, filepath.Join(outside, "r.0.0.mca"), tornRegion())
	write(t, filepath.Join(root, "region", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	beforeOpening(t, "r.0.0.mca", func() {
		replace(t, filepath.Join(root, "region"), func(path string) error { return os.Symlink(outside, path) })
	})

	report, err := checkWithin(t, root)
	// The region is judged in the directory that was opened. The root was listed
	// before the swap, so neither the link nor region.moved is visited.
	assertScannedOnly(t, report, err, 1)
}

func TestWalkerRefusesASymlinkedRoot(t *testing.T) {
	target := t.TempDir()
	write(t, filepath.Join(target, "region", "r.0.0.mca"), tornRegion())
	root := filepath.Join(t.TempDir(), "work")
	if err := os.Symlink(target, root); err != nil {
		t.Fatal(err)
	}
	report, err := checkWithin(t, root)
	assertFailedWithoutAVerdict(t, report, err)
}

func TestWalkerBoundsItsDepth(t *testing.T) {
	nest := func(t *testing.T, levels int) string {
		t.Helper()
		root := t.TempDir()
		dir := root
		for i := 0; i < levels; i++ {
			dir = filepath.Join(dir, "d")
		}
		write(t, filepath.Join(dir, "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
		return root
	}
	t.Run("at the bound", func(t *testing.T) {
		report, err := checkWithin(t, nest(t, maxWalkDepth))
		assertScannedOnly(t, report, err, 1)
	})
	t.Run("one level deeper", func(t *testing.T) {
		report, err := checkWithin(t, nest(t, maxWalkDepth+1))
		assertFailedWithoutAVerdict(t, report, err)
	})
}

func TestWalkerLeavesNoDescriptorOpen(t *testing.T) {
	openFds := func() int {
		entries, err := os.ReadDir("/proc/self/fd")
		if err != nil {
			t.Skipf("cannot list descriptors: %v", err)
		}
		return len(entries)
	}
	healthy := representativeWorkingSet(t)
	swapped := t.TempDir()
	path := write(t, filepath.Join(swapped, "a", "b", "region", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))

	before := openFds()
	if _, err := checkWithin(t, healthy); err != nil {
		t.Fatalf("CheckWorkingSet: %v", err)
	}
	beforeOpening(t, "r.0.0.mca", func() {
		replace(t, path, func(path string) error { return unix.Mkfifo(path, 0o600) })
	})
	if _, err := checkWithin(t, swapped); err == nil {
		t.Fatal("CheckWorkingSet = nil error on a region file swapped for a FIFO")
	}
	if after := openFds(); after != before {
		t.Errorf("open descriptors: %d before, %d after", before, after)
	}
}
