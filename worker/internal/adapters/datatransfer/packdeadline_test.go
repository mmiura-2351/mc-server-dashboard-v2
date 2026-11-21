package datatransfer

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
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

// A pack also stops between entries that write nothing to the archive (issue
// #3340): symlinks and special files are left out without a write, so a check on
// the writes alone would let the walk stat every one of them after its context
// ended. Here the context ends at the first symlink's stat; no entry after it is
// stat'd or opened.
func TestPackSnapshotStopsBetweenEntriesThatWriteNothing(t *testing.T) {
	scratch := t.TempDir()
	srcDir := filepath.Join(scratch, "server")
	if err := os.MkdirAll(srcDir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(srcDir, "a.dat"), []byte("a"), 0o640); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"l1", "l2", "l3"} {
		if err := os.Symlink("a.dat", filepath.Join(srcDir, name)); err != nil {
			t.Fatal(err)
		}
	}
	if err := unix.Mkfifo(filepath.Join(srcDir, "p1"), 0o640); err != nil {
		t.Fatal(err)
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
		return origOpen(dirFd, name, flags)
	}
	statAt = func(dirFd int, name string, st *unix.Stat_t) error {
		if cancelled {
			visitedAfter = append(visitedAfter, name)
		}
		if name == "l1" {
			cancel()
			cancelled = true
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

// cancellingWriter ends its context once it holds more than after bytes, and
// counts the writes that reach it from then on.
type cancellingWriter struct {
	cancel      context.CancelFunc
	after       int
	n           int
	writesAfter int
}

func (w *cancellingWriter) Write(p []byte) (int, error) {
	if w.n > w.after {
		w.writesAfter++
	}
	w.n += len(p)
	if w.n > w.after {
		w.cancel()
	}
	return len(p), nil
}

// Inside a file of many copy buffers the pack stops at the next buffer (issue
// #3340): nothing more reaches the archive once its context has ended mid-copy.
func TestPackStopsWithinOneCopyBufferOfALargeFile(t *testing.T) {
	srcDir := filepath.Join(t.TempDir(), "server")
	if err := os.MkdirAll(srcDir, 0o750); err != nil {
		t.Fatal(err)
	}
	const buffer = 32 << 10 // io.Copy's buffer
	if err := os.WriteFile(filepath.Join(srcDir, "r.0.0.mca"), make([]byte, 5*buffer+7), 0o640); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w := &cancellingWriter{cancel: cancel, after: buffer}
	err := packTar(ctx, srcDir, w, slog.Default())
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("packTar = %v, want the context's error", err)
	}
	if w.writesAfter != 0 {
		t.Fatalf("%d writes reached the archive after the context ended", w.writesAfter)
	}
	if w.n >= 5*buffer {
		t.Fatalf("the archive holds %d bytes: the copy ran on after the context ended", w.n)
	}
}

// A snapshot whose context ends during the upload leaves no spool behind (issue
// #3340): the upload is aborted and the spool removed, as on any failed upload.
func TestSnapshotCancelledDuringTheUploadRemovesTheSpool(t *testing.T) {
	scratch := t.TempDir()
	srcDir := filepath.Join(scratch, "server")
	if err := os.MkdirAll(srcDir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(srcDir, "level.dat"), []byte("level"), 0o640); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	srv := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		cancel()
		_, _ = io.Copy(io.Discard, r.Body)
		<-r.Context().Done() // the Worker drops the request
	}))
	defer srv.Close()

	if _, err := New(srv.Client()).Snapshot(ctx, srv.URL, "tok", srcDir, 0, ""); !errors.Is(err, context.Canceled) {
		t.Fatalf("Snapshot err = %v, want context.Canceled", err)
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
