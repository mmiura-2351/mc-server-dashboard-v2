package containerdriver

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"sync"
	"testing"
	"time"

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

// lchownRecorder stands in for os.Lchown, which only root may use to give a file
// away. It records each call instead.
type lchownRecorder struct {
	mu    sync.Mutex
	paths []string
	err   error
}

func (r *lchownRecorder) lchown(path string, uid, gid int) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if uid != testRunAsUID || gid != testRunAsGID {
		return errors.New("lchown called with an identity other than the run-as user")
	}
	r.paths = append(r.paths, path)
	return r.err
}

func (r *lchownRecorder) seen() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return slices.Clone(r.paths)
}

// hardenedDriver builds a driver that runs its containers as the test run-as
// user and records the hand-over instead of performing it.
func hardenedDriver(docker dockerAPI) (*Driver, *lchownRecorder) {
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
	rec := &lchownRecorder{}
	d.lchown = rec.lchown
	return d, rec
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

// The launch container runs as the configured unprivileged user and without
// CAP_NET_RAW (issue #2600).
func TestLaunchContainerRunsNonRootWithoutNetRaw(t *testing.T) {
	docker := newFakeDocker()
	d, _ := hardenedDriver(docker)

	if _, err := d.Start(context.Background(), spec()); err != nil {
		t.Fatalf("Start: %v", err)
	}

	assertHardened(t, docker.createSpec)
}

// With no run-as user configured the driver falls back to the Worker process's
// own uid:gid, never to the image's default user (issue #2600).
func TestRunAsDefaultsToTheWorkersOwnUser(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	if _, err := d.Start(context.Background(), spec()); err != nil {
		t.Fatalf("Start: %v", err)
	}

	want := fmt.Sprintf("%d:%d", os.Getuid(), os.Getgid())
	if got := docker.createSpec.User; got != want {
		t.Fatalf("User = %q, want the Worker's own %q", got, want)
	}
}

// Every container of a Forge start — the install container, its retry, and the
// launch container the supervisor creates afterwards — is hardened the same way
// (issue #2600).
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

// A working set whose entries the run-as user does not own is handed to it before
// the container is created — the first start after an upgrade, where the tree
// still holds files the old root server wrote, and every start after a hydrate,
// which writes the tree as the Worker's own user. A symlink is re-owned itself and
// never followed (issue #2600).
func TestStartHandsWorkingSetToRunAsUserBeforeCreate(t *testing.T) {
	outside := filepath.Join(t.TempDir(), "outside.txt")
	if err := os.WriteFile(outside, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "world", "region"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "world", "region", "r.0.0.mca"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(dir, "link")); err != nil {
		t.Fatal(err)
	}

	docker := newForgeFakeDocker()
	d, rec := hardenedDriver(docker)
	var seenAtCreate []string
	docker.onCreateHook = func(CreateSpec) { seenAtCreate = rec.seen() }

	s := spec()
	s.WorkingDir = dir
	if _, err := d.Start(context.Background(), s); err != nil {
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
}

// Entries the run-as user already owns are left alone: a Worker that runs as the
// run-as user itself (a non-root host process) makes no chown call (issue #2600).
func TestStartLeavesAnAlreadyOwnedWorkingSetAlone(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "server.jar"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))
	rec := &lchownRecorder{}
	d.lchown = rec.lchown

	s := spec()
	s.WorkingDir = dir
	if _, err := d.Start(context.Background(), s); err != nil {
		t.Fatalf("Start: %v", err)
	}

	if got := rec.seen(); len(got) != 0 {
		t.Fatalf("lchown called for %v, want no call for an already-owned working set", got)
	}
}

// A working set that cannot be handed over fails the start before any container
// is created: a server that cannot write its world must not boot (issue #2600).
func TestStartFailsWhenWorkingSetCannotBeHandedOver(t *testing.T) {
	dir := t.TempDir()
	docker := newFakeDocker()
	d, rec := hardenedDriver(docker)
	rec.err = os.ErrPermission

	s := spec()
	s.WorkingDir = dir
	_, err := d.Start(context.Background(), s)

	if !errors.Is(err, os.ErrPermission) {
		t.Fatalf("Start error = %v, want the hand-over's permission error", err)
	}
	if docker.createCalls != 0 {
		t.Fatalf("createCalls = %d, want no container created", docker.createCalls)
	}
}

// The launch container created after a Forge install gets its own hand-over, so
// entries that appeared during the install — the install log the Worker itself
// writes, as its own user — reach the run-as user before the server starts
// (issue #2600).
func TestForgeLaunchHandsOverEntriesCreatedDuringInstall(t *testing.T) {
	dir := t.TempDir()
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
