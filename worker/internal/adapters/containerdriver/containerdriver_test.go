package containerdriver

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// fakeDocker is an in-memory dockerAPI. Wait blocks until the test (or a
// stop/kill) releases it, so no Docker daemon runs in CI.
type fakeDocker struct {
	mu sync.Mutex

	createSpec CreateSpec
	createErr  error
	startErr   error

	stopCalled bool
	stopNoExit bool
	killCalled bool
	removed    []string

	listResult []Container
	listErr    error
	removeErr  error

	exitCode int64
	exitErr  error
	exited   chan struct{}
}

func newFakeDocker() *fakeDocker {
	return &fakeDocker{exited: make(chan struct{})}
}

func (f *fakeDocker) Create(_ context.Context, spec CreateSpec) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.createErr != nil {
		return "", f.createErr
	}
	f.createSpec = spec
	return "container-1", nil
}

func (f *fakeDocker) Start(_ context.Context, _ string) error {
	return f.startErr
}

func (f *fakeDocker) Stop(_ context.Context, _ string, _ time.Duration) error {
	f.mu.Lock()
	f.stopCalled = true
	noExit := f.stopNoExit
	f.mu.Unlock()
	if !noExit {
		f.exit(0, nil)
	}
	return nil
}

func (f *fakeDocker) Kill(_ context.Context, _ string) error {
	f.mu.Lock()
	f.killCalled = true
	f.mu.Unlock()
	f.exit(137, nil)
	return nil
}

func (f *fakeDocker) Wait(_ context.Context, _ string) (int64, error) {
	<-f.exited
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.exitCode, f.exitErr
}

func (f *fakeDocker) Remove(_ context.Context, id string) error {
	f.mu.Lock()
	f.removed = append(f.removed, id)
	f.mu.Unlock()
	return f.removeErr
}

func (f *fakeDocker) List(_ context.Context, _, _ string) ([]Container, error) {
	return f.listResult, f.listErr
}

// exit releases Wait with the given code/error, simulating container exit.
func (f *fakeDocker) exit(code int64, err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	select {
	case <-f.exited:
	default:
		f.exitCode = code
		f.exitErr = err
		close(f.exited)
	}
}

func (f *fakeDocker) stopWasCalled() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.stopCalled
}

func (f *fakeDocker) killWasCalled() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.killCalled
}

// fakeControl is an in-memory ServerControl.
type fakeControl struct {
	stopCalled bool
	onStop     func()
}

func (c *fakeControl) Execute(_ context.Context, line string) (string, error) {
	if line == "stop" {
		c.stopCalled = true
		if c.onStop != nil {
			c.onStop()
		}
	}
	return "", nil
}

func (c *fakeControl) Close() error { return nil }

func images() *ImageSelector {
	return NewImageSelector(map[int]string{21: "eclipse-temurin:21-jre"})
}

func newTestDriver(docker *fakeDocker, ctrl execution.ServerControl, ctrlErr error) *Driver {
	return New(docker, images(), func(context.Context, execution.InstanceSpec) (execution.ServerControl, error) {
		return ctrl, ctrlErr
	}, Options{WorkerID: "w1", StopTimeout: 50 * time.Millisecond})
}

func spec() execution.InstanceSpec {
	return execution.InstanceSpec{ServerID: "s1", WorkingDir: "/scratch/s1", MinecraftVersion: "1.21", JarRelpath: "server.jar"}
}

// drainClosed reads ch until it closes, so any goroutine writing to it finishes.
func drainClosed(ch <-chan execution.StatusEvent) {
	for range ch { //nolint:revive // intentionally draining to channel close
	}
}

// drainTo collects status events until it sees want or times out.
func drainTo(t *testing.T, ch <-chan execution.StatusEvent, want execution.ServerState) {
	t.Helper()
	deadline := time.After(2 * time.Second)
	for {
		select {
		case ev, ok := <-ch:
			if !ok {
				t.Fatalf("event channel closed before reaching %v", want)
			}
			if ev.State == want {
				return
			}
		case <-deadline:
			t.Fatalf("timed out waiting for %v", want)
		}
	}
}

func TestStartReachesRunning(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
	if inst.Status() != execution.StateRunning {
		t.Fatalf("Status = %v, want running", inst.Status())
	}
}

// Start wires the working-dir bind mount, the deterministic name/labels, and the
// resolved base image onto the create spec.
func TestStartCreateSpec(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	if _, err := d.Start(context.Background(), spec()); err != nil {
		t.Fatalf("Start: %v", err)
	}

	got := docker.createSpec
	if got.Name != "mcsd-s1" {
		t.Fatalf("Name = %q, want mcsd-s1", got.Name)
	}
	if got.Image != "eclipse-temurin:21-jre" {
		t.Fatalf("Image = %q, want eclipse-temurin:21-jre", got.Image)
	}
	if len(got.Binds) != 1 || got.Binds[0] != "/scratch/s1:/data" {
		t.Fatalf("Binds = %v, want [/scratch/s1:/data]", got.Binds)
	}
	if got.Labels[labelWorkerID] != "w1" || got.Labels[labelServerID] != "s1" {
		t.Fatalf("Labels = %v, want worker/server labels", got.Labels)
	}
}

func TestCrashEmitsCrashed(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	// Container exits unexpectedly → crashed.
	docker.exit(1, nil)
	drainTo(t, inst.Events(), execution.StateCrashed)
}

// The crashed terminal event is emitted exactly once.
func TestCrashEmitsCrashedOnce(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
	docker.exit(1, nil)

	crashed := 0
	for ev := range inst.Events() {
		if ev.State == execution.StateCrashed {
			crashed++
		}
	}
	if crashed != 1 {
		t.Fatalf("crashed events = %d, want 1", crashed)
	}
}

// A graceful stop prefers RCON "stop"; when it succeeds the container exits and
// the instance reaches stopped without docker stop/kill.
func TestGracefulStopViaRCON(t *testing.T) {
	docker := newFakeDocker()
	ctrl := &fakeControl{onStop: func() { docker.exit(0, nil) }}
	d := newTestDriver(docker, ctrl, nil)

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	if !ctrl.stopCalled {
		t.Fatal("expected RCON stop to be called")
	}
	if docker.stopWasCalled() {
		t.Fatal("docker stop should not be called when RCON stop succeeds")
	}
}

// When RCON is unavailable, a graceful stop falls back to docker stop.
func TestGracefulStopFallsBackToDockerStop(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("rcon dial failed"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	if !docker.stopWasCalled() {
		t.Fatal("expected docker stop fallback")
	}
	if docker.killWasCalled() {
		t.Fatal("docker kill should not be needed when docker stop exits the container")
	}
}

// When the container ignores docker stop past the timeout, the driver escalates
// to docker kill.
func TestGracefulStopEscalatesToKill(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("rcon dial failed"))
	// docker stop does not exit the container; only kill does.
	docker.stopNoExit = true

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	if !docker.killWasCalled() {
		t.Fatal("expected docker kill escalation")
	}
}

// A forced stop skips RCON and goes straight to docker stop.
func TestForcedStopSkipsRCON(t *testing.T) {
	docker := newFakeDocker()
	ctrl := &fakeControl{onStop: func() { docker.exit(0, nil) }}
	d := newTestDriver(docker, ctrl, nil)

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if err := inst.Stop(context.Background(), false); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	if ctrl.stopCalled {
		t.Fatal("forced stop must not use RCON")
	}
	if !docker.stopWasCalled() {
		t.Fatal("forced stop should call docker stop")
	}
}

// Stopping a crashed instance is a prompt no-op success.
func TestStopOnCrashedIsPromptNoOp(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	docker.exit(1, nil)
	drainTo(t, inst.Events(), execution.StateCrashed)

	done := make(chan error, 1)
	start := time.Now()
	go func() { done <- inst.Stop(context.Background(), true) }()
	select {
	case stopErr := <-done:
		if stopErr != nil {
			t.Fatalf("Stop on crashed instance: %v", stopErr)
		}
	case <-time.After(time.Second):
		t.Fatal("Stop on crashed instance did not return promptly")
	}
	if elapsed := time.Since(start); elapsed >= 100*time.Millisecond {
		t.Fatalf("Stop spun the timeout: took %v", elapsed)
	}
	if docker.stopWasCalled() || docker.killWasCalled() {
		t.Fatal("Stop should not act on an already-dead container")
	}
}

func TestStartImageSelectFailure(t *testing.T) {
	docker := newFakeDocker()
	// No image configured for the version's Java major.
	d := New(docker, NewImageSelector(map[int]string{8: "old"}), func(context.Context, execution.InstanceSpec) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{WorkerID: "w1", StopTimeout: 50 * time.Millisecond})

	if _, err := d.Start(context.Background(), spec()); err == nil {
		t.Fatal("expected Start to fail when no image is configured")
	}
}

func TestStartCreateFailure(t *testing.T) {
	docker := newFakeDocker()
	docker.createErr = errors.New("daemon unreachable")
	d := newTestDriver(docker, nil, nil)

	if _, err := d.Start(context.Background(), spec()); err == nil {
		t.Fatal("expected Start to fail when create fails")
	}
}

// A failed start removes the created-but-unstarted container.
func TestStartFailureCleansUpContainer(t *testing.T) {
	docker := newFakeDocker()
	docker.startErr = errors.New("start refused")
	d := newTestDriver(docker, nil, nil)

	if _, err := d.Start(context.Background(), spec()); err == nil {
		t.Fatal("expected Start to fail when start fails")
	}
	if len(docker.removed) != 1 || docker.removed[0] != "container-1" {
		t.Fatalf("removed = %v, want [container-1]", docker.removed)
	}
}

// After a clean exit the container is removed.
func TestExitRemovesContainer(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	docker.exit(1, nil)
	// Drain to channel close so supervise's Remove has run.
	drainClosed(inst.Events())
	docker.mu.Lock()
	removed := append([]string(nil), docker.removed...)
	docker.mu.Unlock()
	if len(removed) != 1 || removed[0] != "container-1" {
		t.Fatalf("removed = %v, want [container-1]", removed)
	}
}

// Sweep removes every container the daemon reports for this Worker.
func TestSweepRemovesWorkerContainers(t *testing.T) {
	docker := newFakeDocker()
	docker.listResult = []Container{{ID: "a", Name: "/mcsd-s1"}, {ID: "b", Name: "/mcsd-s2"}}
	d := newTestDriver(docker, nil, nil)

	if err := d.Sweep(context.Background()); err != nil {
		t.Fatalf("Sweep: %v", err)
	}
	if len(docker.removed) != 2 {
		t.Fatalf("removed = %v, want 2 containers", docker.removed)
	}
}

// Sweep surfaces a list error rather than silently skipping recovery.
func TestSweepListError(t *testing.T) {
	docker := newFakeDocker()
	docker.listErr = errors.New("list failed")
	d := newTestDriver(docker, nil, nil)

	if err := d.Sweep(context.Background()); err == nil {
		t.Fatal("expected Sweep to return the list error")
	}
}
