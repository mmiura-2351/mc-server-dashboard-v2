package instancemanager

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// The working-set owner the ownership tests pretend the server runs as: an identity distinct from the test
// process, standing in for the run-as user under a root Worker.
const (
	serverUID = 25565
	serverGID = 25566
)

// asRootWorker makes every working-set directory report serverUID:serverGID as
// its owner and records, by inode, what the Worker re-owns — the Worker/server
// identity split a root Worker has, which an unprivileged test process cannot
// produce on disk. The returned func reports whether path was re-owned.
func asRootWorker(t *testing.T) (chowned func(path string) bool) {
	t.Helper()
	prevOwner, prevChown := dirOwner, fchownFd
	t.Cleanup(func() { dirOwner, fchownFd = prevOwner, prevChown })

	inodes := map[uint64]bool{}
	dirOwner = func(int) (int, int, error) { return serverUID, serverGID, nil }
	fchownFd = func(fd, uid, gid int) error {
		if uid != serverUID || gid != serverGID {
			t.Errorf("fchown to %d:%d, want the working set's owner %d:%d", uid, gid, serverUID, serverGID)
		}
		var st unix.Stat_t
		if err := unix.Fstat(fd, &st); err != nil {
			return err
		}
		inodes[st.Ino] = true
		return nil
	}
	return func(path string) bool {
		var st unix.Stat_t
		if err := unix.Lstat(path, &st); err != nil {
			t.Fatalf("lstat %s: %v", path, err)
		}
		return inodes[st.Ino]
	}
}

// Overwriting a file of a running server leaves the replacement owned by the working set's owner, not by the
// Worker: under a root Worker the server runs as another user and must still read and rewrite its own file.
func TestEditFileOverwriteKeepsWorkingSetOwner(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	full := writeWorkingFile(t, m, "s1", "server.properties", []byte("motd=old"))
	chowned := asRootWorker(t)

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile",
		Path: "server.properties", Content: []byte("motd=new"),
	})
	if !res.Success {
		t.Fatalf("EditFile result = %+v, want success", res)
	}

	if !chowned(full) {
		t.Fatal("the replaced file was left owned by the Worker, want the working set's owner")
	}
}

// A new nested file gets the working set's owner on the file AND on every parent directory the edit had to
// create, so the server can traverse to it. The directory that already existed is not touched.
func TestEditFileNewNestedFileInheritsWorkingSetOwner(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	root := filepath.Join(m.scratchDir, "s1")
	if err := os.MkdirAll(filepath.Join(root, "plugins"), 0o750); err != nil {
		t.Fatal(err)
	}
	chowned := asRootWorker(t)

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile",
		Path: "plugins/Essentials/userdata/x.yml", Content: []byte("a: b"),
	})
	if !res.Success {
		t.Fatalf("EditFile result = %+v, want success", res)
	}

	for _, rel := range []string{"plugins/Essentials", "plugins/Essentials/userdata", "plugins/Essentials/userdata/x.yml"} {
		if !chowned(filepath.Join(root, filepath.FromSlash(rel))) {
			t.Errorf("%s was left owned by the Worker, want the working set's owner", rel)
		}
	}
	if chowned(filepath.Join(root, "plugins")) {
		t.Error("plugins already existed and must keep its owner untouched")
	}
}

// An edit whose ownership cannot be set fails and keeps the old content, rather than publishing a file the
// server cannot read.
func TestEditFileFailsWhenOwnershipCannotBeSet(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	full := writeWorkingFile(t, m, "s1", "server.properties", []byte("motd=old"))
	asRootWorker(t)
	fchownFd = func(int, int, int) error { return unix.EPERM }

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile",
		Path: "server.properties", Content: []byte("motd=new"),
	})

	if res.Success {
		t.Fatal("EditFile succeeded, want a failure")
	}
	if got, _ := os.ReadFile(full); string(got) != "motd=old" {
		t.Fatalf("content = %q, want the old content kept", got)
	}
}
