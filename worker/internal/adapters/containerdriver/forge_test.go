package containerdriver

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// forgeFakeDocker is a dockerAPI fake that tracks containers per id, so a Forge
// install+launch (two containers under one instance) can be driven distinctly. It
// is intentionally separate from fakeDocker, which models a single container.
type forgeFakeDocker struct {
	mu sync.Mutex

	createSpecs []CreateSpec
	nextID      int
	// exited maps a container id to its exit channel; Wait blocks on it, exit
	// closes it.
	exited  map[string]chan struct{}
	codes   map[string]int64
	errs    map[string]error
	removed []string
	// logBodies maps a container id to the multiplexed log stream Logs returns.
	logBodies map[string]string
	// waitGates, when non-nil for a given id, drives Wait for that container
	// result-by-result instead of the default exit-channel; each Wait call pops
	// one waitResult, so a test can script a transport error before a real exit.
	waitGates map[string]chan waitResult
	// inspectGate, when non-nil, scripts Inspect calls result-by-result.
	inspectGate chan inspectStep
}

func newForgeFakeDocker() *forgeFakeDocker {
	return &forgeFakeDocker{
		exited:    map[string]chan struct{}{},
		codes:     map[string]int64{},
		errs:      map[string]error{},
		logBodies: map[string]string{},
		waitGates: map[string]chan waitResult{},
	}
}

func (f *forgeFakeDocker) Create(_ context.Context, spec CreateSpec) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.createSpecs = append(f.createSpecs, spec)
	f.nextID++
	id := spec.Name // use the deterministic name as the id so tests can target it
	f.exited[id] = make(chan struct{})
	return id, nil
}

func (f *forgeFakeDocker) Start(_ context.Context, _ string) error { return nil }

func (f *forgeFakeDocker) Stop(_ context.Context, id string, _ time.Duration) error {
	f.exit(id, 0, nil)
	return nil
}

func (f *forgeFakeDocker) Kill(_ context.Context, id string) error {
	f.exit(id, 137, nil)
	return nil
}

func (f *forgeFakeDocker) Wait(_ context.Context, id string) (int64, error) {
	f.mu.Lock()
	gate := f.waitGates[id]
	ch := f.exited[id]
	f.mu.Unlock()
	if gate != nil {
		r := <-gate
		return r.code, r.err
	}
	if ch != nil {
		<-ch
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.codes[id], f.errs[id]
}

func (f *forgeFakeDocker) Remove(_ context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removed = append(f.removed, id)
	return nil
}

func (f *forgeFakeDocker) Inspect(_ context.Context, _ string) (ContainerInfo, error) {
	f.mu.Lock()
	gate := f.inspectGate
	f.mu.Unlock()
	if gate != nil {
		step := <-gate
		return step.info, step.err
	}
	return ContainerInfo{}, errNotFound
}

func (f *forgeFakeDocker) List(_ context.Context, _, _ string) ([]Container, error) {
	return nil, nil
}

func (f *forgeFakeDocker) Logs(_ context.Context, id string) (io.ReadCloser, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return io.NopCloser(strings.NewReader(f.logBodies[id])), nil
}

func (f *forgeFakeDocker) Stats(_ context.Context, _ string) (ContainerStats, error) {
	return ContainerStats{}, nil
}

// exit releases Wait for id with the given code/error.
func (f *forgeFakeDocker) exit(id string, code int64, err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	ch := f.exited[id]
	if ch == nil {
		return
	}
	select {
	case <-ch:
	default:
		f.codes[id] = code
		f.errs[id] = err
		close(ch)
	}
}

func (f *forgeFakeDocker) names() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for _, s := range f.createSpecs {
		out = append(out, s.Name)
	}
	return out
}

func (f *forgeFakeDocker) wasRemoved(id string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, r := range f.removed {
		if r == id {
			return true
		}
	}
	return false
}

func forgeDriver(docker dockerAPI) *Driver {
	return New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{
		WorkerID:             "w1",
		StopTimeout:          50 * time.Millisecond,
		GameBindIP:           "0.0.0.0",
		ReadinessTimeout:     20 * time.Millisecond,
		ConflictPollInterval: time.Millisecond,
		ConflictDeadline:     100 * time.Millisecond,
	})
}

func forgeSpec(dir string) execution.InstanceSpec {
	return execution.InstanceSpec{
		ServerID:         "s1",
		WorkingDir:       dir,
		MinecraftVersion: "1.21",
		JarRelpath:       "server.jar",
		LaunchMode:       execution.LaunchModeForgeArgsfile,
	}
}

const forgeArgsRel = "libraries/net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt"

func writeArgsfile(t *testing.T, dir string) {
	t.Helper()
	p := filepath.Join(dir, filepath.FromSlash(forgeArgsRel))
	if err := os.MkdirAll(filepath.Dir(p), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte("-x"), 0o600); err != nil {
		t.Fatal(err)
	}
}

// A Forge start with the args file already present launches one container (the
// launch container, deterministic name), reaching running, with no install
// container created (issue #305).
func TestForgeContainerArgsfilePresentLaunches(t *testing.T) {
	dir := t.TempDir()
	writeArgsfile(t, dir)
	docker := newForgeFakeDocker()
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if got := docker.names(); len(got) != 1 || got[0] != "mcsd-s1" {
		t.Fatalf("created containers = %v, want [mcsd-s1]", got)
	}
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// A Forge start with no args file creates the install container under the
// distinct mcsd-<id>-install name, runs it, then on success creates+starts the
// launch container under the deterministic name as the SAME instance (issue #305).
// The install container is removed before the launch create.
func TestForgeContainerInstallThenLaunch(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	if inst.Status() != execution.StateStarting {
		t.Fatalf("Status during install = %v, want starting", inst.Status())
	}
	// Only the install container exists so far, under the distinct name.
	if got := docker.names(); len(got) != 1 || got[0] != "mcsd-s1-install" {
		t.Fatalf("created containers = %v, want [mcsd-s1-install]", got)
	}
	if !hasArg(docker.createSpecs[0].Cmd, "--installServer") {
		t.Fatalf("install Cmd = %v, want --installServer", docker.createSpecs[0].Cmd)
	}

	// The install produces the args file then exits cleanly.
	writeArgsfile(t, dir)
	docker.exit("mcsd-s1-install", 0, nil)

	drainTo(t, inst.Events(), execution.StateRunning)

	got := docker.names()
	if len(got) != 2 || got[0] != "mcsd-s1-install" || got[1] != "mcsd-s1" {
		t.Fatalf("created containers = %v, want [mcsd-s1-install mcsd-s1]", got)
	}
	if !docker.wasRemoved("mcsd-s1-install") {
		t.Fatal("install container must be removed after it exits")
	}
	if !hasArg(docker.createSpecs[1].Cmd, "@"+containerWorkDir+"/"+forgeArgsRel) {
		t.Fatalf("launch Cmd = %v, want the @argsfile", docker.createSpecs[1].Cmd)
	}
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// Both the Forge install container and the post-install launch container carry
// the per-server memory ceiling as the Docker host-config Memory limit, converted
// MiB→bytes (issue #707).
func TestForgeContainerMemoryLimit(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	d := forgeDriver(docker)

	s := forgeSpec(dir)
	s.MemoryLimitMB = 1024
	inst, err := d.Start(context.Background(), s)
	if err != nil {
		t.Fatalf("Start: %v", err)
	}

	const wantBytes = int64(1024) * 1024 * 1024
	if got := docker.createSpecs[0].MemoryLimitBytes; got != wantBytes {
		t.Fatalf("install MemoryLimitBytes = %d, want %d (1024 MiB)", got, wantBytes)
	}

	writeArgsfile(t, dir)
	docker.exit("mcsd-s1-install", 0, nil)
	drainTo(t, inst.Events(), execution.StateRunning)

	if len(docker.createSpecs) < 2 {
		t.Fatalf("createSpecs = %v, want install + launch", docker.createSpecs)
	}
	if got := docker.createSpecs[1].MemoryLimitBytes; got != wantBytes {
		t.Fatalf("launch MemoryLimitBytes = %d, want %d (1024 MiB)", got, wantBytes)
	}

	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// Both the Forge install container and the post-install launch container carry
// the per-server CPU weight, proportional to CPUMillis (1024 shares = 1 core),
// so 2000m → 2048 (issue #724).
func TestForgeContainerCPUShares(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	d := forgeDriver(docker)

	s := forgeSpec(dir)
	s.CPUMillis = 2000
	inst, err := d.Start(context.Background(), s)
	if err != nil {
		t.Fatalf("Start: %v", err)
	}

	if got := docker.createSpecs[0].CPUShares; got != 2048 {
		t.Fatalf("install CPUShares = %d, want 2048 (2000m)", got)
	}

	writeArgsfile(t, dir)
	docker.exit("mcsd-s1-install", 0, nil)
	drainTo(t, inst.Events(), execution.StateRunning)

	if len(docker.createSpecs) < 2 {
		t.Fatalf("createSpecs = %v, want install + launch", docker.createSpecs)
	}
	if got := docker.createSpecs[1].CPUShares; got != 2048 {
		t.Fatalf("launch CPUShares = %d, want 2048 (2000m)", got)
	}

	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// A Forge install container exiting non-zero reports crashed and never creates the
// launch container (issue #305). Install failure surfaces as crashed via the
// status pump, not a command error code.
func TestForgeContainerInstallFailureCrashesNoLaunch(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	docker.exit("mcsd-s1-install", 1, errors.New("install exited 1"))

	drainTo(t, inst.Events(), execution.StateCrashed)
	if got := docker.names(); len(got) != 1 {
		t.Fatalf("created containers = %v, want only the install container", got)
	}
}

// Stop during the install phase terminates the install container and reports
// stopped; no launch container is created (issue #305).
func TestForgeContainerStopDuringInstall(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	if inst.Status() != execution.StateStarting {
		t.Fatalf("Status during install = %v, want starting", inst.Status())
	}

	if err := inst.Stop(context.Background(), false); err != nil {
		t.Fatalf("Stop during install: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	if got := docker.names(); len(got) != 1 {
		t.Fatalf("created containers = %v, want only the install container (no launch)", got)
	}
}

// The install container's output is captured to logs/forge-install.log in the
// working dir (issue #305).
func TestForgeContainerInstallOutputWrittenToLog(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	var body strings.Builder
	body.Write(frame(dockerStreamStdout, "downloading libraries\n"))
	body.Write(frame(dockerStreamStderr, "a warning\n"))
	docker.logBodies["mcsd-s1-install"] = body.String()
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	writeArgsfile(t, dir)
	docker.exit("mcsd-s1-install", 0, nil)
	drainTo(t, inst.Events(), execution.StateRunning)

	data, err := os.ReadFile(filepath.Join(dir, filepath.FromSlash(execution.ForgeInstallLogRelpath)))
	if err != nil {
		t.Fatalf("read install log: %v", err)
	}
	text := string(data)
	if !strings.Contains(text, "downloading libraries") || !strings.Contains(text, "a warning") {
		t.Fatalf("install log = %q, want the installer output", text)
	}
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// The JAR launch (default mode) keeps its historical in-container Cmd
// byte-for-byte: `java -jar /data/server.jar nogui` (issue #305 parity).
func TestJarContainerCmdParity(t *testing.T) {
	docker := newForgeFakeDocker()
	d := forgeDriver(docker)
	s := execution.InstanceSpec{ServerID: "s1", WorkingDir: t.TempDir(), MinecraftVersion: "1.21", JarRelpath: "server.jar"}

	inst, err := d.Start(context.Background(), s)
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	want := []string{"java", "-jar", "/data/server.jar", "nogui"}
	got := docker.createSpecs[0].Cmd
	if len(got) != len(want) {
		t.Fatalf("Cmd = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("Cmd = %v, want %v", got, want)
		}
	}
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// A Stop that wins the stopping latch after the install container has exited but
// before the launch container is started must abort the launch: no launch
// container is started, Stop reports cleanly, and the instance ends stopped with
// no server left running (issue #306). The beforeLaunch hook fires in the exact
// install-exit→launch window the critical section must close, driving a Stop to
// win the latch there. The launch container has been created at that point, so the
// abort must remove it before it can start.
func TestForgeContainerStopWinsLatchBeforeLaunch(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	contInst := inst.(*instance)

	stopErr := make(chan error, 1)
	contInst.beforeLaunch = func() {
		// The install container has exited and the launch container is created (but
		// not started); a Stop arriving now must win the latch and abort the launch.
		go func() { stopErr <- inst.Stop(context.Background(), false) }()
		for inst.Status() != execution.StateStopping {
			time.Sleep(time.Millisecond)
		}
	}

	// The install produces the args file, then exits cleanly, driving the supervisor
	// into the handoff window.
	writeArgsfile(t, dir)
	docker.exit("mcsd-s1-install", 0, nil)

	drainTo(t, inst.Events(), execution.StateStopped)

	if err := <-stopErr; err != nil {
		t.Fatalf("Stop returned %v, want clean nil (no orphan, no survived-kill)", err)
	}
	if inst.Status() != execution.StateStopped {
		t.Fatalf("final status = %v, want stopped (no server left running)", inst.Status())
	}
	if !docker.wasRemoved("mcsd-s1") {
		t.Fatal("the created-but-unstarted launch container must be removed on abort")
	}
}

// A Wait TRANSPORT error on the install container while the container is GONE
// (404) must emit crashed (issue #881). Without the re-inspect treatment, the
// transport error is reported directly as crashed with the transport-error
// message; with it, the re-inspect confirms gone → crashed with the correct
// "no args file" detail (the install produced no argsfile so the re-plan after
// Wait would crash anyway). This test verifies the install path runs through
// the same re-inspect loop as supervise rather than short-circuiting to a crash
// on the raw transport error.
func TestInstallWaitTransportErrorContainerGoneEmitsCrashed(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	docker.waitGates[installID] = make(chan waitResult)
	// Inspect: first call → errNotFound (container gone after blip).
	docker.inspectGate = make(chan inspectStep, 1)
	docker.inspectGate <- inspectStep{err: errNotFound}
	// Shrink the probe bound to keep the deadline path fast.
	prevDeadline, prevInterval := waitTransportProbeDeadline, waitTransportProbeInterval
	waitTransportProbeDeadline, waitTransportProbeInterval = 30*time.Millisecond, time.Millisecond
	t.Cleanup(func() {
		waitTransportProbeDeadline, waitTransportProbeInterval = prevDeadline, prevInterval
	})
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}

	// Deliver a transport error on the install Wait. The re-inspect returns
	// errNotFound → the container is gone → superviseInstall should report crashed.
	docker.waitGates[installID] <- waitResult{err: errors.New("containerdriver: POST /wait: EOF")}

	drainTo(t, inst.Events(), execution.StateCrashed)
	if inst.Status() != execution.StateCrashed {
		t.Fatalf("Status = %v, want crashed after install container gone", inst.Status())
	}
}

// A Wait TRANSPORT error on the install container while the container is STILL
// RUNNING means the daemon blipped but the install is ongoing: superviseInstall
// must re-attach a waiter and continue, not crash prematurely (issue #881).
func TestInstallWaitTransportErrorContainerRunningContinuesSupervising(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	docker.waitGates[installID] = make(chan waitResult)
	// Inspect: first call → Running (install still going).
	docker.inspectGate = make(chan inspectStep, 1)
	docker.inspectGate <- inspectStep{info: ContainerInfo{ID: installID, Running: true}}
	prevDeadline, prevInterval := waitTransportProbeDeadline, waitTransportProbeInterval
	waitTransportProbeDeadline, waitTransportProbeInterval = 100*time.Millisecond, time.Millisecond
	t.Cleanup(func() {
		waitTransportProbeDeadline, waitTransportProbeInterval = prevDeadline, prevInterval
	})
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}

	// Transport error: re-inspect finds Running → re-attach. Second push lands only
	// after the re-attached Wait reads it, proving supervision continued.
	docker.waitGates[installID] <- waitResult{err: errors.New("containerdriver: POST /wait: connection reset")}

	// Now the install exits successfully and produces the args file.
	writeArgsfile(t, dir)
	docker.waitGates[installID] <- waitResult{code: 0}

	drainTo(t, inst.Events(), execution.StateRunning)
	if inst.Status() != execution.StateRunning {
		t.Fatalf("Status = %v, want running after install re-attach and clean exit", inst.Status())
	}
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

func hasArg(args []string, want string) bool {
	for _, a := range args {
		if a == want {
			return true
		}
	}
	return false
}
