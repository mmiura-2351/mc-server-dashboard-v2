package instancemanager

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

// openParentBeneath walks beneath root by descriptor and refuses symlinks, including during parent creation.
// The caller must close the returned parent fd and perform the final operation relative to it.
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

	// The scratch root is trusted; walk all child components with O_NOFOLLOW.
	// Edits create missing parents, while reads surface a missing root as ENOENT.
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
					// The directory is the Worker's creation; give it its parent's owner so the server can traverse it.
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

// Inherit directory ownership for new files and parents so the container user can still read and write them.
// Both descriptors are already resolved beneath scratch; no symlink-following path lookup is needed.
func inheritOwner(parentFd, fd int) error {
	uid, gid, err := dirOwner(parentFd)
	if err != nil {
		return err
	}
	return fchownFd(fd, uid, gid)
}
