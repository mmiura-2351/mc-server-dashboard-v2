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
