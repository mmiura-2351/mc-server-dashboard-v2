package containerdriver

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

// The hand-over re-owns inodes, and a file with more than one name may have one
// of them outside the working set. These tests build real hard links in temp
// dirs and pin that such a file is handed over only when every name of it is
// inside the tree being walked (issue #3338).

// linkedSet builds an empty working set next to a directory outside it, on the
// same filesystem so the two can share inodes.
func linkedSet(t *testing.T) (dir, outside string) {
	t.Helper()
	// Resolve symlinks so recorded paths (read back from /proc) compare equal.
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	dir, outside = filepath.Join(base, "s1"), filepath.Join(base, "outside")
	for _, p := range []string{dir, outside} {
		if err := os.Mkdir(p, 0o750); err != nil {
			t.Fatal(err)
		}
	}
	return dir, outside
}

// writeAt creates a file, and the directories leading to it.
func writeAt(t *testing.T, path string) string {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// linkAt gives the file at target one more name, creating the directories
// leading to it.
func linkAt(t *testing.T, target, name string) string {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(name), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(target, name); err != nil {
		t.Fatal(err)
	}
	return name
}

func inodeOf(t *testing.T, path string) uint64 {
	t.Helper()
	var st unix.Stat_t
	if err := unix.Lstat(path, &st); err != nil {
		t.Fatal(err)
	}
	return st.Ino
}

// reowned counts the recorded ownership changes that landed on the inode behind
// path, under whichever of its names.
func reowned(t *testing.T, rec *chownRecorder, path string) int {
	t.Helper()
	want, n := inodeOf(t, path), 0
	for _, p := range rec.seen() {
		if inodeOf(t, p) == want {
			n++
		}
	}
	return n
}

// A file inside the working set that is also named outside it keeps its owner:
// re-owning it would re-own the outside file. It is counted, with one of its
// paths, and the walk goes on.
func TestHandOverLeavesAFileThatIsAlsoNamedOutsideTheWorkingSet(t *testing.T) {
	dir, outside := linkedSet(t)
	canary := writeAt(t, filepath.Join(outside, "canary"))
	inside := linkAt(t, canary, filepath.Join(dir, "world", "level.dat"))
	plain := writeAt(t, filepath.Join(dir, "world", "session.lock"))
	d, rec := hardenedDriver(newFakeDocker())

	stats, err := d.handOverWorkingSet(context.Background(), dir)

	if err != nil {
		t.Fatalf("hand-over: %v", err)
	}
	if n := reowned(t, rec, canary); n != 0 {
		t.Fatalf("re-owned %v, which changes the owner of the file outside the working set", rec.seen())
	}
	if stats.skipped != 1 || stats.skippedExample != inside {
		t.Fatalf("skipped = %d (example %q), want 1 (%q)", stats.skipped, stats.skippedExample, inside)
	}
	// root, world, world/level.dat, world/session.lock: everything but the link.
	if reowned(t, rec, plain) != 1 || stats.entries != 4 || stats.changed != 3 {
		t.Fatalf("hand-over = %+v, re-owned %v; want the rest of the tree handed over", stats, rec.seen())
	}
}

// A symlink can be hard-linked too, and re-owning one that is named outside the
// working set changes the outside link just the same.
func TestHandOverLeavesASymlinkThatIsAlsoNamedOutsideTheWorkingSet(t *testing.T) {
	dir, outside := linkedSet(t)
	link := filepath.Join(outside, "link")
	if err := os.Symlink("anywhere", link); err != nil {
		t.Fatal(err)
	}
	linkAt(t, link, filepath.Join(dir, "link"))
	d, rec := hardenedDriver(newFakeDocker())

	stats, err := d.handOverWorkingSet(context.Background(), dir)

	if err != nil || stats.skipped != 1 || reowned(t, rec, link) != 0 {
		t.Fatalf("hand-over = %+v, %v, re-owned %v; want the linked symlink left alone", stats, err, rec.seen())
	}
}

// A link the server made between two of its own files is handed over: every name
// of the inode is inside the tree, so no file outside it can be affected. One
// ownership change covers both names — it is the inode that is re-owned.
func TestHandOverReownsAFileWhoseNamesAreAllInTheWorkingSet(t *testing.T) {
	for _, tc := range []struct{ name, first, second string }{
		{"same directory", "a.dat", "b.dat"},
		{"nested directories", "world/region/r.0.0.mca", "backups/2026/10/world/region/r.0.0.mca"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir, _ := linkedSet(t)
			first := writeAt(t, filepath.Join(dir, filepath.FromSlash(tc.first)))
			linkAt(t, first, filepath.Join(dir, filepath.FromSlash(tc.second)))
			d, rec := hardenedDriver(newFakeDocker())

			stats, err := d.handOverWorkingSet(context.Background(), dir)

			if err != nil {
				t.Fatalf("hand-over: %v", err)
			}
			if n := reowned(t, rec, first); n != 1 {
				t.Fatalf("the linked file was re-owned %d times (%v), want once", n, rec.seen())
			}
			if stats.skipped != 0 || stats.changed != len(rec.seen()) {
				t.Fatalf("hand-over = %+v with %d ownership changes; want none skipped and each change counted",
					stats, len(rec.seen()))
			}
		})
	}
}

// Two names inside do not make a file the working set's own when a third is
// outside.
func TestHandOverLeavesAFileWithOneOfThreeNamesOutside(t *testing.T) {
	dir, outside := linkedSet(t)
	canary := writeAt(t, filepath.Join(outside, "canary"))
	linkAt(t, canary, filepath.Join(dir, "a", "one"))
	linkAt(t, canary, filepath.Join(dir, "b", "two"))
	d, rec := hardenedDriver(newFakeDocker())

	stats, err := d.handOverWorkingSet(context.Background(), dir)

	if err != nil || stats.skipped != 1 || reowned(t, rec, canary) != 0 {
		t.Fatalf("hand-over = %+v, %v, re-owned %v; want the file left alone and counted once", stats, err, rec.seen())
	}
}

// The count is exact however many names a file has, and skipped files are
// counted per file, not per name.
func TestHandOverCountsEveryNameOfAHeavilyLinkedFile(t *testing.T) {
	const names = 500
	dir, outside := linkedSet(t)
	own := writeAt(t, filepath.Join(dir, "own", "0"))
	shared := writeAt(t, filepath.Join(outside, "canary"))
	for i := 1; i < names; i++ {
		linkAt(t, own, filepath.Join(dir, "own", fmt.Sprint(i)))
	}
	for i := 0; i < names; i++ {
		linkAt(t, shared, filepath.Join(dir, "shared", fmt.Sprint(i)))
	}
	d, rec := hardenedDriver(newFakeDocker())

	stats, err := d.handOverWorkingSet(context.Background(), dir)

	if err != nil {
		t.Fatalf("hand-over: %v", err)
	}
	if own, shared := reowned(t, rec, own), reowned(t, rec, shared); own != 1 || shared != 0 {
		t.Fatalf("re-owned the working set's own file %d times and the shared one %d times, want 1 and 0", own, shared)
	}
	// root, own, shared and the own file; each name is an entry.
	if stats.skipped != 1 || stats.changed != 4 || stats.entries != 3+2*names {
		t.Fatalf("hand-over = %+v, want 1 file skipped, 4 changes, %d entries", stats, 3+2*names)
	}
}

// The names found only prove a file is the working set's own while its link
// count holds still, so a file whose link count moved during the walk is left
// alone. Both ways it can move would otherwise complete the count of a file that
// is also named outside: a name that goes away after it was counted (an edit
// replacing it, say) leaves as many names counted as there are links left, and a
// name added inside stands in for the outside one.
func TestHandOverLeavesAFileWhoseLinkCountMovedDuringTheWalk(t *testing.T) {
	// Each directory is re-owned right before the walk enters it, which is when
	// the steps below run: first for one of a/ and b/, then for the other.
	for _, tc := range []struct {
		name          string
		before        []string
		first, second func(t *testing.T, canary, firstDir, secondDir string)
	}{
		{
			name:   "a counted name removed",
			before: []string{"a", "b"},
			second: func(t *testing.T, _, firstDir, _ string) {
				if err := os.Remove(filepath.Join(firstDir, "name")); err != nil {
					t.Error(err)
				}
			},
		},
		{
			name: "a name added",
			first: func(t *testing.T, canary, firstDir, _ string) {
				linkAt(t, canary, filepath.Join(firstDir, "name"))
			},
			second: func(t *testing.T, canary, _, secondDir string) {
				linkAt(t, canary, filepath.Join(secondDir, "name"))
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir, outside := linkedSet(t)
			canary := writeAt(t, filepath.Join(outside, "canary"))
			dirs := []string{filepath.Join(dir, "a"), filepath.Join(dir, "b")}
			for _, p := range dirs {
				if err := os.Mkdir(p, 0o750); err != nil {
					t.Fatal(err)
				}
			}
			for _, name := range tc.before {
				linkAt(t, canary, filepath.Join(dir, name, "name"))
			}
			d, rec := hardenedDriver(newFakeDocker())
			var entered []string
			rec.before = func(path string) {
				if path != dirs[0] && path != dirs[1] {
					return
				}
				entered = append(entered, path)
				if len(entered) == 1 && tc.first != nil {
					other := dirs[0]
					if path == other {
						other = dirs[1]
					}
					tc.first(t, canary, path, other)
				}
				if len(entered) == 2 {
					tc.second(t, canary, entered[0], path)
				}
			}

			stats, err := d.handOverWorkingSet(context.Background(), dir)

			if err != nil || stats.skipped != 1 || reowned(t, rec, canary) != 0 {
				t.Fatalf("hand-over = %+v, %v, re-owned %v; want the file left alone", stats, err, rec.seen())
			}
		})
	}
}

// Skipped files do not fail the create. They are reported once per hand-over —
// a count and one path, with the server — not once per file.
func TestCreateWarnsOnceAboutFilesLeftWithTheirOwner(t *testing.T) {
	dir, outside := linkedSet(t)
	for i := 0; i < 3; i++ {
		canary := writeAt(t, filepath.Join(outside, fmt.Sprint(i)))
		linkAt(t, canary, filepath.Join(dir, "world", fmt.Sprint(i)))
	}
	docker := newFakeDocker()
	d, _ := hardenedDriver(docker)
	logged := &syncBuffer{}
	d.logger = slog.New(slog.NewTextHandler(logged, nil))

	if _, err := d.createServerContainer(context.Background(), specIn(dir), CreateSpec{Name: "mcsd-s1"}); err != nil {
		t.Fatalf("create: %v", err)
	}

	if docker.createCalls != 1 {
		t.Fatalf("createCalls = %d, want the container created", docker.createCalls)
	}
	out := logged.String()
	if n := strings.Count(out, "level=WARN"); n != 1 {
		t.Fatalf("worker log = %q, want exactly one warning (got %d)", out, n)
	}
	for _, want := range []string{"server_id=s1", "skipped=3", "example=" + filepath.Join(dir, "world", "0")} {
		if !strings.Contains(out, want) {
			t.Errorf("worker log = %q, want %q in the warning", out, want)
		}
	}
}

// A hand-over that skipped nothing warns about nothing.
func TestCreateDoesNotWarnWhenNothingWasSkipped(t *testing.T) {
	dir, _ := linkedSet(t)
	linkAt(t, writeAt(t, filepath.Join(dir, "a")), filepath.Join(dir, "b"))
	d, _ := hardenedDriver(newFakeDocker())
	logged := &syncBuffer{}
	d.logger = slog.New(slog.NewTextHandler(logged, nil))

	if _, err := d.createServerContainer(context.Background(), specIn(dir), CreateSpec{Name: "mcsd-s1"}); err != nil {
		t.Fatalf("create: %v", err)
	}

	if out := logged.String(); strings.Contains(out, "level=WARN") {
		t.Fatalf("worker log = %q, want no warning", out)
	}
}

// The recorder stands in for the kernel everywhere above. This is the real call:
// a multiply-linked file is re-owned through a descriptor of the file itself,
// which chownAtNoFollow must accept — and for a symlink it is the link that is
// re-owned, never its target. Re-owning to the owner the file already has needs
// no privilege, so this runs for any user.
func TestChownAtNoFollowReownsTheFileBehindADescriptor(t *testing.T) {
	dir, _ := linkedSet(t)
	file := writeAt(t, filepath.Join(dir, "file"))
	link := filepath.Join(dir, "link")
	if err := os.Symlink(filepath.Join(dir, "absent"), link); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{file, link} {
		fd, err := unix.Open(path, unix.O_PATH|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if err != nil {
			t.Fatal(err)
		}
		err = chownAtNoFollow(fd, "", os.Getuid(), os.Getgid())
		_ = unix.Close(fd)
		if err != nil {
			t.Fatalf("chownAtNoFollow on a descriptor of %s: %v", path, err)
		}
	}
}

// otherGroup returns a group, other than the process's own, that the process may
// give its files to: any group for root, a supplementary group otherwise.
func otherGroup(t *testing.T) int {
	t.Helper()
	if os.Getuid() == 0 {
		return os.Getgid() + 1
	}
	groups, err := os.Getgroups()
	if err != nil {
		t.Fatal(err)
	}
	for _, g := range groups {
		if g != os.Getgid() {
			return g
		}
	}
	t.Skip("the test user has no second group to give a file to")
	return 0
}

// The whole rule against the real kernel, with no recorder: the run-as user is
// the test user under another of its groups, which an unprivileged process may
// really hand a file to. Both names of the working set's own file end up with
// that group; the file named outside keeps its own.
func TestHandOverReallyReownsOnlyFilesWhollyInTheWorkingSet(t *testing.T) {
	gid := otherGroup(t)
	dir, outside := linkedSet(t)
	first := writeAt(t, filepath.Join(dir, "world", "a"))
	second := linkAt(t, first, filepath.Join(dir, "backups", "a"))
	canary := writeAt(t, filepath.Join(outside, "canary"))
	planted := linkAt(t, canary, filepath.Join(dir, "world", "planted"))
	d, _ := hardenedDriver(newFakeDocker())
	d.runAsUID, d.runAsGID, d.chownAt = os.Getuid(), gid, chownAtNoFollow

	stats, err := d.handOverWorkingSet(context.Background(), dir)

	if err != nil || stats.skipped != 1 {
		t.Fatalf("hand-over = %+v, %v; want one file skipped", stats, err)
	}
	groupOf := func(path string) int {
		var st unix.Stat_t
		if err := unix.Lstat(path, &st); err != nil {
			t.Fatal(err)
		}
		return int(st.Gid)
	}
	for _, p := range []string{dir, first, second} {
		if got := groupOf(p); got != gid {
			t.Errorf("%s has group %d, want the run-as group %d", p, got, gid)
		}
	}
	for _, p := range []string{canary, planted} {
		if got := groupOf(p); got != os.Getgid() {
			t.Errorf("%s has group %d, want it left with group %d", p, got, os.Getgid())
		}
	}
}
