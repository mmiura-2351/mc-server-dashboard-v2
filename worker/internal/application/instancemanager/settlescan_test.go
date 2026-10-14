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

func scanOrFail(t *testing.T, root string) regionState {
	t.Helper()
	state, err := scanRegionState(root)
	if err != nil {
		t.Fatalf("scanRegionState: %v", err)
	}
	return state
}

// A name that holds no region file, swapped after the scan listed it as a
// directory, is skipped: the scan completes with the same region state it reports
// when nothing is swapped, so a server flipping such a name cannot keep the save
// from settling. Whatever replaced the directory is not followed (issue #3388).
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
			want := scanOrFail(t, root)
			beforeSettleOpen(t, "zz", func() { plant(t, filepath.Join(root, "zz")) })

			got := scanOrFail(t, root)
			if !regionStateEqual(want, got) {
				t.Fatalf("region state with zz swapped mid-scan = %v, want %v", got, want)
			}
		})
	}
}

// A directory of region files swapped mid-scan is skipped the same way, and its
// region files are then missing from the scan — which is what keeps the settle-wait
// polling: the scan differs from one that saw them.
func TestScanRegionStateMissesTheRegionsOfADirectorySwappedMidScan(t *testing.T) {
	root := settleWorkingDir(t)
	stable := scanOrFail(t, root)
	region := filepath.Join(root, "world", "region")
	settleWrite(t, region+".partner", "")
	beforeSettleOpen(t, "region", func() { exchange(t, region, region+".partner") })

	got := scanOrFail(t, root)
	if len(got) != 0 {
		t.Fatalf("region state with world/region swapped for a file mid-scan = %v, want empty", got)
	}
	if regionStateEqual(stable, got) {
		t.Fatal("a scan that lost the region directory reads as equal to one that saw it")
	}
}

// The scan goes on past a skipped directory: whichever of two region-holding
// directories it opens first is swapped for a file, and the other's region file is
// still recorded, whatever order the directory lists them in.
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

	got := scanOrFail(t, root)
	other := map[string]string{"a": "b", "b": "a"}[swapped]
	if _, ok := got[filepath.Join(root, other, "r.0.0.mca")]; !ok || len(got) != 1 {
		t.Fatalf("region state with %s swapped mid-scan = %v, want only %s/r.0.0.mca", swapped, got, other)
	}
}

// A region file gone between the listing and its lstat is left out rather than
// failing the scan, and the scan goes on: whichever of two region files it lstats
// first is removed, and the other is still recorded.
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

	got := scanOrFail(t, root)
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

	got := scanOrFail(t, root)
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
		if got := scanOrFail(t, nest(t, settleScanMaxDepth)); len(got) != 1 {
			t.Fatalf("region state = %v, want the one region file", got)
		}
	})
	t.Run("one level deeper", func(t *testing.T) {
		if state, err := scanRegionState(nest(t, settleScanMaxDepth+1)); err == nil {
			t.Fatalf("scanRegionState = %v, nil; want an error", state)
		}
	})
}

// flipRootNameEveryScan makes every settle scan meet what a server looping
// renameat2(RENAME_EXCHANGE) on a root-level name produces: .mcsd_generation is a
// directory when the scan lists the root and a file when the scan opens it, and is
// a directory again for the next scan's listing. during runs inside each flip. It
// returns the working dir and a counter of the flips.
func flipRootNameEveryScan(t *testing.T, during func(root string)) (string, *int) {
	t.Helper()
	root := settleWorkingDir(t)
	marker := filepath.Join(root, ".mcsd_generation")
	settleWrite(t, filepath.Join(marker, "x"), "")
	settleWrite(t, marker+".partner", "")
	flips := new(int)
	orig := settleOpenDirAt
	t.Cleanup(func() { settleOpenDirAt = orig })
	settleOpenDirAt = func(dirFd int, name string) (int, error) {
		if name != ".mcsd_generation" {
			return orig(dirFd, name)
		}
		*flips++
		exchange(t, marker, marker+".partner")
		defer exchange(t, marker, marker+".partner")
		during(root)
		return orig(dirFd, name)
	}
	return root, flips
}

// The issue's shape end to end: server code flipping a root-level name between a
// directory and a file on every scan cannot keep a static world from settling.
func TestSettleWorkingSetSettlesWhileARootNameIsSwappedEveryScan(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	m.settleBudget = 5 * time.Second
	root, flips := flipRootNameEveryScan(t, func(string) {})

	if !m.settleWorkingSet(context.Background(), "s1", root) {
		t.Fatal("settleWorkingSet = false; a swapped root-level name kept a static world from settling")
	}
	if *flips < 2 {
		t.Fatalf("flips = %d, want one per scan (at least two)", *flips)
	}
}

// Under the same swapping, a region file that keeps changing still keeps the
// working set unsettled: skipping swapped entries does not relax the criterion.
func TestSettleWorkingSetStaysUnsettledWhileARegionKeepsChanging(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	m.settleBudget = 50 * time.Millisecond
	root, flips := flipRootNameEveryScan(t, func(root string) {
		// One more byte per scan: every scan sees a size the previous one did not.
		f, err := os.OpenFile(filepath.Join(root, "world", "region", "r.0.0.mca"), os.O_APPEND|os.O_WRONLY, 0)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = f.WriteString("x")
		_ = f.Close()
	})

	if m.settleWorkingSet(context.Background(), "s1", root) {
		t.Fatal("settleWorkingSet = true while a region file changed on every scan")
	}
	if *flips < 2 {
		t.Fatalf("flips = %d, want one per scan (at least two)", *flips)
	}
}
