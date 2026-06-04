package instancemanager

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

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
}

func newFakeInstance(id string) *fakeInstance {
	i := &fakeInstance{serverID: id, state: execution.StateRunning, events: make(chan execution.StatusEvent, 8)}
	i.events <- execution.StatusEvent{ServerID: id, State: execution.StateRunning}
	return i
}

func (i *fakeInstance) Stop(_ context.Context, graceful bool) error {
	i.mu.Lock()
	i.stopped = true
	i.graceful = graceful
	i.state = execution.StateStopped
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

// fakeControl is an in-memory ServerControl for ServerCommand forwarding.
type fakeControl struct {
	reply string
	lines []string
}

func (c *fakeControl) Execute(_ context.Context, line string) (string, error) {
	c.lines = append(c.lines, line)
	return c.reply, nil
}

func (c *fakeControl) Close() error { return nil }

func newManager(t *testing.T, d execution.ExecutionDriver, ctrl execution.ServerControl) *Manager {
	t.Helper()
	scratch := t.TempDir()
	return New(map[string]execution.ExecutionDriver{"host-process": d}, scratch,
		func(context.Context, string, string) (execution.ServerControl, error) { return ctrl, nil })
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

func TestUnknownKindIsInternalError(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	res := m.Handle(context.Background(), session.Command{CommandID: "c6", ServerID: "s1", Kind: "Mystery"})
	if res.Success || res.ErrorCode != session.CommandErrorInternal {
		t.Fatalf("unknown kind = %+v, want INTERNAL", res)
	}
}
