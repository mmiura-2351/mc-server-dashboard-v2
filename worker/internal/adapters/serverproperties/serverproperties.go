// Package serverproperties reads a server's server.properties out of its working
// set for the adapters that need values from it: the RCON credentials (rcon), the
// container driver's published ports (containerdriver) and the tunnel's game port
// (tunnel). It returns the file's bytes; parsing stays with
// internal/javaproperties, in the charset each caller needs.
//
// The file sits in a working set the server's own code — which the operator does
// not control — has bind-mounted and can rewrite at any moment, and under the
// shipped topology the Worker reading it is root (issue #3335). Read by path, a
// server.properties replaced by a symlink to another server's file had the Worker
// authenticate to this server's RCON with the other server's password; one
// pointing at /dev/zero was read without limit; a FIFO blocked the caller. So,
// like the snapshot walk (datatransfer) and the install log (containerdriver),
// the read never resolves a path below the working-set root: the root is opened,
// the file is opened relative to that descriptor with O_NOFOLLOW (the refusal of
// a link and the open are one system call) and O_NONBLOCK (a FIFO cannot block
// it), what was opened is judged with fstat on the descriptor itself, and at most
// MaxSize bytes are read from it.
package serverproperties

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"golang.org/x/sys/unix"
)

// FileName is the name of the file in the working-set root.
const FileName = "server.properties"

// MaxSize is the largest server.properties Read returns; a larger file is
// refused. A vanilla server.properties is some sixty short keys, on the order of
// a kilobyte, and its longest values — a MOTD, a resource-pack prompt, legacy
// JSON generator-settings — add a few KiB at most, so 1 MiB is hundreds of times
// any real file while keeping the read a trivial allocation.
const MaxSize = 1 << 20

// openAt opens the entry name of the directory behind dirFd. It is a variable so
// a test can replace the file, or the working set, at the exact point between the
// two opens; production never reassigns it.
var openAt = func(dirFd int, name string, flags int) (int, error) {
	for {
		fd, err := unix.Openat(dirFd, name, flags, 0)
		if !errors.Is(err, unix.EINTR) {
			return fd, err
		}
	}
}

// Read returns the content of the server.properties in the root of workingDir.
//
// An absent file — or an absent working set — is an error satisfying
// errors.Is(err, fs.ErrNotExist), which a caller with defaults to fall back on
// can tell apart. Everything else that is not a regular file of at most MaxSize
// bytes is refused with a different error and no bytes: a symlink (dangling or
// not, never followed), a FIFO, socket, device or directory (never read, never
// blocked on), a larger file, and a working set that is itself a symlink. No
// error carries any of the file's content.
func Read(workingDir string) ([]byte, error) {
	path := filepath.Join(workingDir, FileName)

	// The root is the one name resolved by path: it is the Worker's own entry in
	// the scratch root, which the server (confined to what is beneath it) cannot
	// replace.
	dirFd, err := unix.Open(workingDir, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: path, Err: err}
	}
	defer func() { _ = unix.Close(dirFd) }()

	fd, err := openAt(dirFd, FileName, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_NOCTTY|unix.O_CLOEXEC)
	if errors.Is(err, unix.ELOOP) {
		return nil, fmt.Errorf("serverproperties: %s is a symbolic link; refusing to follow it", path)
	}
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: path, Err: err}
	}
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		_ = unix.Close(fd)
		return nil, &os.PathError{Op: "stat", Path: path, Err: err}
	}
	if st.Mode&unix.S_IFMT != unix.S_IFREG {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("serverproperties: %s is not a regular file; refusing to read it", path)
	}
	// The descriptor keeps its O_NONBLOCK, which has no effect on a regular file.
	f := os.NewFile(uintptr(fd), path)
	defer func() { _ = f.Close() }()

	// One byte past the cap tells an oversized file from one exactly at it, however
	// large the file is or grows meanwhile.
	data, err := io.ReadAll(io.LimitReader(f, MaxSize+1))
	if err != nil {
		return nil, &os.PathError{Op: "read", Path: path, Err: err}
	}
	if len(data) > MaxSize {
		return nil, fmt.Errorf("serverproperties: %s is larger than %d bytes; refusing to read it", path, MaxSize)
	}
	return data, nil
}
