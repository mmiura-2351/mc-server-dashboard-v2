package instancemanager

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// Ordering of an old instance's status events against the events of whatever
// takes the id over from it (issue #3597). The API stamps each event on receipt,
// so an old instance's late `stopped` queued after a restart's relaunch, or after
// a replacement instance's events, leaves the row at `stopped` over a server that
// is starting or running. Each test delays the old instance's pump on its terminal
// across the handoff, lets the new leg report, and then lets the old pump go on.

// pumpHold parks the pump of the first instance that forwards a `stopped` on that
// event until release closes. held closes once it is parked; next closes when the
// same pump reaches its next event, which proves its forward of the held one is
// over.
type pumpHold struct {
	held, release, next chan struct{}
	releaseOnce         func()
	old                 execution.Instance
}

// holdFirstStopped installs a pumpHold on m. It must run before any pump starts:
// the hook is read by the pumps without a lock.
func holdFirstStopped(t *testing.T, m *Manager) *pumpHold {
	t.Helper()
	h := &pumpHold{held: make(chan struct{}), release: make(chan struct{}), next: make(chan struct{})}
	var mu sync.Mutex
	var passed bool
	m.beforePumpForward = func(inst execution.Instance, ev execution.StatusEvent) {
		mu.Lock()
		switch {
		case h.old == nil && ev.State == execution.StateStopped:
			h.old = inst
			mu.Unlock()
			close(h.held)
			<-h.release
			return
		case h.old != nil && inst == h.old && !passed:
			passed = true
			mu.Unlock()
			close(h.next)
			return
		}
		mu.Unlock()
	}
	// Registered after newManager's Close, so it runs first: a test that fails
	// while the pump is parked must not leave Close waiting for it.
	var once sync.Once
	t.Cleanup(func() { once.Do(func() { close(h.release) }) })
	h.releaseOnce = func() { once.Do(func() { close(h.release) }) }
	return h
}

// finish lets the parked pump go on with its held event, then puts one more
// event on its stream, so the hook proves the pump is done with the held one.
func (h *pumpHold) finish(t *testing.T, events chan execution.StatusEvent) {
	t.Helper()
	h.releaseOnce()
	events <- execution.StatusEvent{ServerID: s1, State: execution.StateStopped}
	select {
	case <-h.next:
	case <-time.After(5 * time.Second):
		t.Fatal("the old pump never reached its next event")
	}
}

// collectUntil reads s1's states off the stream until want arrives.
func collectUntil(t *testing.T, m *Manager, want string) []string {
	t.Helper()
	var states []string
	deadline := time.After(5 * time.Second)
	for {
		select {
		case ev := <-m.Events():
			if ev.ServerID != s1 {
				continue
			}
			states = append(states, ev.State)
			if ev.State == want {
				return states
			}
		case <-deadline:
			t.Fatalf("%s never reported %s; got %v", s1, want, states)
		}
	}
}

// drainS1 reads s1's states already on the stream.
func drainS1(m *Manager) []string {
	var states []string
	for {
		select {
		case ev := <-m.Events():
			if ev.ServerID == s1 {
				states = append(states, ev.State)
			}
		default:
			return states
		}
	}
}

// startPumpsFor starts the pumps of an instance a test places by hand, with its
// forwarding registered first, as launchReserved does for one it registers.
func startPumpsFor(m *Manager, serverID string, inst execution.Instance) {
	m.statusMu.Lock()
	m.forwarding[inst] = struct{}{}
	m.statusMu.Unlock()
	m.startPumps(serverID, inst)
}

func assertStates(t *testing.T, got, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s reported %v, want %v: the old instance's stopped is out of order", s1, got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			t.Fatalf("%s reported %v, want %v: the old instance's stopped is out of order", s1, got, want)
		}
	}
}

// relaunchGatedDriver launches plain fakes and holds every launch after the first
// until release closes, so a restart's relaunch can be held in legStarting.
type relaunchGatedDriver struct {
	mu      sync.Mutex
	insts   []*fakeInstance
	entered chan struct{}
	release chan struct{}
}

func (d *relaunchGatedDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.mu.Lock()
	first := len(d.insts) == 0
	d.mu.Unlock()
	if !first {
		d.entered <- struct{}{}
		<-d.release
	}
	inst := newFakeInstance(spec.ServerID)
	d.mu.Lock()
	d.insts = append(d.insts, inst)
	d.mu.Unlock()
	return inst, nil
}

// (a) A restart whose old pump still holds the stop's `stopped` when the relaunch
// begins. Neither the resync's `starting` for the relaunch nor the new instance's
// `running` may come before it.
func TestRestartOrdersTheOldInstancesStoppedBeforeTheRelaunch(t *testing.T) {
	d := &relaunchGatedDriver{entered: make(chan struct{}, 1), release: make(chan struct{})}
	m := newManager(t, d, nil)
	h := holdFirstStopped(t, m)
	startHeld(t, m)
	old := d.insts[0]

	done := make(chan session.CommandResult, 1)
	go func() {
		done <- m.Handle(context.Background(), session.Command{CommandID: "c2", ServerID: s1, Kind: "RestartServer"})
	}()
	awaitEnter(t, h.held)
	awaitEnter(t, d.entered)
	m.ResyncStatus()
	close(d.release)
	if res := <-done; !res.Success {
		t.Fatalf("restart = %+v, want success", res)
	}
	states := collectUntil(t, m, execution.StateRunning.String())
	h.finish(t, old.events)
	states = append(states, drainS1(m)...)

	assertStates(t, states, []string{"stopped", "starting", "running"})
}

// (b) A stop that confirms termination and releases the id while the old pump
// still holds the `stopped`: an operator retry stop of a failed-stop orphan, the
// converger's own retry stop, and a plain stop, which releases the id the same
// way. A replacement start's `running` may not come before the old `stopped`.
func TestReleasedStopOrdersTheOldInstancesStoppedBeforeAReplacement(t *testing.T) {
	for _, tc := range []string{"orphan retry", "converger retry", "stop"} {
		t.Run(tc, func(t *testing.T) {
			d := &fakeDriver{}
			m := newManager(t, d, nil)
			shrinkOrphanConverger(m)
			seedScratch(t, m, s1)
			h := holdFirstStopped(t, m)
			var old *fakeInstance
			if tc == "stop" {
				startHeld(t, m)
				old = d.inst
			} else {
				old = newFakeInstance(s1)
				startPumpsFor(m, s1, old)
				drainStatuses(t, m, 1) // the fake's own running
			}

			switch tc {
			case "converger retry":
				m.recordOrphan(s1, old, "container", "1.21")
				awaitEnter(t, h.held)
				waitFor(t, func() bool {
					m.mu.Lock()
					defer m.mu.Unlock()
					_, orphan := m.orphans[s1]
					_, reserved := m.reserved[s1]
					return !orphan && !reserved
				})
			default:
				if tc == "orphan retry" {
					m.mu.Lock()
					m.orphans[s1] = orphanEntry{inst: old, driver: "container"}
					m.mu.Unlock()
				}
				res := m.Handle(context.Background(), session.Command{CommandID: "c2", ServerID: s1, Kind: "StopServer"})
				if !res.Success {
					t.Fatalf("stop = %+v, want success", res)
				}
				awaitEnter(t, h.held)
			}
			if res := m.Handle(context.Background(), startCmd()); !res.Success {
				t.Fatalf("replacement start = %+v, want success", res)
			}
			states := collectUntil(t, m, execution.StateRunning.String())
			h.finish(t, old.events)
			states = append(states, drainS1(m)...)

			assertStates(t, states, []string{"stopped", "running"})
		})
	}
}

// (c) A failed-stop orphan that exits on its own while its pump is delayed on the
// `stopped`, so the record stays put and the converger retires it after two dead
// probes. A replacement start's `running` may not be followed by the old
// instance's `stopped`.
func TestRetiredOrphansLateStoppedDoesNotFollowAReplacement(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	shrinkOrphanConverger(m)
	seedScratch(t, m, s1)
	h := holdFirstStopped(t, m)
	old := newFakeInstance(s1)
	startPumpsFor(m, s1, old)
	drainStatuses(t, m, 1) // the fake's own running

	exitOnItsOwn(old, false)
	awaitEnter(t, h.held)
	m.recordOrphan(s1, old, "container", "1.21")
	waitFor(t, func() bool {
		m.mu.Lock()
		defer m.mu.Unlock()
		_, orphan := m.orphans[s1]
		return !orphan
	})
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("replacement start = %+v, want success", res)
	}
	states := collectUntil(t, m, execution.StateRunning.String())
	h.finish(t, old.events)
	states = append(states, drainS1(m)...)

	assertStates(t, states, []string{"stopped", "running"})
}
