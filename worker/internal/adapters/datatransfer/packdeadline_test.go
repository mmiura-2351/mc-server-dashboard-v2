package datatransfer

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

// A pack whose context ends mid-walk (issue #3511) stops at its next write: it
// returns the context's error, leaves no archive behind, and walks no further.
// The context is cancelled from inside the walk, at the open of b.dat, so the
// test does not depend on timing.
func TestPackSnapshotStopsOnceItsContextIsDone(t *testing.T) {
	scratch := t.TempDir()
	srcDir := filepath.Join(scratch, "server")
	if err := os.MkdirAll(srcDir, 0o750); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"a.dat", "b.dat", "c.dat"} {
		if err := os.WriteFile(filepath.Join(srcDir, name), []byte(strings.Repeat(name[:1], 4096)), 0o640); err != nil {
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
		if name == "b.dat" {
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

	spool, _, err := New(nil).PackSnapshot(ctx, srcDir)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("PackSnapshot = %q, %v; want the context's error", spool, err)
	}
	if len(visitedAfter) != 0 {
		t.Fatalf("the pack walked on to %v after its context ended", visitedAfter)
	}
	if left, _ := filepath.Glob(filepath.Join(scratch, snapshotSpoolPrefix+"*.tar")); len(left) != 0 {
		t.Fatalf("spools left in the scratch root: %v", left)
	}
}

// A pack whose context has already ended creates no archive at all.
func TestPackSnapshotRefusesAnExpiredContext(t *testing.T) {
	scratch := t.TempDir()
	srcDir := filepath.Join(scratch, "server")
	if err := os.MkdirAll(srcDir, 0o750); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, _, err := New(nil).PackSnapshot(ctx, srcDir); !errors.Is(err, context.Canceled) {
		t.Fatalf("PackSnapshot err = %v, want context.Canceled", err)
	}
	if left, _ := filepath.Glob(filepath.Join(scratch, snapshotSpoolPrefix+"*.tar")); len(left) != 0 {
		t.Fatalf("spools left in the scratch root: %v", left)
	}
}
