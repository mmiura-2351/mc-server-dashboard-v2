package instancemanager

import (
	"context"
	"errors"
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

// beforeSettleOpen runs swap immediately before every open of the directory called
// name — after the scan has listed it as a directory. The hook makes the ordering
// deterministic.
func beforeSettleOpen(t *testing.T, name string, swap func()) {
	t.Helper()
	orig := settleOpenDirAt
	t.Cleanup(func() { settleOpenDirAt = orig })
	settleOpenDirAt = func(dirFd int, entry string) (int, error) {
		if entry == name {
			swap()
		}
		return orig(dirFd, entry)
	}
}

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

// scanOrFail is a scan that must be complete and certain.
func scanOrFail(t *testing.T, root string, mem *settleMemory) regionState {
	t.Helper()
	state, err := scanRegionState(root, mem)
	if err != nil {
		t.Fatalf("scanRegionState: %v", err)
	}
	return state
}

// scanUncertain is a scan that must report errSettleUncertain; it returns what the
// scan still recorded.
func scanUncertain(t *testing.T, root string, mem *settleMemory) regionState {
	t.Helper()
	state, err := scanRegionState(root, mem)
	if !errors.Is(err, errSettleUncertain) {
		t.Fatalf("scanRegionState = %v, %v; want errSettleUncertain", state, err)
	}
	return state
}

// A name this settle-wait has seen as a directory with no region file, swapped
// after the scan listed it as a directory, is skipped: the scan is certain and
// reports the same region state as when nothing is swapped, so a server flipping
// such a name cannot keep the save from settling. Whatever replaced the directory
// is not followed (issue #3388).
func TestScanRegionStateSkipsADirectorySwappedMidScan(t *testing.T) {
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
			mem := newSettleMemory()
			want := scanOrFail(t, root, mem)
			beforeSettleOpen(t, "zz", func() { plant(t, filepath.Join(root, "zz")) })

			got := scanOrFail(t, root, mem)
			if !regionStateEqual(want, got) {
				t.Fatalf("region state with zz swapped mid-scan = %v, want %v", got, want)
			}
		})
	}
}

// The same swap on a name this settle-wait has never descended into leaves the scan
// uncertain: nothing says it holds no region file. The scan still records the rest.
func TestScanRegionStateIsUncertainAboutADirectoryItHasNotSeen(t *testing.T) {
	root := settleWorkingDir(t)
	zz := filepath.Join(root, "zz")
	settleWrite(t, zz+".partner", "")
	beforeSettleOpen(t, "zz", func() { exchange(t, zz, zz+".partner") })

	if got := scanUncertain(t, root, newSettleMemory()); len(got) != 2 {
		t.Fatalf("region state with an unseen zz swapped mid-scan = %v, want both region files", got)
	}
}

// A directory that held region files when last seen, swapped mid-scan, leaves the
// scan uncertain: the region files it misses may be the ones still being written.
func TestScanRegionStateIsUncertainAboutASkippedRegionDirectory(t *testing.T) {
	root := settleWorkingDir(t)
	mem := newSettleMemory()
	scanOrFail(t, root, mem)
	region := filepath.Join(root, "world", "region")
	settleWrite(t, region+".partner", "")
	beforeSettleOpen(t, "region", func() { exchange(t, region, region+".partner") })

	if got := scanUncertain(t, root, mem); len(got) != 0 {
		t.Fatalf("region state with world/region swapped for a file mid-scan = %v, want empty", got)
	}
}

// Region files an earlier scan saw, moved into a directory already seen empty that
// is then swapped at the open, leave the scan uncertain: they dropped out of view
// while something was skipped.
func TestScanRegionStateIsUncertainWhenARegionFileItSawIsMissing(t *testing.T) {
	root := settleWorkingDir(t)
	mem := newSettleMemory()
	scanOrFail(t, root, mem)
	zz := filepath.Join(root, "zz")
	if err := os.Rename(filepath.Join(root, "world", "region"), filepath.Join(zz, "region")); err != nil {
		t.Fatal(err)
	}
	settleWrite(t, zz+".partner", "")
	beforeSettleOpen(t, "zz", func() { exchange(t, zz, zz+".partner") })

	if got := scanUncertain(t, root, mem); len(got) != 0 {
		t.Fatalf("region state = %v, want empty", got)
	}
}

// A region file deleted between two scans that skipped nothing is an ordinary
// change: the later scan is complete, so it is certain without the file.
func TestScanRegionStateIsCertainAboutARegionFileDeletedWithoutASkip(t *testing.T) {
	root := settleWorkingDir(t)
	mem := newSettleMemory()
	scanOrFail(t, root, mem)
	if err := os.Remove(filepath.Join(root, "world", "region", "r.1.0.mca")); err != nil {
		t.Fatal(err)
	}

	if got := scanOrFail(t, root, mem); len(got) != 1 {
		t.Fatalf("region state after deleting r.1.0.mca = %v, want the one left", got)
	}
}

// A directory counts as region-free only if its whole subtree was seen: one whose
// subdirectory was skipped unseen on the scan that descended it may hold region
// files, so skipping it later leaves the scan uncertain.
func TestScanRegionStateIsUncertainAboutADirectoryNotSeenWhole(t *testing.T) {
	root := t.TempDir()
	a := filepath.Join(root, "a")
	c := filepath.Join(a, "b", "c")
	settleWrite(t, filepath.Join(c, "x"), "")
	settleWrite(t, c+".partner", "")
	settleWrite(t, a+".partner", "")
	mem := newSettleMemory()
	beforeSettleOpen(t, "c", func() { exchange(t, c, c+".partner") })
	scanUncertain(t, root, mem)
	exchange(t, c, c+".partner") // c is a directory again

	beforeSettleOpen(t, "a", func() { exchange(t, a, a+".partner") })
	scanUncertain(t, root, mem)
}

// A directory whose region files all vanished at the lstat on the scan that
// descended it is not taken for region-free: skipping it later leaves the scan
// uncertain.
func TestScanRegionStateIsUncertainAboutADirectoryWhoseRegionFilesVanished(t *testing.T) {
	root := settleWorkingDir(t)
	region := filepath.Join(root, "world", "region")
	mem := newSettleMemory()
	orig := settleStatAt
	t.Cleanup(func() { settleStatAt = orig })
	settleStatAt = func(int, string, *unix.Stat_t) error { return unix.ENOENT }
	scanUncertain(t, root, mem)
	settleStatAt = orig

	settleWrite(t, region+".partner", "")
	beforeSettleOpen(t, "region", func() { exchange(t, region, region+".partner") })
	scanUncertain(t, root, mem)
}

// The scan goes on past a skip: whichever of two region-holding directories it
// opens first is swapped for a file, and the other's region file is still recorded,
// whatever order the directory lists them in.
func TestScanRegionStateGoesOnPastASwappedDirectory(t *testing.T) {
	root := t.TempDir()
	for _, d := range []string{"a", "b"} {
		settleWrite(t, filepath.Join(root, d, "r.0.0.mca"), "a")
		settleWrite(t, filepath.Join(root, d+".partner"), "")
	}
	var swapped string
	orig := settleOpenDirAt
	t.Cleanup(func() { settleOpenDirAt = orig })
	settleOpenDirAt = func(dirFd int, name string) (int, error) {
		if swapped == "" {
			swapped = name
			exchange(t, filepath.Join(root, name), filepath.Join(root, name+".partner"))
		}
		return orig(dirFd, name)
	}

	got := scanUncertain(t, root, newSettleMemory())
	other := map[string]string{"a": "b", "b": "a"}[swapped]
	if _, ok := got[filepath.Join(root, other, "r.0.0.mca")]; !ok || len(got) != 1 {
		t.Fatalf("region state with %s swapped mid-scan = %v, want only %s/r.0.0.mca", swapped, got, other)
	}
}

// A region file gone between the listing and its lstat leaves the scan uncertain —
// a region file vanishing is region churn — and the scan goes on: whichever of two
// region files it lstats first is removed, and the other is still recorded.
func TestScanRegionStateSkipsARegionFileGoneMidScan(t *testing.T) {
	root := settleWorkingDir(t)
	region := filepath.Join(root, "world", "region")
	var removed string
	orig := settleStatAt
	t.Cleanup(func() { settleStatAt = orig })
	settleStatAt = func(dirFd int, name string, st *unix.Stat_t) error {
		if removed == "" {
			removed = name
			if err := os.Remove(filepath.Join(region, name)); err != nil {
				t.Fatal(err)
			}
		}
		return orig(dirFd, name, st)
	}

	got := scanUncertain(t, root, newSettleMemory())
	other := map[string]string{"r.0.0.mca": "r.1.0.mca", "r.1.0.mca": "r.0.0.mca"}[removed]
	if _, ok := got[filepath.Join(region, other)]; !ok || len(got) != 1 {
		t.Fatalf("region state with %s removed mid-scan = %v, want only %s", removed, got, other)
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

	got := scanOrFail(t, root, newSettleMemory())
	if s, ok := got[link]; !ok || s.size != int64(len(outside)) {
		t.Fatalf("region state of %s = %+v (present %v), want the link's own size %d", link, s, ok, len(outside))
	}
}

// Only a failure that says the entry changed is skipped. Any other failure is a
// failed scan, as before: the scan did not see what is there.
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
			if state, err := scanRegionState(root, newSettleMemory()); !errors.Is(err, errno) {
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
			if state, err := scanRegionState(root, newSettleMemory()); !errors.Is(err, errno) {
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
		if got := scanOrFail(t, nest(t, settleScanMaxDepth), newSettleMemory()); len(got) != 1 {
			t.Fatalf("region state = %v, want the one region file", got)
		}
	})
	t.Run("one level deeper", func(t *testing.T) {
		if state, err := scanRegionState(nest(t, settleScanMaxDepth+1), newSettleMemory()); err == nil {
			t.Fatalf("scanRegionState = %v, nil; want an error", state)
		}
	})
}

// flipOnOpen makes the settle scans meet what a server looping
// renameat2(RENAME_EXCHANGE) between path and a regular file beside it produces:
// path is a directory when its parent is listed and the file when the scan opens
// it, and a directory again for the next scan's listing. The flip starts at the
// fromOpen-th open of path's name (1 = the first scan). during runs at every open of
// that name, flipped or not, so it runs once per scan. It returns a counter of the
// flips.
func flipOnOpen(t *testing.T, path string, fromOpen int, during func()) *int {
	t.Helper()
	partner := path + ".partner"
	settleWrite(t, partner, "")
	name := filepath.Base(path)
	opens, flips := 0, new(int)
	orig := settleOpenDirAt
	t.Cleanup(func() { settleOpenDirAt = orig })
	settleOpenDirAt = func(dirFd int, entry string) (int, error) {
		if entry != name {
			return orig(dirFd, entry)
		}
		during()
		if opens++; opens < fromOpen {
			return orig(dirFd, entry)
		}
		*flips++
		exchange(t, path, partner)
		defer exchange(t, path, partner)
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

// settleFor runs one settle-wait over root with the given budget.
func settleFor(t *testing.T, root string, budget time.Duration) bool {
	t.Helper()
	m := newManager(t, &fakeDriver{}, nil)
	m.settleBudget = budget
	return m.settleWorkingSet(context.Background(), "s1", root)
}

// wantFlips fails unless the flip ran at least least times.
func wantFlips(t *testing.T, flips *int, least int) {
	t.Helper()
	if *flips < least {
		t.Fatalf("flips = %d, want at least %d", *flips, least)
	}
}

// The issue's shape end to end: server code flipping a root-level name between a
// directory and a file on every scan cannot keep a static world from settling, once
// one scan has seen the name as a directory with no region file — which a real flip
// loop gives, the name being a directory at most of the scan's opens.
func TestSettleWorkingSetSettlesWhileARootNameIsSwappedEveryScan(t *testing.T) {
	root := settleWorkingDir(t)
	settleWrite(t, filepath.Join(root, ".mcsd_generation", "x"), "")
	flips := flipOnOpen(t, filepath.Join(root, ".mcsd_generation"), 2, func() {})

	if !settleFor(t, root, 5*time.Second) {
		t.Fatal("settleWorkingSet = false; a swapped root-level name kept a static world from settling")
	}
	wantFlips(t, flips, 1)
}

// A name the settle-wait never once sees as a directory stays unknown, so its flip
// keeps the wait uncertain until the budget: a refusal, the conservative outcome.
func TestSettleWorkingSetStaysUnsettledWhileANameItNeverSawIsSwapped(t *testing.T) {
	root := settleWorkingDir(t)
	settleWrite(t, filepath.Join(root, ".mcsd_generation", "x"), "")
	flips := flipOnOpen(t, filepath.Join(root, ".mcsd_generation"), 1, func() {})

	if settleFor(t, root, 50*time.Millisecond) {
		t.Fatal("settleWorkingSet = true; a name never seen as a directory was skipped as region-free")
	}
	wantFlips(t, flips, 2)
}

// Under the same swapping of an unrelated name, a region file that keeps changing
// still keeps the working set unsettled: skipping does not relax the criterion.
func TestSettleWorkingSetStaysUnsettledWhileARegionKeepsChanging(t *testing.T) {
	root := settleWorkingDir(t)
	settleWrite(t, filepath.Join(root, ".mcsd_generation", "x"), "")
	grow := appendRegion(t, filepath.Join(root, "world", "region", "r.0.0.mca"))
	flips := flipOnOpen(t, filepath.Join(root, ".mcsd_generation"), 2, grow)

	if settleFor(t, root, 50*time.Millisecond) {
		t.Fatal("settleWorkingSet = true while a region file changed on every scan")
	}
	wantFlips(t, flips, 2)
}

// The review's case: the region directory itself is exchanged with a regular file
// beside it after every listing of its parent, and its region file grows through an
// open descriptor during each scan. Every scan misses the region directory, so
// every scan reports the same empty state; the wait must not take that for a
// settled save, whether or not an earlier scan saw the region files.
func TestSettleWorkingSetStaysUnsettledWhileTheRegionDirectoryIsSwappedEveryScan(t *testing.T) {
	for name, fromOpen := range map[string]int{"from the first scan": 1, "after one scan saw it": 2} {
		t.Run(name, func(t *testing.T) {
			root := settleWorkingDir(t)
			region := filepath.Join(root, "world", "region")
			grow := appendRegion(t, filepath.Join(region, "r.0.0.mca"))
			flips := flipOnOpen(t, region, fromOpen, grow)

			if settleFor(t, root, 50*time.Millisecond) {
				t.Fatal("settleWorkingSet = true while the swapped region directory was written on every scan")
			}
			wantFlips(t, flips, 2)
		})
	}
}

// A region file that vanishes at every lstat — moved aside after the listing and
// back after the lstat — keeps the wait unsettled: that is region churn.
func TestSettleWorkingSetStaysUnsettledWhileARegionFileVanishesEveryScan(t *testing.T) {
	root := settleWorkingDir(t)
	path := filepath.Join(root, "world", "region", "r.1.0.mca")
	vanished := 0
	orig := settleStatAt
	t.Cleanup(func() { settleStatAt = orig })
	settleStatAt = func(dirFd int, name string, st *unix.Stat_t) error {
		if name != "r.1.0.mca" {
			return orig(dirFd, name, st)
		}
		vanished++
		if err := os.Rename(path, path+".aside"); err != nil {
			t.Fatal(err)
		}
		defer func() {
			if err := os.Rename(path+".aside", path); err != nil {
				t.Fatal(err)
			}
		}()
		return orig(dirFd, name, st)
	}

	if settleFor(t, root, 50*time.Millisecond) {
		t.Fatal("settleWorkingSet = true while a region file vanished on every scan")
	}
	if vanished < 2 {
		t.Fatalf("vanished = %d, want one per scan (at least two)", vanished)
	}
}
