//go:build linux

package instancemanager

import "golang.org/x/sys/unix"

// openat2Beneath opens relDir under dirfd with RESOLVE_BENEATH, so the kernel
// itself refuses any component that is a symlink or that would escape dirfd
// (Linux 5.6+). The returned fd is an O_PATH directory handle suitable for the
// subsequent openat/renameat. On a kernel without openat2 the syscall returns
// ENOSYS and the caller falls back to the per-component O_NOFOLLOW walk.
func openat2Beneath(dirfd int, relDir string) (int, error) {
	return unix.Openat2(dirfd, relDir, &unix.OpenHow{
		Flags:   unix.O_PATH | unix.O_DIRECTORY | unix.O_CLOEXEC,
		Resolve: unix.RESOLVE_BENEATH | unix.RESOLVE_NO_SYMLINKS,
	})
}
