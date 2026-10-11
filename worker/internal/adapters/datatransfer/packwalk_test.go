package datatransfer

import (
	"archive/tar"
	"bytes"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// These tests pin the snapshot walk against a working set that changes under it
// (issue #3334). A snapshot can be taken of a running server, whose code shares
// the working set with the Worker and can replace an entry between the moment the
// walk decides what the entry is and the moment it opens it.

// beforeOpening runs swap once, immediately before the walk opens the entry called
// name — after the walk has already decided what kind of entry it is. That is the
// window a running server has; the hook makes the ordering deterministic.
func beforeOpening(t *testing.T, name string, swap func()) {
	t.Helper()
	orig := openAt
	t.Cleanup(func() { openAt = orig })
	done := false
	openAt = func(dirFd int, entry string, flags int) (int, error) {
		if !done && entry == name {
			done = true
			swap()
		}
		return orig(dirFd, entry, flags)
	}
}

// openDirFd opens dir the way the walk opens the working-set root; the descriptor
// is closed when the test ends.
func openDirFd(t *testing.T, dir string) int {
	t.Helper()
	fd, err := unix.Open(dir, packOpenDirFlags, 0)
	if err != nil {
		t.Fatalf("open %s: %v", dir, err)
	}
	t.Cleanup(func() { _ = unix.Close(fd) })
	return fd
}

// walkRoot runs walkInto over root as packTar does, without a path-length limit.
func walkRoot(t *testing.T, tw *tar.Writer, root string, log *slog.Logger) error {
	t.Helper()
	fd, err := unix.Open(root, packOpenDirFlags, 0)
	if err != nil {
		t.Fatalf("open %s: %v", root, err)
	}
	return walkInto(tw, fd, "", unix.PathMax, 0, log)
}

// packMembers packs srcDir and returns each member's body by name (a directory
// member maps to an empty body).
func packMembers(t *testing.T, srcDir string, log *slog.Logger) map[string]string {
	t.Helper()
	var buf bytes.Buffer
	if err := packTar(srcDir, &buf, log); err != nil {
		t.Fatalf("packTar: %v", err)
	}
	members := map[string]string{}
	tr := tar.NewReader(&buf)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return members
		}
		if err != nil {
			t.Fatalf("tar.Next: %v", err)
		}
		body, err := io.ReadAll(tr)
		if err != nil {
			t.Fatalf("read member %q: %v", hdr.Name, err)
		}
		members[hdr.Name] = string(body)
	}
}

// outsideSecret is what a file outside the working set holds; it must never show
// up in an archive.
const outsideSecret = "TOP-SECRET host file, not part of any working set"

// assertNothingFromOutside fails when any member carries the outside file's bytes
// or sits under one of the given names.
func assertNothingFromOutside(t *testing.T, members map[string]string, forbiddenNames ...string) {
	t.Helper()
	for name, body := range members {
		if strings.Contains(body, outsideSecret[:10]) {
			t.Errorf("member %q carries the content of a file outside the working set: %q", name, body)
		}
	}
	for _, name := range forbiddenNames {
		if _, ok := members[name]; ok {
			t.Errorf("member %q was archived from outside the working set", name)
		}
	}
}

// writeTree creates dir/<rel> = content for each entry, with parents.
func writeTree(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	for rel, content := range files {
		full := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(full), 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(content), 0o640); err != nil {
			t.Fatal(err)
		}
	}
}

// A directory replaced by a symlink after the walk has seen it as a directory must
// not lead the walk out of the working set.
func TestPackDoesNotFollowADirectorySwappedForASymlink(t *testing.T) {
	base := t.TempDir()
	work, outside := filepath.Join(base, "work"), filepath.Join(base, "outside")
	writeTree(t, work, map[string]string{"kept.txt": "keep", "plugins/inside.txt": "inside"})
	writeTree(t, outside, map[string]string{"secret.txt": outsideSecret})

	beforeOpening(t, "plugins", func() {
		if err := os.Rename(filepath.Join(work, "plugins"), filepath.Join(work, "plugins.moved")); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(outside, filepath.Join(work, "plugins")); err != nil {
			t.Fatal(err)
		}
	})

	h := &capturingHandler{}
	members := packMembers(t, work, slog.New(h))

	assertNothingFromOutside(t, members, "plugins/secret.txt")
	if members["kept.txt"] != "keep" {
		t.Errorf("kept.txt = %q, want it archived untouched", members["kept.txt"])
	}
	const wantMsg = "snapshot: entry changed type between stat and open; skipping"
	if !h.hasMessage(wantMsg) {
		t.Errorf("expected log message %q, captured records: %v", wantMsg, h.records)
	}
}

// A regular file replaced by a symlink after the walk has seen it as a regular
// file must not have the link's target packed under its name.
func TestPackDoesNotFollowAFileSwappedForASymlink(t *testing.T) {
	base := t.TempDir()
	work, outside := filepath.Join(base, "work"), filepath.Join(base, "outside")
	// Longer than the secret, so a followed link would be archived in full rather
	// than capped at the size the walk saw.
	writeTree(t, work, map[string]string{"kept.txt": "keep", "server.properties": strings.Repeat("#", 2*len(outsideSecret))})
	writeTree(t, outside, map[string]string{"secret.txt": outsideSecret})

	leaf := filepath.Join(work, "server.properties")
	beforeOpening(t, "server.properties", func() {
		if err := os.Remove(leaf); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(filepath.Join(outside, "secret.txt"), leaf); err != nil {
			t.Fatal(err)
		}
	})

	h := &capturingHandler{}
	members := packMembers(t, work, slog.New(h))

	assertNothingFromOutside(t, members, "server.properties")
	if members["kept.txt"] != "keep" {
		t.Errorf("kept.txt = %q, want it archived untouched", members["kept.txt"])
	}
	const wantMsg = "snapshot: entry changed type between stat and open; skipping"
	if !h.hasMessage(wantMsg) {
		t.Errorf("expected log message %q, captured records: %v", wantMsg, h.records)
	}
}

// A regular file replaced by a FIFO nobody writes to must neither block the walk
// nor be read.
func TestPackDoesNotBlockOnAFileSwappedForAFIFO(t *testing.T) {
	work := filepath.Join(t.TempDir(), "work")
	writeTree(t, work, map[string]string{"kept.txt": "keep", "latest.log": "log"})

	leaf := filepath.Join(work, "latest.log")
	beforeOpening(t, "latest.log", func() {
		if err := os.Remove(leaf); err != nil {
			t.Fatal(err)
		}
		if err := syscall.Mkfifo(leaf, 0o640); err != nil {
			t.Fatal(err)
		}
	})

	type result struct {
		buf bytes.Buffer
		err error
	}
	done := make(chan *result, 1)
	go func() {
		r := &result{}
		r.err = packTar(work, &r.buf, slog.Default())
		done <- r
	}()

	select {
	case r := <-done:
		if r.err != nil {
			t.Fatalf("packTar: %v", r.err)
		}
		tr := tar.NewReader(&r.buf)
		for {
			hdr, err := tr.Next()
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Fatalf("tar.Next: %v", err)
			}
			if hdr.Name == "latest.log" {
				t.Error("a FIFO was archived as latest.log")
			}
		}
	case <-time.After(5 * time.Second):
		// Unblock the stuck open so the goroutine does not outlive the test.
		if w, err := os.OpenFile(leaf, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = w.Close()
		}
		t.Fatal("packTar blocked on a FIFO swapped in for a regular file")
	}
}

// The two remaining replacements — a directory that became a file, a file that
// became a directory — are skipped too: the member is never written from an entry
// other than the kind the walk set out to archive.
func TestPackSkipsAnEntryThatChangedKind(t *testing.T) {
	tests := []struct {
		name   string
		entry  string
		swap   func(t *testing.T, path string)
		absent []string
	}{
		{
			name:  "directory replaced by a regular file",
			entry: "plugins",
			swap: func(t *testing.T, path string) {
				if err := os.RemoveAll(path); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(path, []byte("now a file"), 0o640); err != nil {
					t.Fatal(err)
				}
			},
			absent: []string{"plugins", "plugins/", "plugins/inside.txt"},
		},
		{
			name:  "regular file replaced by a directory",
			entry: "latest.log",
			swap: func(t *testing.T, path string) {
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
				if err := os.Mkdir(path, 0o750); err != nil {
					t.Fatal(err)
				}
			},
			absent: []string{"latest.log", "latest.log/"},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			work := filepath.Join(t.TempDir(), "work")
			writeTree(t, work, map[string]string{"kept.txt": "keep", "latest.log": "log", "plugins/inside.txt": "inside"})
			beforeOpening(t, tc.entry, func() { tc.swap(t, filepath.Join(work, tc.entry)) })

			h := &capturingHandler{}
			members := packMembers(t, work, slog.New(h))

			for _, name := range tc.absent {
				if _, ok := members[name]; ok {
					t.Errorf("member %q was archived from an entry that changed kind", name)
				}
			}
			if members["kept.txt"] != "keep" {
				t.Errorf("kept.txt = %q, want it archived untouched", members["kept.txt"])
			}
			const wantMsg = "snapshot: entry changed type between stat and open; skipping"
			if !h.hasMessage(wantMsg) {
				t.Errorf("expected log message %q, captured records: %v", wantMsg, h.records)
			}
		})
	}
}

// The walk holds one descriptor per level, so it refuses a tree deeper than its
// bound instead of spending descriptors without limit; a tree at the bound packs.
func TestPackBoundsItsDepth(t *testing.T) {
	// Single-byte names keep the deepest path far below the path-length limit.
	nested := func(levels int) string {
		work := filepath.Join(t.TempDir(), "work")
		if err := os.MkdirAll(filepath.Join(work, strings.Repeat("d/", levels)), 0o750); err != nil {
			t.Fatal(err)
		}
		return work
	}

	members := packMembers(t, nested(maxPackDepth), slog.Default())
	deepest := strings.Repeat("d/", maxPackDepth)
	if _, ok := members[deepest]; !ok {
		t.Fatalf("a tree %d levels deep must pack down to its deepest directory", maxPackDepth)
	}

	var buf bytes.Buffer
	err := packTar(nested(maxPackDepth+1), &buf, slog.Default())
	if err == nil || !strings.Contains(err.Error(), "nested deeper") {
		t.Fatalf("a tree %d levels deep must fail the pack on its depth, got: %v", maxPackDepth+1, err)
	}
}

// A member whose path is longer than the limit fails the pack, as a path too long
// to resolve failed the path-based walk: such a member could not be unpacked.
func TestPackRefusesAMemberPathOverTheLimit(t *testing.T) {
	work := filepath.Join(t.TempDir(), "work")
	writeTree(t, work, map[string]string{"dir/12345.txt": "x"})

	run := func(maxRel int) error {
		fd, err := unix.Open(work, packOpenDirFlags, 0)
		if err != nil {
			t.Fatal(err)
		}
		return walkInto(tar.NewWriter(io.Discard), fd, "", maxRel, 0, slog.Default())
	}
	if err := run(len("dir/12345.txt")); err != nil {
		t.Fatalf("a member path exactly at the limit must pack, got: %v", err)
	}
	if err := run(len("dir/12345.txt") - 1); !errors.Is(err, unix.ENAMETOOLONG) {
		t.Fatalf("a member path over the limit must fail with ENAMETOOLONG, got: %v", err)
	}
}

// The root itself is opened without following a link: a snapshot source that is a
// symlink is refused, not walked.
func TestPackRefusesASymlinkedRoot(t *testing.T) {
	base := t.TempDir()
	writeTree(t, filepath.Join(base, "real"), map[string]string{"file.txt": "x"})
	link := filepath.Join(base, "link")
	if err := os.Symlink(filepath.Join(base, "real"), link); err != nil {
		t.Fatal(err)
	}

	var buf bytes.Buffer
	err := packTar(link, &buf, slog.Default())
	if err == nil || !strings.Contains(err.Error(), "is not a directory") {
		t.Fatalf("a symlinked snapshot source must be refused, got: %v", err)
	}
}

// Every descriptor the walk opens is closed again, whether the pack succeeds, is
// refused part-way down a deep tree, or skips an entry that changed under it.
func TestPackLeavesNoDescriptorOpen(t *testing.T) {
	openFds := func() int {
		entries, err := os.ReadDir("/proc/self/fd")
		if err != nil {
			t.Skipf("no /proc/self/fd to count descriptors with: %v", err)
		}
		return len(entries)
	}
	deep := filepath.Join(t.TempDir(), "work")
	if err := os.MkdirAll(filepath.Join(deep, strings.Repeat("d/", maxPackDepth+1)), 0o750); err != nil {
		t.Fatal(err)
	}
	swapped := filepath.Join(t.TempDir(), "work")
	writeTree(t, swapped, map[string]string{"latest.log": "log"})
	beforeOpening(t, "latest.log", func() {
		leaf := filepath.Join(swapped, "latest.log")
		if err := os.Remove(leaf); err != nil {
			t.Fatal(err)
		}
		if err := syscall.Mkfifo(leaf, 0o640); err != nil {
			t.Fatal(err)
		}
	})
	quiescent := representativeWorkingSet(t)

	before := openFds()
	if err := packTar(quiescent, io.Discard, slog.Default()); err != nil {
		t.Fatalf("packTar: %v", err)
	}
	if err := packTar(deep, io.Discard, slog.Default()); err == nil {
		t.Fatal("the over-deep tree must fail the pack")
	}
	if err := packTar(swapped, io.Discard, slog.Default()); err != nil {
		t.Fatalf("packTar: %v", err)
	}
	if after := openFds(); after != before {
		t.Fatalf("open descriptors: %d before, %d after", before, after)
	}
}
