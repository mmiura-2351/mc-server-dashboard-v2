package instancemanager

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
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
	// seq, when set, records a "stop" marker on Stop so a test can assert the
	// terminate ordered against the RCON recorder (the #1007 flush-before-stop).
	seq *[]string
}

func newFakeInstance(id string) *fakeInstance {
	i := &fakeInstance{serverID: id, state: execution.StateRunning, events: make(chan execution.StatusEvent, 8)}
	i.events <- execution.StatusEvent{ServerID: id, State: execution.StateRunning}
	return i
}

func (i *fakeInstance) Stop(_ context.Context, graceful bool, _ ...func(context.Context)) error {
	i.mu.Lock()
	i.stopped = true
	i.graceful = graceful
	i.state = execution.StateStopped
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

func (i *fakeInstance) Events() <-chan execution.StatusEvent { return i.events }

func (i *fakeInstance) wasStopped() (stopped, graceful bool) {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.stopped, i.graceful
}

// fakeControl is an in-memory ServerControl for ServerCommand forwarding. When
// seq is set, every executed line is also appended to it so a test can assert the
// RCON ordering against another recorder (the snapshot save-off / save-on bracket,
// #694). failOnCancelled makes Execute return the context error when ctx is
// already cancelled, so a test can prove the deferred save-on ran on a live,
// detached context rather than the request's dead one.
//
// failLines maps a specific command line to the error Execute returns for it, so a
// test can fail one step of the quiesce bracket (e.g. save-all) while others
// succeed (#907 partial-quiesce path). When poison is set, fakeControl models the
// real rcon client: the FIRST Execute error marks the connection broken, and every
// subsequent Execute returns rcon.ErrConnBroken until the client is redialed — the
// data-loss interaction the poisoned-restore fix addresses.
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

// rconFailInstance models a driver instance whose RCON "stop" fails, causing
// the driver to fall back to docker stop. The preFallback hook is invoked (if
// supplied) on the graceful path, just as the real driver does (#1007). This
// lets the instancemanager tests verify the flush wiring.
type rconFailInstance struct {
	*fakeInstance
}

func newRconFailInstance(id string) *rconFailInstance {
	return &rconFailInstance{fakeInstance: newFakeInstance(id)}
}

func (i *rconFailInstance) Stop(ctx context.Context, graceful bool, preFallback ...func(context.Context)) error {
	// Call the pre-fallback hook (the flush) before terminate, just as the real
	// containerdriver does on the graceful path.
	if graceful && len(preFallback) > 0 && preFallback[0] != nil {
		preFallback[0](ctx)
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
	m := New(map[string]execution.ExecutionDriver{"host-process": d}, scratch,
		func(context.Context, string, string) (execution.ServerControl, error) {
			// The real openControl never yields a nil control without an error (main.go).
			// Tests that don't wire one exercise RCON-free paths; surface that as a dial
			// failure so the #1007 stop-flush (and the snapshot quiesce) degrade gracefully
			// instead of dereferencing a nil control.
			if ctrl == nil {
				return nil, fmt.Errorf("test: no rcon control configured")
			}
			return ctrl, nil
		})
	// Drop the quiesce settle-wait poll interval to zero by default so a running-id
	// snapshot test does not pay the real 2s poll (#907); tests that exercise the
	// settle-wait itself override it explicitly.
	m.settlePollInterval = 0
	return m
}

func startCmd() session.Command {
	return session.Command{CommandID: "c1", ServerID: "s1", Kind: "StartServer", Driver: "host-process", MinecraftVersion: "1.21"}
}

func TestStartServerCreatesWorkingDirAndStarts(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)

	res := m.Handle(context.Background(), startCmd())
	if !res.Success {
		t.Fatalf("StartServer result = %+v, want success", res)
	}
	if d.startCount() != 1 {
		t.Fatalf("driver started %d times, want 1", d.startCount())
	}
	wantDir := filepath.Join(m.scratchDir, "s1")
	d.mu.Lock()
	gotDir := d.started[0].WorkingDir
	d.mu.Unlock()
	if gotDir != wantDir {
		t.Fatalf("working dir = %q, want %q", gotDir, wantDir)
	}
	if info, err := os.Stat(wantDir); err != nil || !info.IsDir() {
		t.Fatalf("working dir not created: %v", err)
	}
}

// The command's memory limit (bytes on the wire, #706) is converted to MiB on the
// InstanceSpec ceiling; unset stays 0 (default heap).
func TestStartConvertsMemoryLimitBytesToSpecMiB(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	cmd := startCmd()
	cmd.MemoryLimitBytes = 2048 * 1024 * 1024

	if res := m.Handle(context.Background(), cmd); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}
	d.mu.Lock()
	got := d.started[0].MemoryLimitMB
	d.mu.Unlock()
	if got != 2048 {
		t.Fatalf("MemoryLimitMB = %d, want 2048", got)
	}
}

func TestStartDefaultMemoryLimitIsZero(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}
	d.mu.Lock()
	got := d.started[0].MemoryLimitMB
	d.mu.Unlock()
	if got != 0 {
		t.Fatalf("MemoryLimitMB = %d, want 0 (unset)", got)
	}
}

// The command's CPU allocation (millicores, #723) is carried as-is onto the
// InstanceSpec; unset stays 0 (default weight). No derivation.
func TestStartCarriesCPUMillisToSpec(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	cmd := startCmd()
	cmd.CPUMillis = 2000

	if res := m.Handle(context.Background(), cmd); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}
	d.mu.Lock()
	got := d.started[0].CPUMillis
	d.mu.Unlock()
	if got != 2000 {
		t.Fatalf("CPUMillis = %d, want 2000", got)
	}
}

func TestStartDefaultCPUMillisIsZero(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}
	d.mu.Lock()
	got := d.started[0].CPUMillis
	d.mu.Unlock()
	if got != 0 {
		t.Fatalf("CPUMillis = %d, want 0 (unset)", got)
	}
}

// An unset launch mode (the default) launches with the historical JAR mode, so
// the spec carries LaunchModeJar — the byte-for-byte original behavior (#305).
func TestStartDefaultLaunchModeIsJar(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)

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

// A "forge-argsfile" launch mode threads LaunchModeForgeArgsfile onto the spec so
// the driver runs the install-then-launch sequence (#305).
func TestStartForgeLaunchMode(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
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

// An unrecognized launch mode is a malformed command: it fails with INTERNAL
// (an unpinned code, so the #294 contract table is untouched) and never starts
// the driver (#305).
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
	cmd.Driver = "container" // not registered

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
	_ = m.Handle(context.Background(), startCmd())

	res := m.Handle(context.Background(), session.Command{CommandID: "c3", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	if stopped, graceful := d.inst.wasStopped(); !stopped || !graceful {
		t.Fatalf("instance not gracefully stopped: stopped=%v graceful=%v", stopped, graceful)
	}
}

// A graceful stop whose RCON "stop" succeeds: the fakeInstance does not call the
// preFallback hook (it is a minimal fake), so no save-all appears in the sequence.
// The real driver calls preFallback always before stop on the graceful path (#1007).
func TestStopServerGracefulRCONSuccessSkipsFlush(t *testing.T) {
	var seq []string
	d := &fakeDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq}
	m := newManager(t, d, ctrl)
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

// When the driver calls the preFallback hook on the graceful path, the flush
// must run: save-all + settle lands the dirty chunks on disk before the process
// is terminated (#1007). The rconFailInstance models this by calling the
// preFallback hook.
func TestStopServerGracefulRCONFailureFlushesBeforeTerminate(t *testing.T) {
	var seq []string
	d := &rconFailDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq}
	m := newManager(t, d, ctrl)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	d.inst.seq = &seq

	res := m.Handle(context.Background(), session.Command{CommandID: "c3", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	want := []string{"save-all", "stop"}
	if !equalLines(seq, want) {
		t.Fatalf("operation order = %v, want %v (graceful stop must flush before terminate)", seq, want)
	}
}

// A force stop (cmd.Force) is the operator's "kill it now" escape hatch and must
// NOT attempt the graceful save-all flush — it intentionally skips the save so a
// wedged or unresponsive server can still be terminated (#1007).
func TestStopServerForceSkipsFlush(t *testing.T) {
	var seq []string
	d := &fakeDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq}
	m := newManager(t, d, ctrl)
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

// A failed save-all on the graceful-stop flush must DEGRADE to terminating the
// server, not wedge the stop (#1007): the flush is best-effort, and a stop that
// could not save must still complete (the API gives stop dispatch a bounded
// budget and an unflushed world is no worse than today's pre-fix behavior). Uses
// rconFailDriver so the preFallback hook fires and exercises the save-all code
// path.
func TestStopServerGracefulProceedsWhenSaveFails(t *testing.T) {
	var seq []string
	d := &rconFailDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq, failLines: map[string]error{"save-all": fmt.Errorf("rcon down")}}
	m := newManager(t, d, ctrl)
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

func TestServerCommandForwardsOutput(t *testing.T) {
	d := &fakeDriver{}
	ctrl := &fakeControl{reply: "There are 0 players"}
	m := newManager(t, d, ctrl)
	_ = m.Handle(context.Background(), startCmd())

	res := m.Handle(context.Background(), session.Command{CommandID: "c4", ServerID: "s1", Kind: "ServerCommand", Line: "list"})
	if !res.Success || res.Output != "There are 0 players" {
		t.Fatalf("ServerCommand = %+v, want output", res)
	}
	if len(ctrl.lines) != 1 || ctrl.lines[0] != "list" {
		t.Fatalf("forwarded lines = %v, want [list]", ctrl.lines)
	}
}

// TestOpenControlReceivesRunningServerDriver pins the per-server driver
// resolution the RCON dial host depends on: on a worker that advertises both
// drivers and a container network, the manager must hand openControl the driver
// that actually runs each server, so the wiring resolves a host-process server's
// RCON to the host loopback (empty host) rather than dialing the unreachable
// container name (issue #218). The seam carries the driver name; the empty-host
// resolution itself lives in main.go's openControl, exercised here through the
// driver value the manager passes.
func TestOpenControlReceivesRunningServerDriver(t *testing.T) {
	var gotDriver string
	scratch := t.TempDir()
	drivers := map[string]execution.ExecutionDriver{
		"host-process": &fakeDriver{},
		"container":    &fakeDriver{},
	}
	m := New(drivers, scratch, func(_ context.Context, _ string, driver string) (execution.ServerControl, error) {
		gotDriver = driver
		return &fakeControl{reply: "ok"}, nil
	})

	// Start a host-process server on the mixed-driver worker.
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}

	res := m.Handle(context.Background(), session.Command{CommandID: "c6", ServerID: "s1", Kind: "ServerCommand", Line: "list"})
	if !res.Success {
		t.Fatalf("ServerCommand = %+v, want success", res)
	}
	if gotDriver != "host-process" {
		t.Fatalf("openControl driver = %q, want host-process (so the host-process server resolves loopback, not the container name)", gotDriver)
	}
}

func TestStatusEventsAreForwarded(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
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

func TestRestartStopsAndStarts(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
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

// An in-place restart with fakeInstance (which does not call preFallback): no
// save-all appears in the sequence. The real driver calls preFallback always
// before stop on the graceful path (#1007).
func TestRestartRCONSuccessSkipsFlush(t *testing.T) {
	var seq []string
	d := &fakeDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq}
	m := newManager(t, d, ctrl)
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

// When a restart uses rconFailInstance (which calls preFallback), the flush
// must run: the relaunch re-reads the same on-disk scratch, so unflushed dirty
// chunks would roll the block edits back (#1007).
func TestRestartRCONFailureFlushesBeforeTerminate(t *testing.T) {
	var seq []string
	d := &rconFailDriver{}
	ctrl := &fakeControl{reply: "ok", seq: &seq}
	m := newManager(t, d, ctrl)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	d.inst.seq = &seq

	res := m.Handle(context.Background(), session.Command{CommandID: "c5", ServerID: "s1", Kind: "RestartServer"})
	if !res.Success {
		t.Fatalf("restart = %+v, want success", res)
	}
	want := []string{"save-all", "stop"}
	if !equalLines(seq, want) {
		t.Fatalf("operation order = %v, want %v (graceful restart must flush before terminate)", seq, want)
	}
}

// A successful restart's result carries the RestartServer's correlation id, not
// the internal StartServer command's id, so the API can match it to the command
// it issued.
func TestRestartResultCarriesOriginalCorrelationID(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())

	res := m.Handle(context.Background(), session.Command{CommandID: "restart-id", ServerID: "s1", Kind: "RestartServer"})
	if !res.Success {
		t.Fatalf("restart = %+v, want success", res)
	}
	if res.CommandID != "restart-id" {
		t.Fatalf("restart result CommandID = %q, want %q", res.CommandID, "restart-id")
	}
}

// A driver Start error wrapping execution.ErrPortConflict surfaces as the
// sanitized port_conflict code, not the generic internal one (issue #225).
func TestStartPortConflictSurfacesCode(t *testing.T) {
	d := &fakeDriver{startErr: fmt.Errorf("containerdriver: start: %w", execution.ErrPortConflict)}
	m := newManager(t, d, nil)

	res := m.Handle(context.Background(), startCmd())
	if res.Success || res.ErrorCode != session.CommandErrorPortConflict {
		t.Fatalf("start = %+v, want PORT_CONFLICT failure", res)
	}
}

// A driver Start error wrapping execution.ErrImageMissing surfaces as the
// sanitized image_missing code (issue #225).
func TestStartImageMissingSurfacesCode(t *testing.T) {
	d := &fakeDriver{startErr: fmt.Errorf("containerdriver: create: %w", execution.ErrImageMissing)}
	m := newManager(t, d, nil)

	res := m.Handle(context.Background(), startCmd())
	if res.Success || res.ErrorCode != session.CommandErrorImageMissing {
		t.Fatalf("start = %+v, want IMAGE_MISSING failure", res)
	}
}

// An unclassified driver Start error keeps the generic internal code (issue #225).
func TestStartUnclassifiedFailureIsInternal(t *testing.T) {
	d := &fakeDriver{startErr: fmt.Errorf("daemon unreachable")}
	m := newManager(t, d, nil)

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
