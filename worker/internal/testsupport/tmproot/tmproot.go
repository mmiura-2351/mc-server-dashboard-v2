// Package tmproot puts a test binary's temp dirs on a tmpfs (issues #3783,
// #3805). A package's TestMain calls Use before m.Run, and one of its tests calls
// RequireTempDirUnderRoot to pin that wiring.
//
// It is an ordinary package rather than a _test.go file because a _test.go file
// is visible only to its own package's test binary, and several packages share
// this selector. Only _test.go files import it: the depguard rule "testsupport"
// in worker/.golangci.yml refuses it in any other file.
package tmproot

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

const (
	// Env names the tmp root explicitly; it wins over GOTMPDIR and the tmpfs.
	// The api suite reads the same variable (issue #3722, api/README.md).
	Env       = "MCSD_TEST_TMPDIR"
	tmpfsRoot = "/dev/shm"
	// tmpfsMinFree is the room tmpfsRoot needs before it is used, as in the api
	// suite: a container's 64 MiB default keeps the default tmp dir.
	tmpfsMinFree = 1 << 30
)

// selected is the root Use chose; RequireTempDirUnderRoot checks a test's temp
// dir against it.
var selected string

// Use points TMPDIR and GOTMPDIR, and with them every t.TempDir() of the test
// binary, at a tmpfs. t.TempDir() makes its dirs under GOTMPDIR when that is set
// and under TMPDIR otherwise, so both are set. The packages that call it fsync
// for real: on a disk under host I/O load one fsync can stall for seconds, and
// the pre-push gate runs them beside the api suite's fs-heavy tests. On a tmpfs
// an fsync does not wait on the disk.
//
// The root is printed, so a failed or timed-out run says where its temp dirs
// were.
func Use() error {
	root, err := selectTmpRoot(os.Getenv, tmpfsRoot, os.TempDir(), tmpfsMinFree)
	if err != nil {
		return err
	}
	for _, name := range []string{"TMPDIR", "GOTMPDIR"} {
		if err := os.Setenv(name, root); err != nil {
			return err
		}
	}
	selected = root
	fmt.Printf("tmp root: %s (override: %s)\n", root, Env)
	return nil
}

// RequireTempDirUnderRoot fails t unless Use ran in this test binary and
// t.TempDir() lies under the root it chose.
func RequireTempDirUnderRoot(t *testing.T) {
	t.Helper()
	if selected == "" {
		t.Fatal("TestMain selected no tmp root")
	}

	if dir := t.TempDir(); !strings.HasPrefix(dir, selected+string(filepath.Separator)) {
		t.Fatalf("t.TempDir() = %q, want it under the selected root %q", dir, selected)
	}
}

// selectTmpRoot returns the root getenv names explicitly: Env, or else GOTMPDIR,
// where a developer keeps the go command's and the tests' temp dirs. A named root
// that is not a directory is refused. With neither set it returns tmpfs when that
// is a writable directory with minFree bytes free, and fallback when it is not.
// The root comes back clean.
func selectTmpRoot(getenv func(string) string, tmpfs, fallback string, minFree uint64) (string, error) {
	for _, name := range []string{Env, "GOTMPDIR"} {
		dir := getenv(name)
		if dir == "" {
			continue
		}
		if info, err := os.Stat(dir); err != nil || !info.IsDir() {
			return "", fmt.Errorf("%s=%s is not a directory", name, dir)
		}
		return filepath.Clean(dir), nil
	}
	var st unix.Statfs_t
	if unix.Statfs(tmpfs, &st) == nil && unix.Access(tmpfs, unix.W_OK|unix.X_OK) == nil &&
		st.Bavail*uint64(st.Frsize) >= minFree {
		return filepath.Clean(tmpfs), nil
	}
	return filepath.Clean(fallback), nil
}
