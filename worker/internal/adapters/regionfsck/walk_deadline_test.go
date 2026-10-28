package regionfsck

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

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/regioncheck"
)

// A check whose open is stuck past the snapshot command's deadline (issue #3511)
// returns at the deadline with the deadline's error and no Report. The stuck call
// is abandoned, and once it returns the walk stops instead of scanning on.
func TestCheckerStopsAtTheDeadlineWhileAnOpenIsStuck(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{"r.0.0.mca", "r.0.1.mca", "r.0.2.mca"} {
		if err := os.WriteFile(filepath.Join(root, name), tornRegion(), 0o640); err != nil {
			t.Fatal(err)
		}
	}

	entered := make(chan struct{})
	release := make(chan struct{})
	var released atomic.Bool
	var mu sync.Mutex
	var visitedAfter []string
	origOpen, origStat := openAt, statAt
	t.Cleanup(func() { openAt, statAt = origOpen, origStat })
	openAt = func(dirFd int, name string, flags int) (int, error) {
		if name == "r.0.1.mca" && !released.Load() {
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
		report regioncheck.Report
		err    error
	}
	done := make(chan result, 1)
	go func() {
		report, err := Checker{}.CheckWorkingSet(ctx, root)
		done <- result{report, err}
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the check never reached r.0.1.mca")
	}

	var r result
	select {
	case r = <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("CheckWorkingSet did not return at its deadline while an open was stuck")
	}
	if !errors.Is(r.err, context.DeadlineExceeded) || r.report.Scanned != 0 || len(r.report.Corrupt) != 0 {
		t.Fatalf("CheckWorkingSet = %+v, %v; want no report and the deadline's error", r.report, r.err)
	}

	// Let the stuck open return and wait for the abandoned walk to end.
	unblock()
	buf := make([]byte, 1<<20)
	for deadline := time.Now().Add(5 * time.Second); strings.Contains(string(buf[:runtime.Stack(buf, true)]), "regionfsck.walk"); {
		if time.Now().After(deadline) {
			t.Fatal("the abandoned walk did not end")
		}
		time.Sleep(time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(visitedAfter) != 0 {
		t.Fatalf("the abandoned walk scanned on to %v", visitedAfter)
	}
}
