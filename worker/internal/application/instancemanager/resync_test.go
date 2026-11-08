package instancemanager

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// drainStatuses collects up to want status events off the merged stream within a
// short deadline, so a test can assert what ResyncStatus emitted without racing.
func drainStatuses(t *testing.T, m *Manager, want int) []session.StatusEvent {
	t.Helper()
	var got []session.StatusEvent
	deadline := time.After(2 * time.Second)
	for len(got) < want {
		select {
		case ev := <-m.Events():
			got = append(got, ev)
		case <-deadline:
			t.Fatalf("got %d status events, want %d", len(got), want)
		}
	}
	return got
}

// ResyncStatus re-emits a StatusChange for every held instance reflecting its
// current state (issue #985), so an API restart moves the server out of
// observed=unknown at once rather than over the reconciler grace window.
func TestResyncStatusReEmitsHeldInstances(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	running := newFakeInstance("srv-run")
	starting := newFakeInstance("srv-start")
	starting.state = execution.StateStarting
	m.instances["srv-run"] = running
	m.instances["srv-start"] = starting

	m.ResyncStatus()

	got := drainStatuses(t, m, 2)
	states := map[string]string{}
	for _, ev := range got {
		states[ev.ServerID] = ev.State
	}
	if states["srv-run"] != "running" {
		t.Errorf("srv-run state = %q, want running", states["srv-run"])
	}
	if states["srv-start"] != "starting" {
		t.Errorf("srv-start state = %q, want starting", states["srv-start"])
	}
}

// A control-plane reconnect must not hide a failed-stop orphan (issue #2468
// item 3): ResyncStatus reports every recorded orphan as `unknown`, the honest
// answer for a process this Worker could not confirm dead. Before this it
// snapshotted only m.instances, from which orphans have already been evicted, so
// a reconnect left the API's row asserting a staler state as fact.
func TestResyncStatusReportsOrphanUnknown(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	m.orphans["srv-orphan"] = orphanEntry{inst: newFakeInstance("srv-orphan"), driver: "container"}

	m.ResyncStatus()

	got := drainStatuses(t, m, 1)
	if got[0].ServerID != "srv-orphan" || got[0].State != "unknown" {
		t.Fatalf("resync event = %+v, want srv-orphan reported unknown", got[0])
	}
}

// On a fresh process the instances map is empty, so ResyncStatus emits nothing.
func TestResyncStatusEmptyEmitsNothing(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)

	m.ResyncStatus()

	select {
	case ev := <-m.Events():
		t.Fatalf("empty manager emitted a status event: %+v", ev)
	case <-time.After(50 * time.Millisecond):
	}
}

// ResyncStatus must not hold m.mu while emitting (sendStatus can coalesce and
// wake the dispatcher). This test proves the lock is released before the emit by
// having a concurrent goroutine that needs m.mu run while the events sink is
// blocked: if ResyncStatus held m.mu across the emit, the concurrent lock would
// deadlock with the unread sink. The sink has capacity 32, so to truly block the
// emit we fill it first, then assert the concurrent m.mu acquisition still
// completes.
func TestResyncStatusDoesNotHoldLockDuringEmit(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	// Fill the events sink (cap 32) so the next emit must block/coalesce rather
	// than complete instantly.
	for i := 0; i < cap(m.events); i++ {
		m.events <- session.StatusEvent{ServerID: "filler"}
	}
	m.instances["srv-run"] = newFakeInstance("srv-run")

	resyncDone := make(chan struct{})
	go func() {
		m.ResyncStatus()
		close(resyncDone)
	}()

	// A concurrent operation that takes m.mu must not be blocked by the resync's
	// in-flight emit. If ResyncStatus held m.mu across the (now coalescing) emit,
	// this would block until the sink drained; it does not, so it returns at once.
	locked := make(chan struct{})
	go func() {
		m.mu.Lock()
		_ = len(m.instances) // touch guarded state so the critical section is real
		m.mu.Unlock()
		close(locked)
	}()

	select {
	case <-locked:
	case <-time.After(2 * time.Second):
		t.Fatal("m.mu acquisition blocked: ResyncStatus held the lock across its emit")
	}

	// Drain the sink so the coalesced resync emit completes and the goroutine exits.
	go func() {
		for {
			select {
			case <-m.Events():
			case <-resyncDone:
				return
			}
		}
	}()
	select {
	case <-resyncDone:
	case <-time.After(2 * time.Second):
		t.Fatal("ResyncStatus did not complete")
	}
}

// statesOf drains want status events and maps each server id to its state,
// failing on a second report for one id.
func statesOf(t *testing.T, m *Manager, want int) map[string]string {
	t.Helper()
	states := map[string]string{}
	for _, ev := range drainStatuses(t, m, want) {
		if prev, dup := states[ev.ServerID]; dup {
			t.Fatalf("%s reported twice: %q then %q", ev.ServerID, prev, ev.State)
		}
		states[ev.ServerID] = ev.State
	}
	return states
}

// expectNoStatus fails if another status event arrives within a short window.
func expectNoStatus(t *testing.T, m *Manager) {
	t.Helper()
	select {
	case ev := <-m.Events():
		t.Fatalf("unexpected status event: %+v", ev)
	case <-time.After(50 * time.Millisecond):
	}
}

// The servers whose containers the boot sweep removed are reported stopped on the
// first resync after registration (issues #3486, #3514): the API row of an exited
// container a handed-off stop left behind, or of a running orphan the sweep
// stopped, otherwise keeps its stale state until the reconciler's slow path.
func TestResyncStatusReportsTheBootSweptServersStopped(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).WithSweptServers([]string{"srv-exited", "srv-orphan"})

	m.ResyncStatus()

	states := statesOf(t, m, 2)
	if states["srv-exited"] != "stopped" || states["srv-orphan"] != "stopped" {
		t.Fatalf("resync states = %v, want both swept servers stopped", states)
	}
}

// The report is one-shot for the boot: a later resync, after a plain reconnect,
// reports only what the manager holds and never the sweep again.
func TestResyncStatusReportsTheBootSweptServersOnlyOnce(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).WithSweptServers([]string{"srv-swept"})
	m.ResyncStatus()
	drainStatuses(t, m, 1)

	m.ResyncStatus()

	expectNoStatus(t, m)
}

// A swept server the Worker holds again by the time of the resync is not reported
// stopped: a live instance, a failed-stop orphan or an in-flight command says more
// than the sweep did, and stopped would contradict it.
func TestResyncStatusSkipsABootSweptServerTheWorkerHoldsAgain(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).
		WithSweptServers([]string{"srv-live", "srv-orphan", "srv-reserved", "srv-gone"})
	m.instances["srv-live"] = newFakeInstance("srv-live")
	m.orphans["srv-orphan"] = orphanEntry{inst: newFakeInstance("srv-orphan"), driver: "container"}
	m.reserved["srv-reserved"] = reservation{leg: legNone}

	m.ResyncStatus()

	states := statesOf(t, m, 3)
	want := map[string]string{"srv-live": "running", "srv-orphan": "unknown", "srv-gone": "stopped"}
	if len(states) != len(want) || states["srv-live"] != want["srv-live"] ||
		states["srv-orphan"] != want["srv-orphan"] || states["srv-gone"] != want["srv-gone"] {
		t.Fatalf("resync states = %v, want %v", states, want)
	}
	expectNoStatus(t, m)
}

// startHeld starts s1 on d and consumes the running report its pump forwards, so
// the next status event a test reads is the one it is asserting on.
func startHeld(t *testing.T, m *Manager) {
	t.Helper()
	seedScratch(t, m, s1)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	drainStatuses(t, m, 1)
}

// A reconnect while a stop is confirming termination reports the server stopping
// (issue #3302). The stop evicted the instance and holds the id only as a
// reservation, so a resync that read the instances map alone left the API row at
// the unknown the disconnect wrote until the stop's terminal report.
func TestResyncStatusReportsAStopInFlightStopping(t *testing.T) {
	d := &gatedStopDriver{}
	m := newManager(t, d, nil).WithTransfer(&fakeTransfer{})
	startHeld(t, m)
	done := make(chan session.CommandResult, 1)
	go func() {
		done <- m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: s1, Kind: "StopServer"})
	}()
	awaitEnter(t, d.inst.stopEntered)

	m.ResyncStatus()

	if states := statesOf(t, m, 1); states[s1] != "stopping" {
		t.Fatalf("resync states = %v, want %s stopping", states, s1)
	}
	expectNoStatus(t, m)
	close(d.inst.stopRelease)
	if res := <-done; !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
}

// The same holds for a restart's stop leg (issue #3302): the row then reads
// (running, stopping), which is what the reconciler's restart stand-back
// recognises, instead of (running, unknown), which it redispatches a start for.
func TestResyncStatusReportsARestartStopLegStopping(t *testing.T) {
	d := &gatedStopDriver{}
	m := newManager(t, d, nil).WithTransfer(&fakeTransfer{})
	startHeld(t, m)
	stopping := d.inst
	done := make(chan session.CommandResult, 1)
	go func() {
		done <- m.Handle(context.Background(), session.Command{CommandID: "restart", ServerID: s1, Kind: "RestartServer"})
	}()
	awaitEnter(t, stopping.stopEntered)

	m.ResyncStatus()

	if states := statesOf(t, m, 1); states[s1] != "stopping" {
		t.Fatalf("resync states = %v, want %s stopping", states, s1)
	}
	expectNoStatus(t, m)
	close(stopping.stopRelease)
	if res := <-done; !res.Success {
		t.Fatalf("restart = %+v, want success", res)
	}
}

// A reconnect while a start is reserved but not yet launched reports it starting
// (issue #3302): the instance is registered only once driver.Start returns.
func TestResyncStatusReportsAReservedStartStarting(t *testing.T) {
	d := newGatedDriver()
	m := newManager(t, d, nil)
	seedScratch(t, m, s1)
	done := make(chan session.CommandResult, 1)
	go func() { done <- m.Handle(context.Background(), startCmd()) }()
	awaitEnter(t, d.entered)

	m.ResyncStatus()

	if states := statesOf(t, m, 1); states[s1] != "starting" {
		t.Fatalf("resync states = %v, want %s starting", states, s1)
	}
	expectNoStatus(t, m)
	close(d.release)
	if res := <-done; !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
}

// Once a restart's stop is confirmed, the reservation is the relaunch's, and a
// reconnect reports starting rather than the stop leg it has left (issue #3302).
func TestResyncStatusReportsARestartRelaunchStarting(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	startHeld(t, m)
	relaunch := newGatedDriver()
	m.drivers["container"] = relaunch
	done := make(chan session.CommandResult, 1)
	go func() {
		done <- m.Handle(context.Background(), session.Command{CommandID: "restart", ServerID: s1, Kind: "RestartServer"})
	}()
	awaitEnter(t, relaunch.entered)
	if ev := drainStatuses(t, m, 1)[0]; ev.State != "stopped" {
		t.Fatalf("stop leg reported %+v, want stopped", ev)
	}

	m.ResyncStatus()

	if states := statesOf(t, m, 1); states[s1] != "starting" {
		t.Fatalf("resync states = %v, want %s starting", states, s1)
	}
	expectNoStatus(t, m)
	close(relaunch.release)
	if res := <-done; !res.Success {
		t.Fatalf("restart = %+v, want success", res)
	}
}

// A reservation that is not a lifecycle leg (a hydrate, a stopped-id snapshot, the
// scratch reclaim) reports nothing, and an orphan whose retry stop holds the
// reservation is still reported unknown, once: what the orphan record says about
// the process outranks the leg of the stop being retried on it.
func TestResyncStatusReportsNoLegForAQuietReservationAndKeepsTheOrphanUnknown(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	m.reserved["srv-hydrating"] = reservation{leg: legNone}
	m.orphans["srv-orphan"] = orphanEntry{inst: newFakeInstance("srv-orphan"), driver: "container"}
	m.reserved["srv-orphan"] = reservation{leg: legStopping}

	m.ResyncStatus()

	if states := statesOf(t, m, 1); len(states) != 1 || states["srv-orphan"] != "unknown" {
		t.Fatalf("resync states = %v, want only srv-orphan unknown", states)
	}
	expectNoStatus(t, m)
}

// heldLaunchDriver holds driver.Start until the test ends it, and then fails it:
// with err set, once release is closed; with err nil, once the command's context
// ends, returning that context's error (a cancelled or timed-out launch).
type heldLaunchDriver struct {
	entered chan struct{}
	release chan struct{}
	err     error
}

func newHeldLaunchDriver(err error) *heldLaunchDriver {
	return &heldLaunchDriver{entered: make(chan struct{}, 1), release: make(chan struct{}), err: err}
}

func (d *heldLaunchDriver) Start(ctx context.Context, _ execution.InstanceSpec) (execution.Instance, error) {
	d.entered <- struct{}{}
	if d.err == nil {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	<-d.release
	return nil, d.err
}

// A launch whose `starting` a resync reported and that then ends without
// registering an instance — failed, cancelled or past its deadline — reports the
// server `stopped` (issue #3302). Nothing else would: the failed launch emits no
// status of its own, its command result went to the session that dropped, and a
// restart's `stopped` from its stop leg went out before the resync. The API row
// would otherwise sit at (running, starting), which the reconciler never selects.
// The report goes out before the reservation is released, so a replacement start's
// events follow it.
func TestResyncReportedLaunchThatEndsWithoutAnInstanceReportsStopped(t *testing.T) {
	for _, verb := range []string{"StartServer", "RestartServer"} {
		for _, end := range []string{"fails", "cancelled", "deadline"} {
			t.Run(verb+"/"+end, func(t *testing.T) {
				m := newManager(t, &fakeDriver{}, nil)
				var held *heldLaunchDriver
				if end == "fails" {
					held = newHeldLaunchDriver(errors.New("test: launch failed"))
				} else {
					held = newHeldLaunchDriver(nil)
				}
				ctx, cancel := context.WithCancel(context.Background())
				if end == "deadline" {
					ctx, cancel = context.WithTimeout(context.Background(), 300*time.Millisecond)
				}
				defer cancel()
				cmd := startCmd()
				if verb == "RestartServer" {
					startHeld(t, m)
					cmd = session.Command{CommandID: "restart", ServerID: s1, Kind: "RestartServer"}
				} else {
					seedScratch(t, m, s1)
				}
				m.drivers["container"] = held
				done := make(chan session.CommandResult, 1)
				go func() { done <- m.Handle(ctx, cmd) }()
				awaitEnter(t, held.entered)
				if verb == "RestartServer" {
					drainStatuses(t, m, 1) // the stop leg's own stopped
				}
				m.ResyncStatus()
				if states := statesOf(t, m, 1); states[s1] != "starting" {
					t.Fatalf("resync states = %v, want %s starting", states, s1)
				}

				switch end {
				case "fails":
					close(held.release)
				case "cancelled":
					cancel()
				}
				if res := <-done; res.Success {
					t.Fatalf("%s = %+v, want the launch's failure", verb, res)
				}
				m.drivers["container"] = &fakeDriver{}
				if res := m.Handle(context.Background(), startCmd()); !res.Success {
					t.Fatalf("replacement start = %+v, want success", res)
				}

				got := drainStatuses(t, m, 2)
				if got[0].ServerID != s1 || got[0].State != "stopped" || got[1].State != "running" {
					t.Fatalf("status events = %+v, want %s stopped and then the replacement's running", got, s1)
				}
			})
		}
	}
}

// The corrective report is owed only to a resync that reported the launch: a
// failed launch nobody reported says nothing, as before, so the API's handling of
// the command result on an unbroken session meets no extra status (issue #3302).
func TestUnreportedLaunchThatFailsReportsNothing(t *testing.T) {
	held := newHeldLaunchDriver(errors.New("test: launch failed"))
	m := newManager(t, held, nil)
	seedScratch(t, m, s1)
	close(held.release)

	if res := m.Handle(context.Background(), startCmd()); res.Success {
		t.Fatalf("start = %+v, want the launch's failure", res)
	}

	expectNoStatus(t, m)
}
