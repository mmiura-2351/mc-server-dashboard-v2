//go:build !linux

package nofollow

import "golang.org/x/sys/unix"

// openat2Beneath is Linux-only (openat2). Elsewhere it reports ENOSYS so the
// caller uses its per-component O_NOFOLLOW walk, which provides the same
// race-free containment without the single-syscall fast path.
func openat2Beneath(_ int, _ string) (int, error) {
	return -1, unix.ENOSYS
}
