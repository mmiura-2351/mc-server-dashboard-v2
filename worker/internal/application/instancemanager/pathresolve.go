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
				if oerr == nil {
					// The directory is the Worker's creation; give it its parent's
					// owner so the server can traverse it (issue #2600).
					if ownErr := inheritOwner(cur, next); ownErr != nil {
						_ = unix.Close(next)
						_ = unix.Close(cur)
						return -1, "", fmt.Errorf("setting owner of %q: %w", comp, ownErr)
					}
				}
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

// dirOwner reports the uid:gid owning the directory behind dirFd, and fchownFd
// re-owns the file or directory behind fd. Both act on descriptors only; they are
// variables so a test can give the working set an owner other than the test
// process (only root can give a file away).
var (
	dirOwner = func(dirFd int) (uid, gid int, err error) {
		var st unix.Stat_t
		if err := unix.Fstat(dirFd, &st); err != nil {
			return 0, 0, err
		}
		return int(st.Uid), int(st.Gid), nil
	}
	fchownFd = unix.Fchown
)

// inheritOwner gives the entry the Worker just created behind fd the owner of the
// directory it was created in (parentFd).
//
// A server's working set belongs to the user its container runs as, which is not
// the Worker's own under a root Worker (issue #2600). An entry the Worker creates
// while the server is up — an edited file's replacement, a missing parent
// directory — would otherwise be the Worker's, mode 0640 / 0750, and the server
// could no longer read, overwrite or traverse it. The directory's owner is the
// run-as user once the driver has handed the working set over, and the Worker
// itself before that, so this needs no knowledge of who that user is.
//
// Both ends are descriptors already resolved beneath the working-set root, so no
// path is looked up again and a swapped symlink cannot redirect the change.
func inheritOwner(parentFd, fd int) error {
	uid, gid, err := dirOwner(parentFd)
	if err != nil {
		return err
	}
	return fchownFd(fd, uid, gid)
}
