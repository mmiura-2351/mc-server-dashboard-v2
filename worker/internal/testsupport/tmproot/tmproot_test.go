package tmproot

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestMain(m *testing.M) {
	if err := Use(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	os.Exit(m.Run())
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

	got, err := selectTmpRoot(envOf(map[string]string{Env: r.override}), r.candidate, r.fallback, 1)

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
	env := envOf(map[string]string{Env: r.override, "GOTMPDIR": r.goTmpDir})

	got, err := selectTmpRoot(env, r.candidate, r.fallback, 1)

	assertTmpRoot(t, got, err, r.override)
}

func TestANamedRootThatIsNotADirectoryIsRefused(t *testing.T) {
	r := newTmpRoots(t)
	file := filepath.Join(r.override, "file")
	if err := os.WriteFile(file, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{Env, "GOTMPDIR"} {
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
		"override": {env: map[string]string{Env: r.override + "/"}, tmpfs: r.candidate, want: r.override},
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
	RequireTempDirUnderRoot(t)
}

// t.TempDir() makes its dirs under GOTMPDIR when that is set, so Use points
// GOTMPDIR at the root too: this test binary, started with GOTMPDIR set and Env
// naming another dir, makes its temp dirs under the latter.
func TestTempDirsFollowTheSelectedRootPastAPresetGOTMPDIR(t *testing.T) {
	r := newTmpRoots(t)
	cmd := exec.Command(os.Args[0], "-test.run=^TestTempDirsLieUnderTheSelectedRoot$", "-test.count=1", "-test.v")
	cmd.Env = append(os.Environ(), "GOTMPDIR="+r.goTmpDir, Env+"="+r.override)

	out, err := cmd.CombinedOutput()

	if err != nil || !strings.Contains(string(out), "--- PASS: TestTempDirsLieUnderTheSelectedRoot") {
		t.Fatalf("the test binary with GOTMPDIR=%s and %s=%s: %v\n%s", r.goTmpDir, Env, r.override, err, out)
	}
}
