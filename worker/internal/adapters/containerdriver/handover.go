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
//
// Staying inside the tree is still not enough for a file with more than one
// name, because re-owning acts on the inode and another name of it may be
// outside the tree. Such a file is handed over only once every one of its names
// has been found inside (handOverLinked, issue #3338).

// quiescentStates are the Engine container states in which no process of the
// container exists. Every other state — running, paused, restarting, removing,
// and anything this code does not know — counts as possibly alive.
var quiescentStates = map[string]bool{"created": true, "exited": true, "dead": true}

// awaitQuiescent returns nil once no container of serverID — launch or install,
// whoever created it — can be running against the working set. It fails closed:
// while one is or may be alive nothing is handed over, and a daemon that cannot
// say refuses outright.
//
// A live-looking container is waited for, up to the same window the create's own
// name-conflict loop allows (issue #233), before the start is refused. A restart
// needs that: the daemon's container list still reports the previous container
// as running for a moment after its exit was observed, and then as removing
// while the exit-watcher reaps it (issue #226). An orphan that really is alive
// simply outlasts the window.
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
	// skipped is how many multiply-linked files kept their owner because not
	// every name of theirs was found in the tree (handOverLinked), and
	// skippedExample the path of one of them.
	skipped        int
	skippedExample string
}

// inodeKey identifies a file across its names.
type inodeKey struct{ dev, ino uint64 }

// linkedFile is a multiply-linked file the walk has met and not handed over.
type linkedFile struct {
	// nlink is the file's link count when the walk first met it, names how many
	// of its names the walk has met, and first the path of the first of them.
	nlink, names uint64
	first        string
}

// handOverWalk is the state of one hand-over walk.
type handOverWalk struct {
	stats handOverStats
	// linked holds the multiply-linked files not handed over so far. A working
	// set the run-as user already owns adds nothing to it.
	linked map[inodeKey]*linkedFile
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
//
// A multiply-linked file left with its owner (handOverLinked) is not a failure:
// it is counted in the stats, for the caller to report.
func (d *Driver) handOverWorkingSet(ctx context.Context, workingDir string) (handOverStats, error) {
	w := &handOverWalk{linked: map[inodeKey]*linkedFile{}}
	rootFd, err := unix.Open(workingDir, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if errors.Is(err, unix.ENOENT) {
		return w.stats, nil
	}
	if err != nil {
		return w.stats, &os.PathError{Op: "open", Path: workingDir, Err: err}
	}
	defer func() { _ = unix.Close(rootFd) }()

	if _, err := d.handOverNamed(rootFd, ".", workingDir, w); err != nil {
		return w.stats, &os.PathError{Op: "chown", Path: workingDir, Err: err}
	}
	if err := d.handOverDir(ctx, rootFd, workingDir, w); err != nil {
		return w.stats, err
	}
	// What is still held has a name the walk never met.
	w.stats.skipped = len(w.linked)
	for _, f := range w.linked {
		if w.stats.skippedExample == "" || f.first < w.stats.skippedExample {
			w.stats.skippedExample = f.first
		}
	}
	return w.stats, nil
}

// handOverDir hands over every entry of the directory behind dirFd, descending
// into subdirectories. dirPath only labels errors; it is never resolved.
func (d *Driver) handOverDir(ctx context.Context, dirFd int, dirPath string, w *handOverWalk) error {
	names, err := readDirNames(dirFd)
	if err != nil {
		return &os.PathError{Op: "readdir", Path: dirPath, Err: err}
	}
	for _, name := range names {
		if err := ctx.Err(); err != nil {
			return err
		}
		entryPath := filepath.Join(dirPath, name)
		isDir, err := d.handOverNamed(dirFd, name, entryPath, w)
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
		err = d.handOverDir(ctx, childFd, entryPath, w)
		_ = unix.Close(childFd)
		if err != nil {
			return err
		}
	}
	return nil
}

// handOverNamed re-owns the entry name of the directory behind dirFd unless the
// run-as user already owns it, and reports whether the entry is a directory. A
// file with more than one name goes through handOverLinked instead. path only
// labels what is reported; it is never resolved.
func (d *Driver) handOverNamed(dirFd int, name, path string, w *handOverWalk) (isDir bool, err error) {
	var st unix.Stat_t
	if err := unix.Fstatat(dirFd, name, &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		return false, err
	}
	w.stats.entries++
	isDir = st.Mode&unix.S_IFMT == unix.S_IFDIR
	if int(st.Uid) != d.runAsUID || int(st.Gid) != d.runAsGID {
		// A directory's link count is its subdirectories, not other names.
		if !isDir && st.Nlink > 1 {
			return false, d.handOverLinked(dirFd, name, path, w)
		}
		if err := d.chownAt(dirFd, name, d.runAsUID, d.runAsGID); err != nil {
			return false, err
		}
		w.stats.changed++
	}
	return isDir, nil
}

// handOverLinked counts the entry name of the directory behind dirFd as one name
// of a multiply-linked file, and re-owns the file once the walk has met as many
// names of it as it has links.
//
// Re-owning acts on the inode, whichever name it is reached by, so a file that
// is also named outside the working set must not be handed over: that would
// re-own the outside file (issue #3338). Where a file's other names are cannot
// be looked up, but they can be counted: every name the walk meets is inside the
// tree, so when it has met as many as the link count there is none anywhere
// else. Until then the file keeps its owner, and one whose count never completes
// is reported as skipped. A link the server made between two of its own files
// completes; one to a file outside does not. This holds whatever
// fs.protected_hardlinks is set to, and whoever made the link.
//
// The count is only proof while no name of the file is added, removed or moved
// under the walk — which is what awaitQuiescent establishes for the server's
// code. Short of that, a file whose link count is no longer what it was when the
// walk first met it keeps its owner, which covers a name removed after it was
// counted and a name added.
//
// What it does not cover is a name that MOVES during the walk, from where it was
// counted to where the walk has yet to look: it is counted twice at an unchanged
// link count. The Worker's own file edit is not held off while the walk runs and
// publishes by renaming a temporary name, and a server can have put an
// outside-linked file under that temporary name before it stopped — a schedule
// that double-counts with no server code running (issue #3371). Nor does a count
// of names mean anything where the host has mounted part of the working set a
// second time inside it: the same names are then met twice (also #3371).
//
// The entry is opened without following a link (O_PATH: nothing is read, and a
// FIFO or device is not really opened), and both the link count and the
// ownership change go through that one descriptor, so what is counted is what is
// re-owned.
func (d *Driver) handOverLinked(dirFd int, name, path string, w *handOverWalk) error {
	fd, err := unix.Openat(dirFd, name, unix.O_PATH|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return err
	}
	defer func() { _ = unix.Close(fd) }()
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		return err
	}
	key := inodeKey{dev: uint64(st.Dev), ino: st.Ino}
	f := w.linked[key]
	if f == nil {
		f = &linkedFile{nlink: uint64(st.Nlink), first: path}
		w.linked[key] = f
	}
	f.names++
	if f.names != f.nlink || uint64(st.Nlink) != f.nlink {
		return nil
	}
	if err := d.chownAt(fd, "", d.runAsUID, d.runAsGID); err != nil {
		return err
	}
	delete(w.linked, key)
	w.stats.changed++
	return nil
}

// chownAtNoFollow re-owns the entry name of the directory behind dirFd, or —
// given an empty name — the file behind dirFd itself. It never follows a
// symlink: a link is re-owned itself.
func chownAtNoFollow(dirFd int, name string, uid, gid int) error {
	return unix.Fchownat(dirFd, name, uid, gid, unix.AT_SYMLINK_NOFOLLOW|unix.AT_EMPTY_PATH)
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
