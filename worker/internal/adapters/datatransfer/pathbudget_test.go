package datatransfer

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

// These tests pin the snapshot's member-path budget (issue #3341). The pack and
// the hydrate can run on different Workers whose data roots differ in length, so
// the budget is a fixed limit on the member's path relative to the working set,
// and the hydrate never names a member by its joined absolute path. A member at
// the limit lies deeper than PATH_MAX below the root it is restored under, so the
// fixtures are built and read back relative to a directory descriptor.

// pathOfLength returns a slash-separated relative path of exactly n bytes whose
// components are no longer than NAME_MAX.
func pathOfLength(n int) string {
	var parts []string
	for n > unix.NAME_MAX {
		c := unix.NAME_MAX
		if n-c-1 < 1 {
			c = n - 2
		}
		parts = append(parts, strings.Repeat("d", c))
		n -= c + 1
	}
	return strings.Join(append(parts, strings.Repeat("f", n)), "/")
}

// dirAt descends from root through the directories of rel (all of its components
// but the last), creating missing ones when create is set, and returns a
// descriptor of the last one and rel's last component. The descriptor is closed
// when the test ends.
func dirAt(t *testing.T, root, rel string, create bool) (int, string) {
	t.Helper()
	fd, err := unix.Open(root, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		t.Fatalf("open %s: %v", root, err)
	}
	parts := strings.Split(rel, "/")
	for _, dir := range parts[:len(parts)-1] {
		if create {
			if err := unix.Mkdirat(fd, dir, 0o750); err != nil && !errors.Is(err, unix.EEXIST) {
				t.Fatalf("mkdirat: %v", err)
			}
		}
		next, err := unix.Openat(fd, dir, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
		_ = unix.Close(fd)
		if err != nil {
			t.Fatalf("openat: %v", err)
		}
		fd = next
	}
	t.Cleanup(func() { _ = unix.Close(fd) })
	return fd, parts[len(parts)-1]
}

// createAt writes content to root/rel, creating its directories.
func createAt(t *testing.T, root, rel, content string) {
	t.Helper()
	dirFd, name := dirAt(t, root, rel, true)
	fd, err := unix.Openat(dirFd, name, unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_CLOEXEC, 0o640)
	if err != nil {
		t.Fatalf("create %d-byte member: %v", len(rel), err)
	}
	defer func() { _ = unix.Close(fd) }()
	if _, err := unix.Write(fd, []byte(content)); err != nil {
		t.Fatal(err)
	}
}

// readAt returns the content of root/rel.
func readAt(t *testing.T, root, rel string) string {
	t.Helper()
	dirFd, name := dirAt(t, root, rel, false)
	fd, err := unix.Openat(dirFd, name, unix.O_RDONLY|unix.O_CLOEXEC, 0)
	if err != nil {
		t.Fatalf("open %d-byte member: %v", len(rel), err)
	}
	f := os.NewFile(uintptr(fd), name)
	defer func() { _ = f.Close() }()
	got, err := io.ReadAll(f)
	if err != nil {
		t.Fatal(err)
	}
	return string(got)
}

// longDir creates and returns a directory under t.TempDir() whose path is about
// n bytes long.
func longDir(t *testing.T, n int) string {
	t.Helper()
	dir := t.TempDir()
	for len(dir) < n {
		dir = filepath.Join(dir, strings.Repeat("r", min(unix.NAME_MAX, n-len(dir))))
	}
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	return dir
}

// A member at the limit packs from a short data root and is restored under a
// destination root far longer than the source's: the joined path is longer than
// PATH_MAX, and the hydrate does not depend on it. That holds for a file and for
// a directory, whose member name carries one byte more, its trailing slash.
func TestASnapshotMemberAtThePathLimitRestoresUnderALongerRoot(t *testing.T) {
	rel := pathOfLength(unix.PathMax - 1)
	dirRel := rel[:len(rel)-1] + "g" // a sibling of the file, as long
	srcDir := filepath.Join(t.TempDir(), "server")
	if err := os.Mkdir(srcDir, 0o750); err != nil {
		t.Fatal(err)
	}
	createAt(t, srcDir, rel, "deep")
	parentFd, name := dirAt(t, srcDir, dirRel, false)
	if err := unix.Mkdirat(parentFd, name, 0o750); err != nil {
		t.Fatal(err)
	}

	spoolPath, cleanup, err := New(nil).PackSnapshot(context.Background(), srcDir)
	if err != nil {
		t.Fatalf("a member path of %d bytes must pack: %v", len(rel), err)
	}
	defer cleanup()
	spool, err := os.Open(spoolPath)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = spool.Close() }()

	destDir := filepath.Join(longDir(t, 3000), "server")
	if err := unpackAndSwap(spool, destDir, plainMarker(7), slog.Default()); err != nil {
		t.Fatalf("a member that packed must hydrate under a longer root: %v", err)
	}
	if got := readAt(t, destDir, rel); got != "deep" {
		t.Fatalf("the restored member reads %q, want %q", got, "deep")
	}
	parentFd, name = dirAt(t, destDir, dirRel, false)
	var st unix.Stat_t
	if err := unix.Fstatat(parentFd, name, &st, unix.AT_SYMLINK_NOFOLLOW); err != nil || st.Mode&unix.S_IFMT != unix.S_IFDIR {
		t.Fatalf("the directory at the limit was not restored as one: mode %o, %v", st.Mode, err)
	}
}

// The limit is on the member's path relative to the working set, whatever the
// source root's length: under a long root a member at the limit packs and one a
// byte longer fails the pack with an error that names the limit.
func TestPackRefusesAMemberPathOverTheLimit(t *testing.T) {
	for _, tc := range []struct {
		length int
		packs  bool
	}{
		{unix.PathMax - 1, true},
		{unix.PathMax, false},
	} {
		srcDir := filepath.Join(longDir(t, 3000), "server")
		if err := os.Mkdir(srcDir, 0o750); err != nil {
			t.Fatal(err)
		}
		createAt(t, srcDir, pathOfLength(tc.length), "x")

		err := packTar(context.Background(), srcDir, io.Discard, slog.Default())
		switch {
		case tc.packs && err != nil:
			t.Fatalf("a member path of %d bytes must pack: %v", tc.length, err)
		case !tc.packs && !errors.Is(err, unix.ENAMETOOLONG):
			t.Fatalf("a member path of %d bytes must fail with ENAMETOOLONG, got: %v", tc.length, err)
		case !tc.packs && !strings.Contains(err.Error(), "longer than 4095 bytes"):
			t.Fatalf("the refusal must name the limit, got: %.200v", err)
		}
	}
}
