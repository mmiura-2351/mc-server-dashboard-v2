package datatransfer

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// waitForNoGoroutineIn waits until no goroutine's stack holds frame, so a test can
// assert what an abandoned goroutine did once it is provably gone.
func waitForNoGoroutineIn(t *testing.T, frame string) {
	t.Helper()
	buf := make([]byte, 1<<20)
	for deadline := time.Now().Add(5 * time.Second); ; {
		if !strings.Contains(string(buf[:runtime.Stack(buf, true)]), frame) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("a goroutine is still in %s", frame)
		}
		time.Sleep(time.Millisecond)
	}
}

// A pack whose read is stuck past the command's deadline (issue #3511) returns at
// the deadline with the deadline's error and leaves no archive behind. The stuck
// call is abandoned, and once it returns the walk stops: nothing more is read or
// written into the archive, and the archive is at no path a later attempt could
// open.
func TestPackSnapshotStopsAtTheDeadlineWhileAnOpenIsStuck(t *testing.T) {
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

	// The open of b.dat blocks until released, as a read of a stuck file would.
	entered := make(chan struct{})
	release := make(chan struct{})
	var released atomic.Bool
	var mu sync.Mutex
	var visitedAfter []string
	origOpen, origStat := openAt, statAt
	t.Cleanup(func() { openAt, statAt = origOpen, origStat })
	openAt = func(dirFd int, name string, flags int) (int, error) {
		if name == "b.dat" && !released.Load() {
			close(entered)
			<-release
		} else if released.Load() {
			mu.Lock()
			visitedAfter = append(visitedAfter, name)
			mu.Unlock()
		}
		return origOpen(dirFd, name, flags)
	}
	statAt = func(dirFd int, name string, st *unix.Stat_t) error {
		if released.Load() {
			mu.Lock()
			visitedAfter = append(visitedAfter, name)
			mu.Unlock()
		}
		return origStat(dirFd, name, st)
	}
	unblock := sync.OnceFunc(func() { released.Store(true); close(release) })
	t.Cleanup(unblock)

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	type result struct {
		spool string
		err   error
	}
	done := make(chan result, 1)
	go func() {
		spool, _, err := New(nil).PackSnapshot(ctx, srcDir)
		done <- result{spool, err}
	}()

	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the pack never reached b.dat")
	}
	spools, _ := filepath.Glob(filepath.Join(scratch, snapshotSpoolPrefix+"*.tar"))
	if len(spools) != 1 {
		t.Fatalf("spools in the scratch root = %v, want the one being written", spools)
	}
	// Hold the archive's inode so its size stays observable once its name is gone.
	held, err := os.Open(spools[0])
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = held.Close() }()

	var r result
	select {
	case r = <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("PackSnapshot did not return at its deadline while an open was stuck")
	}
	if !errors.Is(r.err, context.DeadlineExceeded) {
		t.Fatalf("PackSnapshot = %q, %v; want the deadline's error", r.spool, r.err)
	}
	if _, err := os.Stat(spools[0]); !os.IsNotExist(err) {
		t.Fatalf("the partial archive is still at %s: stat err = %v", spools[0], err)
	}
	before, err := held.Stat()
	if err != nil {
		t.Fatal(err)
	}

	// Let the stuck open return and wait for the abandoned pack to end.
	unblock()
	waitForNoGoroutineIn(t, "datatransfer.packTar")

	after, err := held.Stat()
	if err != nil {
		t.Fatal(err)
	}
	if after.Size() != before.Size() {
		t.Fatalf("the abandoned pack wrote on: archive grew from %d to %d bytes", before.Size(), after.Size())
	}
	mu.Lock()
	defer mu.Unlock()
	if len(visitedAfter) != 0 {
		t.Fatalf("the abandoned pack walked on to %v", visitedAfter)
	}
	if left, _ := filepath.Glob(filepath.Join(scratch, snapshotSpoolPrefix+"*.tar")); len(left) != 0 {
		t.Fatalf("spools left in the scratch root: %v", left)
	}
}

// A pack whose context has already expired creates no archive at all.
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
