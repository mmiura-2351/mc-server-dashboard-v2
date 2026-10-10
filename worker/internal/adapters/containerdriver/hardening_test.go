package containerdriver

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// The run-as identity the hardening tests configure. It is deliberately not the
// test process's own uid:gid, so every file the test creates counts as "not yet
// owned by the run-as user" and the hand-over has work to do.
var (
	testRunAsUID = os.Getuid() + 1
	testRunAsGID = os.Getgid() + 1
	wantRunAs    = fmt.Sprintf("%d:%d", testRunAsUID, testRunAsGID)
)

// chownRecorder stands in for chownAtNoFollow, which only root may use to give a
// file away. It records the entry each call names — resolved from the directory
// descriptor the call was made against, which is the point: that is where the
// kernel would have applied it — and optionally runs a hook first.
type chownRecorder struct {
	mu     sync.Mutex
	paths  []string
	err    error
	before func(path string)
}

func (r *chownRecorder) chownAt(dirFd int, name string, uid, gid int) error {
	if uid != testRunAsUID || gid != testRunAsGID {
		return errors.New("chown called with an identity other than the run-as user")
	}
	dir, err := os.Readlink(fmt.Sprintf("/proc/self/fd/%d", dirFd))
	if err != nil {
		return err
	}
	path := filepath.Join(dir, name)
	r.mu.Lock()
	r.paths = append(r.paths, path)
	hook, result := r.before, r.err
	r.mu.Unlock()
	if hook != nil {
		hook(path)
	}
	return result
}

func (r *chownRecorder) seen() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := slices.Clone(r.paths)
	slices.Sort(out)
	return out
}

// hardenedDriver builds a driver that runs its containers as the test run-as
// user and records the hand-over instead of performing it.
func hardenedDriver(docker dockerAPI) (*Driver, *chownRecorder) {
	d := New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{
		WorkerID:             "w1",
		StopTimeout:          50 * time.Millisecond,
		ReadinessTimeout:     20 * time.Millisecond,
		ConflictPollInterval: time.Millisecond,
		ConflictDeadline:     100 * time.Millisecond,
		RunAsUID:             testRunAsUID,
		RunAsGID:             testRunAsGID,
	})
	rec := &chownRecorder{}
	d.chownAt = rec.chownAt
	return d, rec
}

// specIn is the default spec with its working set at dir.
func specIn(dir string) execution.InstanceSpec {
	s := spec()
	s.WorkingDir = dir
	return s
}

// workingSet builds a small working set — a nested world file and a symlink
// pointing at a file outside the tree — and returns its root and that outside
// file.
func workingSet(t *testing.T) (dir, outside string) {
	t.Helper()
	// Resolve symlinks so recorded paths (read back from /proc) compare equal.
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	outsideDir := filepath.Join(base, "outside")
	dir = filepath.Join(base, "s1")
	for _, p := range []string{outsideDir, filepath.Join(dir, "world", "region")} {
		if err := os.MkdirAll(p, 0o750); err != nil {
			t.Fatal(err)
		}
	}
	outside = filepath.Join(outsideDir, "secret")
	for _, p := range []string{outside, filepath.Join(dir, "world", "region", "r.0.0.mca")} {
		if err := os.WriteFile(p, []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(outside, filepath.Join(dir, "link")); err != nil {
		t.Fatal(err)
	}
	return dir, outside
}

func assertHardened(t *testing.T, got CreateSpec) {
	t.Helper()
	if got.User != wantRunAs {
		t.Errorf("%s: User = %q, want the run-as user %q", got.Name, got.User, wantRunAs)
	}
	if !reflect.DeepEqual(got.CapDrop, []string{"NET_RAW"}) {
		t.Errorf("%s: CapDrop = %v, want [NET_RAW]", got.Name, got.CapDrop)
	}
	// The JVM is given a home: the run-as uid has no passwd entry in the image.
	if len(got.Cmd) < 2 || got.Cmd[0] != "java" || got.Cmd[1] != "-Duser.home=/tmp" {
		t.Errorf("%s: Cmd = %v, want it to start java -Duser.home=/tmp", got.Name, got.Cmd)
	}
}

// The launch container runs as the configured unprivileged user and without CAP_NET_RAW.
func TestLaunchContainerRunsNonRootWithoutNetRaw(t *testing.T) {
	docker := newFakeDocker()
	d, _ := hardenedDriver(docker)

	if _, err := d.Start(context.Background(), spec()); err != nil {
		t.Fatalf("Start: %v", err)
	}

	assertHardened(t, docker.createSpec)
}

// The driver never resolves its run-as user to root, whatever its caller left unset: a configured user wins, an
// unset one is the Worker's own, and a root Worker gets the fixed unprivileged default.
func TestResolveRunAsNeverYieldsRoot(t *testing.T) {
	tests := []struct {
		name                string
		uid, gid, own, ownG int
		wantUID, wantGID    int
	}{
		{name: "configured", uid: 2000, gid: 3000, own: 0, ownG: 0, wantUID: 2000, wantGID: 3000},
		{name: "unset, unprivileged worker", own: 1000, ownG: 988, wantUID: 1000, wantGID: 988},
		{name: "unset, root worker", own: 0, ownG: 0, wantUID: DefaultRunAsUID, wantGID: DefaultRunAsGID},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			uid, gid := resolveRunAs(tc.uid, tc.gid, tc.own, tc.ownG)
			if uid != tc.wantUID || gid != tc.wantGID {
				t.Fatalf("resolveRunAs = %d:%d, want %d:%d", uid, gid, tc.wantUID, tc.wantGID)
			}
			if uid == 0 {
				t.Fatal("resolveRunAs yielded root")
			}
		})
	}
}

// A driver built with no run-as user still sets a non-root User on the container rather than leaving the image's
// default.
func TestRunAsUnsetStillRunsNonRoot(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	if _, err := d.Start(context.Background(), spec()); err != nil {
		t.Fatalf("Start: %v", err)
	}

	uid, gid := resolveRunAs(0, 0, os.Getuid(), os.Getgid())
	if got, want := docker.createSpec.User, fmt.Sprintf("%d:%d", uid, gid); got != want || uid == 0 {
		t.Fatalf("User = %q, want the non-root %q", got, want)
	}
}

// Every container of a Forge start, the install container, its retry, and the launch container the supervisor
// creates afterwards, is hardened the same way.
func TestForgeInstallRetryAndLaunchContainersAreHardened(t *testing.T) {
	prev := installRetryBackoff
	installRetryBackoff = []time.Duration{time.Millisecond, time.Millisecond}
	t.Cleanup(func() { installRetryBackoff = prev })

	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	docker.waitGates[installID] = make(chan waitResult, 2)
	docker.waitGates[installID] <- waitResult{code: 1, err: statusError{method: "POST", path: "/wait", code: 200, message: "install exited 1"}}
	docker.waitGates[installID] <- waitResult{code: 0}
	creates := 0
	docker.onCreateHook = func(CreateSpec) {
		creates++
		if creates == 2 { // the retry install "produces" the args file
			writeArgsfile(t, dir)
		}
	}
	d, _ := hardenedDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	docker.mu.Lock()
	specs := slices.Clone(docker.createSpecs)
	docker.mu.Unlock()
	if got := docker.names(); !reflect.DeepEqual(got, []string{installID, installID, "mcsd-s1"}) {
		t.Fatalf("created containers = %v, want install, install retry, launch", got)
	}
	for _, s := range specs {
		assertHardened(t, s)
	}

	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// A working set whose entries the run-as user does not own is handed to it before the container is created, the
// first start after an upgrade, where the tree still holds files the old root server wrote, and every start
// after a hydrate, which writes the tree as the Worker's own user. A symlink is re-owned itself and never
// followed.
func TestStartHandsWorkingSetToRunAsUserBeforeCreate(t *testing.T) {
	dir, outside := workingSet(t)
	docker := newForgeFakeDocker()
	d, rec := hardenedDriver(docker)
	var seenAtCreate []string
	docker.onCreateHook = func(CreateSpec) { seenAtCreate = rec.seen() }

	if _, err := d.Start(context.Background(), specIn(dir)); err != nil {
		t.Fatalf("Start: %v", err)
	}

	want := []string{
		dir,
		filepath.Join(dir, "link"),
		filepath.Join(dir, "world"),
		filepath.Join(dir, "world", "region"),
		filepath.Join(dir, "world", "region", "r.0.0.mca"),
	}
	if !reflect.DeepEqual(seenAtCreate, want) {
		t.Fatalf("handed over before create = %v, want %v", seenAtCreate, want)
	}
	if slices.Contains(seenAtCreate, outside) {
		t.Fatalf("the symlink's target %s was re-owned", outside)
	}
}

// Entries the run-as user already owns are left alone: a Worker that runs as the run-as user itself (an
// unprivileged host process) makes no chown call.
func TestStartLeavesAnAlreadyOwnedWorkingSetAlone(t *testing.T) {
	dir, _ := workingSet(t)
	docker := newFakeDocker()
	d, rec := hardenedDriver(docker)
	d.runAsUID, d.runAsGID = os.Getuid(), os.Getgid()

	if _, err := d.Start(context.Background(), specIn(dir)); err != nil {
		t.Fatalf("Start: %v", err)
	}

	if got := rec.seen(); len(got) != 0 {
		t.Fatalf("chown called for %v, want no call for an already-owned working set", got)
	}
}

// A working set that cannot be handed over fails the start before any container is created: a server that cannot
// write its world must not boot.
func TestStartFailsWhenWorkingSetCannotBeHandedOver(t *testing.T) {
	dir, _ := workingSet(t)
	docker := newFakeDocker()
	d, rec := hardenedDriver(docker)
	rec.err = unix.EPERM

	_, err := d.Start(context.Background(), specIn(dir))

	if !errors.Is(err, unix.EPERM) {
		t.Fatalf("Start error = %v, want the hand-over's permission error", err)
	}
	if docker.createCalls != 0 {
		t.Fatalf("createCalls = %d, want no container created", docker.createCalls)
	}
}

// The launch container created after a Forge install gets its own hand-over, so entries that appeared during the
// install, the install log the Worker itself writes, as its own user, reach the run-as user before the server
// starts.
func TestForgeLaunchHandsOverEntriesCreatedDuringInstall(t *testing.T) {
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	docker := newForgeFakeDocker()
	d, rec := hardenedDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	writeArgsfile(t, dir)
	docker.exit("mcsd-s1-install", 0, nil)
	drainTo(t, inst.Events(), execution.StateRunning)

	if !slices.Contains(rec.seen(), filepath.Join(dir, filepath.FromSlash(forgeArgsRel))) {
		t.Fatalf("handed over = %v, want the entry created during the install among them", rec.seen())
	}

	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// A container of the server that is, or may be, alive holds the working set: once it has outlasted the wait, the
// start is refused with no ownership change and no create. That covers an orphan the startup sweep failed to
// stop, which the Manager has no record of, and every state the driver cannot vouch for.
func TestStartRefusesHandOverWhileAContainerOfTheServerMayBeAlive(t *testing.T) {
	for _, state := range []string{"running", "paused", "restarting", "removing", "something-new", ""} {
		t.Run("state "+state, func(t *testing.T) {
			dir, _ := workingSet(t)
			docker := newFakeDocker()
			docker.listResult = []Container{
				{ID: "old-install", Name: "/mcsd-s1-install", State: "exited"},
				{ID: "orphan", Name: "/mcsd-s1", State: state},
			}
			d, rec := hardenedDriver(docker)

			_, err := d.Start(context.Background(), specIn(dir))

			if err == nil || !strings.Contains(err.Error(), "/mcsd-s1") {
				t.Fatalf("Start error = %v, want a refusal naming the live container", err)
			}
			if got := rec.seen(); len(got) != 0 {
				t.Fatalf("ownership changed for %v, want none while a container may be alive", got)
			}
			if docker.createCalls != 0 {
				t.Fatalf("createCalls = %d, want no container created", docker.createCalls)
			}
		})
	}
}

// When the daemon cannot say which containers exist, liveness is unknown and the start is refused the same way.
func TestStartRefusesHandOverWhenLivenessIsUnknown(t *testing.T) {
	dir, _ := workingSet(t)
	docker := newFakeDocker()
	docker.listErr = errors.New("containerdriver: GET /containers/json: connection refused")
	d, rec := hardenedDriver(docker)

	_, err := d.Start(context.Background(), specIn(dir))

	if !errors.Is(err, docker.listErr) {
		t.Fatalf("Start error = %v, want the list failure", err)
	}
	if got := rec.seen(); len(got) != 0 || docker.createCalls != 0 {
		t.Fatalf("chowned %v, createCalls %d; want neither", got, docker.createCalls)
	}
}

// listScript is a dockerAPI whose List answers follow a script (the last answer
// repeats), over an otherwise ordinary fake.
type listScript struct {
	*fakeDocker
	mu      sync.Mutex
	answers [][]Container
	// onList, when set, runs on each List with the number of answers still queued.
	onList func(answersLeft int)
}

func (l *listScript) List(context.Context, string, string) ([]Container, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.onList != nil {
		l.onList(len(l.answers))
	}
	answer := l.answers[0]
	if len(l.answers) > 1 {
		l.answers = l.answers[1:]
	}
	return answer, nil
}

// The previous container of a restart is waited out rather than refused: the daemon still lists it as running
// for a moment after its exit, then as removing while the exit-watcher reaps it. Nothing is handed over until it
// is gone.
func TestStartWaitsOutThePreviousContainerOfARestart(t *testing.T) {
	dir, _ := workingSet(t)
	docker := &listScript{fakeDocker: newFakeDocker(), answers: [][]Container{
		{{ID: "old", Name: "/mcsd-s1", State: "running"}},
		{{ID: "old", Name: "/mcsd-s1", State: "removing"}},
		nil,
	}}
	d, rec := hardenedDriver(docker)
	var chownedWhileAlive []string
	docker.onList = func(answersLeft int) {
		if answersLeft > 1 {
			chownedWhileAlive = append(chownedWhileAlive, rec.seen()...)
		}
	}

	if _, err := d.Start(context.Background(), specIn(dir)); err != nil {
		t.Fatalf("Start: %v", err)
	}

	if len(chownedWhileAlive) != 0 {
		t.Fatalf("re-owned %v while the previous container was still listed alive", chownedWhileAlive)
	}
	if len(rec.seen()) == 0 || docker.createCalls != 1 {
		t.Fatalf("chowned %v, createCalls %d; want the hand-over and one create", rec.seen(), docker.createCalls)
	}
}

// A directory swapped for a symlink in the middle of the walk cannot lead it out of the working set: the walk
// descends by descriptor and refuses the link. The swap happens at the worst moment, right after the directory
// itself was re-owned, before the walk enters it.
func TestHandOverCannotBeRedirectedBySwappingADirectoryForASymlink(t *testing.T) {
	dir, outside := workingSet(t)
	world := filepath.Join(dir, "world")
	d, rec := hardenedDriver(newFakeDocker())
	rec.before = func(path string) {
		if path != world {
			return
		}
		if err := os.Rename(world, world+".real"); err != nil {
			t.Error(err)
		}
		if err := os.Symlink(filepath.Dir(outside), world); err != nil {
			t.Error(err)
		}
	}

	_, err := d.handOverWorkingSet(context.Background(), dir)

	if err == nil {
		t.Fatal("hand-over succeeded across a swapped directory, want an error")
	}
	for _, p := range rec.seen() {
		if !strings.HasPrefix(p, dir+string(os.PathSeparator)) && p != dir {
			t.Fatalf("re-owned %s, outside the working set %s", p, dir)
		}
	}
}

// Once the walk is inside a directory, moving that directory aside and putting a symlink in its place changes
// nothing: its entries are still re-owned where they are, relative to the open descriptor, and nothing behind
// the link is touched.
func TestHandOverStaysInAnOpenedDirectoryThatIsSwappedBehindIt(t *testing.T) {
	dir, outside := workingSet(t)
	world := filepath.Join(dir, "world")
	d, rec := hardenedDriver(newFakeDocker())
	rec.before = func(path string) {
		if path != filepath.Join(world, "region") {
			return
		}
		if err := os.Rename(world, world+".real"); err != nil {
			t.Error(err)
		}
		if err := os.Symlink(filepath.Dir(outside), world); err != nil {
			t.Error(err)
		}
	}

	if _, err := d.handOverWorkingSet(context.Background(), dir); err != nil {
		t.Fatalf("hand-over: %v", err)
	}

	seen := rec.seen()
	if !slices.Contains(seen, filepath.Join(world+".real", "region", "r.0.0.mca")) {
		t.Fatalf("re-owned %v, want the moved directory's own file among them", seen)
	}
	if slices.Contains(seen, outside) || slices.Contains(seen, filepath.Dir(outside)) {
		t.Fatalf("re-owned %v, which reaches behind the planted symlink", seen)
	}
}

// Only a working dir that is absent from the start is tolerated. An entry that vanishes once the walk has begun
// leaves the tree partly handed over, and that is reported, not swallowed.
func TestHandOverReportsAnIncompleteWalk(t *testing.T) {
	t.Run("absent working dir", func(t *testing.T) {
		d, _ := hardenedDriver(newFakeDocker())
		stats, err := d.handOverWorkingSet(context.Background(), filepath.Join(t.TempDir(), "absent"))
		if err != nil || stats.entries != 0 {
			t.Fatalf("hand-over = %+v, %v; want nothing done and no error", stats, err)
		}
	})

	t.Run("entry vanishes mid-walk", func(t *testing.T) {
		dir, _ := workingSet(t)
		d, rec := hardenedDriver(newFakeDocker())
		// The root's two entries are listed before either is visited; while the
		// first is being re-owned, the other one disappears.
		rec.before = func(path string) {
			switch path {
			case filepath.Join(dir, "link"):
				_ = os.RemoveAll(filepath.Join(dir, "world"))
			case filepath.Join(dir, "world"):
				_ = os.Remove(filepath.Join(dir, "link"))
			}
		}

		_, err := d.handOverWorkingSet(context.Background(), dir)

		if !errors.Is(err, unix.ENOENT) {
			t.Fatalf("hand-over error = %v, want the vanished entry reported", err)
		}
	})
}

// The walk stops when the start is cancelled.
func TestHandOverStopsWhenCancelled(t *testing.T) {
	dir, _ := workingSet(t)
	d, rec := hardenedDriver(newFakeDocker())
	ctx, cancel := context.WithCancel(context.Background())
	rec.before = func(string) { cancel() } // cancelled while the root is being re-owned

	_, err := d.handOverWorkingSet(ctx, dir)

	if !errors.Is(err, context.Canceled) {
		t.Fatalf("hand-over error = %v, want context.Canceled", err)
	}
	if got := rec.seen(); !reflect.DeepEqual(got, []string{dir}) {
		t.Fatalf("re-owned %v, want the walk to stop after the root", got)
	}
}

// The hand-over reports how much it visited and changed, which the driver logs with its duration.
func TestHandOverCountsEntries(t *testing.T) {
	dir, _ := workingSet(t)
	d, _ := hardenedDriver(newFakeDocker())

	stats, err := d.handOverWorkingSet(context.Background(), dir)

	// root, link, world, world/region, world/region/r.0.0.mca
	if err != nil || stats.entries != 5 || stats.changed != 5 {
		t.Fatalf("hand-over = %+v, %v; want 5 entries, 5 changed", stats, err)
	}
}
