package instancemanager

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

const (
	// tmpRootEnv names the package's tmp root explicitly; it wins over GOTMPDIR
	// and the tmpfs. The api suite reads the same variable (issue #3722,
	// api/README.md).
	tmpRootEnv = "MCSD_TEST_TMPDIR"
	tmpfsRoot  = "/dev/shm"
	// tmpfsMinFree is the room tmpfsRoot needs before the package uses it, as in
	// the api suite: a container's 64 MiB default keeps the default tmp dir.
	tmpfsMinFree = 1 << 30
)

// selectedTmpRoot is the root useTmpRoot chose; TestTempDirsLieUnderTheSelectedRoot
// checks the package's temp dirs against it.
var selectedTmpRoot string

// useTmpRoot points TMPDIR and GOTMPDIR, and with them every t.TempDir() of this
// package, at a tmpfs (issue #3783). t.TempDir() makes its dirs under GOTMPDIR
// when that is set and under TMPDIR otherwise, so both are set. The package's
// tests fsync for real: on a disk under host I/O load one fsync can stall for
// seconds, and the pre-push gate runs this package beside the api suite's
// fs-heavy tests. On a tmpfs an fsync does not wait on the disk. The tests that
// pin an fsync's order swap the sync seam (syncEditDir, syncSweepScratchRoot), so
// they hold on any filesystem.
//
// The root is printed, so a failed or timed-out run says where its temp dirs
// were.
func useTmpRoot() error {
	root, err := selectTmpRoot(os.Getenv, tmpfsRoot, os.TempDir(), tmpfsMinFree)
	if err != nil {
		return err
	}
	for _, name := range []string{"TMPDIR", "GOTMPDIR"} {
		if err := os.Setenv(name, root); err != nil {
			return err
		}
	}
	selectedTmpRoot = root
	fmt.Printf("tmp root: %s (override: %s)\n", root, tmpRootEnv)
	return nil
}

// selectTmpRoot returns the root getenv names explicitly: tmpRootEnv, or else
// GOTMPDIR, where a developer keeps the go command's and the tests' temp dirs. A
// named root that is not a directory is refused. With neither set it returns
// tmpfs when that is a writable directory with minFree bytes free, and fallback
// when it is not. The root comes back clean.
func selectTmpRoot(getenv func(string) string, tmpfs, fallback string, minFree uint64) (string, error) {
	for _, name := range []string{tmpRootEnv, "GOTMPDIR"} {
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

// tmpRoots is a distinct fallback, candidate tmpfs, override and GOTMPDIR, all
// existing.
type tmpRoots struct{ fallback, candidate, override, goTmpDir string }

func newTmpRoots(t *testing.T) tmpRoots {
	t.Helper()
	return tmpRoots{fallback: t.TempDir(), candidate: t.TempDir(), override: t.TempDir(), goTmpDir: t.TempDir()}
}

// envOf is a getenv that sees vars and nothing else.
func envOf(vars map[string]string) func(string) string {
	return func(name string) string { return vars[name] }
}

func assertTmpRoot(t *testing.T, got string, err error, want string) {
	t.Helper()
	if err != nil || got != want {
		t.Fatalf("selectTmpRoot = (%q, %v), want (%q, nil)", got, err, want)
	}
}

func TestATmpfsWithRoomIsSelected(t *testing.T) {
	r := newTmpRoots(t)

	got, err := selectTmpRoot(envOf(nil), r.candidate, r.fallback, 1)

	assertTmpRoot(t, got, err, r.candidate)
}

func TestATmpfsWithoutRoomKeepsTheFallback(t *testing.T) {
	r := newTmpRoots(t)

	got, err := selectTmpRoot(envOf(nil), r.candidate, r.fallback, 1<<62)

	assertTmpRoot(t, got, err, r.fallback)
}

func TestAMissingTmpfsKeepsTheFallback(t *testing.T) {
	r := newTmpRoots(t)

	got, err := selectTmpRoot(envOf(nil), filepath.Join(r.candidate, "absent"), r.fallback, 1)

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

	got, err := selectTmpRoot(envOf(nil), r.candidate, r.fallback, 1)

	assertTmpRoot(t, got, err, r.fallback)
}

func TestTheOverrideWinsOverATmpfsWithRoom(t *testing.T) {
	r := newTmpRoots(t)

	got, err := selectTmpRoot(envOf(map[string]string{tmpRootEnv: r.override}), r.candidate, r.fallback, 1)

	assertTmpRoot(t, got, err, r.override)
}

// A GOTMPDIR the developer set is where t.TempDir() would have put the dirs, so
// it is kept as the root.
func TestGOTMPDIRWinsOverATmpfsWithRoom(t *testing.T) {
	r := newTmpRoots(t)

	got, err := selectTmpRoot(envOf(map[string]string{"GOTMPDIR": r.goTmpDir}), r.candidate, r.fallback, 1)

	assertTmpRoot(t, got, err, r.goTmpDir)
}

func TestTheOverrideWinsOverGOTMPDIR(t *testing.T) {
	r := newTmpRoots(t)
	env := envOf(map[string]string{tmpRootEnv: r.override, "GOTMPDIR": r.goTmpDir})

	got, err := selectTmpRoot(env, r.candidate, r.fallback, 1)

	assertTmpRoot(t, got, err, r.override)
}

func TestANamedRootThatIsNotADirectoryIsRefused(t *testing.T) {
	r := newTmpRoots(t)
	file := filepath.Join(r.override, "file")
	if err := os.WriteFile(file, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{tmpRootEnv, "GOTMPDIR"} {
		for kind, dir := range map[string]string{"missing": filepath.Join(r.override, "absent"), "file": file} {
			t.Run(name+" "+kind, func(t *testing.T) {
				got, err := selectTmpRoot(envOf(map[string]string{name: dir}), r.candidate, r.fallback, 1)

				if err == nil || !strings.HasPrefix(err.Error(), name+"=") {
					t.Fatalf("selectTmpRoot = (%q, %v), want an error naming %s", got, err, name)
				}
			})
		}
	}
}

// A root given with a trailing slash, as MCSD_TEST_TMPDIR=/tmp/ or TMPDIR=/tmp/
// give it, comes back clean: it is compared as a path prefix.
func TestTheSelectedRootIsClean(t *testing.T) {
	r := newTmpRoots(t)
	absent := filepath.Join(r.candidate, "absent")
	for name, tc := range map[string]struct {
		env         map[string]string
		tmpfs, want string
	}{
		"override": {env: map[string]string{tmpRootEnv: r.override + "/"}, tmpfs: r.candidate, want: r.override},
		"GOTMPDIR": {env: map[string]string{"GOTMPDIR": r.goTmpDir + "/"}, tmpfs: r.candidate, want: r.goTmpDir},
		"tmpfs":    {tmpfs: r.candidate + "/", want: r.candidate},
		"fallback": {tmpfs: absent, want: r.fallback},
	} {
		t.Run(name, func(t *testing.T) {
			got, err := selectTmpRoot(envOf(tc.env), tc.tmpfs, r.fallback+"/", 1)

			assertTmpRoot(t, got, err, tc.want)
		})
	}
}

// TestMain applies the selection before any test: this package's temp dirs are
// made under the root it chose.
func TestTempDirsLieUnderTheSelectedRoot(t *testing.T) {
	if selectedTmpRoot == "" {
		t.Fatal("TestMain selected no tmp root")
	}

	if dir := t.TempDir(); !strings.HasPrefix(dir, selectedTmpRoot+string(filepath.Separator)) {
		t.Fatalf("t.TempDir() = %q, want it under the selected root %q", dir, selectedTmpRoot)
	}
}

// t.TempDir() makes its dirs under GOTMPDIR when that is set, so TestMain points
// GOTMPDIR at the root too: this test binary, started with GOTMPDIR set and
// tmpRootEnv naming another dir, makes its temp dirs under the latter.
func TestTempDirsFollowTheSelectedRootPastAPresetGOTMPDIR(t *testing.T) {
	r := newTmpRoots(t)
	cmd := exec.Command(os.Args[0], "-test.run=^TestTempDirsLieUnderTheSelectedRoot$", "-test.count=1", "-test.v")
	cmd.Env = append(os.Environ(), "GOTMPDIR="+r.goTmpDir, tmpRootEnv+"="+r.override)

	out, err := cmd.CombinedOutput()

	if err != nil || !strings.Contains(string(out), "--- PASS: TestTempDirsLieUnderTheSelectedRoot") {
		t.Fatalf("the test binary with GOTMPDIR=%s and %s=%s: %v\n%s", r.goTmpDir, tmpRootEnv, r.override, err, out)
	}
}
