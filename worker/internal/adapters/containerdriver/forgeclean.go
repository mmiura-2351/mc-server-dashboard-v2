package containerdriver

import (
	"errors"
	"fmt"
	"log/slog"
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
// A component that is absent, a plain file, a symlink or anything else that is
// not a directory has nothing to clean beneath it; the latter two are logged. A
// match is unlinked whatever it is — a symlink planted as the artifact is itself
// removed, never its target — except a directory, which is left in place and
// logged. An absent workingDir is not an error. Any other failure is returned,
// joined, after the rest of the cleanup has run.
func cleanForgeInstallArtifacts(workingDir string, logger *slog.Logger) error {
	rootFd, err := nofollow.OpenDir(workingDir)
	if errors.Is(err, unix.ENOENT) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("open %s: %w", workingDir, err)
	}
	defer func() { _ = unix.Close(rootFd) }()

	w := globWalk{
		openAt:  forgeCleanOpenAt,
		skipped: "stale Forge artifacts not cleaned",
		logger:  logger,
		leaf: func(dirFd int, name, rel string) error {
			// unlinkat never follows a link at name and, without AT_REMOVEDIR, refuses
			// a directory: no type is decided here that a swap could invalidate.
			err := forgeCleanUnlinkAt(dirFd, name, 0)
			switch {
			case err == nil, errors.Is(err, unix.ENOENT):
			case errors.Is(err, unix.EISDIR):
				logger.Warn("stale Forge artifact not removed: it is a directory", "path", rel)
			default:
				return fmt.Errorf("remove %s: %w", rel, err)
			}
			return nil
		},
	}
	var errs []error
	for _, glob := range execution.ForgeInstallArtifactGlobs() {
		errs = append(errs, w.walkAt(rootFd, "", strings.Split(glob, "/")))
	}
	return errors.Join(errs...)
}
