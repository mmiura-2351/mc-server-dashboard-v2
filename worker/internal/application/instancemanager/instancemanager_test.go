package instancemanager

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/rcon"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// fakeDriver records starts and hands out fakeInstances.
type fakeDriver struct {
	mu       sync.Mutex
	started  []execution.InstanceSpec
	inst     *fakeInstance
	startErr error
}

func (d *fakeDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.startErr != nil {
		return nil, d.startErr
	}
	d.started = append(d.started, spec)
	d.inst = newFakeInstance(spec.ServerID)
	return d.inst, nil
}

func (d *fakeDriver) startCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.started)
}

type fakeInstance struct {
	mu       sync.Mutex
	serverID string
	state    execution.ServerState
	events   chan execution.StatusEvent
	stopped  bool
	graceful bool
	// Keep probe liveness independent of cached state so failed-stop tests can model their divergence.
	// A fake derived from state would make convergence checks tautological.
	alive    bool
	aliveErr error
	// probes counts ProbeAlive calls so a test can anchor on the converger's own
	// progress instead of a wall-clock sleep.
	probes int
	// seq, when set, records a "stop" marker on Stop so a test can assert the terminate ordered against the RCON
	// recorder (the flush-before-stop).
	seq *[]string
}

func newFakeInstance(id string) *fakeInstance {
	i := &fakeInstance{
		serverID: id,
		state:    execution.StateRunning,
		alive:    true,
		events:   make(chan execution.StatusEvent, 8),
	}
	i.events <- execution.StatusEvent{ServerID: id, State: execution.StateRunning}
	return i
}

func (i *fakeInstance) Stop(_ context.Context, graceful bool, _ ...func(context.Context) bool) error {
	i.mu.Lock()
	i.stopped = true
	i.graceful = graceful
	i.state = execution.StateStopped
	i.alive = false
	if i.seq != nil {
		*i.seq = append(*i.seq, "stop")
	}
	i.mu.Unlock()
	i.events <- execution.StatusEvent{ServerID: i.serverID, State: execution.StateStopped}
	return nil
}

func (i *fakeInstance) Status() execution.ServerState {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.state
}

// ProbeAlive answers from the independently-held alive/aliveErr, never from state. A non-nil aliveErr models a
// daemon that cannot answer at all, the case the converger reports as `unknown`.
func (i *fakeInstance) ProbeAlive(context.Context) (bool, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.probes++
	if i.aliveErr != nil {
		return false, i.aliveErr
	}
	return i.alive, nil
}

// setAlive fixes what the next ProbeAlive answers, independently of state.
func (i *fakeInstance) setAlive(alive bool, err error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.alive = alive
	i.aliveErr = err
}

// probeCount reports how many times the converger has probed this instance.
func (i *fakeInstance) probeCount() int {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.probes
}

func (i *fakeInstance) Events() <-chan execution.StatusEvent { return i.events }

func (i *fakeInstance) wasStopped() (stopped, graceful bool) {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.stopped, i.graceful
}

// fakeControl records RCON ordering alongside other fake actions.
// Per-line errors simulate a command that may have executed before its reply failed.
type fakeControl struct {
	reply           string
	err             error
	lines           []string
	seq             *[]string
	failOnCancelled bool
	failLines       map[string]error
	poison          bool
	broken          bool
}

func (c *fakeControl) Execute(ctx context.Context, line string) (string, error) {
	if c.poison && c.broken {
		// A prior Execute error poisoned the connection: every reuse fails fast,
		// exactly as rcon.Client does, so the quiesce restore must redial.
		return "", rcon.ErrConnBroken
	}
	if c.failOnCancelled {
		if err := ctx.Err(); err != nil {
			return "", err
		}
	}
	c.lines = append(c.lines, line)
	if c.seq != nil {
		*c.seq = append(*c.seq, line)
	}
	if err, ok := c.failLines[line]; ok {
		if c.poison {
			c.broken = true
		}
		return "", err
	}
	if c.err != nil {
		if c.poison {
			c.broken = true
		}
		return "", c.err
	}
	return c.reply, nil
}

func (c *fakeControl) Close() error { return nil }

// rconFailInstance models a driver instance whose RCON "stop" fails, causing the driver to fall back to docker
// stop. The preFallback hook is invoked (if supplied) on the graceful path, just as the real driver does. This
// lets the instancemanager tests verify the flush wiring.
type rconFailInstance struct {
	*fakeInstance
}

func newRconFailInstance(id string) *rconFailInstance {
	return &rconFailInstance{fakeInstance: newFakeInstance(id)}
}

func (i *rconFailInstance) Stop(ctx context.Context, graceful bool, preFallback ...func(context.Context) bool) error {
	// Call the pre-fallback hook (the flush) before terminate, just as the real
	// containerdriver does on the graceful path. Honor the return value: when
	// the flush succeeds (returns true), the real driver skips RCON stop and
	// docker stop entirely — but rconFailInstance always terminates, so here
	// we just record the call for test observability.
	if graceful && len(preFallback) > 0 && preFallback[0] != nil {
		_ = preFallback[0](ctx)
	}
	return i.fakeInstance.Stop(ctx, graceful)
}

// rconFailDriver hands out rconFailInstances.
type rconFailDriver struct {
	mu   sync.Mutex
	inst *rconFailInstance
}

func (d *rconFailDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.inst = newRconFailInstance(spec.ServerID)
	return d.inst, nil
}

func newManager(t *testing.T, d execution.ExecutionDriver, ctrl execution.ServerControl) *Manager {
	t.Helper()
	scratch := t.TempDir()
	m := New(map[string]execution.ExecutionDriver{"container": d}, scratch,
		func(context.Context, string, string, string) (execution.ServerControl, error) {
			// Model an unwired control as a dial error; production never returns a nil control without an error.
			if ctrl == nil {
				return nil, fmt.Errorf("test: no rcon control configured")
			}
			return ctrl, nil
		})
	// Drop the quiesce settle-wait poll interval to zero by default so a running-id snapshot test does not pay the
	// real 2s poll; tests that exercise the settle-wait itself override it explicitly.
	m.settlePollInterval = 0
	closeWithTest(t, m)
	return m
}

// Register cleanup immediately so assertions that abort a test still close the Manager and join its goroutines.
func closeWithTest(t *testing.T, m *Manager) {
	t.Helper()
	t.Cleanup(m.Close)
}

func startCmd() session.Command {
	return session.Command{CommandID: "c1", ServerID: "s1", Kind: "StartServer", Driver: "container", MinecraftVersion: "1.21"}
}

// A StartServer launches the driver against the id's working dir under scratch, the working set the API's
// preceding HydrateTrigger put there (control_plane.proto, StartServer). The start no longer conjures that
// directory when it is missing: see TestStartRefusedWhenWorkingDirAbsent.
func TestStartServerLaunchesAgainstTheWorkingDir(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	wantDir := seedScratch(t, m, "s1")

	res := m.Handle(context.Background(), startCmd())
	if !res.Success {
		t.Fatalf("StartServer result = %+v, want success", res)
	}
	if d.startCount() != 1 {
		t.Fatalf("driver started %d times, want 1", d.startCount())
	}
	d.mu.Lock()
	gotDir := d.started[0].WorkingDir
	d.mu.Unlock()
	if gotDir != wantDir {
		t.Fatalf("working dir = %q, want %q", gotDir, wantDir)
	}
	if info, err := os.Stat(wantDir); err != nil || !info.IsDir() {
		t.Fatalf("working dir missing: %v", err)
	}
}

// Missing scratch must return the API's working-set-absent refusal so launch replays with a full hydrate.
func TestStartRefusedWhenWorkingDirAbsent(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)

	res := m.Handle(context.Background(), startCmd())

	if res.Success {
		t.Fatalf("StartServer over an absent working dir = %+v, want a refusal", res)
	}
	if res.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("ErrorCode = %v, want %v", res.ErrorCode, session.CommandErrorServerNotFound)
	}
	if !strings.Contains(res.ErrorMessage, "working dir absent") {
		t.Fatalf("refusal message = %q, want the API-pinned phrase \"working dir absent\"", res.ErrorMessage)
	}
	if d.startCount() != 0 {
		t.Fatalf("driver started %d times, want 0: the refusal must precede driver.Start", d.startCount())
	}
	// The refusal must not leave the empty directory behind either: launchReserved's
	// MkdirAll is exactly what would have made the boot look healthy.
	if _, err := os.Stat(filepath.Join(m.scratchDir, "s1")); !os.IsNotExist(err) {
		t.Fatalf("working dir stat err = %v, want it to still be absent", err)
	}
	// The refusal releases the reservation it took, so the corrective launch, the API's re-dispatch after its own
	// hydrate, is not refused BUSY behind this one (the leak shape of). Read the map directly rather than inferring
	// it from a follow-up command's code: a command that fails BEFORE reserve would make the inference vacuous.
	m.mu.Lock()
	leaked := m.reserved["s1"]
	m.mu.Unlock()
	if leaked {
		t.Fatal("reserved[s1] leaked after the working-set refusal: the corrective start would be refused BUSY")
	}
	// And the corrective start itself succeeds once the working set is there.
	seedScratch(t, m, "s1")
	if start := m.Handle(context.Background(), startCmd()); !start.Success {
		t.Fatalf("start after the refusal = %+v, want success once the working set is present", start)
	}
}

// Marker-only scratch is a legitimate first start after a 204 hydrate; a content-based launch guard would reject
// it.
func TestStartAcceptsMarkerOnlyWorkingDir(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	dir := filepath.Join(m.scratchDir, "s1")
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, generationFile), []byte("0"), 0o640); err != nil {
		t.Fatal(err)
	}

	res := m.Handle(context.Background(), startCmd())

	if !res.Success {
		t.Fatalf("StartServer over a marker-only working dir = %+v, want success (the 204 contract)", res)
	}
	if d.startCount() != 1 {
		t.Fatalf("driver started %d times, want 1", d.startCount())
	}
}

// An emptied directory and a marker-temp-only directory both lack the exact marker required by launch.
func TestStartRefusedWhenScratchEmptiedInPlace(t *testing.T) {
	for _, tc := range []struct {
		name string
		seed func(t *testing.T, dir string)
	}{
		{
			name: "empty dir",
			seed: func(*testing.T, string) {},
		},
		{
			name: "marker temp only",
			seed: func(t *testing.T, dir string) {
				t.Helper()
				if err := os.WriteFile(filepath.Join(dir, generationFile+"-123"), []byte("7"), 0o640); err != nil {
					t.Fatal(err)
				}
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := &fakeDriver{}
			m := newManager(t, d, nil)
			dir := filepath.Join(m.scratchDir, "s1")
			if err := os.MkdirAll(dir, 0o750); err != nil {
				t.Fatal(err)
			}
			tc.seed(t, dir)

			res := m.Handle(context.Background(), startCmd())

			if res.Success {
				t.Fatalf("StartServer over an emptied working dir = %+v, want a refusal", res)
			}
			if res.ErrorCode != session.CommandErrorServerNotFound {
				t.Fatalf("ErrorCode = %v, want %v", res.ErrorCode, session.CommandErrorServerNotFound)
			}
			if !strings.Contains(res.ErrorMessage, "working dir absent") {
				t.Fatalf("refusal message = %q, want the API-pinned phrase %q", res.ErrorMessage, "working dir absent")
			}
			if d.startCount() != 0 {
				t.Fatalf("driver started %d times, want 0: the refusal must precede driver.Start", d.startCount())
			}
			m.mu.Lock()
			leaked := m.reserved["s1"]
			m.mu.Unlock()
			if leaked {
				t.Fatal("reserved[s1] leaked after the working-set refusal: the corrective start would be refused BUSY")
			}
		})
	}
}

// A restart must apply the same working-set guard to relaunch after stop; failure leaves API reconciliation to
// recover.
func TestRestartRefusedWhenWorkingSetDestroyed(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{})
	startRunning(t, m)
	dir := filepath.Join(m.scratchDir, "s1")
	if err := os.RemoveAll(dir); err != nil {
		t.Fatal(err)
	}

	res := m.Handle(context.Background(), session.Command{CommandID: "r", ServerID: "s1", Kind: "RestartServer"})

	if res.Success {
		t.Fatalf("RestartServer over a destroyed working set = %+v, want a refusal", res)
	}
	if res.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("ErrorCode = %v, want %v", res.ErrorCode, session.CommandErrorServerNotFound)
	}
	if !strings.Contains(res.ErrorMessage, "working dir absent") {
		t.Fatalf("refusal message = %q, want the API-pinned phrase %q", res.ErrorMessage, "working dir absent")
	}
	if res.CommandID != "r" {
		t.Fatalf("CommandID = %q, want the RestartServer's own correlation id", res.CommandID)
	}
	if d.startCount() != 1 {
		t.Fatalf("driver started %d times, want 1 (the original start only): the relaunch must be refused before driver.Start", d.startCount())
	}
	// The refusal must not manufacture the directory it refused over — that MkdirAll
	// is precisely what made the empty boot look healthy.
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("working dir stat err = %v, want it to still be absent", err)
	}
	// The instance is evicted (the restart's stop confirmed) and the id is left unreserved, so the API's corrective
	// re-launch is not refused BUSY behind it (the leak shape of). Read both maps directly rather than inferring
	// them from a follow-up command.
	m.mu.Lock()
	_, tracked := m.instances["s1"]
	leaked := m.reserved["s1"]
	m.mu.Unlock()
	if tracked {
		t.Fatal("instances[s1] still tracked after the refused relaunch: the server is down, not running")
	}
	if leaked {
		t.Fatal("reserved[s1] leaked after the refused relaunch: the corrective start would be refused BUSY")
	}
	// And the corrective launch — the reconciler's redispatch_start after the API's
	// own hydrate — succeeds once the working set is back.
	seedScratch(t, m, "s1")
	if start := m.Handle(context.Background(), startCmd()); !start.Success {
		t.Fatalf("start after the refused relaunch = %+v, want success once the working set is present", start)
	}
}

// The command's resource allocation reaches the InstanceSpec: the memory limit (bytes on the wire) is converted
// to MiB, and the CPU allocation (millicores) is carried as-is with no derivation. Unset stays 0 (default heap,
// default weight).
func TestStartCarriesResourceAllocationToSpec(t *testing.T) {
	tests := []struct {
		name             string
		memoryLimitBytes uint64
		cpuMillis        uint32
		wantMemoryMB     uint32
		wantCPUMillis    uint32
	}{
		{name: "unset allocation"},
		{name: "memory limit", memoryLimitBytes: 2048 * 1024 * 1024, wantMemoryMB: 2048},
		{name: "cpu allocation", cpuMillis: 2000, wantCPUMillis: 2000},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			d := &fakeDriver{}
			m := newManager(t, d, nil)
			seedScratch(t, m, "s1")
			cmd := startCmd()
			cmd.MemoryLimitBytes = tc.memoryLimitBytes
			cmd.CPUMillis = tc.cpuMillis

			if res := m.Handle(context.Background(), cmd); !res.Success {
				t.Fatalf("StartServer = %+v, want success", res)
			}
			d.mu.Lock()
			got := d.started[0]
			d.mu.Unlock()
			if got.MemoryLimitMB != tc.wantMemoryMB {
				t.Errorf("MemoryLimitMB = %d, want %d", got.MemoryLimitMB, tc.wantMemoryMB)
			}
			if got.CPUMillis != tc.wantCPUMillis {
				t.Errorf("CPUMillis = %d, want %d", got.CPUMillis, tc.wantCPUMillis)
			}
		})
	}
}

// An unset launch mode (the default) launches with the historical JAR mode, so the spec carries LaunchModeJar,
// the byte-for-byte original behavior.
func TestStartDefaultLaunchModeIsJar(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)

	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}
	d.mu.Lock()
	got := d.started[0].LaunchMode
	d.mu.Unlock()
	if got != execution.LaunchModeJar {
		t.Fatalf("LaunchMode = %v, want LaunchModeJar", got)
	}
}

// An explicit "jar" launch mode is the same JAR launch as the default.
func TestStartJarLaunchMode(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	seedScratch(t, m, "s1")
	cmd := startCmd()
	cmd.LaunchMode = "jar"

	if res := m.Handle(context.Background(), cmd); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}
	d.mu.Lock()
	got := d.started[0].LaunchMode
	d.mu.Unlock()
	if got != execution.LaunchModeJar {
		t.Fatalf("LaunchMode = %v, want LaunchModeJar", got)
	}
}

// A "forge-argsfile" launch mode threads LaunchModeForgeArgsfile onto the spec so the driver runs the
// install-then-launch sequence.
func TestStartForgeLaunchMode(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	seedScratch(t, m, "s1")
	cmd := startCmd()
	cmd.LaunchMode = "forge-argsfile"

	if res := m.Handle(context.Background(), cmd); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}
	d.mu.Lock()
	got := d.started[0].LaunchMode
	d.mu.Unlock()
	if got != execution.LaunchModeForgeArgsfile {
		t.Fatalf("LaunchMode = %v, want LaunchModeForgeArgsfile", got)
	}
}

// An unrecognized launch mode is a malformed command: it fails with INTERNAL (an unpinned code, so the contract
// table is untouched) and never starts the driver.
func TestStartUnknownLaunchMode(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	cmd := startCmd()
	cmd.LaunchMode = "bogus"

	res := m.Handle(context.Background(), cmd)
	if res.Success || res.ErrorCode != session.CommandErrorInternal {
		t.Fatalf("unknown launch mode = %+v, want INTERNAL failure", res)
	}
	if d.startCount() != 0 {
		t.Fatalf("driver started %d times, want 0 for an unknown launch mode", d.startCount())
	}
}

func TestStartTwiceIsInvalidState(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)

	seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())
	res := m.Handle(context.Background(), startCmd())
	if res.Success || res.ErrorCode != session.CommandErrorInvalidState {
		t.Fatalf("second start = %+v, want INVALID_STATE failure", res)
	}
}

func TestStartUnknownDriver(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	cmd := startCmd()
	cmd.Driver = "nonexistent" // not registered

	res := m.Handle(context.Background(), cmd)
	if res.Success || res.ErrorCode != session.CommandErrorDriverUnavailable {
		t.Fatalf("unknown driver = %+v, want DRIVER_UNAVAILABLE", res)
	}
}

func TestStopUnknownServer(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	res := m.Handle(context.Background(), session.Command{CommandID: "c2", ServerID: "ghost", Kind: "StopServer"})
	if res.Success || res.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("stop unknown = %+v, want SERVER_NOT_FOUND", res)
	}
}

func TestStopServerGraceful(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())

	res := m.Handle(context.Background(), session.Command{CommandID: "c3", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	if stopped, graceful := d.inst.wasStopped(); !stopped || !graceful {
		t.Fatalf("instance not gracefully stopped: stopped=%v graceful=%v", stopped, graceful)
	}
}

// A graceful stop whose RCON "stop" succeeds: the fakeInstance does not call the preFallback hook (it is a
// minimal fake), so no save-all appears in the sequence. The real driver calls preFallback always before stop on
// the graceful path.
func TestStopServerGracefulRCONSuccessSkipsFlush(t *testing.T) {
	var seq []string
	d := &fakeDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq}
	m := newManager(t, d, ctrl)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	d.inst.seq = &seq

	res := m.Handle(context.Background(), session.Command{CommandID: "c3", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	// The fakeInstance does not call the preFallback hook (minimal fake), so no
	// save-all appears. The real driver calls preFallback always before stop.
	want := []string{"stop"}
	if !equalLines(seq, want) {
		t.Fatalf("operation order = %v, want %v (fakeInstance does not exercise preFallback)", seq, want)
	}
}

// When the driver calls the preFallback hook on the graceful path, the flush must run: save-all + settle lands
// the dirty chunks on disk before the process is terminated. The rconFailInstance models this by calling the
// preFallback hook.
func TestStopServerGracefulRCONFailureFlushesBeforeTerminate(t *testing.T) {
	var seq []string
	d := &rconFailDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq}
	m := newManager(t, d, ctrl)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	d.inst.seq = &seq

	res := m.Handle(context.Background(), session.Command{CommandID: "c3", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	want := []string{"save-off", "save-all", "stop"}
	if !equalLines(seq, want) {
		t.Fatalf("operation order = %v, want %v (graceful stop must save-off then flush before terminate)", seq, want)
	}
}

// A force stop (cmd.Force) is the operator's "kill it now" escape hatch and must NOT attempt the graceful
// save-all flush, it intentionally skips the save so a wedged or unresponsive server can still be terminated.
func TestStopServerForceSkipsFlush(t *testing.T) {
	var seq []string
	d := &fakeDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq}
	m := newManager(t, d, ctrl)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	d.inst.seq = &seq

	res := m.Handle(context.Background(), session.Command{CommandID: "c3", ServerID: "s1", Kind: "StopServer", Force: true})
	if !res.Success {
		t.Fatalf("force stop = %+v, want success", res)
	}
	if len(ctrl.lines) != 0 {
		t.Fatalf("force stop issued RCON %v, want none (force skips the graceful flush)", ctrl.lines)
	}
	want := []string{"stop"}
	if !equalLines(seq, want) {
		t.Fatalf("operation order = %v, want %v (force stop terminates without a save)", seq, want)
	}
}

// Fail save-all inside the graceful-stop hook and require stop to proceed despite the best-effort flush failure.
func TestStopServerGracefulProceedsWhenSaveFails(t *testing.T) {
	var seq []string
	d := &rconFailDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq, failLines: map[string]error{"save-all": fmt.Errorf("rcon down")}}
	m := newManager(t, d, ctrl)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	d.inst.seq = &seq

	res := m.Handle(context.Background(), session.Command{CommandID: "c3", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success even when the save-all flush failed", res)
	}
	if stopped, graceful := d.inst.wasStopped(); !stopped || !graceful {
		t.Fatalf("instance not gracefully stopped after failed flush: stopped=%v graceful=%v", stopped, graceful)
	}
}

// A failed save-off on the graceful-stop flush must still proceed to save-all: save-off is best-effort, if it
// fails, the flush continues with save-all so dirty chunks still land on disk. The stop must succeed regardless.
func TestStopServerGracefulProceedsWhenSaveOffFails(t *testing.T) {
	var seq []string
	d := &rconFailDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq, failLines: map[string]error{"save-off": fmt.Errorf("rcon down")}}
	m := newManager(t, d, ctrl)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	d.inst.seq = &seq

	res := m.Handle(context.Background(), session.Command{CommandID: "c3", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success even when save-off failed", res)
	}
	// save-off failed but save-all must still be attempted.
	want := []string{"save-off", "save-all", "stop"}
	if !equalLines(seq, want) {
		t.Fatalf("operation order = %v, want %v (save-off failure must not block save-all)", seq, want)
	}
}

// A failed save-off poisons the RCON connection, so save-all on the same connection returns ErrConnBroken.
// flushBeforeStopWithDriver must redial a fresh connection so save-all succeeds.
func TestStopServerGracefulRedialsAfterPoisonedSaveOff(t *testing.T) {
	var seq []string
	d := &rconFailDriver{}
	poisonCtrl := &fakeControl{
		reply:     "ok",
		seq:       &seq,
		failLines: map[string]error{"save-off": fmt.Errorf("rcon down")},
		poison:    true,
	}
	// After the poisoned ctrl is closed, openControl returns a fresh ctrl.
	freshCtrl := &fakeControl{reply: "ok", seq: &seq}
	var dialCount int
	scratch := t.TempDir()
	m := New(map[string]execution.ExecutionDriver{"container": d}, scratch,
		func(context.Context, string, string, string) (execution.ServerControl, error) {
			dialCount++
			if dialCount == 1 {
				return poisonCtrl, nil
			}
			return freshCtrl, nil
		})
	closeWithTest(t, m)
	m.settlePollInterval = 0
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	d.inst.seq = &seq

	res := m.Handle(context.Background(), session.Command{CommandID: "c3", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success even when save-off failed on poisoned connection", res)
	}
	want := []string{"save-off", "save-all", "stop"}
	if !equalLines(seq, want) {
		t.Fatalf("operation order = %v, want %v (must redial and issue save-all after poisoned save-off)", seq, want)
	}
	if dialCount < 2 {
		t.Fatalf("openControl dial count = %d, want >= 2 (must redial after save-off poison)", dialCount)
	}
}

// Cascading RCON failure: save-off poisons the connection, the manager redials, but save-all ALSO fails on the
// fresh connection (the Minecraft RCON server is completely unreachable). The stop must still complete, RCON
// failure should never prevent server shutdown.
func TestStopServerGracefulCompletesWhenBothRCONCommandsFail(t *testing.T) {
	var seq []string
	d := &rconFailDriver{}
	poisonCtrl := &fakeControl{
		reply:     "ok",
		seq:       &seq,
		failLines: map[string]error{"save-off": fmt.Errorf("rcon down")},
		poison:    true,
	}
	// The fresh (redialed) control also fails on save-all — total RCON failure.
	freshCtrl := &fakeControl{
		reply:     "ok",
		seq:       &seq,
		failLines: map[string]error{"save-all": fmt.Errorf("rcon unreachable")},
	}
	var dialCount int
	scratch := t.TempDir()
	m := New(map[string]execution.ExecutionDriver{"container": d}, scratch,
		func(context.Context, string, string, string) (execution.ServerControl, error) {
			dialCount++
			if dialCount == 1 {
				return poisonCtrl, nil
			}
			return freshCtrl, nil
		})
	closeWithTest(t, m)
	m.settlePollInterval = 0
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	d.inst.seq = &seq

	res := m.Handle(context.Background(), session.Command{CommandID: "c3", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success even when both RCON commands failed", res)
	}
	if stopped, graceful := d.inst.wasStopped(); !stopped || !graceful {
		t.Fatalf("instance not gracefully stopped after total RCON failure: stopped=%v graceful=%v", stopped, graceful)
	}
	if dialCount < 2 {
		t.Fatalf("openControl dial count = %d, want >= 2 (must redial after save-off poison)", dialCount)
	}
}

func TestServerCommandForwardsOutput(t *testing.T) {
	d := &fakeDriver{}
	ctrl := &fakeControl{reply: "There are 0 players"}
	m := newManager(t, d, ctrl)
	seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())

	res := m.Handle(context.Background(), session.Command{CommandID: "c4", ServerID: "s1", Kind: "ServerCommand", Line: "list"})
	if !res.Success || res.Output != "There are 0 players" {
		t.Fatalf("ServerCommand = %+v, want output", res)
	}
	if len(ctrl.lines) != 1 || ctrl.lines[0] != "list" {
		t.Fatalf("forwarded lines = %v, want [list]", ctrl.lines)
	}
}

// Pass each running server's actual driver to openControl; advertised drivers do not determine its RCON
// topology.
func TestOpenControlReceivesRunningServerDriver(t *testing.T) {
	var gotDriver string
	scratch := t.TempDir()
	drivers := map[string]execution.ExecutionDriver{
		"container": &fakeDriver{},
		"docker":    &fakeDriver{},
	}
	m := New(drivers, scratch, func(_ context.Context, _, driver, _ string) (execution.ServerControl, error) {
		gotDriver = driver
		return &fakeControl{reply: "ok"}, nil
	})
	closeWithTest(t, m)

	// Start a container server on the mixed-driver worker.
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}

	res := m.Handle(context.Background(), session.Command{CommandID: "c6", ServerID: "s1", Kind: "ServerCommand", Line: "list"})
	if !res.Success {
		t.Fatalf("ServerCommand = %+v, want success", res)
	}
	if gotDriver != "container" {
		t.Fatalf("openControl driver = %q, want container (the driver that started the server)", gotDriver)
	}
}

// versionRecorder is an openControl double that records the Minecraft version
// every RCON dial carries, handing out a control that answers every line.
type versionRecorder struct {
	mu       sync.Mutex
	versions []string
}

func (r *versionRecorder) open(_ context.Context, _, _, mcVersion string) (execution.ServerControl, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.versions = append(r.versions, mcVersion)
	return &fakeControl{reply: "ok"}, nil
}

func (r *versionRecorder) dials() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.versions...)
}

// newVersionRecordingManager builds a Manager over d whose RCON dials land in
// the returned recorder.
func newVersionRecordingManager(t *testing.T, d execution.ExecutionDriver) (*Manager, *versionRecorder) {
	t.Helper()
	rec := &versionRecorder{}
	m := New(map[string]execution.ExecutionDriver{"container": d}, t.TempDir(), rec.open).WithTransfer(&fakeTransfer{})
	m.settlePollInterval = 0
	closeWithTest(t, m)
	return m, rec
}

// assertDialsCarry fails unless at least one RCON dial happened and every one
// carried want.
func assertDialsCarry(t *testing.T, got []string, want string) {
	t.Helper()
	if len(got) == 0 {
		t.Fatal("no RCON dial happened")
	}
	for i, v := range got {
		if v != want {
			t.Fatalf("RCON dial %d carried Minecraft version %q, want %q", i, v, want)
		}
	}
}

// TestOpenControlReceivesTheServerMinecraftVersion pins that every RCON dial for a server carries that server's
// Minecraft version, which decides the charset its RCON password is read in. The running-server paths take it
// from the StartServer command; the stop paths, which evict that command before they dial, carry it alongside
// the driver name.
func TestOpenControlReceivesTheServerMinecraftVersion(t *testing.T) {
	for _, tc := range []struct {
		name string
		cmd  session.Command
	}{
		{"console command", session.Command{CommandID: "c", ServerID: "s1", Kind: "ServerCommand", Line: "list"}},
		{"snapshot quiesce", snapshotCmd()},
		{"graceful stop flush", session.Command{CommandID: "c", ServerID: "s1", Kind: "StopServer"}},
		{"restart flush", session.Command{CommandID: "c", ServerID: "s1", Kind: "RestartServer"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m, rec := newVersionRecordingManager(t, &rconFailDriver{})
			startRunning(t, m)

			_ = m.Handle(context.Background(), tc.cmd)

			assertDialsCarry(t, rec.dials(), startCmd().MinecraftVersion)
		})
	}
}

func TestStatusEventsAreForwarded(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())

	// The manager forwards the instance's events, mapping to session.StatusEvent.
	select {
	case ev := <-m.Events():
		if ev.ServerID != "s1" || ev.State != "running" {
			t.Fatalf("forwarded event = %+v, want s1 running", ev)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no status event forwarded")
	}
}

// A crash the driver classified reaches the session with its reason's wire name beside the detail.
func TestCrashReasonIsForwarded(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())
	<-m.Events() // the seeded running event

	d.inst.events <- execution.StatusEvent{
		ServerID: "s1", State: execution.StateCrashed, Detail: "boom",
		CrashReason: execution.CrashReasonForgeInstallJavaIncompatible,
	}

	select {
	case ev := <-m.Events():
		if ev.State != "crashed" || ev.Detail != "boom" || ev.CrashReason != "forge_install_java_incompatible" {
			t.Fatalf("forwarded event = %+v, want crashed/boom/forge_install_java_incompatible", ev)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no status event forwarded")
	}
}

func TestRestartStopsAndStarts(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())
	first := d.inst

	res := m.Handle(context.Background(), session.Command{CommandID: "c5", ServerID: "s1", Kind: "RestartServer"})
	if !res.Success {
		t.Fatalf("restart = %+v, want success", res)
	}
	if stopped, _ := first.wasStopped(); !stopped {
		t.Fatal("restart did not stop the old instance")
	}
	if d.startCount() != 2 {
		t.Fatalf("restart started %d times total, want 2", d.startCount())
	}
}

// An in-place restart with fakeInstance (which does not call preFallback): no save-all appears in the sequence.
// The real driver calls preFallback always before stop on the graceful path.
func TestRestartRCONSuccessSkipsFlush(t *testing.T) {
	var seq []string
	d := &fakeDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq}
	m := newManager(t, d, ctrl)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	d.inst.seq = &seq

	res := m.Handle(context.Background(), session.Command{CommandID: "c5", ServerID: "s1", Kind: "RestartServer"})
	if !res.Success {
		t.Fatalf("restart = %+v, want success", res)
	}
	want := []string{"stop"}
	if !equalLines(seq, want) {
		t.Fatalf("operation order = %v, want %v (fakeInstance does not exercise preFallback)", seq, want)
	}
}

// When a restart uses rconFailInstance (which calls preFallback), the flush must run: the relaunch re-reads the
// same on-disk scratch, so unflushed dirty chunks would roll the block edits back.
func TestRestartRCONFailureFlushesBeforeTerminate(t *testing.T) {
	var seq []string
	d := &rconFailDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq}
	m := newManager(t, d, ctrl)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	d.inst.seq = &seq

	res := m.Handle(context.Background(), session.Command{CommandID: "c5", ServerID: "s1", Kind: "RestartServer"})
	if !res.Success {
		t.Fatalf("restart = %+v, want success", res)
	}
	want := []string{"save-off", "save-all", "stop"}
	if !equalLines(seq, want) {
		t.Fatalf("operation order = %v, want %v (graceful restart must save-off then flush before terminate)", seq, want)
	}
}

// A successful restart's result carries the RestartServer's correlation id, not
// the internal StartServer command's id, so the API can match it to the command
// it issued.
func TestRestartResultCarriesOriginalCorrelationID(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())

	res := m.Handle(context.Background(), session.Command{CommandID: "restart-id", ServerID: "s1", Kind: "RestartServer"})
	if !res.Success {
		t.Fatalf("restart = %+v, want success", res)
	}
	if res.CommandID != "restart-id" {
		t.Fatalf("restart result CommandID = %q, want %q", res.CommandID, "restart-id")
	}
}

// A driver Start error wrapping execution.ErrPortConflict surfaces as the sanitized port_conflict code, not the
// generic internal one.
func TestStartPortConflictSurfacesCode(t *testing.T) {
	d := &fakeDriver{startErr: fmt.Errorf("containerdriver: start: %w", execution.ErrPortConflict)}
	m := newManager(t, d, nil)

	seedScratch(t, m, "s1")
	res := m.Handle(context.Background(), startCmd())
	if res.Success || res.ErrorCode != session.CommandErrorPortConflict {
		t.Fatalf("start = %+v, want PORT_CONFLICT failure", res)
	}
}

// A driver Start error wrapping execution.ErrImageMissing surfaces as the sanitized image_missing code.
func TestStartImageMissingSurfacesCode(t *testing.T) {
	d := &fakeDriver{startErr: fmt.Errorf("containerdriver: create: %w", execution.ErrImageMissing)}
	m := newManager(t, d, nil)

	seedScratch(t, m, "s1")
	res := m.Handle(context.Background(), startCmd())
	if res.Success || res.ErrorCode != session.CommandErrorImageMissing {
		t.Fatalf("start = %+v, want IMAGE_MISSING failure", res)
	}
}

// An unclassified driver Start error keeps the generic internal code.
func TestStartUnclassifiedFailureIsInternal(t *testing.T) {
	d := &fakeDriver{startErr: fmt.Errorf("daemon unreachable")}
	m := newManager(t, d, nil)

	seedScratch(t, m, "s1")
	res := m.Handle(context.Background(), startCmd())
	if res.Success || res.ErrorCode != session.CommandErrorInternal {
		t.Fatalf("start = %+v, want INTERNAL failure", res)
	}
}

func TestUnknownKindIsInternalError(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	res := m.Handle(context.Background(), session.Command{CommandID: "c6", ServerID: "s1", Kind: "Mystery"})
	if res.Success || res.ErrorCode != session.CommandErrorInternal {
		t.Fatalf("unknown kind = %+v, want INTERNAL", res)
	}
}
