package nofollow

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

const (
	fileName = "target"
	maxSize  = 64
)

// readWithin runs OpenDir + ReadFileAt under a deadline: a reader that blocks (a
// FIFO without a writer) must fail the test, not hang the suite.
func readWithin(t *testing.T, dir string) ([]byte, error) {
	t.Helper()
	type result struct {
		data []byte
		err  error
	}
	done := make(chan result, 1)
	go func() {
		dirFd, err := OpenDir(dir)
		if err != nil {
			done <- result{nil, err}
			return
		}
		defer func() { _ = unix.Close(dirFd) }()
		data, err := ReadFileAt(dirFd, fileName, maxSize)
		done <- result{data, err}
	}()
	select {
	case r := <-done:
		return r.data, r.err
	case <-time.After(5 * time.Second):
		t.Fatal("the read blocked")
		return nil, nil
	}
}

func TestReadFileAtReturnsARegularFileUpToTheCap(t *testing.T) {
	for _, size := range []int{0, 5, maxSize} {
		dir := t.TempDir()
		if err := os.WriteFile(filepath.Join(dir, fileName), make([]byte, size), 0o600); err != nil {
			t.Fatal(err)
		}
		data, err := readWithin(t, dir)
		if err != nil || len(data) != size {
			t.Errorf("size %d: read %d bytes, %v", size, len(data), err)
		}
	}
}

func TestAnAbsentFileOrDirectoryIsNotExist(t *testing.T) {
	for name, dir := range map[string]string{
		"file absent":      t.TempDir(),
		"directory absent": filepath.Join(t.TempDir(), "absent"),
	} {
		if data, err := readWithin(t, dir); !errors.Is(err, fs.ErrNotExist) {
			t.Errorf("%s: read = %q, %v; want fs.ErrNotExist", name, data, err)
		}
	}
}

func TestReadFileAtRefusesWhatIsNotABoundedRegularFile(t *testing.T) {
	outside := filepath.Join(t.TempDir(), "outside")
	if err := os.WriteFile(outside, []byte("outside"), 0o600); err != nil {
		t.Fatal(err)
	}
	symlink := func(target string) func(t *testing.T, path string) {
		return func(t *testing.T, path string) {
			if err := os.Symlink(target, path); err != nil {
				t.Fatal(err)
			}
		}
	}
	for name, tc := range map[string]struct {
		plant func(t *testing.T, path string)
		want  error
	}{
		"symlink to an outside file":   {symlink(outside), ErrSymlink},
		"dangling symlink":             {symlink(filepath.Join(t.TempDir(), "nothing")), ErrSymlink},
		"symlink to an endless device": {symlink("/dev/zero"), ErrSymlink},
		"fifo without a writer": {func(t *testing.T, path string) {
			if err := unix.Mkfifo(path, 0o600); err != nil {
				t.Fatal(err)
			}
		}, ErrNotRegular},
		"directory": {func(t *testing.T, path string) {
			if err := os.Mkdir(path, 0o700); err != nil {
				t.Fatal(err)
			}
		}, ErrNotRegular},
		"one byte over the cap": {func(t *testing.T, path string) {
			if err := os.WriteFile(path, make([]byte, maxSize+1), 0o600); err != nil {
				t.Fatal(err)
			}
		}, ErrTooLarge},
		"sparse and huge": {func(t *testing.T, path string) {
			if err := os.WriteFile(path, nil, 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.Truncate(path, 1<<40); err != nil {
				t.Skipf("the filesystem cannot hold a sparse 1 TiB file: %v", err)
			}
		}, ErrTooLarge},
	} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			tc.plant(t, filepath.Join(dir, fileName))
			data, err := readWithin(t, dir)
			if !errors.Is(err, tc.want) || data != nil {
				t.Fatalf("read = %q, %v; want no bytes and %v", data, err, tc.want)
			}
		})
	}
}

func TestOpenDirRefusesASymlinkedDirectory(t *testing.T) {
	base := t.TempDir()
	other := t.TempDir()
	if err := os.WriteFile(filepath.Join(other, fileName), []byte("outside"), 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(base, "dir")
	if err := os.Symlink(other, link); err != nil {
		t.Fatal(err)
	}
	if data, err := readWithin(t, link); err == nil || errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("read = %q, %v; want a refusal", data, err)
	}
}
