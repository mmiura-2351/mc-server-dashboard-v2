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

// installLogDirOwner reports the uid:gid owning the directory behind dirFd, and
// installLogFchown re-owns the entry behind fd. Both act on descriptors only;
// they are variables so a test can give the working set an owner other than the
// test process (only root can give a file away).
var (
	installLogDirOwner = func(dirFd int) (uid, gid int, err error) {
		var st unix.Stat_t
		if err := unix.Fstat(dirFd, &st); err != nil {
			return 0, 0, err
		}
		return int(st.Uid), int(st.Gid), nil
	}
	installLogFchown = unix.Fchown
)

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
// server that writes its own logs there. An entry that already existed is never
// re-owned.
func openInstallLog(workingDir string) (*os.File, error) {
	components := strings.Split(execution.ForgeInstallLogRelpath, "/")
	dirs, leaf := components[:len(components)-1], components[len(components)-1]

	dirFd, err := unix.Open(workingDir, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: workingDir, Err: err}
	}
	defer func() { _ = unix.Close(dirFd) }()

	for _, name := range dirs {
		mkErr := unix.Mkdirat(dirFd, name, 0o750)
		if mkErr != nil && !errors.Is(mkErr, unix.EEXIST) {
			return nil, &os.PathError{Op: "mkdir", Path: name, Err: mkErr}
		}
		next, err := unix.Openat(dirFd, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if err != nil {
			return nil, &os.PathError{Op: "open", Path: name, Err: err}
		}
		if mkErr == nil {
			if err := inheritInstallLogOwner(dirFd, next); err != nil {
				_ = unix.Close(next)
				return nil, &os.PathError{Op: "chown", Path: name, Err: err}
			}
		}
		_ = unix.Close(dirFd)
		dirFd = next
	}

	// O_EXCL never follows a link at the leaf, so a successful create is a new
	// regular file in the directory just opened.
	fd, err := unix.Openat(dirFd, leaf,
		unix.O_WRONLY|unix.O_APPEND|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0o640)
	if err == nil {
		if err := inheritInstallLogOwner(dirFd, fd); err != nil {
			_ = unix.Close(fd)
			return nil, &os.PathError{Op: "chown", Path: leaf, Err: err}
		}
		return os.NewFile(uintptr(fd), leaf), nil
	}
	if !errors.Is(err, unix.EEXIST) {
		return nil, &os.PathError{Op: "open", Path: leaf, Err: err}
	}

	// Something is already there. O_NONBLOCK keeps a planted FIFO from blocking
	// the open; what was opened is then judged on the descriptor itself.
	fd, err = unix.Openat(dirFd, leaf, unix.O_WRONLY|unix.O_APPEND|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: leaf, Err: err}
	}
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		_ = unix.Close(fd)
		return nil, &os.PathError{Op: "stat", Path: leaf, Err: err}
	}
	if st.Mode&unix.S_IFMT != unix.S_IFREG || st.Nlink != 1 {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("%s is not a regular file with a single name", leaf)
	}
	if err := unix.SetNonblock(fd, false); err != nil {
		_ = unix.Close(fd)
		return nil, &os.PathError{Op: "fcntl", Path: leaf, Err: err}
	}
	return os.NewFile(uintptr(fd), leaf), nil
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
