package fdset

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// recorder is a testing.TB that keeps the failures it is handed instead of
// failing the test, so a test can read what RequireNoneOpenedSince reported.
type recorder struct {
	testing.TB
	failures []string
}

func (r *recorder) Helper() {}

func (r *recorder) Errorf(format string, args ...any) {
	r.failures = append(r.failures, fmt.Sprintf(format, args...))
}

func openFile(t *testing.T) *os.File {
	t.Helper()
	f, err := os.Create(filepath.Join(t.TempDir(), "held"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = f.Close() })
	return f
}

// A descriptor opened after the snapshot and still open is reported, by its
// number and what it is.
func TestADescriptorOpenedSinceTheSnapshotIsReported(t *testing.T) {
	before := Take(t)
	f := openFile(t)
	r := &recorder{TB: t}

	RequireNoneOpenedSince(r, before)

	want := fmt.Sprintf("fd %d -> %s", f.Fd(), f.Name())
	if len(r.failures) != 1 || !strings.Contains(r.failures[0], want) {
		t.Fatalf("failures = %q, want one naming %q", r.failures, want)
	}
}

// A descriptor that was open at the snapshot and closed since, by something
// other than the code under test, is not a leak (issue #3974).
func TestADescriptorClosedSinceTheSnapshotIsNotReported(t *testing.T) {
	f := openFile(t)
	before := Take(t)
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	r := &recorder{TB: t}

	RequireNoneOpenedSince(r, before)

	if len(r.failures) != 0 {
		t.Fatalf("failures = %q, want none", r.failures)
	}
}
