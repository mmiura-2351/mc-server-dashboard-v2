package serverproperties

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// These tests pin the reader against what a server can plant at
// server.properties (issue #3335): the file sits in a working set the server's
// own code writes, and the Worker reading it is root in the shipped topology.

// outsidePassword is the credential of a file outside the working set; no read
// may ever return it or mention it.
const outsidePassword = "OUTSIDE-rcon-password"

// readWithin runs Read under a deadline: a reader that blocks (a FIFO without a
// writer) must fail the test, not hang the suite.
func readWithin(t *testing.T, workingDir string) ([]byte, error) {
	t.Helper()
	type result struct {
		data []byte
		err  error
	}
	done := make(chan result, 1)
	go func() {
		data, err := Read(workingDir)
		done <- result{data, err}
	}()
	select {
	case r := <-done:
		return r.data, r.err
	case <-time.After(5 * time.Second):
		t.Fatal("Read blocked")
		return nil, nil
	}
}

// assertRefused fails unless the read was refused: an error that is not "the file
// is absent" (which callers answer with the Minecraft defaults), no bytes, and
// nothing of the outside file in the message.
func assertRefused(t *testing.T, data []byte, err error) {
	t.Helper()
	if err == nil {
		t.Fatalf("Read = %q, nil; want a refusal", data)
	}
	if errors.Is(err, fs.ErrNotExist) {
		t.Errorf("Read error = %v; a refusal must not read as an absent file", err)
	}
	if data != nil {
		t.Errorf("Read returned %d bytes alongside the refusal", len(data))
	}
	if strings.Contains(err.Error(), outsidePassword) {
		t.Errorf("Read error leaks the outside file's content: %v", err)
	}
	if !strings.Contains(err.Error(), FileName) {
		t.Errorf("Read error = %v, want it to name %s", err, FileName)
	}
}

// outsideFile writes a server.properties carrying outsidePassword outside any
// working set and returns its path.
func outsideFile(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "other-server.properties")
	if err := os.WriteFile(path, []byte("enable-rcon=true\nrcon.password="+outsidePassword+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestReadReturnsARegularFile(t *testing.T) {
	dir := t.TempDir()
	const body = "server-port=25599\nrcon.password=inside\n"
	if err := os.WriteFile(filepath.Join(dir, FileName), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	data, err := readWithin(t, dir)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if string(data) != body {
		t.Errorf("Read = %q, want %q", data, body)
	}
}

func TestReadReportsAnAbsentFileAsNotExist(t *testing.T) {
	for name, dir := range map[string]string{
		"file absent":        t.TempDir(),
		"working dir absent": filepath.Join(t.TempDir(), "absent"),
	} {
		t.Run(name, func(t *testing.T) {
			data, err := readWithin(t, dir)
			if !errors.Is(err, fs.ErrNotExist) {
				t.Fatalf("Read = %q, %v; want fs.ErrNotExist", data, err)
			}
		})
	}
}

func TestReadRefusesASymlink(t *testing.T) {
	for name, target := range map[string]string{
		"to a file outside the working set": outsideFile(t),
		"dangling":                          filepath.Join(t.TempDir(), "nothing-here"),
		"to an endless device":              "/dev/zero",
	} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			if err := os.Symlink(target, filepath.Join(dir, FileName)); err != nil {
				t.Fatal(err)
			}
			data, err := readWithin(t, dir)
			assertRefused(t, data, err)
		})
	}
}

func TestReadRefusesAFIFOWithoutBlocking(t *testing.T) {
	dir := t.TempDir()
	if err := unix.Mkfifo(filepath.Join(dir, FileName), 0o600); err != nil {
		t.Fatal(err)
	}
	data, err := readWithin(t, dir)
	assertRefused(t, data, err)
}

func TestReadRefusesADirectory(t *testing.T) {
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, FileName), 0o700); err != nil {
		t.Fatal(err)
	}
	data, err := readWithin(t, dir)
	assertRefused(t, data, err)
}

func TestReadBoundsTheFileSize(t *testing.T) {
	write := func(t *testing.T, size int) string {
		t.Helper()
		dir := t.TempDir()
		if err := os.WriteFile(filepath.Join(dir, FileName), make([]byte, size), 0o600); err != nil {
			t.Fatal(err)
		}
		return dir
	}

	t.Run("at the cap", func(t *testing.T) {
		data, err := readWithin(t, write(t, MaxSize))
		if err != nil || len(data) != MaxSize {
			t.Fatalf("Read = %d bytes, %v; want %d bytes", len(data), err, MaxSize)
		}
	})
	t.Run("one byte over", func(t *testing.T) {
		data, err := readWithin(t, write(t, MaxSize+1))
		assertRefused(t, data, err)
	})
	t.Run("sparse and huge", func(t *testing.T) {
		dir := write(t, 0)
		if err := os.Truncate(filepath.Join(dir, FileName), 1<<40); err != nil {
			t.Skipf("the filesystem cannot hold a sparse 1 TiB file: %v", err)
		}
		data, err := readWithin(t, dir)
		assertRefused(t, data, err)
	})
}

// beforeOpeningTheFile runs swap once, after the reader has opened the working
// set and immediately before it opens server.properties — the window a running
// server has to replace either.
func beforeOpeningTheFile(t *testing.T, swap func()) {
	t.Helper()
	orig := readFileAt
	t.Cleanup(func() { readFileAt = orig })
	done := false
	readFileAt = func(dirFd int, name string, maxSize int) ([]byte, error) {
		if !done {
			done = true
			swap()
		}
		return orig(dirFd, name, maxSize)
	}
}

func TestReadRefusesAFileSwappedForASymlink(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, FileName)
	if err := os.WriteFile(path, []byte("rcon.password=inside\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	outside := outsideFile(t)
	beforeOpeningTheFile(t, func() {
		if err := os.Remove(path); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(outside, path); err != nil {
			t.Fatal(err)
		}
	})
	data, err := readWithin(t, dir)
	assertRefused(t, data, err)
}

// The working set replaced by a link to another directory after the reader opened
// it changes nothing: the file is opened relative to the directory that was
// opened, not by resolving the path again.
func TestReadStaysInTheWorkingSetItOpened(t *testing.T) {
	base := t.TempDir()
	work, other := filepath.Join(base, "work"), filepath.Join(base, "other")
	for dir, password := range map[string]string{work: "inside", other: outsidePassword} {
		if err := os.Mkdir(dir, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, FileName), []byte("rcon.password="+password+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	beforeOpeningTheFile(t, func() {
		if err := os.Rename(work, work+".moved"); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(other, work); err != nil {
			t.Fatal(err)
		}
	})
	data, err := readWithin(t, work)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if string(data) != "rcon.password=inside\n" {
		t.Errorf("Read = %q, want the file of the working set that was opened", data)
	}
}

func TestReadRefusesASymlinkedWorkingSet(t *testing.T) {
	base := t.TempDir()
	other := filepath.Dir(outsideFile(t))
	if err := os.Rename(filepath.Join(other, "other-server.properties"), filepath.Join(other, FileName)); err != nil {
		t.Fatal(err)
	}
	work := filepath.Join(base, "work")
	if err := os.Symlink(other, work); err != nil {
		t.Fatal(err)
	}
	data, err := readWithin(t, work)
	if err == nil || errors.Is(err, fs.ErrNotExist) || data != nil {
		t.Fatalf("Read = %q, %v; want a refusal", data, err)
	}
}

// The messages are what an operator reads in a failed start, tunnel dial or RCON
// open; they name the operation that failed and the full path. Pinned so that the
// reader behind Read (internal/nofollow) cannot change them unnoticed.
func TestReadErrorMessages(t *testing.T) {
	for name, tc := range map[string]struct {
		plant func(t *testing.T, path string)
		want  string // %s is the file's path
	}{
		"missing file": {func(*testing.T, string) {}, "open %s: no such file or directory"},
		"symlink": {func(t *testing.T, path string) {
			if err := os.Symlink(outsideFile(t), path); err != nil {
				t.Fatal(err)
			}
		}, "serverproperties: %s is a symbolic link; refusing to follow it"},
		"non-regular file": {func(t *testing.T, path string) {
			if err := unix.Mkfifo(path, 0o600); err != nil {
				t.Fatal(err)
			}
		}, "serverproperties: %s is not a regular file; refusing to read it"},
		"over the cap": {func(t *testing.T, path string) {
			if err := os.WriteFile(path, make([]byte, MaxSize+1), 0o600); err != nil {
				t.Fatal(err)
			}
		}, "serverproperties: %s is larger than 4194304 bytes; refusing to read it"},
	} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, FileName)
			tc.plant(t, path)
			_, err := readWithin(t, dir)
			if want := fmt.Sprintf(tc.want, path); err == nil || err.Error() != want {
				t.Fatalf("Read error = %v, want %q", err, want)
			}
		})
	}
}

func TestReadLeavesNoDescriptorOpen(t *testing.T) {
	openFds := func() int {
		entries, err := os.ReadDir("/proc/self/fd")
		if err != nil {
			t.Skipf("cannot list descriptors: %v", err)
		}
		return len(entries)
	}
	regular, fifo, absent := t.TempDir(), t.TempDir(), t.TempDir()
	if err := os.WriteFile(filepath.Join(regular, FileName), []byte("a=b\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := unix.Mkfifo(filepath.Join(fifo, FileName), 0o600); err != nil {
		t.Fatal(err)
	}
	before := openFds()
	for _, dir := range []string{regular, fifo, absent} {
		_, _ = readWithin(t, dir)
	}
	if after := openFds(); after != before {
		t.Errorf("open descriptors: %d before, %d after", before, after)
	}
}

// Every refusal of the file itself is one condition for the callers that report
// it (issue #3347): a start, a restart's relaunch or a console command answers it
// with its own error code, so the user is told why instead of seeing a generic
// failure. An absent file is not that condition: callers with defaults take them.
func TestReadRefusalsAreServerPropertiesUnreadable(t *testing.T) {
	for name, plant := range map[string]func(t *testing.T, path string){
		"symlink": func(t *testing.T, path string) {
			if err := os.Symlink(outsideFile(t), path); err != nil {
				t.Fatal(err)
			}
		},
		"fifo": func(t *testing.T, path string) {
			if err := unix.Mkfifo(path, 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"socket": func(t *testing.T, path string) {
			fd, err := unix.Socket(unix.AF_UNIX, unix.SOCK_STREAM, 0)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = unix.Close(fd) })
			if err := unix.Bind(fd, &unix.SockaddrUnix{Name: path}); err != nil {
				t.Skipf("cannot bind a socket at %s: %v", path, err)
			}
		},
		"directory": func(t *testing.T, path string) {
			if err := os.Mkdir(path, 0o700); err != nil {
				t.Fatal(err)
			}
		},
		"over the cap": func(t *testing.T, path string) {
			if err := os.WriteFile(path, make([]byte, MaxSize+1), 0o600); err != nil {
				t.Fatal(err)
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			plant(t, filepath.Join(dir, FileName))
			data, err := readWithin(t, dir)
			assertRefused(t, data, err)
			if !errors.Is(err, execution.ErrServerPropertiesUnreadable) {
				t.Errorf("Read error = %v, want it to be execution.ErrServerPropertiesUnreadable", err)
			}
		})
	}

	t.Run("absent", func(t *testing.T) {
		_, err := readWithin(t, t.TempDir())
		if errors.Is(err, execution.ErrServerPropertiesUnreadable) {
			t.Errorf("Read error = %v; an absent file is not a refused one", err)
		}
	})
}
