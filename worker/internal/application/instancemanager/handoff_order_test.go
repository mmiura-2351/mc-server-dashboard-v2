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

// pumpHold parks the pump of the first instance that forwards a given state on
// that event until release closes. held closes once it is parked; next closes
// when the same pump reaches its next event, which proves its forward of the held
// one is over.
type pumpHold struct {
	held, release, next chan struct{}
	releaseOnce         func()
	old                 execution.Instance
}

// holdFirst installs a pumpHold on m for the first event in state. It must run
// before any pump starts: the hook is read by the pumps without a lock.
func holdFirst(t *testing.T, m *Manager, state execution.ServerState) *pumpHold {
	t.Helper()
	h := &pumpHold{held: make(chan struct{}), release: make(chan struct{}), next: make(chan struct{})}
	var mu sync.Mutex
	var passed bool
	m.beforePumpForward = func(inst execution.Instance, ev execution.StatusEvent) {
		mu.Lock()
		switch {
		case h.old == nil && ev.State == state:
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

// collectUntil reads s1's events off the stream until one in state want arrives.
func collectUntil(t *testing.T, m *Manager, want string) []session.StatusEvent {
	t.Helper()
	var evs []session.StatusEvent
	deadline := time.After(5 * time.Second)
	for {
		select {
		case ev := <-m.Events():
			if ev.ServerID != s1 {
				continue
			}
			evs = append(evs, ev)
			if ev.State == want {
				return evs
			}
		case <-deadline:
			t.Fatalf("%s never reported %s; got %v", s1, want, evs)
		}
	}
}

// drainS1 reads s1's events already on the stream.
func drainS1(m *Manager) []session.StatusEvent {
	var evs []session.StatusEvent
	for {
		select {
		case ev := <-m.Events():
			if ev.ServerID == s1 {
				evs = append(evs, ev)
			}
		default:
			return evs
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

func assertStates(t *testing.T, evs []session.StatusEvent, want []string) {
	t.Helper()
	got := make([]string, len(evs))
	for i, ev := range evs {
		got[i] = ev.State
	}
	if len(got) != len(want) {
		t.Fatalf("%s reported %v, want %v", s1, got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			t.Fatalf("%s reported %v, want %v", s1, got, want)
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
	h := holdFirst(t, m, execution.StateStopped)
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
	evs := collectUntil(t, m, execution.StateRunning.String())
	h.finish(t, old.events)
	evs = append(evs, drainS1(m)...)

	assertStates(t, evs, []string{"stopped", "starting", "running"})
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
			h := holdFirst(t, m, execution.StateStopped)
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
			evs := collectUntil(t, m, execution.StateRunning.String())
			h.finish(t, old.events)
			evs = append(evs, drainS1(m)...)

			assertStates(t, evs, []string{"stopped", "running"})
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
	h := holdFirst(t, m, execution.StateStopped)
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
	evs := collectUntil(t, m, execution.StateRunning.String())
	h.finish(t, old.events)
	evs = append(evs, drainS1(m)...)

	assertStates(t, evs, []string{"stopped", "running"})
}

// classifiedCrash is a crash the driver could explain, as a Forge install's is.
var classifiedCrash = execution.StatusEvent{
	ServerID: s1, State: execution.StateCrashed,
	Detail:      "forge install failed: the installer ran out of memory at the 512 MiB limit",
	CrashReason: execution.CrashReasonForgeInstallOutOfMemory,
}

// assertClassifiedCrash checks that ev is classifiedCrash as reported.
func assertClassifiedCrash(t *testing.T, ev session.StatusEvent) {
	t.Helper()
	want := session.StatusEvent{
		ServerID: s1, State: execution.StateCrashed.String(),
		Detail: classifiedCrash.Detail, CrashReason: classifiedCrash.CrashReason.String(),
	}
	if ev != want {
		t.Fatalf("%s reported %+v, want the instance's own crash %+v", s1, ev, want)
	}
}

// A stop that takes over an instance that already crashed, while its pump holds
// the classified crash, reports that crash whole: its Detail and CrashReason are
// what the API and the UI explain the failure with. A synthesized `crashed` would
// replace a genuine crash with an unexplained one.
func TestStopTakeoverReportsTheClassifiedCrashItTookOver(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	h := holdFirst(t, m, execution.StateCrashed)
	startHeld(t, m)
	old := d.inst

	old.end(classifiedCrash)
	awaitEnter(t, h.held)
	if res := m.Handle(context.Background(), session.Command{CommandID: "c2", ServerID: s1, Kind: "StopServer"}); !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("replacement start = %+v, want success", res)
	}
	evs := collectUntil(t, m, execution.StateRunning.String())
	h.finish(t, old.events)
	evs = append(evs, drainS1(m)...)

	assertStates(t, evs, []string{"crashed", "running"})
	assertClassifiedCrash(t, evs[0])
}

// The same for the orphan converger: a failed-stop orphan that crashes on its own
// while its pump holds the classified crash is retired with that crash, not with
// a synthesized `stopped`.
func TestRetiredOrphanReportsTheClassifiedCrashItTookOver(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	shrinkOrphanConverger(m)
	seedScratch(t, m, s1)
	h := holdFirst(t, m, execution.StateCrashed)
	old := newFakeInstance(s1)
	startPumpsFor(m, s1, old)
	drainStatuses(t, m, 1) // the fake's own running

	old.end(classifiedCrash)
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
	evs := collectUntil(t, m, execution.StateRunning.String())
	h.finish(t, old.events)
	evs = append(evs, drainS1(m)...)

	assertStates(t, evs, []string{"crashed", "running"})
	assertClassifiedCrash(t, evs[0])
}

// A failed-stop orphan whose pump has already forwarded its classified crash,
// with its stream still open, so nothing but the converger retires the record
// (issue #3616). The retirement must not report a synthesized `stopped` over
// the crash: the stream ends with the crash, Detail and CrashReason intact.
func TestRetiredOrphanKeepsTheClassifiedCrashItAlreadyForwarded(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	shrinkOrphanConverger(m)
	old := newFakeInstance(s1)
	startPumpsFor(m, s1, old)
	drainStatuses(t, m, 1) // the fake's own running

	old.end(classifiedCrash)
	evs := collectUntil(t, m, execution.StateCrashed.String())
	m.recordOrphan(s1, old, "container", "1.21")
	waitFor(t, func() bool {
		m.mu.Lock()
		defer m.mu.Unlock()
		return !m.converging[s1]
	})
	// The retirement queued its report before it released the record, so it is
	// on its way to the stream by now.
	select {
	case ev := <-m.Events():
		evs = append(evs, ev)
	case <-time.After(5 * time.Second):
		t.Fatalf("the retirement of %s reported nothing", s1)
	}
	evs = append(evs, drainS1(m)...)

	assertStates(t, evs, []string{"crashed", "crashed"})
	assertClassifiedCrash(t, evs[0])
	assertClassifiedCrash(t, evs[1])
}

// A crash releases the id itself, and a replacement start can claim it at once.
// The replacement is run where the crashed instance's id has been released and
// its `crashed` is not queued yet (beforeCrashReport). Where it can take mu
// there, it runs to its end and the old pump goes on only once the replacement's
// running is on the stream. Where mu is held, it runs concurrently and has to
// wait for the old pump. Either way the old `crashed` must precede the
// replacement's running.
func TestCrashedInstancesCrashPrecedesAReplacementsEvents(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	// One reader for the whole stream, so the hook can wait on what it sees.
	var mu sync.Mutex
	var evs []session.StatusEvent
	running := make(chan struct{})
	quit, collected := make(chan struct{}), make(chan struct{})
	var seeded bool
	go func() {
		defer close(collected)
		for {
			select {
			case ev := <-m.Events():
				if ev.ServerID != s1 {
					continue
				}
				mu.Lock()
				if !seeded { // the seeded instance's own running
					seeded = true
				} else {
					evs = append(evs, ev)
					if ev.State == execution.StateRunning.String() {
						close(running)
					}
				}
				mu.Unlock()
			case <-quit:
				return
			}
		}
	}()

	var res session.CommandResult
	replaced := make(chan struct{})
	var once sync.Once
	m.beforeCrashReport = func(string) {
		once.Do(func() {
			replace := func() { res = m.Handle(context.Background(), startCmd()) }
			if !m.mu.TryLock() {
				go func() {
					defer close(replaced)
					replace()
				}()
				return
			}
			m.mu.Unlock()
			replace()
			close(replaced)
			<-running
		})
	}
	// next closes when the old pump reaches the event after its crash, which
	// proves it is done with the crash. Only the old instance reports stopped.
	next := make(chan struct{})
	m.beforePumpForward = func(_ execution.Instance, ev execution.StatusEvent) {
		if ev.State == execution.StateStopped {
			close(next)
		}
	}
	seedScratch(t, m, s1)
	if r := m.Handle(context.Background(), startCmd()); !r.Success {
		t.Fatalf("seed running instance: %+v", r)
	}
	d.mu.Lock()
	old := d.inst
	d.mu.Unlock()

	old.end(classifiedCrash)
	awaitEnter(t, replaced)
	if !res.Success {
		t.Fatalf("replacement start = %+v, want success", res)
	}
	awaitEnter(t, running)
	old.events <- execution.StatusEvent{ServerID: s1, State: execution.StateStopped}
	awaitEnter(t, next)
	close(quit)
	<-collected
	evs = append(evs, drainS1(m)...)

	assertStates(t, evs, []string{"crashed", "running"})
	assertClassifiedCrash(t, evs[0])
}
