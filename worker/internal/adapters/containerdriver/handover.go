package containerdriver

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"golang.org/x/sys/unix"
)

// Quiesce the working set before ownership handoff, then walk by descriptor with O_NOFOLLOW.
// The root Worker must never follow server-controlled symlinks outside the tree.

// quiescentStates are the Engine container states in which no process of the
// container exists. Every other state — running, paused, restarting, removing,
// and anything this code does not know — counts as possibly alive.
var quiescentStates = map[string]bool{"created": true, "exited": true, "dead": true}

// awaitQuiescent fails closed for live or unobservable launch/install containers, regardless of owner.
// Wait briefly for daemon teardown state to settle after a confirmed exit.
func (d *Driver) awaitQuiescent(ctx context.Context, serverID string) error {
	deadline := time.NewTimer(d.conflictDeadline)
	defer deadline.Stop()
	for {
		containers, err := d.docker.List(ctx, labelServerID, serverID)
		if err != nil {
			return fmt.Errorf("cannot establish that no container of the server is running: %w", err)
		}
		var alive *Container
		for i := range containers {
			if !quiescentStates[containers[i].State] {
				alive = &containers[i]
				break
			}
		}
		if alive == nil {
			return nil
		}
		poll := time.NewTimer(d.conflictPoll)
		select {
		case <-ctx.Done():
			poll.Stop()
			return ctx.Err()
		case <-deadline.C:
			poll.Stop()
			return fmt.Errorf("container %s of the server is %q; its working set is in use", alive.Name, alive.State)
		case <-poll.C:
		}
	}
}

// handOverStats describes one hand-over walk.
type handOverStats struct {
	// entries is every entry visited, the root included; changed is how many of
	// them were re-owned.
	entries, changed int
}

// handOverWorkingSet changes only mismatched ownership and fails if the walk cannot complete.
// An absent root is tolerated here; the Manager's launch guard rejects missing working sets.
func (d *Driver) handOverWorkingSet(ctx context.Context, workingDir string) (handOverStats, error) {
	var stats handOverStats
	rootFd, err := unix.Open(workingDir, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if errors.Is(err, unix.ENOENT) {
		return stats, nil
	}
	if err != nil {
		return stats, &os.PathError{Op: "open", Path: workingDir, Err: err}
	}
	defer func() { _ = unix.Close(rootFd) }()

	if _, err := d.handOverNamed(rootFd, ".", &stats); err != nil {
		return stats, &os.PathError{Op: "chown", Path: workingDir, Err: err}
	}
	if err := d.handOverDir(ctx, rootFd, workingDir, &stats); err != nil {
		return stats, err
	}
	return stats, nil
}

// handOverDir hands over every entry of the directory behind dirFd, descending
// into subdirectories. dirPath only labels errors; it is never resolved.
func (d *Driver) handOverDir(ctx context.Context, dirFd int, dirPath string, stats *handOverStats) error {
	names, err := readDirNames(dirFd)
	if err != nil {
		return &os.PathError{Op: "readdir", Path: dirPath, Err: err}
	}
	for _, name := range names {
		if err := ctx.Err(); err != nil {
			return err
		}
		entryPath := filepath.Join(dirPath, name)
		isDir, err := d.handOverNamed(dirFd, name, stats)
		if err != nil {
			return &os.PathError{Op: "chown", Path: entryPath, Err: err}
		}
		if !isDir {
			continue
		}
		// O_NOFOLLOW: if the directory just re-owned was swapped for a symlink,
		// the open fails here instead of leading the walk out of the tree.
		childFd, err := unix.Openat(dirFd, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if err != nil {
			return &os.PathError{Op: "open", Path: entryPath, Err: err}
		}
		err = d.handOverDir(ctx, childFd, entryPath, stats)
		_ = unix.Close(childFd)
		if err != nil {
			return err
		}
	}
	return nil
}

// handOverNamed re-owns the entry name of the directory behind dirFd unless the
// run-as user already owns it, and reports whether the entry is a directory.
func (d *Driver) handOverNamed(dirFd int, name string, stats *handOverStats) (isDir bool, err error) {
	var st unix.Stat_t
	if err := unix.Fstatat(dirFd, name, &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		return false, err
	}
	stats.entries++
	if int(st.Uid) != d.runAsUID || int(st.Gid) != d.runAsGID {
		if err := d.chownAt(dirFd, name, d.runAsUID, d.runAsGID); err != nil {
			return false, err
		}
		stats.changed++
	}
	return st.Mode&unix.S_IFMT == unix.S_IFDIR, nil
}

// chownAtNoFollow re-owns the entry name of the directory behind dirFd. It never
// follows a symlink: a link is re-owned itself.
func chownAtNoFollow(dirFd int, name string, uid, gid int) error {
	return unix.Fchownat(dirFd, name, uid, gid, unix.AT_SYMLINK_NOFOLLOW)
}

// readDirNames lists the directory behind dirFd. It reads through a duplicate so
// the caller keeps ownership of dirFd.
func readDirNames(dirFd int) ([]string, error) {
	dup, err := unix.Dup(dirFd)
	if err != nil {
		return nil, err
	}
	dir := os.NewFile(uintptr(dup), "")
	defer func() { _ = dir.Close() }()
	return dir.Readdirnames(-1)
}
