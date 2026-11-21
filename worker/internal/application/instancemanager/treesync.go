package instancemanager

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/nofollow"
)

// syncWorkingSet makes the working set at workingDir durable: when it returns nil,
// every file and directory the tree held when it was called is on the disk (issue
// #3999). The running-id snapshot calls it on the tree it has just packed, before
// the generation marker can name that tree. The server writes its world through the
// page cache, and a marker made durable over an unsynced tree can outlive the tree on
// a power loss; the skip-hydrate gate then boots the older world, and that world's
// next snapshot publishes it over newer data.
//
// It is one syncfs(2) of the filesystem the working dir lies on: one system call,
// whatever the size of the tree, and a single device flush. That filesystem holds
// whatever else shares it as well, so the call also flushes the snapshot's spool
// beside the working dir and any other dirty data there.
//
// Where syncfs is unavailable it walks the tree instead and fsyncs each regular file
// and directory (fsyncTree). An older kernel answers ENOSYS, and a seccomp profile
// that does not know syncfs answers EPERM, which syncfs has no use for itself. Any
// other syncfs error is returned as it is: a walk then would report a failed sync as
// a durable tree.
func syncWorkingSet(ctx context.Context, workingDir string) error {
	dirFd, err := nofollow.OpenDir(workingDir)
	if err != nil {
		return &os.PathError{Op: "open", Path: workingDir, Err: err}
	}
	if err := syncfs(dirFd); !errors.Is(err, unix.ENOSYS) && !errors.Is(err, unix.EPERM) {
		_ = unix.Close(dirFd)
		if err != nil {
			return &os.PathError{Op: "syncfs", Path: workingDir, Err: err}
		}
		return nil
	}
	return fsyncTree(ctx, dirFd, workingDir, 0)
}

// syncfs is the one syncfs call syncWorkingSet makes, and fsyncFd the fsync the
// fallback walk makes of each entry. They are variables so a test can answer syncfs
// as an older kernel or a seccomp profile does, and see what the walk syncs;
// production never reassigns them.
var (
	syncfs  = unix.Syncfs
	fsyncFd = unix.Fsync
)

// maxSyncDepth bounds how deep the fallback walk descends. The walk holds one
// descriptor per level, and the tree's shape is the server's to choose, so a tree
// deliberately deepened since the pack must not run out the Worker's descriptor
// table, which every other server on the Worker shares. It is the depth the pack
// itself takes (datatransfer's maxPackDepth), so a tree that just packed is walked.
const maxSyncDepth = 128

// The fallback walk opens entries with these flags only, which are the pack's: a
// symlink is refused rather than followed, and a FIFO swapped in for a file cannot
// block the walk.
const (
	syncOpenDirFlags  = unix.O_RDONLY | unix.O_DIRECTORY | unix.O_NOFOLLOW | unix.O_CLOEXEC
	syncOpenFileFlags = unix.O_RDONLY | unix.O_NOFOLLOW | unix.O_NONBLOCK | unix.O_NOCTTY | unix.O_CLOEXEC
)

// fsyncTree fsyncs every regular file and directory beneath the directory behind
// dirFd, and then that directory, so a name is synced after what it names. It takes
// ownership of dirFd. rel names the directory in errors and is never resolved; depth
// is its level below the working dir. Unlike the hydrate's walk, which syncs
// directories only because the hydrate fsynced each file as it wrote it, this one
// syncs file contents too: the server wrote them. Symlinks and special files are
// left alone, and an entry that vanishes or changes type mid-walk is skipped, as the
// pack skips it. The walk stops once ctx is done.
func fsyncTree(ctx context.Context, dirFd int, rel string, depth int) error {
	dir := os.NewFile(uintptr(dirFd), rel)
	defer func() { _ = dir.Close() }()
	entries, err := dir.ReadDir(-1)
	if err != nil {
		return &os.PathError{Op: "readdir", Path: rel, Err: err}
	}
	for _, e := range entries {
		if err := ctx.Err(); err != nil {
			return err
		}
		entryRel := filepath.Join(rel, e.Name())
		switch e.Type() {
		case fs.ModeDir:
			if depth >= maxSyncDepth {
				return fmt.Errorf("instancemanager: working set is nested deeper than %d directories at %q", maxSyncDepth, entryRel)
			}
			fd, err := nofollow.OpenAt(dirFd, e.Name(), syncOpenDirFlags, 0)
			if errors.Is(err, unix.ENOENT) || errors.Is(err, unix.ENOTDIR) || errors.Is(err, unix.ELOOP) {
				continue
			}
			if err != nil {
				return &os.PathError{Op: "open", Path: entryRel, Err: err}
			}
			if err := fsyncTree(ctx, fd, entryRel, depth+1); err != nil {
				return err
			}
		case 0:
			if err := fsyncRegular(dirFd, e.Name(), entryRel); err != nil {
				return err
			}
		}
	}
	if err := fsyncFd(dirFd); err != nil {
		return &os.PathError{Op: "fsync", Path: rel, Err: err}
	}
	return nil
}

// fsyncRegular fsyncs the regular file name of the directory behind dirFd. An entry
// that is gone, or is no longer a regular file, is skipped.
func fsyncRegular(dirFd int, name, rel string) error {
	fd, err := nofollow.OpenAt(dirFd, name, syncOpenFileFlags, 0)
	if errors.Is(err, unix.ENOENT) || errors.Is(err, unix.ELOOP) {
		return nil
	}
	if err != nil {
		return &os.PathError{Op: "open", Path: rel, Err: err}
	}
	defer func() { _ = unix.Close(fd) }()
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		return &os.PathError{Op: "stat", Path: rel, Err: err}
	}
	if st.Mode&unix.S_IFMT != unix.S_IFREG {
		return nil
	}
	if err := fsyncFd(fd); err != nil {
		return &os.PathError{Op: "fsync", Path: rel, Err: err}
	}
	return nil
}
