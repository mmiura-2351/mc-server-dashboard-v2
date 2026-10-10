//go:build linux

package instancemanager

import "golang.org/x/sys/unix"

// openat2Beneath uses RESOLVE_BENEATH and refuses symlinks, returning an O_PATH directory fd.
// ENOSYS falls back to the component-wise O_NOFOLLOW walk.
func openat2Beneath(dirfd int, relDir string) (int, error) {
	return unix.Openat2(dirfd, relDir, &unix.OpenHow{
		Flags:   unix.O_PATH | unix.O_DIRECTORY | unix.O_CLOEXEC,
		Resolve: unix.RESOLVE_BENEATH | unix.RESOLVE_NO_SYMLINKS,
	})
}
