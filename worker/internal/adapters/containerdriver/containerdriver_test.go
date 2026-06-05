package containerdriver

import (
	"context"
	"errors"
	"io"
	"strings"
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
	// conflictsLeft makes the next N Create calls return errNameConflict before a
	// success, modelling the create racing the async removal of the exited
	// container (issue #226). createCalls counts every Create call.
	conflictsLeft int
	createCalls   int

	// inspectInfo / inspectErr are returned by Inspect when resolving a conflict.
	// inspectSteps, when non-empty, scripts the wait-for-name-free loop: each
	// Inspect call pops the next step (the last step repeats once exhausted), so a
	// test can model the name flickering as the daemon finishes teardown (issue
	// #233).
	inspectInfo  ContainerInfo
	inspectErr   error
	inspectSteps []inspectStep

	stopCalled bool
	stopNoExit bool
	killCalled bool
	removed    []string

	listResult []Container
	listErr    error
	removeErr  error
	// removeErrs, when non-empty, scripts per-call Remove results for the loop
	// (the last entry repeats once exhausted); empty falls back to removeErr.
	removeErrs []error

	exitCode int64
	exitErr  error
	exited   chan struct{}

	// logBody is the multiplexed stream Logs returns; logErr forces a Logs error.
	logBody io.Reader
	logErr  error
	// stats is returned by Stats; statsErr forces a Stats error.
	stats    ContainerStats
	statsErr error
}

// inspectStep is one scripted Inspect result for the wait-for-name-free loop.
type inspectStep struct {
	info ContainerInfo
	err  error
}

func newFakeDocker() *fakeDocker {
	return &fakeDocker{exited: make(chan struct{})}
}

func (f *fakeDocker) Logs(_ context.Context, _ string) (io.ReadCloser, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.logErr != nil {
		return nil, f.logErr
	}
	body := f.logBody
	if body == nil {
		body = strings.NewReader("")
	}
	return io.NopCloser(body), nil
}

func (f *fakeDocker) Stats(_ context.Context, _ string) (ContainerStats, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.stats, f.statsErr
}

func (f *fakeDocker) Create(_ context.Context, spec CreateSpec) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.createCalls++
	if f.createErr != nil {
		return "", f.createErr
	}
	if f.conflictsLeft > 0 {
		f.conflictsLeft--
		return "", errNameConflict
	}
	f.createSpec = spec
	return "container-1", nil
}

func (f *fakeDocker) Inspect(_ context.Context, _ string) (ContainerInfo, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.inspectSteps) > 0 {
		step := f.inspectSteps[0]
		if len(f.inspectSteps) > 1 {
			f.inspectSteps = f.inspectSteps[1:]
		}
		return step.info, step.err
	}
	return f.inspectInfo, f.inspectErr
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
	defer f.mu.Unlock()
	f.removed = append(f.removed, id)
	if len(f.removeErrs) > 0 {
		err := f.removeErrs[0]
		if len(f.removeErrs) > 1 {
			f.removeErrs = f.removeErrs[1:]
		}
		return err
	}
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

// The container's demuxed log stream flows through to Logs() as LogEvents; the
// log channel closes after the container exits and supervise tears down.
func TestContainerLogCaptureFlowsToLogs(t *testing.T) {
	docker := newFakeDocker()
	var body strings.Builder
	body.Write(frame(dockerStreamStdout, "server starting\n"))
	body.Write(frame(dockerStreamStderr, "a warning\n"))
	docker.logBody = strings.NewReader(body.String())

	d := newTestDriver(docker, nil, errors.New("no rcon"))
	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	src, ok := inst.(execution.LogSource)
	if !ok {
		t.Fatal("container instance should be a LogSource")
	}

	// Exit the container so supervise ends the follow, drains, and closes the pump.
	docker.exit(0, nil)

	var stdout, stderr []string
	for ev := range src.Logs() {
		switch ev.Stream {
		case execution.LogStreamStdout:
			stdout = append(stdout, ev.Line)
		case execution.LogStreamStderr:
			stderr = append(stderr, ev.Line)
		}
	}
	if len(stdout) != 1 || stdout[0] != "server starting" {
		t.Fatalf("stdout = %v", stdout)
	}
	if len(stderr) != 1 || stderr[0] != "a warning" {
		t.Fatalf("stderr = %v", stderr)
	}
}

// Sample forwards the Engine stats sample as a MetricsSample; a stats error
// surfaces so the manager can fall back to up-only.
func TestContainerSample(t *testing.T) {
	docker := newFakeDocker()
	docker.stats = ContainerStats{CPUMillis: 250, MemoryBytes: 1 << 20}
	d := newTestDriver(docker, nil, errors.New("no rcon"))
	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer docker.exit(0, nil)

	stats, ok := inst.(execution.StatsSource)
	if !ok {
		t.Fatal("container instance should be a StatsSource")
	}
	got, err := stats.Sample(context.Background())
	if err != nil {
		t.Fatalf("Sample: %v", err)
	}
	if got.CPUMillis != 250 || got.MemoryBytes != 1<<20 || got.ServerID != "s1" {
		t.Fatalf("sample = %+v", got)
	}

	docker.statsErr = errors.New("daemon unreachable")
	if _, err := stats.Sample(context.Background()); err == nil {
		t.Fatal("expected Sample to surface a stats error")
	}
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
	return New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return ctrl, ctrlErr
	}, Options{
		WorkerID:    "w1",
		StopTimeout: 50 * time.Millisecond,
		GameBindIP:  "0.0.0.0",
		// Short conflict-loop timing keeps the wait-for-name-free tests fast.
		ConflictPollInterval: time.Millisecond,
		ConflictDeadline:     100 * time.Millisecond,
	})
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

// Start publishes the game port on the configured GameBindIP while RCON stays on
// loopback (a control channel that must not be exposed).
func TestStartGamePortBindIP(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	if _, err := d.Start(context.Background(), spec()); err != nil {
		t.Fatalf("Start: %v", err)
	}

	var game, rcon *PortMapping
	for i := range docker.createSpec.Ports {
		switch docker.createSpec.Ports[i].ContainerPort {
		case defaultGamePort:
			game = &docker.createSpec.Ports[i]
		case defaultRCONPort:
			rcon = &docker.createSpec.Ports[i]
		}
	}
	if game == nil || rcon == nil {
		t.Fatalf("Ports = %v, want game and rcon mappings", docker.createSpec.Ports)
	}
	if game.HostIP != "0.0.0.0" {
		t.Errorf("game HostIP = %q, want configured 0.0.0.0", game.HostIP)
	}
	if rcon.HostIP != "127.0.0.1" {
		t.Errorf("rcon HostIP = %q, want loopback 127.0.0.1", rcon.HostIP)
	}
}

// When driver.container.network is unset the driver attaches no network and
// publishes RCON on the host loopback (current behavior); RconHost is empty so
// the RCON dial falls back to loopback.
func TestStartNoNetworkPublishesRCON(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	if _, err := d.Start(context.Background(), spec()); err != nil {
		t.Fatalf("Start: %v", err)
	}

	if docker.createSpec.Network != "" {
		t.Errorf("Network = %q, want empty when unset", docker.createSpec.Network)
	}
	if !hasPort(docker.createSpec.Ports, defaultRCONPort) {
		t.Errorf("Ports = %v, want an RCON publication when no network", docker.createSpec.Ports)
	}
	if got := d.RconHost("s1"); got != "" {
		t.Errorf("RconHost = %q, want empty (loopback) when no network", got)
	}
}

// When driver.container.network is set the driver attaches the container to that
// network, DROPS the RCON host publication (RCON never leaves the docker
// network), keeps the game-port publication, and surfaces the RCON dial host as
// the container name (issue #218).
func TestStartWithNetworkDropsRCONPublication(t *testing.T) {
	docker := newFakeDocker()
	d := New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{WorkerID: "w1", StopTimeout: 50 * time.Millisecond, GameBindIP: "0.0.0.0", Network: "mcsd"})

	if _, err := d.Start(context.Background(), spec()); err != nil {
		t.Fatalf("Start: %v", err)
	}

	if docker.createSpec.Network != "mcsd" {
		t.Errorf("Network = %q, want mcsd", docker.createSpec.Network)
	}
	if hasPort(docker.createSpec.Ports, defaultRCONPort) {
		t.Errorf("Ports = %v, want NO RCON publication when network is set", docker.createSpec.Ports)
	}
	if !hasPort(docker.createSpec.Ports, defaultGamePort) {
		t.Errorf("Ports = %v, want the game-port publication kept", docker.createSpec.Ports)
	}
	if got := d.RconHost("s1"); got != "mcsd-s1" {
		t.Errorf("RconHost = %q, want container name mcsd-s1", got)
	}
}

// A graceful stop with a network configured opens RCON at the container name, not
// the loopback, so the in-band stop reaches the MC container across the network.
func TestGracefulStopUsesContainerRconHost(t *testing.T) {
	docker := newFakeDocker()
	var gotHost string
	d := New(docker, images(), func(_ context.Context, _ execution.InstanceSpec, rconHost string) (execution.ServerControl, error) {
		gotHost = rconHost
		return &fakeControl{}, nil
	}, Options{WorkerID: "w1", StopTimeout: 50 * time.Millisecond, Network: "mcsd"})

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
	go drainClosed(inst.Events())

	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if gotHost != "mcsd-s1" {
		t.Errorf("graceful-stop rcon host = %q, want mcsd-s1", gotHost)
	}
}

// hasPort reports whether ports publishes the given container port.
func hasPort(ports []PortMapping, containerPort string) bool {
	for _, p := range ports {
		if p.ContainerPort == containerPort {
			return true
		}
	}
	return false
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

// A container that exits mid-graceful-stop releases the Stop wait via
// close(exited) rather than timing out. The stop is already in flight, so
// supervise records the terminal state as stopped, and the driver never escalates
// to docker stop/kill.
func TestStopWaitSatisfiedByCrash(t *testing.T) {
	docker := newFakeDocker()
	// RCON "stop" does not exit the container immediately; it exits shortly after,
	// and waitExit completes when supervise closes exited.
	ctrl := &fakeControl{onStop: func() {
		go func() {
			time.Sleep(5 * time.Millisecond)
			docker.exit(0, nil)
		}()
	}}
	d := newTestDriver(docker, ctrl, nil)

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	start := time.Now()
	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if elapsed := time.Since(start); elapsed >= 50*time.Millisecond {
		t.Fatalf("Stop timed out instead of completing on exit: took %v", elapsed)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	if docker.stopWasCalled() || docker.killWasCalled() {
		t.Fatal("Stop should not escalate to docker stop/kill when the container exits during the wait")
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
	d := New(docker, NewImageSelector(map[int]string{8: "old"}), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
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

// The wait-for-name-free loop (issue #233) heals every interleaving of the
// create-vs-async-remover race on the deterministic name. The five tests below
// drive the loop branches; a foreign label or a running own container still
// fails immediately (the conservative posture is unchanged).

// The async exit-watcher remover wins the race: the create 409s, the inspect
// finds the name already gone (404), so the driver retries the create and Start
// reaches running.
func TestStartConflictLoopInspect404ThenCreateSucceeds(t *testing.T) {
	docker := newFakeDocker()
	docker.conflictsLeft = 1
	docker.inspectSteps = []inspectStep{{err: errNotFound}}
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	docker.mu.Lock()
	calls, removed := docker.createCalls, docker.removed
	docker.mu.Unlock()
	if calls != 2 {
		t.Fatalf("createCalls = %d, want 2 (conflict then retry)", calls)
	}
	if len(removed) != 0 {
		t.Fatalf("removed = %v, want none (the container was already gone)", removed)
	}
}

// The third race variant: the inspect finds the exited container, the driver
// issues a remove, but the watcher's removal is already in flight so the DELETE
// 409s ("removal in progress"). That counts as progress: the loop keeps polling,
// the next inspect 404s, and the retried create succeeds.
func TestStartConflictLoopRemoveInProgressThen404Succeeds(t *testing.T) {
	docker := newFakeDocker()
	docker.conflictsLeft = 1
	docker.inspectSteps = []inspectStep{
		{info: ContainerInfo{ID: "stale-1", Labels: map[string]string{labelWorkerID: "w1"}, Running: false}},
		{err: errNotFound},
	}
	docker.removeErrs = []error{errRemovalInProgress}
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	docker.mu.Lock()
	calls, removed := docker.createCalls, append([]string(nil), docker.removed...)
	docker.mu.Unlock()
	if calls != 2 {
		t.Fatalf("createCalls = %d, want 2 (conflict then retry)", calls)
	}
	if len(removed) != 1 || removed[0] != "stale-1" {
		t.Fatalf("removed = %v, want [stale-1]", removed)
	}
}

// The inspect finds THIS Worker's exited container; the driver removes it, the
// name frees, and the retried create succeeds.
func TestStartConflictLoopRemovesOwnStoppedThenSucceeds(t *testing.T) {
	docker := newFakeDocker()
	docker.conflictsLeft = 1
	docker.inspectSteps = []inspectStep{
		{info: ContainerInfo{ID: "stale-1", Labels: map[string]string{labelWorkerID: "w1"}, Running: false}},
		{err: errNotFound},
	}
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	docker.mu.Lock()
	calls, removed := docker.createCalls, append([]string(nil), docker.removed...)
	docker.mu.Unlock()
	if calls != 2 {
		t.Fatalf("createCalls = %d, want 2 (conflict then retry)", calls)
	}
	if len(removed) != 1 || removed[0] != "stale-1" {
		t.Fatalf("removed = %v, want [stale-1]", removed)
	}
}

// A foreign-labelled or running own conflict fails immediately, with no polling:
// the driver never removes a container it does not own or a live server.
func TestStartConflictFailsImmediatelyWithoutPolling(t *testing.T) {
	cases := []struct {
		name       string
		info       ContainerInfo
		wantReason string
	}{
		{
			name:       "foreign label",
			info:       ContainerInfo{ID: "foreign-1", Labels: map[string]string{labelWorkerID: "other"}, Running: false},
			wantReason: "not owned",
		},
		{
			name:       "running own",
			info:       ContainerInfo{ID: "live-1", Labels: map[string]string{labelWorkerID: "w1"}, Running: true},
			wantReason: "running",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			docker := newFakeDocker()
			docker.conflictsLeft = 1
			docker.inspectInfo = tc.info
			d := newTestDriver(docker, nil, errors.New("no rcon"))

			_, err := d.Start(context.Background(), spec())
			if err == nil {
				t.Fatalf("expected Start to fail on a %s conflict", tc.name)
			}
			if !strings.Contains(err.Error(), tc.wantReason) {
				t.Fatalf("err = %v, want the decline reason %q in the message", err, tc.wantReason)
			}
			docker.mu.Lock()
			calls, removed := docker.createCalls, docker.removed
			docker.mu.Unlock()
			if calls != 1 {
				t.Fatalf("createCalls = %d, want 1 (no polling)", calls)
			}
			if len(removed) != 0 {
				t.Fatalf("removed = %v, want none (the container is left untouched)", removed)
			}
		})
	}
}

// When the name never frees within the deadline (remove keeps failing for a
// reason other than removal-in-progress), the loop gives up and fails with the
// original conflict wrapped by the last decline reason, keeping #231's
// observability.
func TestStartConflictLoopDeadlineFails(t *testing.T) {
	docker := newFakeDocker()
	// Every create conflicts; the conflict never resolves.
	docker.conflictsLeft = 1000
	docker.inspectInfo = ContainerInfo{ID: "stale-1", Labels: map[string]string{labelWorkerID: "w1"}, Running: false}
	docker.removeErr = errors.New("remove boom")
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	_, err := d.Start(context.Background(), spec())
	if err == nil {
		t.Fatal("expected Start to fail when the name never frees within the deadline")
	}
	if !errors.Is(err, errNameConflict) {
		t.Fatalf("err = %v, want the original name conflict wrapped", err)
	}
	if !strings.Contains(err.Error(), "remove boom") {
		t.Fatalf("err = %v, want the last decline reason in the message", err)
	}
}
