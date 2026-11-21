//go:build linux

package nofollow

import "golang.org/x/sys/unix"

// openat2Beneath is the one openat2 call Openat2Beneath makes.
func openat2Beneath(dirFd int, rel string) (int, error) {
	return unix.Openat2(dirFd, rel, &unix.OpenHow{
		Flags:   unix.O_PATH | unix.O_DIRECTORY | unix.O_CLOEXEC,
		Resolve: unix.RESOLVE_BENEATH | unix.RESOLVE_NO_SYMLINKS,
	})
}
