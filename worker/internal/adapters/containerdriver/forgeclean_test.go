package containerdriver

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// The stale-artifact cleanup runs as the Worker (root, in the shipped topology)
// over a working set that server-controlled code and the Forge installer can
// write, including while the cleanup runs. These tests plant what such code
// could plant and pin that the cleanup never removes anything outside the
// working set (issue #3333).

const (
	forgeVersionsRel = "libraries/net/minecraftforge/forge"
	outsideArgs      = "--outside-args-must-survive\n"
)

// plant writes content to path, creating its parent directories.
func plant(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

// cleanForge runs the cleanup over workingDir and returns what it logged and its
// error.
func cleanForge(t *testing.T, workingDir string) (string, error) {
	t.Helper()
	logged := &syncBuffer{}
	err := cleanForgeInstallArtifacts(workingDir, slog.New(slog.NewTextHandler(logged, nil)))
	return logged.String(), err
}

// assertContent fails unless path still holds exactly want.
func assertContent(t *testing.T, path, want string) {
	t.Helper()
	got, err := os.ReadFile(path) // #nosec G304 -- test path under t.TempDir.
	if err != nil {
		t.Fatalf("%s must survive the cleanup: %v", path, err)
	}
	if string(got) != want {
		t.Fatalf("%s = %q, want it byte-identical to %q", path, got, want)
	}
}

// assertGone fails unless no entry — not even a dangling link — is at path.
func assertGone(t *testing.T, path string) {
	t.Helper()
	if _, err := os.Lstat(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("%s must be removed; lstat err = %v", path, err)
	}
}

// assertSkipLogged fails unless the cleanup warned that it skipped rel.
func assertSkipLogged(t *testing.T, logged, rel string) {
	t.Helper()
	if !strings.Contains(logged, "level=WARN") || !strings.Contains(logged, "path="+rel) {
		t.Fatalf("worker log = %q, want a warning naming %s", logged, rel)
	}
}

// assertSkipWording fails unless a warning names rel, and it says a symlink was
// not followed exactly when the entry is one (issue #3354).
func assertSkipWording(t *testing.T, logged, rel string, symlink bool) {
	t.Helper()
	for _, line := range strings.Split(logged, "\n") {
		if !strings.Contains(line, "level=WARN") || !strings.Contains(line+" ", " path="+rel+" ") {
			continue
		}
		if strings.Contains(line, "symlink") != symlink {
			t.Fatalf("warning %q about %s: mentions a symlink = %v, want %v", line, rel, !symlink, symlink)
		}
		return
	}
	t.Fatalf("worker log = %q, want a warning naming %s", logged, rel)
}

// Every stale args file (one per version directory) and every legacy jar is
// removed, and nothing else is (issue #1127).
func TestCleanForgeInstallArtifactsRemovesAll(t *testing.T) {
	dir := t.TempDir()
	removed := []string{
		forgeVersionsRel + "/1.20.1-47.2.0/unix_args.txt",
		forgeVersionsRel + "/1.20.1-47.3.0/unix_args.txt",
		"forge-1.12.2-14.23.5.2860.jar",
		"forge-1.12.2-14.23.5.2861.jar",
	}
	kept := []string{
		"server.jar",
		"user_jvm_args.txt",
		forgeVersionsRel + "/1.20.1-47.2.0/win_args.txt",
		"mods/forge-addon.jar",
	}
	for _, rel := range append(append([]string{}, removed...), kept...) {
		plant(t, filepath.Join(dir, rel), "x")
	}

	logged, err := cleanForge(t, dir)
	if err != nil {
		t.Fatalf("cleanForgeInstallArtifacts: %v", err)
	}
	for _, rel := range removed {
		assertGone(t, filepath.Join(dir, rel))
	}
	for _, rel := range kept {
		assertContent(t, filepath.Join(dir, rel), "x")
	}
	if logged != "" {
		t.Fatalf("a normal cleanup must log nothing; got %q", logged)
	}
}

// A working set with nothing to clean — or none at all — is not an error.
func TestCleanForgeInstallArtifactsNothingToClean(t *testing.T) {
	for name, dir := range map[string]string{
		"empty working set":  t.TempDir(),
		"absent working set": filepath.Join(t.TempDir(), "absent"),
	} {
		t.Run(name, func(t *testing.T) {
			logged, err := cleanForge(t, dir)
			if err != nil || logged != "" {
				t.Fatalf("err = %v, log = %q; want neither", err, logged)
			}
		})
	}
}

// With any component of the args-file path replaced by a symlink to a directory
// outside the working set that holds a matching unix_args.txt, the outside file
// survives, the link is left alone, and the rest of the cleanup still runs.
func TestCleanForgeInstallArtifactsNeverFollowsASymlinkedComponent(t *testing.T) {
	components := strings.Split(forgeVersionsRel+"/1.20.1-47.2.0", "/")
	for i, component := range components {
		t.Run(component, func(t *testing.T) {
			dir, outside := t.TempDir(), t.TempDir()
			linkRel := strings.Join(components[:i+1], "/")
			rest := filepath.FromSlash(strings.Join(append(components[i+1:], "unix_args.txt"), "/"))
			outsideFile := filepath.Join(outside, rest)
			plant(t, outsideFile, outsideArgs)

			link := filepath.Join(dir, filepath.FromSlash(linkRel))
			if err := os.MkdirAll(filepath.Dir(link), 0o750); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(outside, link); err != nil {
				t.Fatal(err)
			}
			plant(t, filepath.Join(dir, "forge-1.12.2-14.23.5.2860.jar"), "x")

			logged, err := cleanForge(t, dir)
			if err != nil {
				t.Fatalf("a planted link is nothing to clean, not a failure; got %v", err)
			}
			assertContent(t, outsideFile, outsideArgs)
			assertSkipWording(t, logged, linkRel, true)
			if target, err := os.Readlink(link); err != nil || target != outside {
				t.Fatalf("the planted link must be left alone; readlink = %q, %v", target, err)
			}
			assertGone(t, filepath.Join(dir, "forge-1.12.2-14.23.5.2860.jar"))
		})
	}
}

// A symlinked version directory does not stop a real one beside it from being
// cleaned.
func TestCleanForgeInstallArtifactsCleansRealVersionBesideALinkedOne(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	plant(t, filepath.Join(outside, "unix_args.txt"), outsideArgs)
	realArgs := filepath.Join(dir, forgeVersionsRel, "1.20.1-47.3.0", "unix_args.txt")
	plant(t, realArgs, "x")
	if err := os.Symlink(outside, filepath.Join(dir, forgeVersionsRel, "1.20.1-47.2.0")); err != nil {
		t.Fatal(err)
	}

	if _, err := cleanForge(t, dir); err != nil {
		t.Fatal(err)
	}
	assertGone(t, realArgs)
	assertContent(t, filepath.Join(outside, "unix_args.txt"), outsideArgs)
}

// A component that is a regular file is nothing to clean either. It plants
// nothing — it cannot lead anywhere — so it is skipped without a warning: a
// stray file beside the version directories is not worth an operator's attention
// (issue #3354).
func TestCleanForgeInstallArtifactsSkipsANonDirectoryComponent(t *testing.T) {
	for _, rel := range []string{"libraries", forgeVersionsRel, forgeVersionsRel + "/1.20.1-47.2.0"} {
		t.Run(rel, func(t *testing.T) {
			dir := t.TempDir()
			plant(t, filepath.Join(dir, rel), "x")
			logged, err := cleanForge(t, dir)
			if err != nil {
				t.Fatalf("err = %v, want nil", err)
			}
			assertContent(t, filepath.Join(dir, rel), "x")
			if logged != "" {
				t.Fatalf("a plain-file component must be skipped quietly; got %q", logged)
			}
		})
	}
}

// A FIFO where a directory is expected is skipped with a warning that does not
// claim it is a symlink (issue #3354).
func TestCleanForgeInstallArtifactsWarnsOfAFIFOComponentAsNotADirectory(t *testing.T) {
	dir := t.TempDir()
	rel := forgeVersionsRel + "/1.20.1-47.2.0"
	mkfifo(t, filepath.Join(dir, filepath.FromSlash(rel)))

	logged, err := cleanForge(t, dir)
	if err != nil {
		t.Fatalf("err = %v, want nil", err)
	}
	assertSkipWording(t, logged, rel, false)
}

// A symlink planted as the artifact itself is removed — the link, never what it
// points at.
func TestCleanForgeInstallArtifactsRemovesAFinalSymlinkNotItsTarget(t *testing.T) {
	for _, rel := range []string{forgeArgsRel, "forge-1.12.2-14.23.5.2860.jar"} {
		t.Run(rel, func(t *testing.T) {
			dir, outside := t.TempDir(), t.TempDir()
			target := filepath.Join(outside, "unix_args.txt")
			plant(t, target, outsideArgs)
			link := filepath.Join(dir, filepath.FromSlash(rel))
			if err := os.MkdirAll(filepath.Dir(link), 0o750); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(target, link); err != nil {
				t.Fatal(err)
			}

			if _, err := cleanForge(t, dir); err != nil {
				t.Fatal(err)
			}
			assertGone(t, link)
			assertContent(t, target, outsideArgs)
		})
	}
}

// A directory carrying an artifact's name is left in place, even when empty.
func TestCleanForgeInstallArtifactsLeavesADirectory(t *testing.T) {
	for _, rel := range []string{forgeArgsRel, "forge-1.12.2-14.23.5.2860.jar"} {
		t.Run(rel, func(t *testing.T) {
			dir := t.TempDir()
			planted := filepath.Join(dir, filepath.FromSlash(rel))
			if err := os.MkdirAll(planted, 0o750); err != nil {
				t.Fatal(err)
			}
			logged, err := cleanForge(t, dir)
			if err != nil {
				t.Fatalf("err = %v, want nil", err)
			}
			if st, err := os.Lstat(planted); err != nil || !st.IsDir() {
				t.Fatalf("the directory must be left in place; lstat = %v, %v", st, err)
			}
			assertSkipLogged(t, logged, rel)
		})
	}
}

// beforeForgeCleanOpen runs swap once, immediately before the cleanup opens the
// directory called name — after it has listed the parent and matched the name.
func beforeForgeCleanOpen(t *testing.T, name string, swap func()) {
	t.Helper()
	orig := forgeCleanOpenAt
	t.Cleanup(func() { forgeCleanOpenAt = orig })
	done := false
	forgeCleanOpenAt = func(dirFd int, entry string, flags int, mode uint32) (int, error) {
		if !done && filepath.Base(entry) == name {
			done = true
			swap()
		}
		return orig(dirFd, entry, flags, mode)
	}
}

// beforeForgeCleanUnlink runs swap once, immediately before the cleanup unlinks
// the entry called name.
func beforeForgeCleanUnlink(t *testing.T, name string, swap func()) {
	t.Helper()
	orig := forgeCleanUnlinkAt
	t.Cleanup(func() { forgeCleanUnlinkAt = orig })
	done := false
	forgeCleanUnlinkAt = func(dirFd int, entry string, flags int) error {
		if !done && entry == name {
			done = true
			swap()
		}
		return orig(dirFd, entry, flags)
	}
}

// A version directory swapped for a link to an outside directory after the
// cleanup listed it, but before it opened it, is refused.
func TestCleanForgeInstallArtifactsRefusesAVersionSwappedInAfterTheListing(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	plant(t, filepath.Join(outside, "unix_args.txt"), outsideArgs)
	version := filepath.Join(dir, forgeVersionsRel, "1.20.1-47.2.0")
	plant(t, filepath.Join(version, "unix_args.txt"), "x")

	beforeForgeCleanOpen(t, "1.20.1-47.2.0", func() {
		if err := os.RemoveAll(version); err != nil {
			t.Error(err)
		}
		if err := os.Symlink(outside, version); err != nil {
			t.Error(err)
		}
	})

	logged, err := cleanForge(t, dir)
	if err != nil {
		t.Fatalf("err = %v, want nil", err)
	}
	assertContent(t, filepath.Join(outside, "unix_args.txt"), outsideArgs)
	assertSkipLogged(t, logged, forgeVersionsRel+"/1.20.1-47.2.0")
}

// An earlier component swapped for a link while the cleanup is further down: the
// descent continues from the directory it holds open, not from the working-set
// root through whatever now has that component's name.
func TestCleanForgeInstallArtifactsDescendsFromTheDirectoryItOpened(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	outsideFile := filepath.Join(outside, "net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt")
	plant(t, outsideFile, outsideArgs)
	plant(t, filepath.Join(dir, filepath.FromSlash(forgeArgsRel)), "x")
	libraries, moved := filepath.Join(dir, "libraries"), filepath.Join(dir, "moved")

	beforeForgeCleanOpen(t, "1.20.1-47.2.0", func() {
		if err := os.Rename(libraries, moved); err != nil {
			t.Error(err)
		}
		if err := os.Symlink(outside, libraries); err != nil {
			t.Error(err)
		}
	})

	if _, err := cleanForge(t, dir); err != nil {
		t.Fatalf("err = %v, want nil", err)
	}
	assertContent(t, outsideFile, outsideArgs)
	assertGone(t, filepath.Join(moved, "net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt"))
}

// A version directory swapped for a link after the cleanup opened it: the unlink
// acts on the directory that was opened, not on whatever now has its name.
func TestCleanForgeInstallArtifactsUnlinksInTheDirectoryItOpened(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	plant(t, filepath.Join(outside, "unix_args.txt"), outsideArgs)
	version := filepath.Join(dir, forgeVersionsRel, "1.20.1-47.2.0")
	moved := filepath.Join(dir, "moved")
	plant(t, filepath.Join(version, "unix_args.txt"), "x")

	beforeForgeCleanUnlink(t, "unix_args.txt", func() {
		if err := os.Rename(version, moved); err != nil {
			t.Error(err)
		}
		if err := os.Symlink(outside, version); err != nil {
			t.Error(err)
		}
	})

	if _, err := cleanForge(t, dir); err != nil {
		t.Fatalf("err = %v, want nil", err)
	}
	assertContent(t, filepath.Join(outside, "unix_args.txt"), outsideArgs)
	assertGone(t, filepath.Join(moved, "unix_args.txt"))
}

// A legacy jar swapped for a link to an outside file after the cleanup listed
// it: the link is removed, its target is not.
func TestCleanForgeInstallArtifactsUnlinksALinkSwappedInAfterTheListing(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	target := filepath.Join(outside, "forge-1.12.2-14.23.5.2860.jar")
	plant(t, target, outsideArgs)
	jar := filepath.Join(dir, "forge-1.12.2-14.23.5.2860.jar")
	plant(t, jar, "x")

	beforeForgeCleanUnlink(t, "forge-1.12.2-14.23.5.2860.jar", func() {
		if err := os.Remove(jar); err != nil {
			t.Error(err)
		}
		if err := os.Symlink(target, jar); err != nil {
			t.Error(err)
		}
	})

	if _, err := cleanForge(t, dir); err != nil {
		t.Fatalf("err = %v, want nil", err)
	}
	assertGone(t, jar)
	assertContent(t, target, outsideArgs)
}

// An artifact that cannot be removed is reported, and does not stop the others
// from being removed.
func TestCleanForgeInstallArtifactsReportsAFailedRemoveAndContinues(t *testing.T) {
	dir := t.TempDir()
	plant(t, filepath.Join(dir, filepath.FromSlash(forgeArgsRel)), "x")
	plant(t, filepath.Join(dir, "forge-1.12.2-14.23.5.2860.jar"), "x")

	orig := forgeCleanUnlinkAt
	t.Cleanup(func() { forgeCleanUnlinkAt = orig })
	forgeCleanUnlinkAt = func(dirFd int, entry string, flags int) error {
		if entry == "unix_args.txt" {
			return unix.EACCES
		}
		return orig(dirFd, entry, flags)
	}

	_, err := cleanForge(t, dir)
	if !errors.Is(err, unix.EACCES) || !strings.Contains(err.Error(), forgeArgsRel) {
		t.Fatalf("err = %v, want EACCES naming %s", err, forgeArgsRel)
	}
	assertGone(t, filepath.Join(dir, "forge-1.12.2-14.23.5.2860.jar"))
}

// Start cleans before it creates the install container: a legacy jar left by an
// earlier install is gone by then, so the jar this install produces is the only
// match and the launch is not ambiguous (issue #1127).
func TestForgeStartCleansStaleArtifactsBeforeTheInstall(t *testing.T) {
	dir := t.TempDir()
	stale := filepath.Join(dir, "forge-1.12.2-14.23.5.2859.jar")
	plant(t, stale, "x")
	docker := newForgeFakeDocker()
	docker.onCreateHook = func(spec CreateSpec) {
		if spec.Name == "mcsd-s1-install" {
			assertGone(t, stale)
			plant(t, filepath.Join(dir, "forge-1.12.2-14.23.5.2860.jar"), "x")
		}
	}

	inst, err := forgeDriver(docker).Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	docker.exit("mcsd-s1-install", 0, nil)
	drainTo(t, inst.Events(), execution.StateRunning)
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// A retry cleans what the failed attempt left: an args file written by the first
// attempt is gone before the second runs, so the re-plan finds only the second's
// (issue #1127).
func TestForgeInstallRetryCleansTheFailedAttemptsArtifacts(t *testing.T) {
	prev := installRetryBackoff
	installRetryBackoff = []time.Duration{time.Millisecond, time.Millisecond}
	t.Cleanup(func() { installRetryBackoff = prev })

	dir := t.TempDir()
	stale := filepath.Join(dir, forgeVersionsRel, "1.20.1-47.1.0", "unix_args.txt")
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	docker.waitGates[installID] = make(chan waitResult, 2)
	docker.waitGates[installID] <- waitResult{code: 1}
	docker.waitGates[installID] <- waitResult{code: 0}
	var installs atomic.Int32
	docker.onCreateHook = func(spec CreateSpec) {
		if spec.Name != installID {
			return
		}
		if installs.Add(1) == 1 {
			plant(t, stale, "x")
			return
		}
		// The second attempt "produces" its args file, as in production.
		writeArgsfile(t, dir)
	}

	inst, err := forgeDriver(docker).Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
	assertGone(t, stale)
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// A start retried after one whose first installer the daemon did not answer,
// while that installer still runs and is recorded (issue #3645), ends it before
// the stale-artifact cleanup unlinks anything: the cleanup writes the working
// set, so it comes after the leftover/quiescence gate (issue #3680). The
// leftover is stopped and removed once, with what it wrote still in place; then
// the cleanup and the install run.
func TestForgeStartEndsARecordedInstallerBeforeCleaningArtifacts(t *testing.T) {
	dir := t.TempDir()
	jar := filepath.Join(dir, "forge-1.12.2-14.23.5.2859.jar")
	docker := newForgeFakeDocker()
	docker.reportsState = true
	docker.startStallIDs = map[string]bool{forgeInstallID: true}
	docker.removeErrIDs = map[string]error{forgeInstallID: errors.New("daemon busy")}
	d := stalledCallForgeDriver(docker, &syncBuffer{})
	d.initialInstallStartTimeout = 20 * time.Millisecond
	if err := startWithin(context.Background(), t, d, forgeSpec(dir)); !strings.Contains(err.Error(), "install container start timed out") {
		t.Fatalf("first start err = %v, want the installer start's timeout", err)
	}
	if got := unremovedOf(d, "s1"); !slices.Equal(got, []string{forgeInstallID}) {
		t.Fatalf("recorded = %v, want the installer the start left running", got)
	}
	// The installer left running goes on writing the working set.
	plant(t, jar, "x")

	var (
		mu    sync.Mutex
		calls []string
	)
	record := func(call string) {
		_, err := os.Lstat(jar)
		mu.Lock()
		calls = append(calls, fmt.Sprintf("%s, artifact present: %t", call, err == nil))
		mu.Unlock()
	}
	docker.mu.Lock()
	docker.startStallIDs, docker.removeErrIDs = nil, nil
	docker.onStopOrRemove = func(call, id string) {
		if id == forgeInstallID {
			record(call)
		}
	}
	docker.onCreateHook = func(spec CreateSpec) {
		if spec.Name == forgeInstallID {
			record("create")
		}
	}
	docker.mu.Unlock()

	next, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("next Start: %v, want the leftover installer ended and the install started", err)
	}
	mu.Lock()
	got := slices.Clone(calls)
	mu.Unlock()
	want := []string{"stop, artifact present: true", "remove, artifact present: true", "create, artifact present: false"}
	if !slices.Equal(got, want) {
		t.Fatalf("calls = %q, want %q", got, want)
	}
	if got := unremovedOf(d, "s1"); len(got) != 0 {
		t.Fatalf("recorded = %v, want the record cleared", got)
	}
	writeArgsfile(t, dir)
	docker.exit(forgeInstallID, 0, nil)
	drainTo(t, next.Events(), execution.StateRunning)
	docker.exit("mcsd-s1", 0, nil)
	awaitStreamClosed(t, next.Events())
}

// A start the leftover/quiescence gate refuses cleans nothing: a container of
// the server that may still be running keeps the artifacts it wrote, and no
// installer is created (issue #3680).
func TestForgeStartRefusedByTheGateLeavesArtifactsUntouched(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(d *Driver, docker *forgeFakeDocker)
		want  string
	}{
		{
			name: "recorded leftover not removed",
			setup: func(d *Driver, docker *forgeFakeDocker) {
				d.unremoved.add("s1", forgeInstallID)
				docker.inspectGate = make(chan inspectStep, 1)
				docker.inspectGate <- inspectStep{err: errors.New("daemon busy")}
			},
			want: "is not removed: inspect: daemon busy",
		},
		{
			name: "untracked container running",
			setup: func(_ *Driver, docker *forgeFakeDocker) {
				docker.reportsState = true
				docker.running = map[string]bool{forgeInstallID: true}
			},
			want: "its working set is in use",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			jar := filepath.Join(dir, "forge-1.12.2-14.23.5.2859.jar")
			plant(t, jar, "x")
			docker := newForgeFakeDocker()
			d := forgeDriver(docker)
			tc.setup(d, docker)

			err := startWithin(context.Background(), t, d, forgeSpec(dir))
			if !strings.Contains(err.Error(), tc.want) || !errors.Is(err, execution.ErrBackendUnavailable) {
				t.Fatalf("err = %v, want a refusal containing %q, classified as the backend's unavailability", err, tc.want)
			}
			assertContent(t, jar, "x")
			if got := docker.names(); len(got) != 0 {
				t.Fatalf("created = %v, want no container", got)
			}
		})
	}
}

// An install retry the leftover/quiescence gate refuses cleans nothing either:
// the failed attempt's installer, whose Wait the daemon failed and whose
// removal failed, may still be running and keeps what it wrote (issue #3680).
func TestForgeInstallRetryRefusedByTheGateLeavesArtifactsUntouched(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	stale := filepath.Join(dir, "forge-1.12.2-14.23.5.2859.jar")
	docker := newForgeFakeDocker()
	docker.reportsState = true
	docker.removeErrIDs = map[string]error{forgeInstallID: errors.New("daemon busy")}
	docker.waitGates[forgeInstallID] = make(chan waitResult, 1)
	docker.waitGates[forgeInstallID] <- waitResult{err: statusError{method: "POST", path: "/wait", code: 500, message: "wait failed"}}
	docker.onCreateHook = func(spec CreateSpec) {
		if spec.Name == forgeInstallID {
			plant(t, stale, "x")
		}
	}

	inst, err := forgeDriver(docker).Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	last := awaitStreamClosed(t, inst.Events())
	if last.State != execution.StateCrashed || !strings.Contains(last.Detail, "its working set is in use") {
		t.Fatalf("last event = %+v, want the retry refused while the installer may run", last)
	}
	assertContent(t, stale, "x")
	if got := docker.names(); len(got) != 1 {
		t.Fatalf("created = %v, want only the first installer", got)
	}
}
