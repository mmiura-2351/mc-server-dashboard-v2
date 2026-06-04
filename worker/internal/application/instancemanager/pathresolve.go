package instancemanager

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

// openParentBeneath opens the parent directory of target as a dirfd that is
// guaranteed to stay beneath root, closing the residual TOCTOU left by the
// resolve-then-reopen-by-lexical-path approach (issue #122): the final open /
// rename happens *relative to this fd*, so a concurrently swapped intermediate
// symlink between the check and the act can no longer redirect the access.
//
// safeJoin has already rejected lexical escapes (absolute / ".."); this resolves
// each component of the path *under root* refusing to follow symlinks, so an
// intermediate-component symlink the running MC process plants is denied rather
// than followed (FR-FILE-4).
//
// When mkdir is true (the edit path), missing intermediate components are created
// beneath root as part of the same race-free walk, so MkdirAll can no longer
// traverse a link and create dirs outside the root.
//
// It returns the parent dirfd (the caller must close it), the leaf base name to
// open/rename relative to that fd, and any error. A symlink or escape on the
// path yields an error.
func openParentBeneath(root, target string, mkdir bool) (parentFd int, leaf string, err error) {
	rel, err := filepath.Rel(root, target)
	if err != nil {
		return -1, "", fmt.Errorf("relativizing %q under %q: %w", target, root, err)
	}
	components := strings.Split(filepath.ToSlash(rel), "/")
	if len(components) == 0 || components[0] == ".." || components[0] == "." {
		// safeJoin already guarantees this cannot happen; guard defensively.
		return -1, "", fmt.Errorf("refusing path escape %q", target)
	}
	leaf = components[len(components)-1]
	dirs := components[:len(components)-1]

	// The working-set root is trusted (we own the scratch tree), so a plain open
	// is fine; the per-component NOFOLLOW walk below is what enforces containment.
	// For an edit, materialize the root first (a fresh server hydrated to nothing
	// has no working dir yet); for a read, a missing root surfaces as ENOENT so
	// the caller maps it to a not-found result.
	if mkdir {
		if err := os.MkdirAll(root, 0o750); err != nil {
			return -1, "", fmt.Errorf("creating root %q: %w", root, err)
		}
	}
	rootFd, err := unix.Open(root, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return -1, "", err
	}

	// Fast path: with no dirs to create, resolve the parent in one constrained
	// syscall (openat2 RESOLVE_BENEATH) where the kernel supports it. The walk
	// below is the documented fallback when openat2 is unavailable (ENOSYS).
	if !mkdir {
		relParent := filepath.ToSlash(filepath.Join(dirs...))
		if relParent == "" {
			relParent = "."
		}
		fd, oerr := openat2Beneath(rootFd, relParent)
		if oerr == nil {
			_ = unix.Close(rootFd)
			return fd, leaf, nil
		}
		if !errors.Is(oerr, unix.ENOSYS) {
			_ = unix.Close(rootFd)
			return -1, "", oerr
		}
		// ENOSYS: fall through to the per-component O_NOFOLLOW walk.
	}

	cur := rootFd
	for _, comp := range dirs {
		next, oerr := unix.Openat(cur, comp,
			unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
		if oerr != nil {
			if mkdir && errors.Is(oerr, unix.ENOENT) {
				if mkErr := unix.Mkdirat(cur, comp, 0o750); mkErr != nil {
					_ = unix.Close(cur)
					return -1, "", fmt.Errorf("creating %q: %w", comp, mkErr)
				}
				next, oerr = unix.Openat(cur, comp,
					unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
			}
			if oerr != nil {
				_ = unix.Close(cur)
				return -1, "", fmt.Errorf("refusing path escape via symlink %q: %w", target, oerr)
			}
		}
		_ = unix.Close(cur)
		cur = next
	}
	return cur, leaf, nil
}
