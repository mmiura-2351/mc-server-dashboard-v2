package regionfsck

import (
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

// These tests pin what the walk does with a name that is gone by the time it is
// looked at: a running server deletes files all the time (rotated logs, plugin
// temp files), and one such deletion must not cost the whole scan and the
// findings it already holds.

// beforeStatting runs vanish once, immediately before the walk lstats the entry
// called name — after the directory holding it was listed.
func beforeStatting(t *testing.T, name string, vanish func()) {
	t.Helper()
	orig := statAt
	t.Cleanup(func() { statAt = orig })
	done := false
	statAt = func(dirFd int, entry string, st *unix.Stat_t) error {
		if !done && entry == name {
			done = true
			vanish()
		}
		return orig(dirFd, entry, st)
	}
}

// tornThenHealthy builds a working set whose first region file (in walk order) is
// corrupt and whose later entries the tests make vanish, and returns its root.
func tornThenHealthy(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	write(t, filepath.Join(root, "region", "r.0.0.mca"), tornRegion())
	write(t, filepath.Join(root, "region", "r.1.0.mca"), buildRegion(nil, 3, -1, 2))
	write(t, filepath.Join(root, "region", "r.2.0.mca"), buildRegion(nil, 3, -1, 2))
	write(t, filepath.Join(root, "zz", "r.0.0.mca"), buildRegion(nil, 3, -1, 2))
	return root
}

// assertKeptTheEarlierFinding fails unless the scan completed with the corrupt
// region found before the vanish still reported, and the given counts.
func assertKeptTheEarlierFinding(t *testing.T, report Report, err error, scanned, skipped int) {
	t.Helper()
	if err != nil {
		t.Fatalf("CheckWorkingSet: %v; want the scan to complete", err)
	}
	if len(report.Corrupt) != 1 || filepath.Base(report.Corrupt[0].Path) != "r.0.0.mca" {
		t.Errorf("Corrupt = %+v, want the finding made before the vanish", report.Corrupt)
	}
	if report.Scanned != scanned || report.Skipped != skipped {
		t.Errorf("Scanned, Skipped = %d, %d; want %d, %d", report.Scanned, report.Skipped, scanned, skipped)
	}
}

func TestWalkerSkipsARegionFileThatVanishedAfterTheListing(t *testing.T) {
	root := tornThenHealthy(t)
	beforeStatting(t, "r.1.0.mca", func() {
		if err := os.Remove(filepath.Join(root, "region", "r.1.0.mca")); err != nil {
			t.Fatal(err)
		}
	})
	report, err := checkWithin(t, root)
	assertKeptTheEarlierFinding(t, report, err, 3, 1)
}

func TestWalkerSkipsADirectoryThatVanishedAfterTheListing(t *testing.T) {
	root := tornThenHealthy(t)
	beforeStatting(t, "zz", func() {
		if err := os.RemoveAll(filepath.Join(root, "zz")); err != nil {
			t.Fatal(err)
		}
	})
	report, err := checkWithin(t, root)
	assertKeptTheEarlierFinding(t, report, err, 3, 1)
}

// The type a directory listing carries is not what the walk goes by: a region
// file replaced by a link or a FIFO after the listing is seen for what it is by
// the lstat, and left out unopened like any other.
func TestWalkerDoesNotTrustTheListingsEntryType(t *testing.T) {
	outside := write(t, filepath.Join(t.TempDir(), "r.9.9.mca"), tornRegion())
	for name, plant := range map[string]func(path string) error{
		"replaced by a symlink": func(path string) error { return os.Symlink(outside, path) },
		"replaced by a fifo":    func(path string) error { return unix.Mkfifo(path, 0o600) },
	} {
		t.Run(name, func(t *testing.T) {
			root := tornThenHealthy(t)
			path := filepath.Join(root, "region", "r.1.0.mca")
			beforeStatting(t, "r.1.0.mca", func() {
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
				if err := plant(path); err != nil {
					t.Fatal(err)
				}
			})
			opened := false
			origOpen := openAt
			t.Cleanup(func() { openAt = origOpen })
			openAt = func(dirFd int, entry string, flags int) (int, error) {
				opened = opened || entry == "r.1.0.mca"
				return origOpen(dirFd, entry, flags)
			}
			report, err := checkWithin(t, root)
			assertKeptTheEarlierFinding(t, report, err, 3, 0)
			if opened {
				t.Error("the walk opened an entry its lstat said is not a regular file")
			}
		})
	}
}

// A file that is not a region file is not looked at beyond its directory entry,
// so its vanishing is not even noticed.
func TestWalkerIgnoresAFileThatIsNotARegionVanishingAfterTheListing(t *testing.T) {
	root := tornThenHealthy(t)
	write(t, filepath.Join(root, "region", "session.tmp"), []byte("x"))
	write(t, filepath.Join(root, "latest.log"), []byte("x"))

	orig := statAt
	t.Cleanup(func() { statAt = orig })
	removed := false
	statAt = func(dirFd int, entry string, st *unix.Stat_t) error {
		if !removed {
			removed = true
			for _, rel := range []string{"region/session.tmp", "latest.log"} {
				if err := os.Remove(filepath.Join(root, filepath.FromSlash(rel))); err != nil {
					t.Fatal(err)
				}
			}
		}
		if entry == "session.tmp" || entry == "latest.log" {
			t.Errorf("the walk lstat'ed %q, which its directory entry already says cannot matter", entry)
		}
		return orig(dirFd, entry, st)
	}
	report, err := checkWithin(t, root)
	assertKeptTheEarlierFinding(t, report, err, 4, 0)
}
