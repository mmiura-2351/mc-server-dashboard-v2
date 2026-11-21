package nofollow

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

// Openat2Beneath has the kernel refuse a symlink at any component, whether it
// points inside dirFd or out of it, and any path that would leave dirFd: a ".."
// past it, or an absolute path.
func TestOpenat2BeneathRefusesALinkOrAnEscape(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "dir")
	if err := os.MkdirAll(filepath.Join(dir, "sub"), 0o750); err != nil {
		t.Fatal(err)
	}
	// Relative, so resolving it never leaves dir: only the refusal of every link
	// stops it.
	if err := os.Symlink("sub", filepath.Join(dir, "inside")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(root, filepath.Join(dir, "outside")); err != nil {
		t.Fatal(err)
	}
	dirFd, err := OpenDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = unix.Close(dirFd) }()
	if fd, err := Openat2Beneath(dirFd, "."); errors.Is(err, unix.ENOSYS) {
		t.Skip("openat2 is not available on this kernel")
	} else if err == nil {
		_ = unix.Close(fd)
	}

	for rel, want := range map[string]error{
		"sub":                      nil,
		"inside":                   unix.ELOOP,
		"inside/..":                unix.ELOOP,
		"outside/dir":              unix.ELOOP,
		"..":                       unix.EXDEV,
		"sub/../..":                unix.EXDEV,
		filepath.Join(dir, "sub"):  unix.EXDEV,
		filepath.Join(root, "dir"): unix.EXDEV,
	} {
		fd, err := Openat2Beneath(dirFd, rel)
		if err == nil {
			_ = unix.Close(fd)
		}
		if !errors.Is(err, want) {
			t.Errorf("Openat2Beneath(%q) = %v, want %v", rel, err, want)
		}
	}
}
