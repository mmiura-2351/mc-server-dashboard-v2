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

// This file answers the launch planning's questions about a working set: is
// Forge installed (which args file is there), is there a user JVM args file,
// which legacy launch jar did the installer leave.
//
// The working set is written by the server and the Forge installer — code the
// operator does not control — and under the shipped topology the Worker is root.
// Answered by path (filepath.Glob, os.Stat), a symlink planted at any component
// was followed out of the working set: entries outside it decided whether Forge
// was installed, the name of an outside directory went onto the server's command
// line, and whether an arbitrary host path existed could be read back from that
// command line (issue #3352). Like the stale-artifact cleanup (forgeclean.go),
// the lookup therefore never resolves a path below the working-set root: it
// descends by descriptor, each directory opened relative to its parent with
// O_NOFOLLOW, and examines a candidate by name in the directory it holds open,
// without following a link at it.

// launchOpenAt and launchStatAt are the lookup's two system calls. They are
// variables so a test can swap an entry at the exact moment between the lookup
// listing it and examining it.
var (
	launchOpenAt = nofollow.OpenAt
	launchStatAt = unix.Fstatat
)

// containerPathResolver maps working-set-relative paths onto in-container paths
// under /data and answers Exists and Files from the host working dir (the bind
// source) under the execution.PathResolver contract: regular files only, no
// symlink followed. What it passes over for not being one is logged to logger.
func containerPathResolver(workingDir string, logger *slog.Logger) execution.PathResolver {
	files := func(pattern string) ([]string, error) {
		return regularFiles(workingDir, pattern, logger)
	}
	return execution.PathResolver{
		Resolve: func(rel string) string { return containerWorkDir + "/" + rel },
		Exists: func(rel string) bool {
			matches, err := files(rel)
			return err == nil && len(matches) == 1
		},
		Files: files,
	}
}

// regularFiles returns the working-dir-relative slash paths of the regular files
// of workingDir matching the slash-separated pattern.
//
// A component that is absent, a plain file, a symlink or anything else that is
// not a directory has nothing beneath it, and a final entry that is a symlink, a
// directory, a FIFO or a device is not a file; all but the absent and plain-file
// components are logged. An absent workingDir holds no files. Any other failure
// is returned: it is not evidence that nothing is there.
func regularFiles(workingDir, pattern string, logger *slog.Logger) ([]string, error) {
	rootFd, err := nofollow.OpenDir(workingDir)
	if errors.Is(err, unix.ENOENT) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", workingDir, err)
	}
	defer func() { _ = unix.Close(rootFd) }()

	var found []string
	w := globWalk{
		openAt:  launchOpenAt,
		skipped: "working-set entry ignored by the launch planning",
		logger:  logger,
		leaf: func(dirFd int, name, rel string) error {
			// The type is that of the entry itself, never of a link's target, and
			// nothing is opened: a FIFO cannot block this. The answer is only ever a
			// name inside the working set, so an entry swapped afterwards changes what
			// the container finds at that name, not what the Worker touched.
			var st unix.Stat_t
			err := launchStatAt(dirFd, name, &st, unix.AT_SYMLINK_NOFOLLOW)
			switch {
			case errors.Is(err, unix.ENOENT):
			case err != nil:
				return fmt.Errorf("stat %s: %w", rel, err)
			case st.Mode&unix.S_IFMT == unix.S_IFREG:
				found = append(found, rel)
			case st.Mode&unix.S_IFMT == unix.S_IFLNK:
				logger.Warn("working-set entry ignored by the launch planning: a symlink is not followed", "path", rel)
			default:
				logger.Warn("working-set entry ignored by the launch planning: not a regular file", "path", rel)
			}
			return nil
		},
	}
	if err := w.walkAt(rootFd, "", strings.Split(pattern, "/")); err != nil {
		return nil, err
	}
	return found, nil
}
