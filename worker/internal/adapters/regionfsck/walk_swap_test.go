package regionfsck

import (
	"net"
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
// report.
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

// plantSocket binds a unix socket at path; the bind is what creates the entry.
func plantSocket(path string) error {
	l, err := net.Listen("unix", path)
	if err != nil {
		return err
	}
	l.(*net.UnixListener).SetUnlinkOnClose(false)
	return l.Close()
}

// An entry replaced after the walk has decided what it is is skipped and counted,
// as one gone before the walk looked at it is (issue #3350): the scan goes on and
// keeps what it found elsewhere, so a server swapping one name cannot switch the
// check off. Whatever replaced the entry is not followed, read or blocked on, and no
// verdict is made about it (issue #3335).
func TestWalkerSkipsARegionFileSwappedMidWalk(t *testing.T) {
	outside := write(t, filepath.Join(t.TempDir(), "r.0.0.mca"), tornRegion())
	for name, plant := range map[string]func(path string) error{
		"for a symlink to a region outside": func(path string) error { return os.Symlink(outside, path) },
		"for a dangling symlink":            func(path string) error { return os.Symlink(outside+".absent", path) },
		"for a fifo":                        func(path string) error { return unix.Mkfifo(path, 0o600) },
		"for a socket":                      plantSocket,
		"for a directory":                   func(path string) error { return os.Mkdir(path, 0o700) },
		"for nothing":                       func(string) error { return nil },
	} {
		t.Run(name, func(t *testing.T) {
			root := tornThenHealthy(t)
			path := filepath.Join(root, "region", "r.1.0.mca")
			beforeOpening(t, "r.1.0.mca", func() { replace(t, path, plant) })

			report, err := checkWithin(t, root)
			assertKeptTheEarlierFinding(t, report, err, 3, 1)
		})
	}
}

// A directory replaced after the walk has seen it as a directory is skipped the same
// way, and does not lead the walk out of the working set.
func TestWalkerSkipsADirectorySwappedMidWalk(t *testing.T) {
	outside := t.TempDir()
	write(t, filepath.Join(outside, "r.0.0.mca"), tornRegion())
	for name, plant := range map[string]func(path string) error{
		"for a symlink to a directory outside": func(path string) error { return os.Symlink(outside, path) },
		"for a file":                           func(path string) error { return os.WriteFile(path, nil, 0o600) },
		"for nothing":                          func(string) error { return nil },
	} {
		t.Run(name, func(t *testing.T) {
			root := tornThenHealthy(t)
			beforeOpening(t, "zz", func() { replace(t, filepath.Join(root, "zz"), plant) })

			report, err := checkWithin(t, root)
			assertKeptTheEarlierFinding(t, report, err, 3, 1)
		})
	}
}

// Only a failure that says the entry changed is skipped. Any other failure to open
// is a failed check, as before: the scan did not see what is there.
func TestWalkerStillFailsOnAnOpenErrorThatIsNotASwap(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root opens a directory whatever its mode")
	}
	root := tornThenHealthy(t)
	locked := filepath.Join(root, "zz")
	if err := os.Chmod(locked, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(locked, 0o700) })

	report, err := checkWithin(t, root)
	assertFailedWithoutAVerdict(t, report, err)
}

// The shape issue #3350 was found with: a server flips a name in the working-set
// root between a directory and a file while the snapshot's check runs. The flip
// lands before the region directory is reached, and the torn region there is still
// found.
func TestWalkerStillFindsATornRegionWhenARootNameIsSwappedFirst(t *testing.T) {
	root := t.TempDir()
	flipped := filepath.Join(root, ".mcsd_generation")
	if err := os.Mkdir(flipped, 0o700); err != nil {
		t.Fatal(err)
	}
	torn := write(t, filepath.Join(root, "region", "r.0.0.mca"), tornRegion())
	beforeOpening(t, ".mcsd_generation", func() {
		replace(t, flipped, func(path string) error { return os.WriteFile(path, []byte("7"), 0o600) })
	})

	report, err := checkWithin(t, root)
	if err != nil {
		t.Fatalf("CheckWorkingSet: %v; want the scan to complete", err)
	}
	if len(report.Corrupt) != 1 || report.Corrupt[0].Path != torn {
		t.Errorf("Corrupt = %+v, want the torn region", report.Corrupt)
	}
	if report.Scanned != 1 || report.Skipped != 1 {
		t.Errorf("Scanned, Skipped = %d, %d; want 1, 1", report.Scanned, report.Skipped)
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
	if report, err := checkWithin(t, swapped); err != nil || report.Skipped != 1 {
		t.Fatalf("CheckWorkingSet = %+v, %v on a region file swapped for a FIFO; want it skipped", report, err)
	}
	if after := openFds(); after != before {
		t.Errorf("open descriptors: %d before, %d after", before, after)
	}
}
