package instancemanager

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// writeWorkingFile seeds a file under the server's working dir for read tests.
func writeWorkingFile(t *testing.T, m *Manager, serverID, rel string, data []byte) string {
	t.Helper()
	full := filepath.Join(m.scratchDir, serverID, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(full), 0o750); err != nil {
		t.Fatalf("seed dir: %v", err)
	}
	if err := os.WriteFile(full, data, 0o640); err != nil {
		t.Fatalf("seed file: %v", err)
	}
	return full
}

func TestReadFileReturnsBytes(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	writeWorkingFile(t, m, "s1", "server.properties", []byte("motd=hi"))

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: "server.properties",
	})
	if !res.Success {
		t.Fatalf("ReadFile result = %+v, want success", res)
	}
	if string(res.FileContent) != "motd=hi" {
		t.Fatalf("FileContent = %q, want %q", res.FileContent, "motd=hi")
	}
}

func TestReadFileEmptyFileRidesContentArm(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	writeWorkingFile(t, m, "s1", "empty.txt", []byte{})

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: "empty.txt",
	})
	if !res.Success {
		t.Fatalf("ReadFile result = %+v, want success", res)
	}
	// A non-nil empty slice so the transport sends file_content, not no payload.
	if res.FileContent == nil {
		t.Fatal("FileContent is nil for an empty file; want a non-nil empty slice")
	}
	if len(res.FileContent) != 0 {
		t.Fatalf("FileContent = %q, want empty", res.FileContent)
	}
}

func TestReadFileMissingIsServerNotFound(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: "nope.txt",
	})
	if res.Success {
		t.Fatal("ReadFile of a missing file should fail")
	}
	if res.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("ErrorCode = %v, want CommandErrorServerNotFound", res.ErrorCode)
	}
}

func TestReadFileTraversalIsDenied(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	for _, bad := range []string{"../escape", "/etc/passwd", "a/../../escape"} {
		res := m.Handle(context.Background(), session.Command{
			CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: bad,
		})
		if res.Success {
			t.Fatalf("ReadFile %q should be denied", bad)
		}
		if res.ErrorCode != session.CommandErrorFileAccessDenied {
			t.Fatalf("ReadFile %q ErrorCode = %v, want FileAccessDenied", bad, res.ErrorCode)
		}
	}
}

func TestReadFileSymlinkEscapeIsDenied(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	workingDir := filepath.Join(m.scratchDir, "s1")
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	secret := filepath.Join(t.TempDir(), "secret")
	if err := os.WriteFile(secret, []byte("top-secret"), 0o640); err != nil {
		t.Fatalf("seed secret: %v", err)
	}
	if err := os.Symlink(secret, filepath.Join(workingDir, "link")); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: "link",
	})
	if res.Success {
		t.Fatal("ReadFile through a symlink should be denied")
	}
	if res.ErrorCode != session.CommandErrorFileAccessDenied {
		t.Fatalf("ErrorCode = %v, want FileAccessDenied", res.ErrorCode)
	}
}

func TestReadFileIntermediateSymlinkEscapeIsDenied(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	workingDir := filepath.Join(m.scratchDir, "s1")
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	// An intermediate directory component is a symlink pointing outside the
	// working set; the MC process could create such a link inside its own dir.
	outsideDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(outsideDir, "secret"), []byte("top-secret"), 0o640); err != nil {
		t.Fatalf("seed secret: %v", err)
	}
	if err := os.Symlink(outsideDir, filepath.Join(workingDir, "sub")); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: "sub/secret",
	})
	if res.Success {
		t.Fatal("ReadFile through an intermediate symlink should be denied")
	}
	if res.ErrorCode != session.CommandErrorFileAccessDenied {
		t.Fatalf("ErrorCode = %v, want FileAccessDenied", res.ErrorCode)
	}
}

func TestEditFileIntermediateSymlinkEscapeIsDenied(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	workingDir := filepath.Join(m.scratchDir, "s1")
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	outsideDir := t.TempDir()
	if err := os.Symlink(outsideDir, filepath.Join(workingDir, "sub")); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile",
		Path: "sub/escape", Content: []byte("pwned"),
	})
	if res.Success {
		t.Fatal("EditFile through an intermediate symlink should be denied")
	}
	if res.ErrorCode != session.CommandErrorFileAccessDenied {
		t.Fatalf("ErrorCode = %v, want FileAccessDenied", res.ErrorCode)
	}
	// MkdirAll must not have created anything through the link, and the write
	// must not have landed outside the working set.
	if _, err := os.Stat(filepath.Join(outsideDir, "escape")); !os.IsNotExist(err) {
		t.Fatal("a file escaped the working dir through an intermediate symlink")
	}
}

func TestEditFileIntermediateSymlinkViaNewDirsIsDenied(t *testing.T) {
	// The escaping symlink is itself a deeper intermediate component that
	// MkdirAll would have to create part of the path through.
	m := newManager(t, &fakeDriver{}, nil)
	workingDir := filepath.Join(m.scratchDir, "s1")
	if err := os.MkdirAll(filepath.Join(workingDir, "a"), 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	outsideDir := t.TempDir()
	if err := os.Symlink(outsideDir, filepath.Join(workingDir, "a", "b")); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile",
		Path: "a/b/c/escape", Content: []byte("pwned"),
	})
	if res.Success {
		t.Fatal("EditFile through a deeper intermediate symlink should be denied")
	}
	if res.ErrorCode != session.CommandErrorFileAccessDenied {
		t.Fatalf("ErrorCode = %v, want FileAccessDenied", res.ErrorCode)
	}
	if _, err := os.Stat(filepath.Join(outsideDir, "c")); !os.IsNotExist(err) {
		t.Fatal("MkdirAll created dirs outside the working set through a symlink")
	}
}

// TestOpenParentBeneathWriteRidesDirfd is the resolved-dirfd regression for the
// residual TOCTOU (issue #122). A truly concurrent symlink swap is inherently
// racy to test; instead it deterministically swaps the lexical parent for an
// escaping symlink *after* the parent is resolved, then writes through the
// resolved fd. If the write rode the lexical path it would escape; riding the fd,
// it lands on the original (now-renamed) inode and never touches the outside dir.
func TestOpenParentBeneathWriteRidesDirfd(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	root := filepath.Join(m.scratchDir, "s1")
	if err := os.MkdirAll(filepath.Join(root, "sub"), 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	target := filepath.Join(root, "sub", "f.txt")

	parentFd, leaf, err := openParentBeneath(root, target, false)
	if err != nil {
		t.Fatalf("openParentBeneath: %v", err)
	}
	defer func() { _ = unix.Close(parentFd) }()

	// Swap the lexical parent for a symlink pointing outside the working set,
	// simulating the concurrent attacker between resolve and act.
	outside := t.TempDir()
	if err := os.Rename(filepath.Join(root, "sub"), filepath.Join(root, "sub_real")); err != nil {
		t.Fatalf("rename: %v", err)
	}
	if err := os.Symlink(outside, filepath.Join(root, "sub")); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	if err := atomicWriteAt(parentFd, leaf, []byte("safe")); err != nil {
		t.Fatalf("atomicWriteAt: %v", err)
	}

	// The write rode the resolved fd: it landed on the original inode (now
	// sub_real), not through the freshly planted escaping symlink.
	if got, err := os.ReadFile(filepath.Join(root, "sub_real", "f.txt")); err != nil || string(got) != "safe" {
		t.Fatalf("file did not land on the resolved dirfd inode: got=%q err=%v", got, err)
	}
	if _, err := os.Stat(filepath.Join(outside, "f.txt")); !os.IsNotExist(err) {
		t.Fatal("write escaped through the post-resolve symlink swap")
	}
}

// TestOpenParentBeneathResolvesCleanPath pins the happy path (openat2 fast path
// on Linux, the component walk elsewhere): a clean nested path resolves to a
// dirfd under which the leaf opens.
func TestOpenParentBeneathResolvesCleanPath(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	root := filepath.Join(m.scratchDir, "s1")
	if err := os.MkdirAll(filepath.Join(root, "a", "b"), 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(root, "a", "b", "leaf"), []byte("x"), 0o640); err != nil {
		t.Fatalf("seed: %v", err)
	}

	parentFd, leaf, err := openParentBeneath(root, filepath.Join(root, "a", "b", "leaf"), false)
	if err != nil {
		t.Fatalf("openParentBeneath: %v", err)
	}
	defer func() { _ = unix.Close(parentFd) }()

	if leaf != "leaf" {
		t.Fatalf("leaf = %q, want %q", leaf, "leaf")
	}
	fd, err := unix.Openat(parentFd, leaf, unix.O_RDONLY|unix.O_NOFOLLOW, 0)
	if err != nil {
		t.Fatalf("openat leaf via resolved fd: %v", err)
	}
	_ = unix.Close(fd)
}

func TestReadFileOversizedIsDenied(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	writeWorkingFile(t, m, "s1", "big.bin", make([]byte, MaxFileBytes+1))

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "ReadFile", Path: "big.bin",
	})
	if res.Success {
		t.Fatal("ReadFile of an oversized file should be denied")
	}
	if res.ErrorCode != session.CommandErrorFileAccessDenied {
		t.Fatalf("ErrorCode = %v, want FileAccessDenied", res.ErrorCode)
	}
}

func TestEditFileWritesBytes(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile",
		Path: "config/sub.properties", Content: []byte("k=v"),
	})
	if !res.Success {
		t.Fatalf("EditFile result = %+v, want success", res)
	}
	got, err := os.ReadFile(filepath.Join(m.scratchDir, "s1", "config", "sub.properties"))
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if string(got) != "k=v" {
		t.Fatalf("written = %q, want %q", got, "k=v")
	}
}

func TestEditFileTraversalIsDenied(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile",
		Path: "../escape", Content: []byte("x"),
	})
	if res.Success {
		t.Fatal("EditFile traversal should be denied")
	}
	if res.ErrorCode != session.CommandErrorFileAccessDenied {
		t.Fatalf("ErrorCode = %v, want FileAccessDenied", res.ErrorCode)
	}
	// And nothing escaped onto disk.
	if _, err := os.Stat(filepath.Join(m.scratchDir, "escape")); !os.IsNotExist(err) {
		t.Fatal("a file escaped the working dir")
	}
}

func TestEditFileOversizedIsDenied(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile",
		Path: "big.bin", Content: make([]byte, MaxFileBytes+1),
	})
	if res.Success {
		t.Fatal("EditFile of an oversized payload should be denied")
	}
	if res.ErrorCode != session.CommandErrorFileAccessDenied {
		t.Fatalf("ErrorCode = %v, want FileAccessDenied", res.ErrorCode)
	}
}

func TestEditFileSymlinkOverwriteIsDenied(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	workingDir := filepath.Join(m.scratchDir, "s1")
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	outside := filepath.Join(t.TempDir(), "outside")
	if err := os.WriteFile(outside, []byte("orig"), 0o640); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if err := os.Symlink(outside, filepath.Join(workingDir, "link")); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile",
		Path: "link", Content: []byte("pwned"),
	})
	if res.Success {
		t.Fatal("EditFile through a symlink should be denied")
	}
	if res.ErrorCode != session.CommandErrorFileAccessDenied {
		t.Fatalf("ErrorCode = %v, want FileAccessDenied", res.ErrorCode)
	}
	// The symlink target outside the working dir was not overwritten.
	got, err := os.ReadFile(outside)
	if err != nil {
		t.Fatalf("read outside: %v", err)
	}
	if string(got) != "orig" {
		t.Fatalf("outside file = %q, want unchanged %q", got, "orig")
	}
}

func TestEditFileRejectsRoot(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	for _, root := range []string{".", ""} {
		res := m.Handle(context.Background(), session.Command{
			CommandID: "c1", ServerID: "s1", Kind: "EditFile", Path: root, Content: []byte("x"),
		})
		if res.Success || res.ErrorCode != session.CommandErrorFileAccessDenied {
			t.Fatalf("EditFile path %q result = %+v, want FileAccessDenied", root, res)
		}
	}
}

func TestEditFileAtomicWriteLeavesNoTemp(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	res := m.Handle(context.Background(), session.Command{
		CommandID: "c1", ServerID: "s1", Kind: "EditFile", Path: "f.txt", Content: []byte("x"),
	})
	if !res.Success {
		t.Fatalf("EditFile result = %+v, want success", res)
	}
	entries, err := os.ReadDir(filepath.Join(m.scratchDir, "s1"))
	if err != nil {
		t.Fatalf("readdir: %v", err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".edit-") {
			t.Fatalf("leftover temp file %q after atomic write", e.Name())
		}
	}
}
