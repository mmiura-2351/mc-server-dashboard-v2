package instancemanager

import (
	"bytes"
	"context"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// These tests pin the files API's read and edit against what a server can plant in
// its own working set (issue #3337): the working set is bind-mounted into a
// container running code the operator does not control, the Worker touching it is
// root in the shipped topology, and both commands run on the server's command lane.
// They run on a real filesystem as whatever user runs the suite.

// handleWithin runs one command and fails the test when it has not answered in
// time. A command that blocks (a FIFO opened without O_NONBLOCK) would otherwise
// hang the suite; nothing here depends on how fast the answer is, only that there
// is one, so the budget is far beyond what a loaded host needs.
func handleWithin(t *testing.T, m *Manager, cmd session.Command) session.CommandResult {
	t.Helper()
	done := make(chan session.CommandResult, 1)
	go func() { done <- m.Handle(context.Background(), cmd) }()
	select {
	case res := <-done:
		return res
	case <-time.After(30 * time.Second):
		t.Fatalf("%s %q blocked", cmd.Kind, cmd.Path)
		return session.CommandResult{}
	}
}

// plantFifo puts a FIFO at path. When the test ends it opens both ends at once, so
// a command a regression left blocked on the FIFO is released instead of leaking.
func plantFifo(t *testing.T, path string) {
	t.Helper()
	if err := unix.Mkfifo(path, 0o600); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if fd, err := unix.Open(path, unix.O_RDWR|unix.O_NONBLOCK|unix.O_CLOEXEC, 0); err == nil {
			_ = unix.Close(fd)
		}
	})
}

// workingRoot creates and returns the working-set root of server s1.
func workingRoot(t *testing.T, m *Manager) string {
	t.Helper()
	root := filepath.Join(m.scratchDir, "s1")
	if err := os.MkdirAll(root, 0o750); err != nil {
		t.Fatal(err)
	}
	return root
}

func lstat(t *testing.T, path string) unix.Stat_t {
	t.Helper()
	var st unix.Stat_t
	if err := unix.Lstat(path, &st); err != nil {
		t.Fatalf("lstat %s: %v", path, err)
	}
	return st
}

// processUmask reads the umask without changing it (unix.Umask sets it, which
// would race every other test creating a file).
func processUmask(t *testing.T) uint32 {
	t.Helper()
	status, err := os.ReadFile("/proc/self/status")
	if err != nil {
		t.Skipf("cannot read the process umask: %v", err)
	}
	for _, line := range strings.Split(string(status), "\n") {
		if rest, ok := strings.CutPrefix(line, "Umask:"); ok {
			mask, err := strconv.ParseUint(strings.TrimSpace(rest), 8, 32)
			if err != nil {
				t.Fatalf("parsing %q: %v", line, err)
			}
			return uint32(mask)
		}
	}
	t.Skip("the kernel does not report the process umask")
	return 0
}

func assertDenied(t *testing.T, res session.CommandResult, want session.FileAccessReason) {
	t.Helper()
	if res.Success || res.ErrorCode != session.CommandErrorFileAccessDenied || res.FileAccessReason != want {
		t.Fatalf("result = %+v, want FileAccessDenied with reason %v", res, want)
	}
	if res.FileContent != nil {
		t.Fatalf("a refused read carried %d bytes", len(res.FileContent))
	}
}

// assertNoEditTemp fails when the directory still holds one of the edit's temps.
// keep names an entry the test planted itself under that prefix.
func assertNoEditTemp(t *testing.T, dir, keep string) {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.Name() != keep && strings.HasPrefix(e.Name(), ".edit-") {
			t.Fatalf("the edit left its temp %q behind", e.Name())
		}
	}
}

// --- read ---------------------------------------------------------------------

// A FIFO at the read path is refused without being opened for reading in a way
// that can block: with no writer the open itself would block, and with a writer
// that never closes the read would.
func TestReadFileRefusesAFifoWithoutBlocking(t *testing.T) {
	t.Run("without a writer", func(t *testing.T) {
		m := newManager(t, &fakeDriver{}, nil)
		plantFifo(t, filepath.Join(workingRoot(t, m), "pipe"))

		res := handleWithin(t, m, session.Command{CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: "pipe"})

		assertDenied(t, res, session.FileAccessReasonUnspecified)
	})
	t.Run("with a writer that never closes", func(t *testing.T) {
		m := newManager(t, &fakeDriver{}, nil)
		pipe := filepath.Join(workingRoot(t, m), "pipe")
		plantFifo(t, pipe)
		// O_RDWR opens a FIFO at once and counts as its writer.
		writer, err := os.OpenFile(pipe, os.O_RDWR, 0)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = writer.Close() })
		if _, err := writer.WriteString("from the server"); err != nil {
			t.Fatal(err)
		}

		res := handleWithin(t, m, session.Command{CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: "pipe"})

		assertDenied(t, res, session.FileAccessReasonUnspecified)
	})
}

// A socket is not a regular file either; opening one fails outright (ENXIO), and
// that is the same refusal, not an internal error.
func TestReadFileRefusesASocket(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	// A unix socket path is limited to ~100 bytes, so bind it by a short relative name.
	root := workingRoot(t, m)
	t.Chdir(root)
	l, err := net.Listen("unix", "sock")
	if err != nil {
		t.Skipf("cannot bind a unix socket here: %v", err)
	}
	t.Cleanup(func() { _ = l.Close() })

	res := handleWithin(t, m, session.Command{CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: "sock"})

	assertDenied(t, res, session.FileAccessReasonUnspecified)
}

// beforeReadingTheLeaf runs swap once, at the point between the read resolving the
// parent directory and opening the leaf in it — the window a server has to replace
// the file the path named.
func beforeReadingTheLeaf(t *testing.T, swap func()) {
	t.Helper()
	orig := readLeafAt
	t.Cleanup(func() { readLeafAt = orig })
	readLeafAt = func(dirFd int, name string, maxSize int) ([]byte, error) {
		swap()
		return orig(dirFd, name, maxSize)
	}
}

// What the leaf is replaced by after the path was resolved is judged as what it
// now is: a link is not followed, and nothing that is not a regular file is read.
func TestReadFileJudgesALeafSwappedInAfterResolution(t *testing.T) {
	for name, tc := range map[string]struct {
		swapIn func(t *testing.T, path, outside string)
		want   session.FileAccessReason
	}{
		"symlink to an outside file": {func(t *testing.T, path, outside string) {
			if err := os.Symlink(outside, path); err != nil {
				t.Error(err)
			}
		}, session.FileAccessReasonSymlinkRefused},
		"fifo": {func(t *testing.T, path, _ string) {
			plantFifo(t, path)
		}, session.FileAccessReasonUnspecified},
		"directory": {func(t *testing.T, path, _ string) {
			if err := os.Mkdir(path, 0o700); err != nil {
				t.Error(err)
			}
		}, session.FileAccessReasonIsADirectory},
	} {
		t.Run(name, func(t *testing.T) {
			m := newManager(t, &fakeDriver{}, nil)
			leaf := writeWorkingFile(t, m, "s1", "config/app.yml", []byte("inside"))
			outside := filepath.Join(t.TempDir(), "secret")
			if err := os.WriteFile(outside, []byte("top-secret"), 0o600); err != nil {
				t.Fatal(err)
			}
			beforeReadingTheLeaf(t, func() {
				if err := os.Remove(leaf); err != nil {
					t.Error(err)
				}
				tc.swapIn(t, leaf, outside)
			})

			res := handleWithin(t, m, session.Command{CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: "config/app.yml"})

			assertDenied(t, res, tc.want)
		})
	}
}

// A file that grows past the cap after the path was resolved is refused, and the
// read stops at the cap however large the file has become: the Worker never holds
// more than the cap of a file whose size the server controls.
func TestReadFileOfAFileGrownPastTheCapStopsAtTheCap(t *testing.T) {
	const grownTo = 128 << 20
	m := newManager(t, &fakeDriver{}, nil)
	leaf := writeWorkingFile(t, m, "s1", "latest.log", []byte("small"))
	beforeReadingTheLeaf(t, func() {
		// Sparse: the file reads as 128 MiB of zeros without occupying them.
		if err := os.Truncate(leaf, grownTo); err != nil {
			t.Skipf("the filesystem cannot hold a sparse file: %v", err)
		}
	})

	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	res := handleWithin(t, m, session.Command{CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: "latest.log"})
	runtime.ReadMemStats(&after)

	assertDenied(t, res, session.FileAccessReasonPayloadTooLarge)
	// Reading the cap costs a few times the cap (the buffer grows by doubling);
	// reading the whole file costs at least the file.
	if allocated := after.TotalAlloc - before.TotalAlloc; allocated >= grownTo/2 {
		t.Fatalf("the refused read allocated %d bytes, want it bounded by the %d-byte cap", allocated, MaxFileBytes)
	}
}

// The cap is inclusive: a file of exactly MaxFileBytes is returned whole.
func TestReadFileAtTheCapReturnsEveryByte(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	want := bytes.Repeat([]byte("0123456789abcdef"), MaxFileBytes/16)
	writeWorkingFile(t, m, "s1", "big.bin", want)

	res := handleWithin(t, m, session.Command{CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: "big.bin"})

	if !res.Success || !bytes.Equal(res.FileContent, want) {
		t.Fatalf("ReadFile at the cap: success=%v code=%v, %d bytes; want all %d", res.Success, res.ErrorCode, len(res.FileContent), len(want))
	}
}

// --- edit ---------------------------------------------------------------------

// editPlant is one thing a server can put at the name an edit's temp would take.
type editPlant struct {
	name string
	// plant puts it at path. victim is a regular file in the same directory and
	// outside one beyond the working set, for the plants that point somewhere.
	plant func(t *testing.T, path, victim, outside string)
}

func editPlants() []editPlant {
	return []editPlant{
		{"fifo", func(t *testing.T, path, _, _ string) { plantFifo(t, path) }},
		{"symlink to an outside file", func(t *testing.T, path, _, outside string) {
			if err := os.Symlink(outside, path); err != nil {
				t.Fatal(err)
			}
		}},
		{"dangling symlink", func(t *testing.T, path, _, _ string) {
			if err := os.Symlink(filepath.Join(t.TempDir(), "nothing-here"), path); err != nil {
				t.Fatal(err)
			}
		}},
		{"hard link to another file", func(t *testing.T, path, victim, _ string) {
			if err := os.Link(victim, path); err != nil {
				t.Fatal(err)
			}
		}},
		{"directory", func(t *testing.T, path, _, _ string) {
			if err := os.Mkdir(path, 0o700); err != nil {
				t.Fatal(err)
			}
		}},
		{"regular file", func(t *testing.T, path, _, _ string) {
			if err := os.WriteFile(path, []byte("the server's own"), 0o600); err != nil {
				t.Fatal(err)
			}
		}},
	}
}

// editOverAPlant edits server.properties in a working set where plantName holds
// p, and asserts the edit succeeded byte for byte while everything the plant is,
// or points at, is exactly as it was: not written, not truncated, not re-owned,
// not removed.
func editOverAPlant(t *testing.T, m *Manager, p editPlant, plantName string) {
	t.Helper()
	root := workingRoot(t, m)
	target := writeWorkingFile(t, m, "s1", "server.properties", []byte("motd=old"))
	victim := writeWorkingFile(t, m, "s1", "ops.json", []byte("[victim]"))
	outside := filepath.Join(t.TempDir(), "outside")
	if err := os.WriteFile(outside, []byte("outside"), 0o600); err != nil {
		t.Fatal(err)
	}
	planted := filepath.Join(root, plantName)
	p.plant(t, planted, victim, outside)
	plantBefore, victimBefore := lstat(t, planted), lstat(t, victim)
	chowned := asRootWorker(t)

	res := handleWithin(t, m, session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile",
		Path: "server.properties", Content: []byte("motd=new"),
	})

	if !res.Success {
		t.Fatalf("EditFile result = %+v, want success", res)
	}
	if got, err := os.ReadFile(target); err != nil || string(got) != "motd=new" {
		t.Fatalf("target reads %q (%v), want the edit", got, err)
	}
	if !chowned(target) {
		t.Error("the replacement was left owned by the Worker, want the working set's owner")
	}
	for path, want := range map[string]string{victim: "[victim]", outside: "outside"} {
		if got, err := os.ReadFile(path); err != nil || string(got) != want {
			t.Errorf("%s now reads %q (%v), want it untouched %q", path, got, err, want)
		}
		if chowned(path) {
			t.Errorf("%s was re-owned by the edit", path)
		}
	}
	if after := lstat(t, victim); after.Ino != victimBefore.Ino || after.Nlink != victimBefore.Nlink {
		t.Errorf("victim is inode %d with %d links, want inode %d with %d", after.Ino, after.Nlink, victimBefore.Ino, victimBefore.Nlink)
	}
	if after := lstat(t, planted); after.Ino != plantBefore.Ino || after.Mode != plantBefore.Mode || after.Size != plantBefore.Size {
		t.Errorf("the planted entry changed: %+v, was %+v", after, plantBefore)
	}
	assertNoEditTemp(t, root, plantName)
}

// The temp name the edit used to take is predictable, so a server could plant
// something there first. Nothing planted at it is touched any more, and none of it
// can stall or fail the edit.
func TestEditFileIgnoresWhatIsPlantedAtThePredictableTempName(t *testing.T) {
	for _, p := range editPlants() {
		t.Run(p.name, func(t *testing.T) {
			m := newManager(t, &fakeDriver{}, nil)
			editOverAPlant(t, m, p, ".edit-server.properties-tmp")
		})
	}
}

// editTempSuffixes makes the edit draw its temp names from suffixes, in order
// (repeating the last), and returns a counter of the names drawn.
func editTempSuffixes(t *testing.T, suffixes ...string) *int {
	t.Helper()
	orig := editTempSuffix
	t.Cleanup(func() { editTempSuffix = orig })
	drawn := 0
	editTempSuffix = func() string {
		s := suffixes[min(drawn, len(suffixes)-1)]
		drawn++
		return s
	}
	return &drawn
}

// A server that does guess the temp name gains nothing: the temp is created
// exclusively, so whatever already holds the name is left alone and the edit goes
// on under another name.
func TestEditFileRetriesUnderAnotherNameWhenItsTempNameIsTaken(t *testing.T) {
	for _, p := range editPlants() {
		t.Run(p.name, func(t *testing.T) {
			m := newManager(t, &fakeDriver{}, nil)
			drawn := editTempSuffixes(t, "taken", "free")

			editOverAPlant(t, m, p, ".edit-taken-tmp")

			if *drawn != 2 {
				t.Fatalf("drew %d temp names, want the taken one and one more", *drawn)
			}
		})
	}
}

// The retry is bounded: when every name is taken the edit fails, leaving the
// target and the entry in its way as they were.
func TestEditFileGivesUpWhenEveryTempNameIsTaken(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	target := writeWorkingFile(t, m, "s1", "server.properties", []byte("motd=old"))
	taken := writeWorkingFile(t, m, "s1", ".edit-taken-tmp", []byte("the server's own"))
	drawn := editTempSuffixes(t, "taken")

	res := handleWithin(t, m, session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile",
		Path: "server.properties", Content: []byte("motd=new"),
	})

	if res.Success || res.ErrorCode != session.CommandErrorInternal {
		t.Fatalf("EditFile result = %+v, want an internal failure", res)
	}
	if *drawn < 2 || *drawn > 64 {
		t.Fatalf("drew %d temp names, want a bounded retry", *drawn)
	}
	for path, want := range map[string]string{target: "motd=old", taken: "the server's own"} {
		if got, err := os.ReadFile(path); err != nil || string(got) != want {
			t.Errorf("%s reads %q (%v), want it untouched %q", path, got, err, want)
		}
	}
}

// A failed edit removes its temp: here the ownership cannot be set.
func TestEditFileRemovesItsTempWhenItFails(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	root := workingRoot(t, m)
	asRootWorker(t)
	fchownFd = func(int, int, int) error { return unix.EPERM }

	res := handleWithin(t, m, session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile", Path: "server.properties", Content: []byte("motd=new"),
	})

	if res.Success {
		t.Fatal("EditFile succeeded, want a failure")
	}
	assertNoEditTemp(t, root, "")
}

// A directory swapped in at the target after the edit checked it is still refused
// as a directory — the rename cannot replace one — and the temp is removed.
func TestEditFileRefusesADirectorySwappedInAtTheTarget(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	root := workingRoot(t, m)
	target := writeWorkingFile(t, m, "s1", "server.properties", []byte("motd=old"))
	// The temp name is drawn after the target was checked and before the rename.
	orig := editTempSuffix
	t.Cleanup(func() { editTempSuffix = orig })
	editTempSuffix = func() string {
		if err := os.Remove(target); err != nil {
			t.Error(err)
		}
		if err := os.Mkdir(target, 0o700); err != nil {
			t.Error(err)
		}
		return orig()
	}

	res := handleWithin(t, m, session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile", Path: "server.properties", Content: []byte("motd=new"),
	})

	assertDenied(t, res, session.FileAccessReasonIsADirectory)
	if st := lstat(t, target); st.Mode&unix.S_IFMT != unix.S_IFDIR {
		t.Fatalf("the directory at the target was replaced (mode %o)", st.Mode)
	}
	assertNoEditTemp(t, root, "")
}

// The edit replaces the NAME, never what is behind it: a FIFO at the target is not
// opened (so it cannot block), and another name of a hard-linked target keeps the
// old content.
func TestEditFileReplacesTheTargetNameWithoutOpeningIt(t *testing.T) {
	t.Run("fifo", func(t *testing.T) {
		m := newManager(t, &fakeDriver{}, nil)
		target := filepath.Join(workingRoot(t, m), "server.properties")
		plantFifo(t, target)

		res := handleWithin(t, m, session.Command{
			CommandID: "c1", ServerID: "s1", Kind: "EditFile", Path: "server.properties", Content: []byte("motd=new"),
		})

		if !res.Success {
			t.Fatalf("EditFile result = %+v, want success", res)
		}
		if st := lstat(t, target); st.Mode&unix.S_IFMT != unix.S_IFREG {
			t.Fatalf("target mode = %o, want a regular file", st.Mode)
		}
		if got, _ := os.ReadFile(target); string(got) != "motd=new" {
			t.Fatalf("target reads %q, want the edit", got)
		}
	})
	t.Run("hard-linked file", func(t *testing.T) {
		m := newManager(t, &fakeDriver{}, nil)
		target := writeWorkingFile(t, m, "s1", "server.properties", []byte("motd=old"))
		other := filepath.Join(filepath.Dir(target), "other-name")
		if err := os.Link(target, other); err != nil {
			t.Fatal(err)
		}

		res := handleWithin(t, m, session.Command{
			CommandID: "c1", ServerID: "s1", Kind: "EditFile", Path: "server.properties", Content: []byte("motd=new"),
		})

		if !res.Success {
			t.Fatalf("EditFile result = %+v, want success", res)
		}
		if got, _ := os.ReadFile(target); string(got) != "motd=new" {
			t.Fatalf("target reads %q, want the edit", got)
		}
		if got, _ := os.ReadFile(other); string(got) != "motd=old" {
			t.Fatalf("the other name reads %q, want the old content", got)
		}
	})
}

// A legitimate edit is what it always was: the exact bytes, mode 0640 under the
// process umask, a fresh file (not a rewrite in place), a bare success, and no
// temp left — for an overwrite and for a file in directories the edit creates.
func TestEditFileResultIsUnchangedForALegitimateEdit(t *testing.T) {
	wantMode := uint32(0o640) &^ processUmask(t)
	content := []byte("motd=new\n\x00\xff binary too\n")
	for name, tc := range map[string]struct {
		path     string
		existing bool
	}{
		"overwrite":       {"server.properties", true},
		"new nested file": {"plugins/Essentials/config.yml", false},
		// A leaf as long as a name can be: the temp name must not depend on it.
		"new file with the longest name": {strings.Repeat("n", 255), false},
	} {
		t.Run(name, func(t *testing.T) {
			m := newManager(t, &fakeDriver{}, nil)
			root := workingRoot(t, m)
			full := filepath.Join(root, filepath.FromSlash(tc.path))
			var oldIno uint64
			if tc.existing {
				writeWorkingFile(t, m, "s1", tc.path, []byte("motd=old"))
				if err := os.Chmod(full, 0o600); err != nil {
					t.Fatal(err)
				}
				oldIno = lstat(t, full).Ino
			}

			res := handleWithin(t, m, session.Command{
				CommandID: "c1", ServerID: "s1", Kind: "EditFile", Path: tc.path, Content: content,
			})

			if want := (session.CommandResult{CommandID: "c1", Success: true}); res.CommandID != want.CommandID ||
				!res.Success || res.ErrorCode != want.ErrorCode || res.ErrorMessage != "" ||
				res.FileContent != nil || res.FileListing != nil || res.Output != "" {
				t.Fatalf("EditFile result = %+v, want a bare success", res)
			}
			got, err := os.ReadFile(full)
			if err != nil || !bytes.Equal(got, content) {
				t.Fatalf("file reads %q (%v), want %q", got, err, content)
			}
			st := lstat(t, full)
			if st.Mode != unix.S_IFREG|wantMode {
				t.Errorf("mode = %o, want regular %o", st.Mode, wantMode)
			}
			if st.Nlink != 1 {
				t.Errorf("link count = %d, want 1", st.Nlink)
			}
			if tc.existing && st.Ino == oldIno {
				t.Error("the file was rewritten in place, want it replaced by rename")
			}
			assertNoEditTemp(t, filepath.Dir(full), "")

			read := handleWithin(t, m, session.Command{CommandID: "c2", ServerID: "s1", Kind: "ReadFile", Path: tc.path})
			if !read.Success || !bytes.Equal(read.FileContent, content) {
				t.Fatalf("ReadFile after the edit = %+v, want the edit back", read)
			}
		})
	}
}
