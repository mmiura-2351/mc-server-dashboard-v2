package containerdriver

import (
	"errors"
	"fmt"
	"log/slog"
	"path"
	"strings"

	"golang.org/x/sys/unix"
)

// globWalk is a descriptor-relative glob over a working set, shared by the
// stale-artifact cleanup (forgeclean.go) and the launch planning's lookup
// (launchfiles.go). It never resolves a path below the directory it starts
// from: each directory component is opened relative to its parent with
// O_NOFOLLOW, and each final match is handed to leaf by name in the directory
// it holds open.
type globWalk struct {
	// openAt opens a directory component; it is the caller's seam.
	openAt func(dirFd int, name string, flags int, mode uint32) (int, error)
	// skipped prefixes the warning for a directory component passed over.
	skipped string
	logger  *slog.Logger
	// leaf acts on one final match, called name in the directory behind dirFd
	// and rel relative to the working dir.
	leaf func(dirFd int, name, rel string) error
}

// walkAt calls w.leaf for each entry matching the slash-split glob components
// beneath the directory behind dirFd, whose working-dir-relative path is prefix.
//
// A directory component that is absent or a plain file has nothing beneath it
// and is skipped quietly; a symlink, or anything else that is not a directory,
// is skipped with a warning. Any other failure, the leaf's included, is
// returned, joined, after the rest of the walk has run.
func (w globWalk) walkAt(dirFd int, prefix string, components []string) error {
	names := components[:1]
	if strings.ContainsAny(names[0], `*?[\`) {
		all, err := readDirNames(dirFd)
		if err != nil {
			return fmt.Errorf("list %s: %w", prefix, err)
		}
		names = nil
		for _, name := range all {
			matched, err := path.Match(components[0], name)
			if err != nil {
				return fmt.Errorf("match %s: %w", components[0], err)
			}
			if matched {
				names = append(names, name)
			}
		}
	}

	var errs []error
	for _, name := range names {
		rel := path.Join(prefix, name)
		if len(components) == 1 {
			errs = append(errs, w.leaf(dirFd, name, rel))
			continue
		}
		fd, err := w.openAt(dirFd, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		switch {
		case err == nil:
			errs = append(errs, w.walkAt(fd, rel, components[1:]))
			_ = unix.Close(fd)
		case errors.Is(err, unix.ENOENT):
		case errors.Is(err, unix.ELOOP), errors.Is(err, unix.ENOTDIR):
			// The open cannot tell a symlink from a file: ask the entry itself,
			// without following it. Only the log line depends on the answer.
			var st unix.Stat_t
			statErr := unix.Fstatat(dirFd, name, &st, unix.AT_SYMLINK_NOFOLLOW)
			switch {
			case statErr == nil && st.Mode&unix.S_IFMT == unix.S_IFREG:
			case statErr == nil && st.Mode&unix.S_IFMT == unix.S_IFLNK:
				w.logger.Warn(w.skipped+": a symlink is not followed", "path", rel)
			default:
				w.logger.Warn(w.skipped+": not a directory", "path", rel)
			}
		default:
			errs = append(errs, fmt.Errorf("open %s: %w", rel, err))
		}
	}
	return errors.Join(errs...)
}
