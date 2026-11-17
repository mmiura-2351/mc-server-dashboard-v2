package instancemanager

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

const (
	// tmpRootEnv names the package's tmp root explicitly; it wins over the tmpfs.
	// The api suite reads the same variable (issue #3722, api/README.md).
	tmpRootEnv = "MCSD_TEST_TMPDIR"
	tmpfsRoot  = "/dev/shm"
	// tmpfsMinFree is the room tmpfsRoot needs before the package uses it, as in
	// the api suite: a container's 64 MiB default keeps the default tmp dir.
	tmpfsMinFree = 1 << 30
)

// useTmpRoot points TMPDIR, and with it every t.TempDir() of this package, at a
// tmpfs (issue #3783). The package's tests fsync for real: on a disk under host
// I/O load one fsync can stall for seconds, and the pre-push gate runs this
// package beside the api suite's fs-heavy tests. On a tmpfs an fsync does not
// wait on the disk. The tests that pin an fsync's order swap the sync seam
// (syncEditDir, syncSweepScratchRoot), so they hold on any filesystem.
//
// The root is exported as tmpRootEnv, so TestTempDirsLieUnderTheSelectedRoot can
// check the wiring, and printed, so a failed or timed-out run says where its
// temp dirs were.
func useTmpRoot() error {
	root, err := selectTmpRoot(os.Getenv(tmpRootEnv), tmpfsRoot, os.TempDir(), tmpfsMinFree)
	if err != nil {
		return err
	}
	if err := os.Setenv(tmpRootEnv, root); err != nil {
		return err
	}
	if err := os.Setenv("TMPDIR", root); err != nil {
		return err
	}
	fmt.Printf("tmp root: %s (override: %s)\n", root, tmpRootEnv)
	return nil
}

// selectTmpRoot returns override when it is set, and refuses one that is not a
// directory. Otherwise it returns tmpfs when that is a writable directory with
// minFree bytes free, and fallback when it is not. The root comes back clean.
func selectTmpRoot(override, tmpfs, fallback string, minFree uint64) (string, error) {
	if override != "" {
		if info, err := os.Stat(override); err != nil || !info.IsDir() {
			return "", fmt.Errorf("%s=%s is not a directory", tmpRootEnv, override)
		}
		return filepath.Clean(override), nil
	}
	var st unix.Statfs_t
	if unix.Statfs(tmpfs, &st) == nil && unix.Access(tmpfs, unix.W_OK|unix.X_OK) == nil &&
		st.Bavail*uint64(st.Bsize) >= minFree {
		return filepath.Clean(tmpfs), nil
	}
	return filepath.Clean(fallback), nil
}

// tmpRoots is a distinct fallback, candidate tmpfs and override, all existing.
type tmpRoots struct{ fallback, candidate, override string }

func newTmpRoots(t *testing.T) tmpRoots {
	t.Helper()
	return tmpRoots{fallback: t.TempDir(), candidate: t.TempDir(), override: t.TempDir()}
}

func assertTmpRoot(t *testing.T, got string, err error, want string) {
	t.Helper()
	if err != nil || got != want {
		t.Fatalf("selectTmpRoot = (%q, %v), want (%q, nil)", got, err, want)
	}
}

func TestATmpfsWithRoomIsSelected(t *testing.T) {
	r := newTmpRoots(t)

	got, err := selectTmpRoot("", r.candidate, r.fallback, 1)

	assertTmpRoot(t, got, err, r.candidate)
}

func TestATmpfsWithoutRoomKeepsTheFallback(t *testing.T) {
	r := newTmpRoots(t)

	got, err := selectTmpRoot("", r.candidate, r.fallback, 1<<62)

	assertTmpRoot(t, got, err, r.fallback)
}

func TestAMissingTmpfsKeepsTheFallback(t *testing.T) {
	r := newTmpRoots(t)

	got, err := selectTmpRoot("", filepath.Join(r.candidate, "absent"), r.fallback, 1)

	assertTmpRoot(t, got, err, r.fallback)
}

func TestAnUnwritableTmpfsKeepsTheFallback(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root writes through any mode bits")
	}
	r := newTmpRoots(t)
	if err := os.Chmod(r.candidate, 0o500); err != nil {
		t.Fatal(err)
	}

	got, err := selectTmpRoot("", r.candidate, r.fallback, 1)

	assertTmpRoot(t, got, err, r.fallback)
}

func TestTheOverrideWinsOverATmpfsWithRoom(t *testing.T) {
	r := newTmpRoots(t)

	got, err := selectTmpRoot(r.override, r.candidate, r.fallback, 1)

	assertTmpRoot(t, got, err, r.override)
}

func TestAnOverrideThatIsNotADirectoryIsRefused(t *testing.T) {
	r := newTmpRoots(t)

	got, err := selectTmpRoot(filepath.Join(r.override, "absent"), r.candidate, r.fallback, 1)

	if err == nil {
		t.Fatalf("selectTmpRoot = %q, want an error for an override that is not a directory", got)
	}
}

// A root given with a trailing slash, as MCSD_TEST_TMPDIR=/tmp/ or TMPDIR=/tmp/
// give it, comes back clean: it is exported and compared as a path prefix.
func TestTheSelectedRootIsClean(t *testing.T) {
	r := newTmpRoots(t)
	for name, tc := range map[string]struct{ override, tmpfs, want string }{
		"override": {override: r.override + "/", tmpfs: r.candidate, want: r.override},
		"tmpfs":    {tmpfs: r.candidate + "/", want: r.candidate},
		"fallback": {tmpfs: filepath.Join(r.candidate, "absent"), want: r.fallback},
	} {
		t.Run(name, func(t *testing.T) {
			got, err := selectTmpRoot(tc.override, tc.tmpfs, r.fallback+"/", 1)

			assertTmpRoot(t, got, err, tc.want)
		})
	}
}

// TestMain applies the selection: the root it chose is exported, and this
// package's temp dirs are made under it.
func TestTempDirsLieUnderTheSelectedRoot(t *testing.T) {
	root := os.Getenv(tmpRootEnv)
	if root == "" {
		t.Fatalf("%s is unset: TestMain selected no tmp root", tmpRootEnv)
	}

	if dir := t.TempDir(); !strings.HasPrefix(dir, root+string(filepath.Separator)) {
		t.Fatalf("t.TempDir() = %q, want it under the selected root %q", dir, root)
	}
}
