// Package serverproperties reads a server's server.properties out of its working
// set for the adapters that need values from it: the RCON credentials (rcon), the
// container driver's published ports (containerdriver) and the tunnel's game port
// (tunnel). It returns the file's bytes; parsing stays with
// internal/javaproperties, in the charset each caller needs. RCONPort resolves
// the one value two of them must agree on, rcon.port.
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
	"os"
	"path/filepath"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/nofollow"
)

// FileName is the name of the file in the working-set root.
const FileName = "server.properties"

// MaxSize is the largest server.properties Read returns; a larger file is
// refused. It is the cap on a file edit through the dashboard
// (instancemanager.MaxFileBytes, 4 MiB) and must never be below it: a
// server.properties the Worker itself accepted as an edit must not then be
// refused for its size when the server is started. An adapter cannot import the
// application package that owns that constant, so the value is repeated here and
// the relationship is pinned by a test on the instancemanager side
// (TestServerPropertiesReaderAcceptsAnythingAnEditCanWrite). A real
// server.properties is some sixty short keys, on the order of a kilobyte, so the
// cap is far above any legitimate file while keeping the read a bounded, small
// allocation.
const MaxSize = 4 << 20

// readFileAt reads the entry name of the directory behind dirFd. It is a variable
// so a test can replace the file, or the working set, at the exact point between
// the two opens; production never reassigns it.
var readFileAt = nofollow.ReadFileAt

// Read returns the content of the server.properties in the root of workingDir.
//
// An absent file — or an absent working set — is an error satisfying
// errors.Is(err, fs.ErrNotExist), which a caller with defaults to fall back on
// can tell apart. Everything else that is not a regular file of at most MaxSize
// bytes is refused with a different error and no bytes: a symlink (dangling or
// not, never followed), a FIFO, socket, device or directory (never read, never
// blocked on), a larger file, and a working set that is itself a symlink. No
// error carries any of the file's content.
//
// A refusal of the file itself — everything above but the symlinked working set,
// which is the Worker's own entry and not the server's to replace — also
// satisfies errors.Is(err, execution.ErrServerPropertiesUnreadable), so the
// operation it fails can tell the user why (issue #3347).
func Read(workingDir string) ([]byte, error) {
	path := filepath.Join(workingDir, FileName)

	// The root is the one name resolved by path: it is the Worker's own entry in
	// the scratch root, which the server (confined to what is beneath it) cannot
	// replace.
	dirFd, err := nofollow.OpenDir(workingDir)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: path, Err: err}
	}
	defer func() { _ = unix.Close(dirFd) }()

	data, err := readFileAt(dirFd, FileName, MaxSize)
	if errors.Is(err, unix.ENXIO) {
		// A socket cannot even be opened; it is refused like any other special file.
		err = nofollow.ErrNotRegular
	}
	switch {
	case errors.Is(err, nofollow.ErrSymlink), errors.Is(err, nofollow.ErrNotRegular):
		return nil, refusal{fmt.Errorf("serverproperties: %s %w", path, err)}
	case errors.Is(err, nofollow.ErrTooLarge):
		return nil, refusal{fmt.Errorf("serverproperties: %s is larger than %d bytes; refusing to read it", path, MaxSize)}
	case err != nil:
		// The reader names the stage that failed and the bare file name; report the
		// same stage under the full path.
		var pathErr *os.PathError
		if errors.As(err, &pathErr) {
			return nil, &os.PathError{Op: pathErr.Op, Path: path, Err: pathErr.Err}
		}
		return nil, &os.PathError{Op: "read", Path: path, Err: err}
	}
	return data, nil
}

// refusal is Read's error for a file it refused. Its message is the reader's own,
// and it is execution.ErrServerPropertiesUnreadable to errors.Is besides
// whatever the wrapped error is.
type refusal struct{ err error }

func (r refusal) Error() string { return r.err.Error() }

func (r refusal) Unwrap() error { return r.err }

func (refusal) Is(target error) bool { return target == execution.ErrServerPropertiesUnreadable }
