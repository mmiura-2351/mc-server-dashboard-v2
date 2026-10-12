package containerdriver

import (
	"errors"
	"fmt"
	"log/slog"
	"path"
	"strings"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/nofollow"
)

// This file removes stale Forge install outputs before a (re-)install.
//
// The working set is written by the server and the Forge installer — code the
// operator does not control — and under the shipped topology the Worker is root.
// Globbing and removing by path would follow a symlink planted at any component
// of libraries/net/minecraftforge/forge/<version>/ and delete a unix_args.txt
// outside the working set (issue #3333). Like the install-log open
// (installlog.go), the cleanup therefore never resolves a path below the
// working-set root: it descends by descriptor, each directory opened relative to
// its parent with O_NOFOLLOW, and unlinks a match by name in the directory it
// holds open.

// forgeCleanOpenAt and forgeCleanUnlinkAt are the cleanup's two system calls.
// They are variables so a test can swap an entry at the exact moment between the
// cleanup listing it and acting on it.
var (
	forgeCleanOpenAt   = nofollow.OpenAt
	forgeCleanUnlinkAt = unix.Unlinkat
)

// cleanForgeInstallArtifacts removes every entry of workingDir matching one of
// execution.ForgeInstallArtifactGlobs, so a subsequent install starts from a
// clean slate and the re-plan never sees a stale or ambiguous match (issue
// #1127).
//
// A component that is absent, a symlink or anything else that is not a directory
// has nothing to clean beneath it; the latter two are logged. A match is
// unlinked whatever it is — a symlink planted as the artifact is itself removed,
// never its target — except a directory, which is left in place and logged. An
// absent workingDir is not an error. Any other failure is returned, joined, after
// the rest of the cleanup has run.
func cleanForgeInstallArtifacts(workingDir string, logger *slog.Logger) error {
	rootFd, err := nofollow.OpenDir(workingDir)
	if errors.Is(err, unix.ENOENT) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("open %s: %w", workingDir, err)
	}
	defer func() { _ = unix.Close(rootFd) }()

	var errs []error
	for _, glob := range execution.ForgeInstallArtifactGlobs() {
		errs = append(errs, removeGlobAt(rootFd, "", strings.Split(glob, "/"), logger))
	}
	return errors.Join(errs...)
}

// removeGlobAt unlinks the entries matching the slash-split glob components
// beneath the directory behind dirFd, whose working-dir-relative path is prefix.
func removeGlobAt(dirFd int, prefix string, components []string, logger *slog.Logger) error {
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
			// unlinkat never follows a link at name and, without AT_REMOVEDIR, refuses
			// a directory: no type is decided here that a swap could invalidate.
			err := forgeCleanUnlinkAt(dirFd, name, 0)
			switch {
			case err == nil, errors.Is(err, unix.ENOENT):
			case errors.Is(err, unix.EISDIR):
				logger.Warn("stale Forge artifact not removed: it is a directory", "path", rel)
			default:
				errs = append(errs, fmt.Errorf("remove %s: %w", rel, err))
			}
			continue
		}
		fd, err := forgeCleanOpenAt(dirFd, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		switch {
		case err == nil:
			errs = append(errs, removeGlobAt(fd, rel, components[1:], logger))
			_ = unix.Close(fd)
		case errors.Is(err, unix.ENOENT):
		case errors.Is(err, unix.ELOOP), errors.Is(err, unix.ENOTDIR):
			logger.Warn("stale Forge artifacts not cleaned: not a directory (a symlink is not followed)", "path", rel)
		default:
			errs = append(errs, fmt.Errorf("open %s: %w", rel, err))
		}
	}
	return errors.Join(errs...)
}
