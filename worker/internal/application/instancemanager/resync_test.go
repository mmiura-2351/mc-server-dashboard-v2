package instancemanager

import (
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
	m.reserved["srv-reserved"] = true

	m.ResyncStatus()

	states := statesOf(t, m, 3)
	want := map[string]string{"srv-live": "running", "srv-orphan": "unknown", "srv-gone": "stopped"}
	if len(states) != len(want) || states["srv-live"] != want["srv-live"] ||
		states["srv-orphan"] != want["srv-orphan"] || states["srv-gone"] != want["srv-gone"] {
		t.Fatalf("resync states = %v, want %v", states, want)
	}
	expectNoStatus(t, m)
}
