package containerdriver

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
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
	// started tracks the ids passed to Start in call order.
	started []string
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
	// onCreateHook, when non-nil, is called with the spec after a Create
	// succeeds. Tests use it to inject side effects (e.g., writing files) when
	// a specific container is created.
	onCreateHook func(spec CreateSpec)
	// stopNoExitIDs prevents Stop from releasing Wait for the named containers,
	// so the Stop falls through to the kill escalation (mirroring fakeDocker's
	// stopNoExit for install-phase survived-kill tests).
	stopNoExitIDs map[string]bool
	// killNoExitIDs prevents Kill from releasing Wait for the named containers,
	// so the post-Kill waitExitDone times out and triggers the survived-kill
	// path.
	killNoExitIDs map[string]bool
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
	f.createSpecs = append(f.createSpecs, spec)
	f.nextID++
	id := spec.Name // use the deterministic name as the id so tests can target it
	f.exited[id] = make(chan struct{})
	hook := f.onCreateHook
	f.mu.Unlock()
	if hook != nil {
		hook(spec)
	}
	return id, nil
}

func (f *forgeFakeDocker) ImagePull(_ context.Context, _ string) error { return nil }

func (f *forgeFakeDocker) Start(_ context.Context, id string) error {
	f.mu.Lock()
	f.started = append(f.started, id)
	f.mu.Unlock()
	return nil
}

func (f *forgeFakeDocker) Stop(_ context.Context, id string, _ time.Duration) error {
	f.mu.Lock()
	suppress := f.stopNoExitIDs[id]
	f.mu.Unlock()
	if !suppress {
		f.exit(id, 0, nil)
	}
	return nil
}

func (f *forgeFakeDocker) Kill(_ context.Context, id string) error {
	f.mu.Lock()
	suppress := f.killNoExitIDs[id]
	f.mu.Unlock()
	if !suppress {
		f.exit(id, 137, nil)
	}
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

// A Forge install container exiting non-zero is retried up to maxInstallRetries
// times; after all attempts fail the instance reports crashed with an attempt
// count and never creates the launch container (issue #305, #1128).
func TestForgeContainerInstallFailureCrashesNoLaunch(t *testing.T) {
	prev := installRetryBackoff
	installRetryBackoff = []time.Duration{time.Millisecond, time.Millisecond}
	t.Cleanup(func() { installRetryBackoff = prev })

	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	// Use a statusError so isTransportError returns false (matching the real
	// Docker client's behavior for a non-zero exit).
	installErr := statusError{method: "POST", path: "/wait", code: 200, message: "install exited 1"}
	docker.waitGates[installID] = make(chan waitResult, 3)
	docker.waitGates[installID] <- waitResult{code: 1, err: installErr}
	docker.waitGates[installID] <- waitResult{code: 1, err: installErr}
	docker.waitGates[installID] <- waitResult{code: 1, err: installErr}
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}

	drainTo(t, inst.Events(), execution.StateCrashed)
	// 1 initial install + 2 retry installs = 3 install containers, no launch.
	if got := docker.names(); len(got) != 3 {
		t.Fatalf("created containers = %v, want 3 install attempts", got)
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

// A Wait TRANSPORT error on the install container where the container is gone
// and no argsfile was produced still crashes — the re-plan check (not the stale
// transport error) is the authority: no install artifacts means the install
// failed (issue #895).
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
	// errNotFound → container is gone → waitErr cleared → re-plan finds no
	// argsfile → crashes with "no args file", not the stale transport error.
	docker.waitGates[installID] <- waitResult{err: errors.New("containerdriver: POST /wait: EOF")}

	drainTo(t, inst.Events(), execution.StateCrashed)
	if inst.Status() != execution.StateCrashed {
		t.Fatalf("Status = %v, want crashed after install container gone with no argsfile", inst.Status())
	}
}

// A Wait TRANSPORT error on the install container where the container is gone
// BUT the install actually succeeded (argsfile written) must fall through to the
// re-plan check and proceed to launch, not crash with the stale transport error
// (issue #895).
func TestInstallWaitTransportErrorContainerGoneButArgsfileExistsLaunches(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	docker.waitGates[installID] = make(chan waitResult)
	// Inspect: first call → errNotFound (container gone after blip).
	docker.inspectGate = make(chan inspectStep, 1)
	docker.inspectGate <- inspectStep{err: errNotFound}
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

	// The install container ran and produced the argsfile before exiting.
	writeArgsfile(t, dir)

	// Deliver a transport error on the install Wait. The re-inspect returns
	// errNotFound → container is gone, but the argsfile exists → re-plan finds
	// a launchable configuration → proceeds to launch, not crash.
	docker.waitGates[installID] <- waitResult{err: errors.New("containerdriver: POST /wait: EOF")}

	drainTo(t, inst.Events(), execution.StateRunning)
	if inst.Status() != execution.StateRunning {
		t.Fatalf("Status = %v, want running after install transport error with argsfile present", inst.Status())
	}
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
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

// A Forge install that produces a legacy forge-*.jar (no unix_args.txt) proceeds
// to launch via JAR mode instead of crashing (issue #1093).
func TestForgeContainerLegacyJarFallback(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}

	// The install produces a legacy forge jar (no args file).
	legacyJar := "forge-1.12.2-14.23.5.2860.jar"
	if err := os.WriteFile(filepath.Join(dir, legacyJar), []byte("jar"), 0o600); err != nil {
		t.Fatal(err)
	}
	docker.exit("mcsd-s1-install", 0, nil)

	drainTo(t, inst.Events(), execution.StateRunning)

	got := docker.names()
	if len(got) != 2 || got[0] != "mcsd-s1-install" || got[1] != "mcsd-s1" {
		t.Fatalf("created containers = %v, want [mcsd-s1-install mcsd-s1]", got)
	}
	if !docker.wasRemoved("mcsd-s1-install") {
		t.Fatal("install container must be removed after it exits")
	}
	// The launch container should use JAR mode with the legacy forge jar.
	if !hasArg(docker.createSpecs[1].Cmd, "-jar") {
		t.Fatalf("launch Cmd = %v, want -jar mode", docker.createSpecs[1].Cmd)
	}
	if !hasArg(docker.createSpecs[1].Cmd, containerWorkDir+"/"+legacyJar) {
		t.Fatalf("launch Cmd = %v, want legacy forge jar path", docker.createSpecs[1].Cmd)
	}
	if !hasArg(docker.createSpecs[1].Cmd, "nogui") {
		t.Fatalf("launch Cmd = %v, want nogui", docker.createSpecs[1].Cmd)
	}

	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// A Forge install that produces no args file and no legacy forge jar still
// crashes with the original error message (issue #1093).
func TestForgeContainerNoArgsNoLegacyJarCrashes(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	// Install exits cleanly but produces nothing.
	docker.exit("mcsd-s1-install", 0, nil)

	drainTo(t, inst.Events(), execution.StateCrashed)
	if inst.Status() != execution.StateCrashed {
		t.Fatalf("Status = %v, want crashed", inst.Status())
	}
	if got := docker.names(); len(got) != 1 {
		t.Fatalf("created containers = %v, want only the install container", got)
	}
}

// First install attempt fails, second succeeds: the instance proceeds to launch
// without crashing (issue #1128).
func TestForgeInstallRetrySucceeds(t *testing.T) {
	prev := installRetryBackoff
	installRetryBackoff = []time.Duration{time.Millisecond, time.Millisecond}
	t.Cleanup(func() { installRetryBackoff = prev })

	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	installErr := statusError{method: "POST", path: "/wait", code: 200, message: "download failed"}
	docker.waitGates[installID] = make(chan waitResult, 2)
	docker.waitGates[installID] <- waitResult{code: 1, err: installErr}
	docker.waitGates[installID] <- waitResult{code: 0}
	// Write the argsfile when the retry creates the second install container.
	// This runs after CleanForgeInstallArtifacts and before Wait, so the
	// re-plan finds the argsfile exactly as in production (the install container
	// "produced" it).
	var createCount atomic.Int32
	docker.onCreateHook = func(spec CreateSpec) {
		n := createCount.Add(1)
		if n == 2 && spec.Name == installID {
			writeArgsfile(t, dir)
		}
	}
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}

	drainTo(t, inst.Events(), execution.StateRunning)

	// 2 install containers + 1 launch container.
	got := docker.names()
	if len(got) != 3 || got[2] != "mcsd-s1" {
		t.Fatalf("created containers = %v, want 2 installs + 1 launch", got)
	}
	docker.exit("mcsd-s1", 0, nil)
	drainClosed(inst.Events())
}

// All install attempts fail: the instance crashes with an attempt-count message
// (issue #1128).
func TestForgeInstallRetryExhausted(t *testing.T) {
	prev := installRetryBackoff
	installRetryBackoff = []time.Duration{time.Millisecond, time.Millisecond}
	t.Cleanup(func() { installRetryBackoff = prev })

	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	installErr := statusError{method: "POST", path: "/wait", code: 200, message: "download failed"}
	docker.waitGates[installID] = make(chan waitResult, 3)
	docker.waitGates[installID] <- waitResult{code: 1, err: installErr}
	docker.waitGates[installID] <- waitResult{code: 1, err: installErr}
	docker.waitGates[installID] <- waitResult{code: 1, err: installErr}
	d := forgeDriver(docker)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}

	ev := drainToEvent(t, inst.Events(), execution.StateCrashed)
	if !strings.Contains(ev.Detail, "after 3 attempts") {
		t.Fatalf("crash detail = %q, want 'after 3 attempts'", ev.Detail)
	}
	// 3 install containers, no launch.
	if got := docker.names(); len(got) != 3 {
		t.Fatalf("created containers = %v, want 3 install attempts", got)
	}
}

// Stop during the retry backoff aborts the retry and reports stopped (issue #1128).
func TestForgeInstallRetryStopDuringBackoff(t *testing.T) {
	// Use a long backoff so Stop arrives during the sleep.
	prev := installRetryBackoff
	installRetryBackoff = []time.Duration{2 * time.Second, 2 * time.Second}
	t.Cleanup(func() { installRetryBackoff = prev })

	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	installErr := statusError{method: "POST", path: "/wait", code: 200, message: "download failed"}
	docker.waitGates[installID] = make(chan waitResult, 1)
	docker.waitGates[installID] <- waitResult{code: 1, err: installErr}
	// A generous StopTimeout so Stop's waitExitDone does not time out before the
	// supervisor's backoff poll notices the stopping latch (50ms tick).
	d := New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{
		WorkerID:             "w1",
		StopTimeout:          500 * time.Millisecond,
		GameBindIP:           "0.0.0.0",
		ReadinessTimeout:     20 * time.Millisecond,
		ConflictPollInterval: time.Millisecond,
		ConflictDeadline:     100 * time.Millisecond,
	})

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}

	// Wait for the first attempt to fail and enter backoff, then Stop.
	// The first install is removed before backoff, so wait until that happens.
	deadline := time.After(2 * time.Second)
	for !docker.wasRemoved(installID) {
		select {
		case <-deadline:
			t.Fatal("timed out waiting for first install to be removed")
		default:
			time.Sleep(time.Millisecond)
		}
	}

	if err := inst.Stop(context.Background(), false); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	// Only 1 install container created (the retry never ran because Stop arrived).
	if got := docker.names(); len(got) != 1 {
		t.Fatalf("created containers = %v, want only 1 install (retry aborted)", got)
	}
}

// installBackoffOrStopping must detect a stop via the sticky stopRequested flag,
// not the transient stopping flag: a Stop whose kill fails or is survived clears
// stopping but leaves stopRequested set. Without the sticky read, the backoff
// poll misses the abort and the retry proceeds (issue #1442).
func TestInstallBackoffOrStoppingReadsStopRequested(t *testing.T) {
	inst := &instance{}
	// Simulate a Stop whose kill was survived: stopping is cleared but
	// stopRequested remains set.
	inst.stopRequested = true
	inst.stopping = false

	got := inst.installBackoffOrStopping(100 * time.Millisecond)
	if !got {
		t.Fatal("installBackoffOrStopping returned false with stopRequested=true, stopping=false; want true (issue #1442)")
	}
}

// An install container that survives docker kill and then dies after the
// survived-kill latch reset must still be recorded stopped (not a spurious
// crash): the sticky stopRequested flag tells superviseInstall the exit was
// operator-requested, even though the transient stopping latch was cleared by
// the survived-kill failure path (issue #595, mirrors #257 for the install phase).
func TestInstallSurvivedKillThenLateExitRecordsStopped(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	// Use waitGates so superviseInstall's Wait blocks until we push a result.
	docker.waitGates[installID] = make(chan waitResult)
	// Stop must not release the install Wait (falls through to kill), and Kill
	// must not release it either (the container "survives" the kill for the full
	// waitExitDone timeout).
	docker.stopNoExitIDs = map[string]bool{installID: true}
	docker.killNoExitIDs = map[string]bool{installID: true}
	d := New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{
		WorkerID:             "w1",
		StopTimeout:          50 * time.Millisecond,
		GameBindIP:           "0.0.0.0",
		ReadinessTimeout:     20 * time.Millisecond,
		ConflictPollInterval: time.Millisecond,
		ConflictDeadline:     100 * time.Millisecond,
	})

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}

	// Stop escalates to kill, the kill "survives" the whole waitExitDone timeout,
	// so Stop returns an error and resets the stopping latch.
	if err := inst.Stop(context.Background(), false); err == nil {
		t.Fatal("expected Stop to fail when the install container survives docker kill")
	}

	// The install container finally exits. superviseInstall must read the sticky
	// stopRequested (not the cleared stopping) and report stopped.
	docker.waitGates[installID] <- waitResult{code: 137}

	drainTo(t, inst.Events(), execution.StateStopped)
	if got := inst.Status(); got != execution.StateStopped {
		t.Fatalf("Status = %v after the late install exit, want stopped (not a spurious crash)", got)
	}
}

// The exitObserved guard in the install phase prevents the survived-kill restore
// from stomping a terminal state that superviseInstall already set. The install
// container exits in the window between waitExitDone timing out and the
// survived-kill restore re-acquiring the lock; superviseInstall sets exitObserved
// under the lock, and the restore skips the reset (issue #595, mirrors #392 for
// the install phase).
func TestInstallSurvivedKillRestoreDoesNotStompTerminalState(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	// Use waitGates so superviseInstall's Wait blocks until we push a result.
	docker.waitGates[installID] = make(chan waitResult)
	// Stop falls through to kill; kill does not release the container.
	docker.stopNoExitIDs = map[string]bool{installID: true}
	docker.killNoExitIDs = map[string]bool{installID: true}
	d := New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{
		WorkerID:             "w1",
		StopTimeout:          50 * time.Millisecond,
		GameBindIP:           "0.0.0.0",
		ReadinessTimeout:     20 * time.Millisecond,
		ConflictPollInterval: time.Millisecond,
		ConflictDeadline:     100 * time.Millisecond,
	})

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	contInst := inst.(*instance)

	contInst.beforeSurvivedReset = func() {
		// The install container exits in the window; wait for superviseInstall to
		// record the terminal state before the restore re-acquires the lock.
		docker.waitGates[installID] <- waitResult{code: 137}
		drainTo(t, inst.Events(), execution.StateStopped)
	}

	// The container exits during the window, so Stop succeeds rather than
	// reporting a survived-kill failure.
	if err := inst.Stop(context.Background(), false); err != nil {
		t.Fatalf("Stop = %v, want nil once the container exits in the wait window", err)
	}
	if got := inst.Status(); got != execution.StateStopped {
		t.Fatalf("Status = %v after the exit-in-window Stop, want stopped (not stomped back)", got)
	}
}

// A Stop that arrives after the first install attempt fails but before the retry
// container is started must win: the retry container is never started, Stop
// returns nil, and the instance reaches stopped. Without the guarded publish
// (issue #1987), Stop captures a stale containerID pointing at the removed old
// container, gets 404s, checks the stale exitObserved=true, and returns a false
// nil while the retried installer keeps running.
func TestForgeInstallRetryStopWinsLatchBeforeRetryStart(t *testing.T) {
	prev := installRetryBackoff
	installRetryBackoff = []time.Duration{time.Millisecond, time.Millisecond}
	t.Cleanup(func() { installRetryBackoff = prev })

	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	installErr := statusError{method: "POST", path: "/wait", code: 200, message: "install exited 1"}
	// Unbuffered: Wait blocks until we push a result, giving us time to set the
	// hook before the retry path runs.
	docker.waitGates[installID] = make(chan waitResult)

	hookCh := make(chan struct{})
	d := New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{
		WorkerID:             "w1",
		StopTimeout:          500 * time.Millisecond,
		GameBindIP:           "0.0.0.0",
		ReadinessTimeout:     20 * time.Millisecond,
		ConflictPollInterval: time.Millisecond,
		ConflictDeadline:     100 * time.Millisecond,
	})

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	contInst := inst.(*instance)

	// Install the hook while the supervisor is blocked in Wait (attempt 0):
	// no data race because the supervisor cannot read the field until we push
	// the exit result below.
	contInst.beforeRetryStart = func() {
		// Signal the main goroutine to issue Stop.
		close(hookCh)
		// Wait for Stop to set the latch (give it plenty of time).
		deadline := time.After(2 * time.Second)
		for {
			contInst.mu.Lock()
			sr := contInst.stopRequested
			contInst.mu.Unlock()
			if sr {
				return
			}
			select {
			case <-deadline:
				return
			default:
				time.Sleep(time.Millisecond)
			}
		}
	}

	// Trigger attempt 0 failure → retry path.
	docker.waitGates[installID] <- waitResult{code: 1, err: installErr}

	// Wait for the hook to fire (meaning attempt 0 failed, retry container created).
	select {
	case <-hookCh:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for beforeRetryStart hook")
	}

	// Issue Stop: it must return nil (success) because the guarded publish
	// detects stopRequested and aborts the retry.
	if err := inst.Stop(context.Background(), false); err != nil {
		t.Fatalf("Stop: %v", err)
	}

	drainTo(t, inst.Events(), execution.StateStopped)

	// The retry container must never have been started (only the initial
	// attempt's container was started).
	docker.mu.Lock()
	startCount := 0
	for _, s := range docker.started {
		if s == installID {
			startCount++
		}
	}
	docker.mu.Unlock()
	if startCount > 1 {
		t.Fatalf("retry container %q was started %d times; want at most 1 (initial attempt only)", installID, startCount)
	}

	// The retry container must have been removed (cleanup of the unstarted container).
	if !docker.wasRemoved(installID) {
		t.Fatal("retry container was not removed after Stop aborted it")
	}
}

// When install attempt N exits (non-zero, triggering a retry) and attempt N+1
// starts a new container, exitObserved must be reset so a Stop during attempt
// N+1 whose kill is survived still triggers the survived-kill restore. Without
// the reset, exitObserved stays true from the old container and Stop returns a
// false nil — the new container is still alive (issue #595).
func TestInstallRetryResetsExitObservedForNewContainer(t *testing.T) {
	prev := installRetryBackoff
	installRetryBackoff = []time.Duration{time.Millisecond, time.Millisecond}
	t.Cleanup(func() { installRetryBackoff = prev })

	dir := t.TempDir()
	docker := newForgeFakeDocker()
	installID := "mcsd-s1-install"
	installErr := statusError{method: "POST", path: "/wait", code: 200, message: "install exited 1"}
	// Attempt 0: non-zero exit → retry. Attempt 1: blocks on waitGate so Stop
	// can race it.
	docker.waitGates[installID] = make(chan waitResult, 1)
	docker.waitGates[installID] <- waitResult{code: 1, err: installErr}
	// Stop and Kill on the retry container must NOT release Wait (survived kill).
	docker.stopNoExitIDs = map[string]bool{installID: true}
	docker.killNoExitIDs = map[string]bool{installID: true}
	d := New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{
		WorkerID:             "w1",
		StopTimeout:          50 * time.Millisecond,
		GameBindIP:           "0.0.0.0",
		ReadinessTimeout:     20 * time.Millisecond,
		ConflictPollInterval: time.Millisecond,
		ConflictDeadline:     100 * time.Millisecond,
	})

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}

	// Wait until the retry container is started (issue #1987: with the guarded
	// publish, we must wait for Start to complete — not just Create — otherwise
	// Stop may win the lock before Start and abort the retry instead of racing
	// the Wait). The install container name is reused, so count Start calls.
	deadline := time.After(2 * time.Second)
	for {
		docker.mu.Lock()
		startCount := 0
		for _, s := range docker.started {
			if s == installID {
				startCount++
			}
		}
		docker.mu.Unlock()
		if startCount >= 2 {
			break
		}
		select {
		case <-deadline:
			t.Fatal("timed out waiting for retry install container to be started")
		default:
			time.Sleep(time.Millisecond)
		}
	}

	// Stop during attempt 1: the kill survives, so Stop must fail (not return
	// a false nil from a stale exitObserved left by attempt 0).
	if err := inst.Stop(context.Background(), false); err == nil {
		t.Fatal("expected Stop to fail when the retry container survives docker kill; got nil (stale exitObserved?)")
	}
}

// drainToEvent collects status events until it sees want, returning the matching
// event so the caller can inspect its Detail field.
func drainToEvent(t *testing.T, ch <-chan execution.StatusEvent, want execution.ServerState) execution.StatusEvent {
	t.Helper()
	deadline := time.After(2 * time.Second)
	for {
		select {
		case ev, ok := <-ch:
			if !ok {
				t.Fatalf("event channel closed before reaching %v", want)
			}
			if ev.State == want {
				return ev
			}
		case <-deadline:
			t.Fatalf("timed out waiting for %v", want)
		}
	}
}

func hasArg(args []string, want string) bool {
	for _, a := range args {
		if a == want {
			return true
		}
	}
	return false
}
