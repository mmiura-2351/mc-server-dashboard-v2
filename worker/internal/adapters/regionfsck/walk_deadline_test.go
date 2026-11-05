package regionfsck

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

// A check whose context ends mid-walk (issue #3511) stops at its next entry: it
// returns the context's error and no Report, and scans no further. The context is
// cancelled from inside the walk, at the open of r.0.1.mca, so the test does not
// depend on timing.
func TestCheckerStopsOnceItsContextIsDone(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{"r.0.0.mca", "r.0.1.mca", "r.0.2.mca"} {
		if err := os.WriteFile(filepath.Join(root, name), tornRegion(), 0o640); err != nil {
			t.Fatal(err)
		}
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var visitedAfter []string
	cancelled := false
	origOpen, origStat := openAt, statAt
	t.Cleanup(func() { openAt, statAt = origOpen, origStat })
	openAt = func(dirFd int, name string, flags int) (int, error) {
		if cancelled {
			visitedAfter = append(visitedAfter, name)
		}
		if name == "r.0.1.mca" {
			cancel()
			cancelled = true
		}
		return origOpen(dirFd, name, flags)
	}
	statAt = func(dirFd int, name string, st *unix.Stat_t) error {
		if cancelled {
			visitedAfter = append(visitedAfter, name)
		}
		return origStat(dirFd, name, st)
	}

	report, err := Checker{}.CheckWorkingSet(ctx, root)
	if !errors.Is(err, context.Canceled) || report.Scanned != 0 || len(report.Corrupt) != 0 {
		t.Fatalf("CheckWorkingSet = %+v, %v; want no report and the context's error", report, err)
	}
	if len(visitedAfter) != 0 {
		t.Fatalf("the check scanned on to %v after its context ended", visitedAfter)
	}
}
