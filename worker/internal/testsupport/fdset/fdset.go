// Package fdset pins that code under test leaves no descriptor of its own open:
// a test takes a Snapshot of the process's open descriptors, runs the code, and
// calls RequireNoneOpenedSince.
//
// It compares the sets of fd numbers, not their counts (issue #3974). The test
// binary's descriptor table is shared with everything else running in it, and a
// descriptor an earlier test left behind can close while the code under test
// runs, as a connection net/http's transport closes from its own goroutine does.
// A count then comes out lower and fails a test whose code leaked nothing.
//
// The kernel hands out the lowest free number, so a leak that lands on the
// number of a descriptor closed in the same window is not seen. Such a run is
// rare; the next one sees the leak.
//
// It is an ordinary package rather than a _test.go file because several
// packages' tests share it. Only _test.go files import it: the depguard rule
// "testsupport" in worker/.golangci.yml refuses it in any other file.
package fdset

import (
	"os"
	"slices"
	"strconv"
	"testing"
)

// Snapshot is the descriptors open in the process at one moment: each fd number
// with what /proc/self/fd names it.
type Snapshot map[int]string

// Take returns the descriptors open now. It skips t where /proc/self/fd cannot be
// listed. The listing's own descriptor is left out, as is one closed before its
// name could be read.
func Take(t testing.TB) Snapshot {
	t.Helper()
	dir, err := os.Open("/proc/self/fd")
	if err != nil {
		t.Skipf("no /proc/self/fd to list descriptors with: %v", err)
	}
	defer func() { _ = dir.Close() }()
	names, err := dir.Readdirnames(-1)
	if err != nil {
		t.Skipf("no /proc/self/fd to list descriptors with: %v", err)
	}
	own := int(dir.Fd())
	open := Snapshot{}
	for _, name := range names {
		fd, err := strconv.Atoi(name)
		if err != nil || fd == own {
			continue
		}
		if target, err := os.Readlink("/proc/self/fd/" + name); err == nil {
			open[fd] = target
		}
	}
	return open
}

// RequireNoneOpenedSince fails t once for each descriptor open now whose number
// was not open at before, naming the number and what the descriptor is.
func RequireNoneOpenedSince(t testing.TB, before Snapshot) {
	t.Helper()
	after := Take(t)
	fds := make([]int, 0, len(after))
	for fd := range after {
		if _, ok := before[fd]; !ok {
			fds = append(fds, fd)
		}
	}
	slices.Sort(fds)
	for _, fd := range fds {
		t.Errorf("descriptor left open: fd %d -> %s", fd, after[fd])
	}
}
