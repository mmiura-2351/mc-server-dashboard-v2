package nofollow

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
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
		}, ErrIsDir},
		"one byte over the cap": {func(t *testing.T, path string) {
			if err := os.WriteFile(path, make([]byte, maxSize+1), 0o600); err != nil {
				t.Fatal(err)
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
			// A directory is told apart, and is still "not a regular file" to a
			// caller that does not care.
			if isDir := name == "directory"; errors.Is(err, ErrIsDir) != isDir || (isDir && !errors.Is(err, ErrNotRegular)) {
				t.Fatalf("read = %v; ErrIsDir must mark a directory and only that, as an ErrNotRegular", err)
			}
		})
	}
}

// A file far larger than the cap is refused without being read past it. The
// bound is pinned by what the read allocates, not by the file being too large to
// read: a regressed bound then fails here instead of reading the whole file.
func TestReadFileAtReadsNoFurtherThanTheCapOfAHugeFile(t *testing.T) {
	const size = 128 << 20
	dir := t.TempDir()
	path := filepath.Join(dir, fileName)
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	// Sparse: the file reads as 128 MiB of zeros without occupying them.
	if err := os.Truncate(path, size); err != nil {
		t.Skipf("the filesystem cannot hold a sparse file: %v", err)
	}

	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	data, err := readWithin(t, dir)
	runtime.ReadMemStats(&after)

	if !errors.Is(err, ErrTooLarge) || data != nil {
		t.Fatalf("read = %d bytes, %v; want no bytes and ErrTooLarge", len(data), err)
	}
	// Reading the cap costs a few times the cap; reading the file costs at least the
	// file. The bound is far from both, so it does not depend on the allocator.
	if allocated := after.TotalAlloc - before.TotalAlloc; allocated >= size/2 {
		t.Fatalf("the refused read allocated %d bytes, want it bounded by the %d-byte cap", allocated, maxSize)
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

// A file that is within the cap when it is opened and judged, and grows past it
// before it is read, is refused — and the read stops at the cap however large the
// file has become. A size taken at the stat says nothing about the read: the
// server owns the file and can extend it under the open descriptor.
func TestReadFileAtStopsAtTheCapWhenTheFileGrowsAfterItWasJudged(t *testing.T) {
	const grownTo = 128 << 20
	dir := t.TempDir()
	path := filepath.Join(dir, fileName)
	if err := os.WriteFile(path, make([]byte, maxSize), 0o600); err != nil {
		t.Fatal(err)
	}
	grew := false
	t.Cleanup(func() { afterStat = func() {} })
	afterStat = func() {
		// Sparse: the file reads as 128 MiB of zeros without occupying them.
		if err := os.Truncate(path, grownTo); err != nil {
			t.Skipf("the filesystem cannot hold a sparse file: %v", err)
		}
		grew = true
	}
	dirFd, err := OpenDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = unix.Close(dirFd) }()

	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	data, err := ReadFileAt(dirFd, fileName, maxSize)
	runtime.ReadMemStats(&after)

	if !grew {
		t.Fatal("the file was never grown: the read did not pass the point after its stat")
	}
	if !errors.Is(err, ErrTooLarge) || data != nil {
		t.Fatalf("read = %d bytes, %v; want no bytes and ErrTooLarge", len(data), err)
	}
	// Reading the cap costs a few times the cap; reading the file costs at least the
	// file. The bound is far from both, so it does not depend on the allocator.
	if allocated := after.TotalAlloc - before.TotalAlloc; allocated >= grownTo/2 {
		t.Fatalf("the refused read allocated %d bytes, want it bounded by the %d-byte cap", allocated, maxSize)
	}
}
