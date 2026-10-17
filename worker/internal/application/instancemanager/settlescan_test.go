package instancemanager

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// These tests swap an entry between the moment the settle scan lists it and the
// moment it opens or lstats it — the window a running server has, since the scan
// runs as root over a tree that server writes (issue #3388).

// settleWrite writes data at path, creating its parents.
func settleWrite(t *testing.T, path, data string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(data), 0o600); err != nil {
		t.Fatal(err)
	}
}

// settleWorkingDir is a working set with two region files and a root-level
// directory, zz, that holds none.
func settleWorkingDir(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	settleWrite(t, filepath.Join(root, "world", "region", "r.0.0.mca"), "a")
	settleWrite(t, filepath.Join(root, "world", "region", "r.1.0.mca"), "bb")
	settleWrite(t, filepath.Join(root, "zz", "level.dat"), "c")
	return root
}

// exchange atomically swaps the entries at a and b, as a server looping
// renameat2(RENAME_EXCHANGE) does.
func exchange(t *testing.T, a, b string) {
	t.Helper()
	if err := unix.Renameat2(unix.AT_FDCWD, a, unix.AT_FDCWD, b, unix.RENAME_EXCHANGE); err != nil {
		t.Fatal(err)
	}
}

// onOpen runs hook immediately before every open of a directory called name —
// after the scan has listed it as a directory — and passes the open's ordinal
// (1 = the first open of that name). The hook makes the ordering deterministic.
func onOpen(t *testing.T, name string, hook func(n int)) {
	t.Helper()
	orig := settleOpenDirAt
	t.Cleanup(func() { settleOpenDirAt = orig })
	n := 0
	settleOpenDirAt = func(dirFd int, entry string) (int, error) {
		if entry == name {
			n++
			hook(n)
		}
		return orig(dirFd, entry)
	}
}

// swappedDuringOpen makes the open of path meet what a server looping
// renameat2(RENAME_EXCHANGE) between path and a regular file beside it produces:
// a directory when its parent is listed, the file when the scan opens it, and a
// directory again for the next listing.
func swappedDuringOpen(t *testing.T, path string) (before, after func()) {
	t.Helper()
	partner := path + ".partner"
	settleWrite(t, partner, "")
	return func() { exchange(t, path, partner) }, func() { exchange(t, path, partner) }
}

// flipOnOpen wires swappedDuringOpen into every open of path whose ordinal flip
// accepts, and runs during at every open of path's name, flipped or not.
func flipOnOpen(t *testing.T, path string, flip func(n int) bool, during func()) *int {
	t.Helper()
	before, after := swappedDuringOpen(t, path)
	flips := new(int)
	orig := settleOpenDirAt
	t.Cleanup(func() { settleOpenDirAt = orig })
	name, n := filepath.Base(path), 0
	settleOpenDirAt = func(dirFd int, entry string) (int, error) {
		if entry != name {
			return orig(dirFd, entry)
		}
		n++
		during()
		if !flip(n) {
			return orig(dirFd, entry)
		}
		*flips++
		before()
		defer after()
		return orig(dirFd, entry)
	}
	return flips
}

// appendRegion adds one byte through an already open descriptor, the way the
// server's save writes a region file: every scan sees a size the last one did not.
func appendRegion(t *testing.T, path string) func() {
	t.Helper()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = f.Close() })
	return func() { _, _ = f.WriteString("x") }
}

// settleFor runs one settle-wait over root with the given budget, logging to h.
func settleFor(t *testing.T, root string, budget time.Duration, h *capturingSlogHandler) bool {
	t.Helper()
	m := newManager(t, &fakeDriver{}, nil).WithLogger(slog.New(h))
	m.settleBudget = budget
	return m.settleWorkingSet(context.Background(), s1, root)
}

func always(int) bool { return true }

// A directory swapped after the scan listed it makes the scan uncertain: it did not
// see what is there, so it cannot certify anything. Whatever replaced the directory
// is not followed out of the working set (issue #3388).
func TestScanRegionStateIsUncertainAboutADirectorySwappedMidScan(t *testing.T) {
	outside := t.TempDir()
	settleWrite(t, filepath.Join(outside, "r.9.9.mca"), "outside")
	for name, plant := range map[string]func(t *testing.T, path string){
		"exchanged with a file": func(t *testing.T, path string) {
			settleWrite(t, path+".partner", "")
			exchange(t, path, path+".partner")
		},
		"for a symlink to a directory outside": func(t *testing.T, path string) {
			if err := os.Rename(path, path+".moved"); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(outside, path); err != nil {
				t.Fatal(err)
			}
		},
		"for nothing": func(t *testing.T, path string) {
			if err := os.Rename(path, path+".moved"); err != nil {
				t.Fatal(err)
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			root := settleWorkingDir(t)
			onOpen(t, "zz", func(int) { plant(t, filepath.Join(root, "zz")) })

			if state, err := scanRegionState(root); !errors.Is(err, errSettleUncertain) {
				t.Fatalf("scanRegionState = %v, %v; want errSettleUncertain", state, err)
			}
		})
	}
}

// A region file gone between the listing and its lstat makes the scan uncertain.
func TestScanRegionStateIsUncertainAboutARegionFileGoneMidScan(t *testing.T) {
	root := settleWorkingDir(t)
	path := filepath.Join(root, "world", "region", "r.1.0.mca")
	orig := settleStatAt
	t.Cleanup(func() { settleStatAt = orig })
	settleStatAt = func(dirFd int, name string, st *unix.Stat_t) error {
		if name == "r.1.0.mca" {
			_ = os.Remove(path)
		}
		return orig(dirFd, name, st)
	}

	if state, err := scanRegionState(root); !errors.Is(err, errSettleUncertain) {
		t.Fatalf("scanRegionState = %v, %v; want errSettleUncertain", state, err)
	}
}

// A link named like a region file is recorded as the link: the lstat does not follow
// it to whatever it points at outside the working set.
func TestScanRegionStateDoesNotFollowARegionNamedLink(t *testing.T) {
	root := settleWorkingDir(t)
	outside := filepath.Join(t.TempDir(), "big")
	settleWrite(t, outside, strings.Repeat("x", 4096))
	link := filepath.Join(root, "world", "region", "r.2.0.mca")
	if err := os.Symlink(outside, link); err != nil {
		t.Fatal(err)
	}

	got, err := scanRegionState(root)
	if err != nil {
		t.Fatalf("scanRegionState: %v", err)
	}
	if s, ok := got[link]; !ok || s.size != int64(len(outside)) {
		t.Fatalf("region state of %s = %+v (present %v), want the link's own size %d", link, s, ok, len(outside))
	}
}

// Only a failure that says the entry changed makes the scan uncertain. Any other
// failure is a failed scan, as before.
func TestScanRegionStateStillFailsOnAnErrorThatIsNotASwap(t *testing.T) {
	for _, errno := range []unix.Errno{unix.EACCES, unix.EIO} {
		t.Run("open "+errno.Error(), func(t *testing.T) {
			root := settleWorkingDir(t)
			orig := settleOpenDirAt
			t.Cleanup(func() { settleOpenDirAt = orig })
			settleOpenDirAt = func(dirFd int, name string) (int, error) {
				if name == "zz" {
					return -1, errno
				}
				return orig(dirFd, name)
			}
			if state, err := scanRegionState(root); !errors.Is(err, errno) {
				t.Fatalf("scanRegionState = %v, %v; want an error carrying %v", state, err, errno)
			}
		})
		t.Run("lstat "+errno.Error(), func(t *testing.T) {
			root := settleWorkingDir(t)
			orig := settleStatAt
			t.Cleanup(func() { settleStatAt = orig })
			settleStatAt = func(dirFd int, name string, st *unix.Stat_t) error {
				if name == "r.1.0.mca" {
					return errno
				}
				return orig(dirFd, name, st)
			}
			if state, err := scanRegionState(root); !errors.Is(err, errno) {
				t.Fatalf("scanRegionState = %v, %v; want an error carrying %v", state, err, errno)
			}
		})
	}
}

// The scan holds one descriptor per directory level, so it is bounded at the
// depth the region check and the snapshot walk refuse past.
func TestScanRegionStateBoundsItsDepth(t *testing.T) {
	nest := func(t *testing.T, levels int) string {
		t.Helper()
		root := t.TempDir()
		settleWrite(t, filepath.Join(root, strings.Repeat("d/", levels), "r.0.0.mca"), "a")
		return root
	}
	t.Run("at the bound", func(t *testing.T) {
		if got, err := scanRegionState(nest(t, settleScanMaxDepth)); err != nil || len(got) != 1 {
			t.Fatalf("scanRegionState = %v, %v; want the one region file", got, err)
		}
	})
	t.Run("one level deeper", func(t *testing.T) {
		if state, err := scanRegionState(nest(t, settleScanMaxDepth+1)); err == nil {
			t.Fatalf("scanRegionState = %v, nil; want an error", state)
		}
	})
}

// Round-1 review schedule: the region directory is exchanged with a regular file
// beside it after every listing of its parent, and its region file grows through
// an open descriptor during every scan. No scan sees the region directory, so none
// can certify the save; the budget-exhausted WARN counts the uncertain scans.
func TestSettleWorkingSetStaysUnsettledWhileTheRegionDirectoryIsSwappedEveryScan(t *testing.T) {
	root := settleWorkingDir(t)
	region := filepath.Join(root, "world", "region")
	flips := flipOnOpen(t, region, always, appendRegion(t, filepath.Join(region, "r.0.0.mca")))
	h := &capturingSlogHandler{}

	if settleFor(t, root, 50*time.Millisecond, h) {
		t.Fatal("settleWorkingSet = true while the swapped region directory was written on every scan")
	}
	if *flips < 2 {
		t.Fatalf("flips = %d, want one per scan (at least two)", *flips)
	}
	if n := uncertainScansLogged(t, h); n != int64(*flips) {
		t.Fatalf("WARN uncertain_scans = %d, want %d (one per swapped scan)", n, *flips)
	}
}

// A fresh world's empty region directory, seen complete on the first scan, then
// swapped at every open while the save creates and grows a region file in it: the
// complete empty scan must not pair with the incomplete ones that follow.
func TestSettleWorkingSetStaysUnsettledWhenAnEmptyRegionDirectoryIsSwappedAfterTheFirstScan(t *testing.T) {
	root := t.TempDir()
	region := filepath.Join(root, "world", "region")
	if err := os.MkdirAll(region, 0o750); err != nil {
		t.Fatal(err)
	}
	before, after := swappedDuringOpen(t, region)
	flips := 0
	orig := settleOpenDirAt
	t.Cleanup(func() { settleOpenDirAt = orig })
	opens := 0
	settleOpenDirAt = func(dirFd int, name string) (int, error) {
		if name != "region" {
			return orig(dirFd, name)
		}
		if opens++; opens == 1 {
			return orig(dirFd, name)
		}
		flips++
		f, err := os.OpenFile(filepath.Join(region, "r.0.0.mca"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = f.WriteString("x")
		_ = f.Close()
		before()
		defer after()
		return orig(dirFd, name)
	}

	if settleFor(t, root, 50*time.Millisecond, &capturingSlogHandler{}) {
		t.Fatal("settleWorkingSet = true: a complete empty scan paired with an incomplete one")
	}
	if flips < 2 {
		t.Fatalf("flips = %d, want at least two", flips)
	}
}

// uncertainScansLogged returns the uncertain_scans attribute of the
// budget-exhausted WARN.
func uncertainScansLogged(t *testing.T, h *capturingSlogHandler) int64 {
	t.Helper()
	for _, r := range h.records {
		if r.Level != slog.LevelWarn || !strings.Contains(r.Message, "did not settle within budget") {
			continue
		}
		var n int64 = -1
		r.Attrs(func(a slog.Attr) bool {
			if a.Key == "uncertain_scans" {
				n = a.Value.Int64()
			}
			return true
		})
		return n
	}
	t.Fatal("no budget-exhausted WARN")
	return 0
}

// Round-2 review schedule: the region directory is swapped at its open on the
// first scan, moved into zz between scans, and zz is then swapped at every open
// while the region keeps growing. Every scan is incomplete, so none settles.
func TestSettleWorkingSetStaysUnsettledWhenTheRegionDirectoryMovesIntoASwappedDirectory(t *testing.T) {
	root := settleWorkingDir(t)
	region := filepath.Join(root, "world", "region")
	zz := filepath.Join(root, "zz")
	grow := appendRegion(t, filepath.Join(region, "r.0.0.mca"))
	regionBefore, regionAfter := swappedDuringOpen(t, region)
	zzBefore, zzAfter := swappedDuringOpen(t, zz)
	moved := false
	scans := 0
	orig := settleOpenDirAt
	t.Cleanup(func() { settleOpenDirAt = orig })
	settleOpenDirAt = func(dirFd int, name string) (int, error) {
		switch {
		case name == "region" && !moved:
			scans++
			grow()
			regionBefore()
			fd, err := orig(dirFd, name)
			regionAfter()
			if err := os.Rename(region, filepath.Join(zz, "region")); err != nil {
				t.Fatal(err)
			}
			moved = true
			return fd, err
		case name == "zz" && moved:
			scans++
			grow()
			zzBefore()
			defer zzAfter()
		}
		return orig(dirFd, name)
	}

	if settleFor(t, root, 50*time.Millisecond, &capturingSlogHandler{}) {
		t.Fatal("settleWorkingSet = true while the moved, swapped region directory was written on every scan")
	}
	if scans < 3 {
		t.Fatalf("swapped scans = %d, want at least three", scans)
	}
}

// Continuous swapping of a name that holds no region file also keeps the wait
// unsettled until the budget: an incomplete scan cannot tell that name from a
// region directory. This is the deliberate cost of never certifying an incomplete
// observation.
func TestSettleWorkingSetStaysUnsettledWhileAnUnrelatedNameIsSwappedEveryScan(t *testing.T) {
	root := settleWorkingDir(t)
	flips := flipOnOpen(t, filepath.Join(root, "zz"), always, func() {})

	if settleFor(t, root, 50*time.Millisecond, &capturingSlogHandler{}) {
		t.Fatal("settleWorkingSet = true although every scan was incomplete")
	}
	if *flips < 2 {
		t.Fatalf("flips = %d, want one per scan (at least two)", *flips)
	}
}

// A swap on one scan does not stop the wait: the next two complete, identical scans
// settle it.
func TestSettleWorkingSetSettlesAfterASwapOnOneScan(t *testing.T) {
	root := settleWorkingDir(t)
	flips := flipOnOpen(t, filepath.Join(root, "zz"), func(n int) bool { return n == 1 }, func() {})

	if !settleFor(t, root, 5*time.Second, &capturingSlogHandler{}) {
		t.Fatal("settleWorkingSet = false after a single swapped scan")
	}
	if *flips != 1 {
		t.Fatalf("flips = %d, want 1", *flips)
	}
}

// An incomplete scan does not pair with the complete one after it either: here the
// first scan is incomplete, the second sees a world with no region file yet, and
// the save creates one right after. Only the third and fourth scans, both complete
// and identical, settle.
func TestSettleWorkingSetNeedsTwoCompleteScans(t *testing.T) {
	root := t.TempDir()
	settleWrite(t, filepath.Join(root, "zz", "level.dat"), "c")
	flipOnOpen(t, filepath.Join(root, "zz"), func(n int) bool { return n == 1 }, func() {})
	m := newManager(t, &fakeDriver{}, nil)
	m.settleBudget = 5 * time.Second
	scans := 0
	m.scanRegion = func(root string) (regionState, error) {
		state, err := scanRegionState(root)
		if scans++; scans == 2 {
			settleWrite(t, filepath.Join(root, "world", "region", "r.0.0.mca"), "a")
		}
		return state, err
	}

	if !m.settleWorkingSet(context.Background(), s1, root) {
		t.Fatal("settleWorkingSet = false")
	}
	if scans != 4 {
		t.Fatalf("settled after %d scans, want 4 (an incomplete scan paired with nothing)", scans)
	}
}

// A region file deleted between scans is an ordinary change: once two later
// complete scans agree, the wait settles.
func TestSettleWorkingSetSettlesAfterARegionFileIsDeleted(t *testing.T) {
	root := settleWorkingDir(t)
	m := newManager(t, &fakeDriver{}, nil)
	m.settleBudget = 5 * time.Second
	scans := 0
	m.scanRegion = func(root string) (regionState, error) {
		state, err := scanRegionState(root)
		if scans++; scans == 1 {
			if err := os.Remove(filepath.Join(root, "world", "region", "r.1.0.mca")); err != nil {
				t.Fatal(err)
			}
		}
		return state, err
	}

	if !m.settleWorkingSet(context.Background(), s1, root) {
		t.Fatal("settleWorkingSet = false after a region file was deleted once")
	}
	if scans != 3 {
		t.Fatalf("scans = %d, want 3 (the deletion, then two agreeing scans)", scans)
	}
}
