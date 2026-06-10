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
	// stopBlocksUntilCancel models a wedged daemon: Stop ignores the timeout and
	// blocks until its context is cancelled, returning the context error. It drives
	// the bounded-Sweep test (issue #338); without a per-call deadline on Sweep's
	// Stop call this would block forever and the test would hang.
	stopBlocksUntilCancel bool
	// stopped records the ids passed to Stop (in order), and stopErr forces a Stop
	// failure. Used by the Sweep tests to assert the graceful-stop-before-remove
	// ordering for running orphans (issue #336).
	stopped    []string
	stopErr    error
	killCalled bool
	killCalls  int
	// killNoExit models a container that survives docker kill: Kill records the
	// call but does not release Wait, so the post-Kill waitExit times out.
	killNoExit bool
	// killSurvive counts how many leading Kill calls survive (do not release Wait);
	// each Kill decrements it, and a Kill at zero exits the container. It models a
	// container that lingers through the first kill(s) and dies on a later retry,
	// driving the re-attemptable-Stop path (issue #253).
	killSurvive int
	removed     []string

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

func (f *fakeDocker) Stop(ctx context.Context, id string, _ time.Duration) error {
	f.mu.Lock()
	f.stopCalled = true
	f.stopped = append(f.stopped, id)
	noExit := f.stopNoExit
	stopErr := f.stopErr
	blockUntilCancel := f.stopBlocksUntilCancel
	f.mu.Unlock()
	if blockUntilCancel {
		<-ctx.Done()
		return ctx.Err()
	}
	if stopErr != nil {
		return stopErr
	}
	if !noExit {
		f.exit(0, nil)
	}
	return nil
}

func (f *fakeDocker) Kill(_ context.Context, _ string) error {
	f.mu.Lock()
	f.killCalled = true
	f.killCalls++
	survive := f.killNoExit || f.killSurvive > 0
	if f.killSurvive > 0 {
		f.killSurvive--
	}
	f.mu.Unlock()
	if !survive {
		f.exit(137, nil)
	}
	return nil
}

func (f *fakeDocker) killCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.killCalls
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
		// A short readiness fallback lets tests that do not feed a Done marker reach
		// running promptly via the timeout path (issue #345).
		ReadinessTimeout: 20 * time.Millisecond,
		// Short conflict-loop timing keeps the wait-for-name-free tests fast.
		ConflictPollInterval: time.Millisecond,
		ConflictDeadline:     100 * time.Millisecond,
		// A short sweep margin keeps the wedged-daemon Sweep test fast (issue #338).
		SweepCallMargin: 50 * time.Millisecond,
	})
}

// newReadinessTestDriver builds a driver with an explicit readiness fallback
// timeout so the readiness tests (issue #345) can isolate the marker path (long
// timeout) from the fallback path (short timeout).
func newReadinessTestDriver(docker *fakeDocker, readinessTimeout time.Duration) *Driver {
	return New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{
		WorkerID:             "w1",
		StopTimeout:          50 * time.Millisecond,
		GameBindIP:           "0.0.0.0",
		ReadinessTimeout:     readinessTimeout,
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

// awaitLogLine reads the Logs() stream until a line containing want surfaces. It
// is the deterministic synchronization point the hold-on-starting test relies on:
// once a benign boot line appears on Logs(), the capture goroutine has demuxed the
// boot window, and since markReadyIfDone runs synchronously before a line is
// queued (logpump.go), any readiness marker present would already have fired
// Ready. So the marker has provably NOT been seen yet — no sleep needed.
func awaitLogLine(t *testing.T, ch <-chan execution.LogEvent, want string) {
	t.Helper()
	deadline := time.After(2 * time.Second)
	for {
		select {
		case ev, ok := <-ch:
			if !ok {
				t.Fatalf("log channel closed before reaching %q", want)
			}
			if strings.Contains(ev.Line, want) {
				return
			}
		case <-deadline:
			t.Fatalf("timed out waiting for log line %q", want)
		}
	}
}

// observedRunning reports whether any StateRunning event is currently buffered on
// ch. It drains non-blocking: every emit up to the caller's synchronization point
// has already completed, so a running event — if one was wrongly emitted before
// readiness — is sitting in the buffer to be observed here.
func observedRunning(ch <-chan execution.StatusEvent) bool {
	for {
		select {
		case ev := <-ch:
			if ev.State == execution.StateRunning {
				return true
			}
		default:
			return false
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

// Running is reported only after the server logs its startup-complete "Done"
// line; until then the instance holds StateStarting so a client gating console
// input on running does not hit the RCON boot window (issue #345).
//
// This pins the PR's core invariant: running is never observed BEFORE the
// readiness marker. The container log stream is held open (an io.Pipe) without the
// Done line and the readiness timeout is long, so neither the marker path nor the
// fallback can fire. A benign boot line driven through to Logs() is the
// deterministic synchronization point (see awaitLogLine): once it surfaces, the
// instance must still be starting with no running event emitted; only after the
// Done frame is written does running arrive. Re-introducing the pre-fix immediate
// StateRunning emit in beginLaunchTail makes the negative assertions below fail.
func TestStartHoldsStartingUntilReadyMarker(t *testing.T) {
	pr, pw := io.Pipe()
	docker := newFakeDocker()
	docker.logBody = pr
	// A long readiness timeout means only the Done marker can drive running here;
	// the fallback path is covered by TestStartReachesRunningViaFallbackTimeout.
	d := newReadinessTestDriver(docker, 10*time.Second)

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	src, ok := inst.(execution.LogSource)
	if !ok {
		t.Fatal("container instance should be a LogSource")
	}

	// Drive a benign boot frame (NOT the marker) and wait for it on Logs(). Its
	// arrival proves the capture goroutine demuxed the boot window without seeing the
	// marker, so awaitReady cannot have transitioned to running.
	if _, err := pw.Write(frame(dockerStreamStdout,
		"[12:00:00] [Server thread/INFO]: Starting minecraft server\n")); err != nil {
		t.Fatalf("write boot frame: %v", err)
	}
	awaitLogLine(t, src.Logs(), "Starting minecraft server")

	// The negative assertions: the instance is still starting and no running event
	// was emitted before the readiness marker.
	if got := inst.Status(); got != execution.StateStarting {
		t.Fatalf("Status = %v before the readiness marker, want starting", got)
	}
	if observedRunning(inst.Events()) {
		t.Fatal("running was emitted before the readiness marker")
	}

	// Now feed the marker frame; running must arrive.
	if _, err := pw.Write(frame(dockerStreamStdout,
		`[12:00:03] [Server thread/INFO]: Done (3.210s)! For help, type "help"`+"\n")); err != nil {
		t.Fatalf("write done frame: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
	_ = pw.Close()
}

// With no readiness marker in the logs, the instance still reaches running once
// the fallback timeout elapses, so a server whose log format differs never
// sticks in starting forever (issue #345).
func TestStartReachesRunningViaFallbackTimeout(t *testing.T) {
	docker := newFakeDocker()
	// No log body, so the Done marker never appears; only the fallback can run it.
	d := newReadinessTestDriver(docker, 30*time.Millisecond)

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
}

// A container that exits while still starting (before any readiness marker)
// surfaces as crashed, not running: a boot crash (e.g. eula=false) must not be
// masked by the readiness wait (issue #345).
func TestStartExitDuringStartingReportsCrashed(t *testing.T) {
	docker := newFakeDocker()
	// A long readiness timeout: the exit, not the fallback, must drive the state.
	d := newReadinessTestDriver(docker, 10*time.Second)

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	// The container exits before any Done line; the instance must go crashed.
	docker.exit(1, nil)
	drainTo(t, inst.Events(), execution.StateCrashed)
	if inst.Status() == execution.StateRunning {
		t.Fatal("instance reported running after a boot crash")
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

// The launch container carries the per-server memory ceiling as the Docker
// host-config Memory limit, converted MiB→bytes (issue #707).
func TestStartLaunchContainerMemoryLimit(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	s := spec()
	s.MemoryLimitMB = 2048
	if _, err := d.Start(context.Background(), s); err != nil {
		t.Fatalf("Start: %v", err)
	}

	const wantBytes = int64(2048) * 1024 * 1024
	if got := docker.createSpec.MemoryLimitBytes; got != wantBytes {
		t.Fatalf("MemoryLimitBytes = %d, want %d (2048 MiB)", got, wantBytes)
	}
}

// An unset memory ceiling (0) leaves the launch container unconstrained: the
// create payload carries no memory limit (issue #707).
func TestStartLaunchContainerNoMemoryLimit(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	if _, err := d.Start(context.Background(), spec()); err != nil {
		t.Fatalf("Start: %v", err)
	}

	if got := docker.createSpec.MemoryLimitBytes; got != 0 {
		t.Fatalf("MemoryLimitBytes = %d, want 0 (unconstrained)", got)
	}
}

// The launch container's CPU weight is proportional to the per-server CPU
// allocation: CPUMillis is mapped to CpuShares at 1024 shares = 1 core, so
// 2000m → 2048 (issue #724).
func TestStartLaunchContainerCPUShares(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	s := spec()
	s.CPUMillis = 2000
	if _, err := d.Start(context.Background(), s); err != nil {
		t.Fatalf("Start: %v", err)
	}

	if got := docker.createSpec.CPUShares; got != 2048 {
		t.Fatalf("CPUShares = %d, want 2048 (2000m)", got)
	}
}

// An unset CPU allocation (0) keeps the historical fixed weight (2048), so
// existing servers do not regress (issue #724).
func TestStartLaunchContainerNoCPUMillisKeepsDefaultShares(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("no rcon"))

	if _, err := d.Start(context.Background(), spec()); err != nil {
		t.Fatalf("Start: %v", err)
	}

	if got := docker.createSpec.CPUShares; got != gameServerCPUShares {
		t.Fatalf("CPUShares = %d, want %d (default)", got, gameServerCPUShares)
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
	}, Options{WorkerID: "w1", StopTimeout: 50 * time.Millisecond, ReadinessTimeout: 20 * time.Millisecond, Network: "mcsd"})

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

// Once a stop has begun, escalation is decoupled from the caller's context
// (issue #770): a cancelled ctx — e.g. the gRPC session stream dropping mid-stop
// — must NOT fail the docker calls/waits immediately and record a still-healthy,
// still-stopping container as a failed-stop orphan. The RCON "stop" is accepted
// and the container exits within its grace, so Stop completes cleanly without
// docker stop/kill even though ctx was cancelled before Stop ran.
func TestStopDetachesEscalationFromContextCancellation(t *testing.T) {
	docker := newFakeDocker()
	// RCON "stop" is accepted; the container exits shortly after, well inside the
	// stop timeout. This models a graceful stop in flight.
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

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	// ctx is already cancelled, modelling the dropped stream. Pre-fix the cancelled
	// ctx made waitExit return false instantly and docker Stop/Kill fail, recording
	// a failed-stop orphan; now the escalation runs on a detached context, so the
	// container keeps its full grace and exits on its own.
	if err := inst.Stop(ctx, true); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	if docker.stopWasCalled() || docker.killWasCalled() {
		t.Fatal("Stop escalated to docker stop/kill despite the RCON stop succeeding within grace")
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

// When docker kill fails to terminate the container, the post-Kill waitExit times
// out. Stop must report this as a failure so the manager reports the command
// failed, the API keeps the assignment, and the reconciler retries (issue #211);
// reporting success here would let the API unassign while the container lingers.
func TestGracefulStopFailsWhenContainerSurvivesKill(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("rcon dial failed"))
	// Neither docker stop nor docker kill exits the container.
	docker.stopNoExit = true
	docker.killNoExit = true

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if err := inst.Stop(context.Background(), true); err == nil {
		t.Fatal("expected Stop to fail when the container survives docker kill")
	}
	if !docker.killWasCalled() {
		t.Fatal("expected docker kill escalation")
	}
}

// A stop escalation that hits the survived-docker-kill failure path while the
// instance is still starting must not relabel the still-booting container as
// running. Stop is reachable from starting because readiness gating holds
// starting through the MC boot (issue #350); the survived-kill reset restores the
// pre-stop state, so a starting instance stays starting rather than misreporting
// running to the control plane (issue #352).
func TestSurvivedKillFromStartingDoesNotReportRunning(t *testing.T) {
	pr, pw := io.Pipe()
	defer func() { _ = pw.Close() }()
	docker := newFakeDocker()
	docker.logBody = pr
	// Neither docker stop nor docker kill exits the container.
	docker.stopNoExit = true
	docker.killNoExit = true
	// A long readiness timeout with no Done marker holds the instance in starting.
	d := newReadinessTestDriver(docker, 10*time.Second)

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	src, ok := inst.(execution.LogSource)
	if !ok {
		t.Fatal("container instance should be a LogSource")
	}

	// Synchronize on a benign boot frame to prove the boot window was demuxed
	// without the readiness marker, so the instance is provably still starting.
	if _, err := pw.Write(frame(dockerStreamStdout,
		"[12:00:00] [Server thread/INFO]: Starting minecraft server\n")); err != nil {
		t.Fatalf("write boot frame: %v", err)
	}
	awaitLogLine(t, src.Logs(), "Starting minecraft server")
	if got := inst.Status(); got != execution.StateStarting {
		t.Fatalf("Status = %v before Stop, want starting", got)
	}

	if err := inst.Stop(context.Background(), true); err == nil {
		t.Fatal("expected Stop to fail when the container survives docker kill")
	}
	if got := inst.Status(); got != execution.StateStarting {
		t.Fatalf("Status = %v after survived-kill Stop, want starting (not running)", got)
	}
}

// The container can exit during the post-kill confirm wait, in the window between
// waitExitDone timing out and the survived-kill restore re-acquiring the lock:
// supervise sets the terminal state, and the restore must not stomp it back to
// the pre-stop state (issue #392). The beforeSurvivedReset hook drives the exit
// and supervise into that exact window, then the restore runs.
func TestSurvivedKillRestoreDoesNotStompTerminalState(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true // docker stop falls through to docker kill
	docker.killNoExit = true // the first confirm wait times out: the kill "survived"
	d := newTestDriver(docker, nil, errors.New("rcon dial failed"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	in := inst.(*instance)
	in.beforeSurvivedReset = func() {
		// The container exits in the window; wait for supervise to record the terminal
		// state before the restore re-acquires the lock.
		docker.exit(137, nil)
		drainTo(t, inst.Events(), execution.StateStopped)
	}

	// The container did exit (during the window), so Stop succeeds rather than
	// reporting a survived-kill failure.
	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("Stop = %v, want nil once the container exits in the wait window", err)
	}
	if got := inst.Status(); got != execution.StateStopped {
		t.Fatalf("Status = %v after the exit-in-window Stop, want stopped (not stomped back)", got)
	}
}

// A container that survives the kill for the whole timeout and then dies after the
// survived-kill restore reset the stopping latch must still be recorded stopped,
// not a spurious crash: a stop was requested (issue #257). The reset clears
// stopping (so a retry can run), but the sticky stop intent makes supervise
// report the operator-requested stop correctly.
func TestSurvivedKillThenLateExitRecordsStopped(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true // docker stop falls through to docker kill
	docker.killNoExit = true // the kill survives for the whole confirm wait
	d := newTestDriver(docker, nil, errors.New("rcon dial failed"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	// The kill survives the whole timeout, so Stop fails with the survived-kill
	// error and the latch is reset.
	if err := inst.Stop(context.Background(), true); err == nil {
		t.Fatal("expected Stop to fail when the container survives docker kill")
	}

	// The orphan dies later; supervise must record it stopped, not crashed, because
	// a stop was requested.
	docker.exit(137, nil)
	drainTo(t, inst.Events(), execution.StateStopped)
	if got := inst.Status(); got != execution.StateStopped {
		t.Fatalf("Status = %v after the late exit, want stopped (not a spurious crash)", got)
	}
}

// After a Stop that fails because the container survives docker kill, a retry
// Stop must re-run the kill-and-confirm sequence rather than short-circuit on the
// stopping latch. When the container then dies on the retry kill, the retry
// returns success (issue #253). Without the latch reset the retry would return a
// false nil.
func TestStopReattemptableAfterSurvivedKill(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("rcon dial failed"))
	// docker stop never exits the container, so each Stop falls through to docker
	// kill; the first kill survives and the next kill exits it.
	docker.stopNoExit = true
	docker.killSurvive = 1

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if err := inst.Stop(context.Background(), true); err == nil {
		t.Fatal("expected first Stop to fail when the container survives docker kill")
	}

	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("retry Stop = %v, want success once the container dies on the retry kill", err)
	}
	if docker.killCount() != 2 {
		t.Fatalf("docker kill called %d times, want 2 (initial + retry re-issues the kill)", docker.killCount())
	}
	drainTo(t, inst.Events(), execution.StateStopped)
}

// A retry Stop while the container is STILL surviving the kill must fail again,
// never return a false nil: the orphan is still alive and the API must keep the
// assignment (issue #253).
func TestRetryStopStillSurvivingFailsAgain(t *testing.T) {
	docker := newFakeDocker()
	d := newTestDriver(docker, nil, errors.New("rcon dial failed"))
	docker.stopNoExit = true
	docker.killNoExit = true // every kill survives

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if err := inst.Stop(context.Background(), true); err == nil {
		t.Fatal("expected first Stop to fail")
	}
	if err := inst.Stop(context.Background(), true); err == nil {
		t.Fatal("retry Stop returned nil while the container still survives; want a failure")
	}
	if docker.killCount() != 2 {
		t.Fatalf("docker kill called %d times, want 2 (the retry must re-issue the kill, not short-circuit)", docker.killCount())
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

// A docker start error whose daemon message reports a host-port collision is
// classified as a port conflict so the instance manager can emit the sanitized
// port_conflict code (issue #225).
func TestStartPortConflictClassified(t *testing.T) {
	docker := newFakeDocker()
	docker.startErr = errors.New(
		"containerdriver: POST /containers/c/start: status 500: " +
			"driver failed programming external connectivity on endpoint mcsd-s1: " +
			"Bind for 0.0.0.0:25565 failed: port is already allocated")
	d := newTestDriver(docker, nil, nil)

	_, err := d.Start(context.Background(), spec())
	if !errors.Is(err, execution.ErrPortConflict) {
		t.Fatalf("Start error = %v, want wrapped ErrPortConflict", err)
	}
}

// A docker create error whose daemon message reports a missing image is
// classified as image-missing so the instance manager can emit the sanitized
// image_missing code (issue #225).
func TestStartImageMissingClassified(t *testing.T) {
	docker := newFakeDocker()
	docker.createErr = errors.New(
		"containerdriver: POST /containers/create: status 404: " +
			"No such image: eclipse-temurin:21-jre")
	d := newTestDriver(docker, nil, nil)

	_, err := d.Start(context.Background(), spec())
	if !errors.Is(err, execution.ErrImageMissing) {
		t.Fatalf("Start error = %v, want wrapped ErrImageMissing", err)
	}
}

// A pull-access-denied create error (a private/typo image the daemon cannot
// pull) is also classified as image-missing (issue #225).
func TestStartPullAccessDeniedClassifiedImageMissing(t *testing.T) {
	docker := newFakeDocker()
	docker.createErr = errors.New(
		"containerdriver: POST /containers/create: status 404: " +
			"pull access denied for eclipse-temurin, repository does not exist " +
			"or may require 'docker login'")
	d := newTestDriver(docker, nil, nil)

	_, err := d.Start(context.Background(), spec())
	if !errors.Is(err, execution.ErrImageMissing) {
		t.Fatalf("Start error = %v, want wrapped ErrImageMissing", err)
	}
}

// An unclassified start failure carries neither sanitized category, so the
// instance manager keeps the generic internal code (issue #225).
func TestStartUnclassifiedFailureNoCategory(t *testing.T) {
	docker := newFakeDocker()
	docker.startErr = errors.New("containerdriver: POST /containers/c/start: status 500: out of memory")
	d := newTestDriver(docker, nil, nil)

	_, err := d.Start(context.Background(), spec())
	if err == nil {
		t.Fatal("expected Start to fail")
	}
	if errors.Is(err, execution.ErrPortConflict) || errors.Is(err, execution.ErrImageMissing) {
		t.Fatalf("unclassified failure should carry no sanitized category, got %v", err)
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

// A RUNNING orphan is stopped gracefully (docker stop with the grace) before it
// is removed, so the MC server's SIGTERM shutdown hook saves the world instead of
// being SIGKILLed by the force-remove (issue #336).
func TestSweepGracefullyStopsRunningContainerBeforeRemove(t *testing.T) {
	docker := newFakeDocker()
	docker.listResult = []Container{{ID: "a", Name: "/mcsd-s1", State: "running"}}
	d := newTestDriver(docker, nil, nil)

	if err := d.Sweep(context.Background()); err != nil {
		t.Fatalf("Sweep: %v", err)
	}
	if len(docker.stopped) != 1 || docker.stopped[0] != "a" {
		t.Fatalf("stopped = %v, want [a]", docker.stopped)
	}
	if len(docker.removed) != 1 || docker.removed[0] != "a" {
		t.Fatalf("removed = %v, want [a]", docker.removed)
	}
}

// A non-running orphan (exited/created) keeps the force-remove-only behavior: no
// graceful stop is issued (issue #336).
func TestSweepRemovesExitedContainerWithoutStop(t *testing.T) {
	docker := newFakeDocker()
	docker.listResult = []Container{{ID: "a", Name: "/mcsd-s1", State: "exited"}}
	d := newTestDriver(docker, nil, nil)

	if err := d.Sweep(context.Background()); err != nil {
		t.Fatalf("Sweep: %v", err)
	}
	if len(docker.stopped) != 0 {
		t.Fatalf("stopped = %v, want none (exited container is force-removed)", docker.stopped)
	}
	if len(docker.removed) != 1 || docker.removed[0] != "a" {
		t.Fatalf("removed = %v, want [a]", docker.removed)
	}
}

// A graceful-stop failure on a running orphan must not leak the container: Sweep
// still removes it (force) and surfaces the stop error in the joined result
// (issue #336).
func TestSweepStopFailureStillRemovesAndSurfaces(t *testing.T) {
	docker := newFakeDocker()
	docker.listResult = []Container{{ID: "a", Name: "/mcsd-s1", State: "running"}}
	docker.stopErr = errors.New("stop boom")
	d := newTestDriver(docker, nil, nil)

	err := d.Sweep(context.Background())
	if err == nil {
		t.Fatal("expected Sweep to surface the stop failure")
	}
	if !strings.Contains(err.Error(), "stop boom") {
		t.Fatalf("error = %v, want it to contain the stop failure", err)
	}
	if len(docker.removed) != 1 || docker.removed[0] != "a" {
		t.Fatalf("removed = %v, want [a] (the container must not leak)", docker.removed)
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

// A wedged daemon must not block worker startup: Sweep bounds each daemon call so
// a Stop that never returns is cut off by its per-call deadline and surfaced as a
// stop error, rather than hanging startup forever (issue #338). The fake's Stop
// blocks until its context is cancelled, so without the bound this test would
// hang; with it, Sweep returns within the deadline and still force-removes the
// orphan.
func TestSweepBoundsWedgedStop(t *testing.T) {
	docker := newFakeDocker()
	docker.listResult = []Container{{ID: "a", Name: "/mcsd-s1", State: "running"}}
	docker.stopBlocksUntilCancel = true
	d := newTestDriver(docker, nil, nil)

	done := make(chan error, 1)
	go func() { done <- d.Sweep(context.Background()) }()

	select {
	case err := <-done:
		if err == nil {
			t.Fatal("expected Sweep to surface the bounded stop failure")
		}
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("error = %v, want it to wrap context.DeadlineExceeded", err)
		}
		if len(docker.removed) != 1 || docker.removed[0] != "a" {
			t.Fatalf("removed = %v, want [a] (the orphan must not leak)", docker.removed)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Sweep hung on a wedged daemon; the per-call deadline did not fire")
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

// A transient (non-404) inspect error mid-loop is not fatal: the driver treats
// it as "name still in use", keeps polling, and recovers when the next inspect
// 404s and the retried create succeeds.
func TestStartConflictLoopInspectErrorThenRecovers(t *testing.T) {
	docker := newFakeDocker()
	docker.conflictsLeft = 1
	docker.inspectSteps = []inspectStep{
		{err: errors.New("inspect boom")},
		{err: errNotFound},
	}
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
		t.Fatalf("createCalls = %d, want 2 (conflict then retry after recovery)", calls)
	}
	if len(removed) != 0 {
		t.Fatalf("removed = %v, want none (the transient error never triggers a remove)", removed)
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

// TestEmitCoalescesTerminalEventOnFullBuffer bursts more events than the 8-slot
// buffer with no consumer, then drains. The latest-state-wins coalescing must
// have kept the terminal event so it is never dropped (issue #790).
func TestEmitCoalescesTerminalEventOnFullBuffer(t *testing.T) {
	inst := &instance{
		spec:   execution.InstanceSpec{ServerID: "srv-790"},
		events: make(chan execution.StatusEvent, 8),
	}

	// Burst well past the buffer capacity, ending on the terminal state.
	for i := 0; i < 32; i++ {
		inst.emit(execution.StateRunning, "")
	}
	inst.emit(execution.StateCrashed, "process exited unexpectedly")

	// Drain the buffer; the last buffered event must be the terminal one.
	var last execution.StatusEvent
	for {
		select {
		case ev := <-inst.events:
			last = ev
			continue
		default:
		}
		break
	}
	if last.State != execution.StateCrashed {
		t.Fatalf("terminal event dropped: last buffered state = %v, want %v", last.State, execution.StateCrashed)
	}
}
