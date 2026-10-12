// Package nofollow reads one named file out of a directory another user can
// write, without ever resolving a path below that directory.
//
// A server's working set is bind-mounted into a container running code the
// operator does not control, and under the shipped topology the Worker reading it
// is root. Read by path, a file the server replaced by a symlink is followed out
// of the working set, one pointing at /dev/zero is read without limit, and a FIFO
// blocks the caller. So the directory is opened once (OpenDir), the file is
// opened relative to that descriptor with O_NOFOLLOW (the refusal of a link and
// the open are one system call) and O_NONBLOCK (a FIFO cannot block it), what was
// opened is judged with fstat on the descriptor itself, and at most a
// caller-chosen number of bytes is read from it (ReadFileAt).
//
// It is a layer-neutral helper like internal/javaproperties, so that the
// server.properties reader (an adapter) and the generation-marker reader (the
// application package) share one implementation without either importing the
// other's layer.
package nofollow

import (
	"errors"
	"io"
	"io/fs"
	"os"

	"golang.org/x/sys/unix"
)

// The refusals of ReadFileAt. None of them is fs.ErrNotExist: a caller that
// answers an absent file with a default must not answer a planted one that way.
var (
	// ErrSymlink reports that the name is a symbolic link, dangling or not.
	ErrSymlink = errors.New("is a symbolic link; refusing to follow it")
	// ErrNotRegular reports a FIFO, socket, device or directory.
	ErrNotRegular = errors.New("is not a regular file; refusing to read it")
	// ErrTooLarge reports a file larger than the caller's cap.
	ErrTooLarge = errors.New("is larger than the read cap; refusing to read it")
)

// OpenDir opens the directory at path and returns its descriptor, which the
// caller closes. The path is the one name resolved as a path, so it must be one
// the other user cannot replace (a working-set root is the Worker's own entry in
// the scratch root); a symlink at its final component is refused all the same.
func OpenDir(path string) (int, error) {
	return OpenAt(unix.AT_FDCWD, path, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
}

// OpenAt is unix.Openat retried on EINTR.
func OpenAt(dirFd int, name string, flags int, mode uint32) (int, error) {
	for {
		fd, err := unix.Openat(dirFd, name, flags, mode)
		if !errors.Is(err, unix.EINTR) {
			return fd, err
		}
	}
}

// ReadFileAt returns the content of the entry name of the directory behind dirFd.
//
// An absent entry is an error satisfying errors.Is(err, fs.ErrNotExist).
// Everything else that is not a regular file of at most maxSize bytes is refused
// with ErrSymlink, ErrNotRegular or ErrTooLarge and no bytes; nothing is ever
// read from, or blocked on, what is refused. A system-call failure — the absent
// entry included — is a *fs.PathError naming the stage that failed ("open",
// "stat" or "read") and the entry, so a caller can report it under its own path
// without losing which operation it was.
func ReadFileAt(dirFd int, name string, maxSize int) ([]byte, error) {
	fd, err := OpenAt(dirFd, name, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_NOCTTY|unix.O_CLOEXEC, 0)
	if errors.Is(err, unix.ELOOP) {
		return nil, ErrSymlink
	}
	if err != nil {
		return nil, &fs.PathError{Op: "open", Path: name, Err: err}
	}
	// The descriptor keeps its O_NONBLOCK, which has no effect on a regular file.
	f := os.NewFile(uintptr(fd), name)
	defer func() { _ = f.Close() }()

	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		return nil, &fs.PathError{Op: "stat", Path: name, Err: err}
	}
	if st.Mode&unix.S_IFMT != unix.S_IFREG {
		return nil, ErrNotRegular
	}
	// One byte past the cap tells an oversized file from one exactly at it, however
	// large the file is or grows meanwhile.
	data, err := io.ReadAll(io.LimitReader(f, int64(maxSize)+1))
	if err != nil {
		// Already a *fs.PathError{Op: "read", Path: name}, from the os.File.
		return nil, err
	}
	if len(data) > maxSize {
		return nil, ErrTooLarge
	}
	return data, nil
}
