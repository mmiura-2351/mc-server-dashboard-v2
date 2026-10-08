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

// This file is the working-set hand-over: before the driver creates a container
// it makes the run-as user the owner of the server's bind-mounted working set,
// so a server that no longer runs as root can write its world, logs and config
// (issue #2600).
//
// The tree is written by code the operator does not control, and under the
// shipped topology the Worker walking it is root. Two things keep that from
// becoming a way to re-own files outside the tree:
//
//   - nothing may be running against the tree: awaitQuiescent establishes that
//     no container of this server is alive before the walk starts, and refuses
//     the start when it cannot tell;
//   - the walk never resolves a path. It descends by descriptor — each directory
//     is opened relative to its parent's descriptor with O_NOFOLLOW, each entry
//     is re-owned relative to its directory's descriptor without following a
//     link — so a directory swapped for a symlink mid-walk is refused, not
//     followed.

// quiescentStates are the Engine container states in which no process of the
// container exists. Every other state — running, paused, restarting, and
// anything this code does not know — counts as possibly alive.
var quiescentStates = map[string]bool{"created": true, "exited": true, "dead": true}

// containerStateRemoving is the state of a container whose removal is in flight.
// Its processes may not be gone yet, so it is waited out rather than trusted.
const containerStateRemoving = "removing"

// awaitQuiescent returns nil once no container of serverID — launch or install,
// whoever created it — can be running against the working set. It fails closed:
// a container that is or may be alive, and a daemon that cannot say, both refuse.
//
// A container still being removed is the one state worth waiting for: a restart
// races the exit-watcher's asynchronous removal of the previous container (issue
// #226), which finishes within the same window the create's own name-conflict
// loop already allows (issue #233).
func (d *Driver) awaitQuiescent(ctx context.Context, serverID string) error {
	deadline := time.NewTimer(d.conflictDeadline)
	defer deadline.Stop()
	for {
		containers, err := d.docker.List(ctx, labelServerID, serverID)
		if err != nil {
			return fmt.Errorf("cannot establish that no container of the server is running: %w", err)
		}
		removing := ""
		for _, c := range containers {
			switch {
			case quiescentStates[c.State]:
			case c.State == containerStateRemoving:
				removing = c.Name
			default:
				return fmt.Errorf("container %s of the server is %q; its working set is in use", c.Name, c.State)
			}
		}
		if removing == "" {
			return nil
		}
		poll := time.NewTimer(d.conflictPoll)
		select {
		case <-ctx.Done():
			poll.Stop()
			return ctx.Err()
		case <-deadline.C:
			poll.Stop()
			return fmt.Errorf("container %s of the server is still being removed; its working set may be in use", removing)
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

// handOverWorkingSet makes the run-as user the owner of workingDir and of every
// entry beneath it that it does not already own, and reports what it did.
//
// Entries already owned are left untouched, so a Worker that runs as the run-as
// user itself (an unprivileged host process) makes no chown call. Such a Worker
// cannot give away a file it does not own; one it meets — left by a container
// that ran as root before this change — fails the start with the path, rather
// than booting a server that cannot save its world.
//
// Only an absent workingDir is tolerated (nothing to hand over; the Manager's
// working-set guard refuses a start without one before the driver is reached).
// Any failure after the walk has begun — an entry that vanished, a directory
// that turned into something else — is an error: the tree was not fully handed
// over, and the caller must not start a server on it.
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
