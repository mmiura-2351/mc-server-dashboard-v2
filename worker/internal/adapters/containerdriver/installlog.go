package containerdriver

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// This file opens logs/forge-install.log for the install supervisor.
//
// The Worker writes that log while the install container is running, into a
// working set the installer — code the operator does not control — is writing
// too, and under the shipped topology the Worker is root. Opening it by path
// would follow whatever the installer planted at logs/ or at the log itself and
// turn the capture into a root write outside the working set (issue #3298). Like
// the hand-over walk (handover.go), the open therefore never resolves a path: it
// descends by descriptor from the working-set root, each component opened
// relative to its parent with O_NOFOLLOW, so the refusal and the open are one
// operation and a link swapped in at any moment is refused, not followed.
//
// For the same reason it never removes anything. The installer owns the
// directories involved and can replace any entry in them at any moment, and an
// entry can only be removed by name: whatever check came first, the removal
// could land on what the installer put there since. So instead of creating an
// entry and taking it back when it cannot be handed to the working set's owner,
// the open establishes that before it gives anything a name (issue #3338).

// The descriptor-only calls of this file that a test replaces.
var (
	// installLogDirOwner reports the uid:gid owning the directory behind dirFd,
	// and installLogFchown re-owns the entry behind fd; replaced to give the
	// working set an owner other than the test process (only root can give a
	// file away).
	installLogDirOwner = func(dirFd int) (uid, gid int, err error) {
		var st unix.Stat_t
		if err := unix.Fstat(dirFd, &st); err != nil {
			return 0, 0, err
		}
		return int(st.Uid), int(st.Gid), nil
	}
	installLogFchown = unix.Fchown
	// installLogOpenUnnamed creates a file with no name in the directory behind
	// dirFd; replaced to stand in for a filesystem that has no such files.
	installLogOpenUnnamed = func(dirFd int) (int, error) {
		return unix.Openat(dirFd, ".", unix.O_TMPFILE|unix.O_WRONLY|unix.O_APPEND|unix.O_CLOEXEC, 0o640)
	}
	// installLogAfterMkdir runs between the creation of logs/ and its open, so a
	// test can replace the directory at that exact point; production never
	// reassigns it.
	installLogAfterMkdir = func() {}
)

const installLogDirFlags = unix.O_RDONLY | unix.O_DIRECTORY | unix.O_NOFOLLOW | unix.O_CLOEXEC

// openInstallLog opens execution.ForgeInstallLogRelpath beneath workingDir for
// appending, creating the log and its directory when absent.
//
// It refuses — with nothing created or written outside the working set — a
// symlink at any component (dangling or not), a logs/ that is not a directory,
// and a log that is not a regular file with exactly one name: a FIFO or device
// would hand the output to whoever planted it, and a hard link would append to a
// file kept under another name.
//
// What it creates takes the owner of the directory it is created in, which is the
// run-as user once the driver has handed the working set over (issue #2600):
// left as the Worker's, a logs/ made here would be closed to the unprivileged
// server that writes its own logs there. When that owner cannot be set (a root
// Worker without CAP_CHOWN) the open is refused with nothing created
// (createInstallLogDir, createInstallLog). An entry that already existed is never
// re-owned, and nothing is ever removed.
func openInstallLog(workingDir string) (*os.File, error) {
	components := strings.Split(execution.ForgeInstallLogRelpath, "/")
	dirs, leaf := components[:len(components)-1], components[len(components)-1]

	dirFd, err := unix.Open(workingDir, installLogDirFlags, 0)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: workingDir, Err: err}
	}
	defer func() { _ = unix.Close(dirFd) }()

	for _, name := range dirs {
		next, err := unix.Openat(dirFd, name, installLogDirFlags, 0)
		if errors.Is(err, unix.ENOENT) {
			next, err = createInstallLogDir(dirFd, name)
		} else if err != nil {
			err = &os.PathError{Op: "open", Path: name, Err: err}
		}
		if err != nil {
			return nil, err
		}
		_ = unix.Close(dirFd)
		dirFd = next
	}

	fd, err := openExistingInstallLog(dirFd, leaf)
	if errors.Is(err, unix.ENOENT) {
		fd, err = createInstallLog(dirFd, leaf)
		if errors.Is(err, unix.EEXIST) {
			// The installer created one in the meantime; it is judged like any other.
			fd, err = openExistingInstallLog(dirFd, leaf)
		}
	}
	if err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(fd), leaf), nil
}

// createInstallLogDir creates the directory name in the directory behind dirFd
// and returns it open, owned like its parent.
//
// A directory cannot exist without a name, so whether one made here could be
// handed over is found out first, on a file that has none: if that file cannot
// be given the parent's owner, no directory is created.
//
// What is opened after the mkdir need not be the directory just made — the
// installer may have put another in its place. That one is used all the same,
// and given the parent's owner all the same: it was reached from the working-set
// root without following a link, so it is a directory of the working set, which
// has no name anywhere else, and the working set's owner is what the hand-over
// gives every such directory anyway.
func createInstallLogDir(dirFd int, name string) (int, error) {
	probe, err := installLogOpenUnnamed(dirFd)
	if err == nil {
		err = inheritInstallLogOwner(dirFd, probe)
		_ = unix.Close(probe)
		if err != nil {
			return -1, &os.PathError{Op: "chown", Path: name, Err: err}
		}
	} else if !unnamedFilesUnsupported(err) {
		return -1, &os.PathError{Op: "open", Path: name, Err: err}
	}

	mkErr := unix.Mkdirat(dirFd, name, 0o750)
	if mkErr != nil && !errors.Is(mkErr, unix.EEXIST) {
		return -1, &os.PathError{Op: "mkdir", Path: name, Err: mkErr}
	}
	installLogAfterMkdir()
	fd, err := unix.Openat(dirFd, name, installLogDirFlags, 0)
	if err != nil {
		return -1, &os.PathError{Op: "open", Path: name, Err: err}
	}
	if mkErr == nil {
		// Reached only where the filesystem has no unnamed files to find this out
		// on beforehand (or the answer changed since).
		if err := inheritInstallLogOwner(dirFd, fd); err != nil {
			_ = unix.Close(fd)
			return -1, &os.PathError{Op: "chown", Path: name, Err: errors.Join(err, errInstallLogEntryLeft)}
		}
	}
	return fd, nil
}

// errInstallLogEntryLeft marks a refusal that left an entry of the Worker's own
// behind: it could not be handed over after it had a name, and is not removed
// (see the top of this file).
var errInstallLogEntryLeft = errors.New("the entry was created and is left in place, owned by the worker")

// createInstallLog creates the log leaf in the directory behind dirFd and
// returns it open for appending, owned like that directory. An entry already at
// leaf fails it with EEXIST.
//
// The file is created without a name, given its owner, and only then linked in
// at leaf, so a file that cannot be handed over never appears in the working
// set and there is nothing to take back. The link cannot replace an entry and
// does not follow one.
func createInstallLog(dirFd int, leaf string) (int, error) {
	fd, err := installLogOpenUnnamed(dirFd)
	if unnamedFilesUnsupported(err) {
		return createNamedInstallLog(dirFd, leaf)
	}
	if err != nil {
		return -1, &os.PathError{Op: "open", Path: leaf, Err: err}
	}
	if err := inheritInstallLogOwner(dirFd, fd); err != nil {
		_ = unix.Close(fd)
		return -1, &os.PathError{Op: "chown", Path: leaf, Err: err}
	}
	// The /proc form needs no capability; naming the descriptor directly
	// (AT_EMPTY_PATH) would need CAP_DAC_READ_SEARCH.
	err = unix.Linkat(unix.AT_FDCWD, fmt.Sprintf("/proc/self/fd/%d", fd), dirFd, leaf, unix.AT_SYMLINK_FOLLOW)
	if err != nil {
		_ = unix.Close(fd)
		return -1, &os.PathError{Op: "link", Path: leaf, Err: err}
	}
	return fd, nil
}

// unnamedFilesUnsupported reports whether err is a filesystem (EOPNOTSUPP) or a
// kernel from before O_TMPFILE (EISDIR) declining to create a file with no name.
func unnamedFilesUnsupported(err error) bool {
	return errors.Is(err, unix.EOPNOTSUPP) || errors.Is(err, unix.EISDIR)
}

// createNamedInstallLog is createInstallLog on a filesystem without unnamed
// files: the log has its name from the start. One that then cannot be handed
// over is left where it is, and reported so.
func createNamedInstallLog(dirFd int, leaf string) (int, error) {
	// O_EXCL never follows a link at the leaf, so a successful create is a new
	// regular file in the directory just opened.
	fd, err := unix.Openat(dirFd, leaf,
		unix.O_WRONLY|unix.O_APPEND|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0o640)
	if err != nil {
		return -1, &os.PathError{Op: "open", Path: leaf, Err: err}
	}
	if err := inheritInstallLogOwner(dirFd, fd); err != nil {
		_ = unix.Close(fd)
		return -1, &os.PathError{Op: "chown", Path: leaf, Err: errors.Join(err, errInstallLogEntryLeft)}
	}
	return fd, nil
}

// openExistingInstallLog opens the log leaf of the directory behind dirFd for
// appending, refusing anything but a regular file with a single name.
func openExistingInstallLog(dirFd int, leaf string) (int, error) {
	// O_NONBLOCK keeps a planted FIFO from blocking the open; what was opened is
	// then judged on the descriptor itself.
	fd, err := unix.Openat(dirFd, leaf, unix.O_WRONLY|unix.O_APPEND|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	if err != nil {
		return -1, &os.PathError{Op: "open", Path: leaf, Err: err}
	}
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		_ = unix.Close(fd)
		return -1, &os.PathError{Op: "stat", Path: leaf, Err: err}
	}
	if st.Mode&unix.S_IFMT != unix.S_IFREG || st.Nlink != 1 {
		_ = unix.Close(fd)
		return -1, fmt.Errorf("%s is not a regular file with a single name", leaf)
	}
	if err := unix.SetNonblock(fd, false); err != nil {
		_ = unix.Close(fd)
		return -1, &os.PathError{Op: "fcntl", Path: leaf, Err: err}
	}
	return fd, nil
}

// inheritInstallLogOwner gives the entry just created behind fd the owner of the
// directory it was created in (parentFd).
func inheritInstallLogOwner(parentFd, fd int) error {
	uid, gid, err := installLogDirOwner(parentFd)
	if err != nil {
		return err
	}
	return installLogFchown(fd, uid, gid)
}
