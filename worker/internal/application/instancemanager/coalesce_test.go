package instancemanager

import (
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// fillSink pushes filler events until the events sink is full, stalling it so
// subsequent sendStatus calls take the coalescing path. It returns the number of
// filler events parked in the buffer so the caller can drain past them.
func fillSink(t *testing.T, m *Manager) int {
	t.Helper()
	n := 0
	for {
		select {
		case m.events <- session.StatusEvent{ServerID: "filler", State: "running"}:
			n++
		default:
			return n
		}
	}
}

// drainState reads one event for serverID, skipping filler/other-server events,
// and returns its State. It fails if no such event arrives.
func drainState(t *testing.T, m *Manager, serverID string) string {
	t.Helper()
	timeout := time.After(2 * time.Second)
	for {
		select {
		case ev := <-m.events:
			if ev.ServerID == serverID {
				return ev.State
			}
		case <-timeout:
			t.Fatalf("no event for %s", serverID)
			return ""
		}
	}
}

// TestStalledSinkCoalescesToLatest: under a stalled sink, N rapid status
// transitions for one server collapse to the latest once the sink drains.
func TestStalledSinkCoalescesToLatest(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)

	filler := fillSink(t, m)

	states := []string{"starting", "running", "stopping", "stopped"}
	for _, s := range states {
		m.sendStatus(session.StatusEvent{ServerID: "s1", State: s})
	}

	// Drain the filler so the dispatcher's blocked send can complete.
	for range filler {
		<-m.events
	}

	got := drainState(t, m, "s1")
	if got != "stopped" {
		t.Fatalf("coalesced state = %q, want latest %q", got, "stopped")
	}

	// No further s1 event should remain: intermediates were coalesced away.
	select {
	case ev := <-m.events:
		if ev.ServerID == "s1" {
			t.Fatalf("unexpected extra s1 event %+v after latest", ev)
		}
	case <-time.After(100 * time.Millisecond):
	}
}

// TestCoalesceMultiServerIsolation: a burst from server A must not lose B's
// latest state; each server converges to its own newest status.
func TestCoalesceMultiServerIsolation(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)

	filler := fillSink(t, m)

	for _, s := range []string{"starting", "running", "stopping"} {
		m.sendStatus(session.StatusEvent{ServerID: "a", State: s})
	}
	m.sendStatus(session.StatusEvent{ServerID: "b", State: "running"})
	for _, s := range []string{"crashed", "stopped"} {
		m.sendStatus(session.StatusEvent{ServerID: "a", State: s})
	}

	for range filler {
		<-m.events
	}

	latest := map[string]string{}
	deadline := time.After(2 * time.Second)
	for len(latest) < 2 {
		select {
		case ev := <-m.events:
			if ev.ServerID == "a" || ev.ServerID == "b" {
				latest[ev.ServerID] = ev.State
			}
		case <-deadline:
			t.Fatalf("only saw %v before timeout", latest)
		}
	}
	if latest["a"] != "stopped" {
		t.Fatalf("server a = %q, want stopped", latest["a"])
	}
	if latest["b"] != "running" {
		t.Fatalf("server b = %q, want running (B's latest lost)", latest["b"])
	}
}

// TestUnstalledDeliversEveryTransitionInOrder: when the sink has room, the fast
// path delivers every transition in order (coalescing only kicks in under
// pressure).
func TestUnstalledDeliversEveryTransitionInOrder(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)

	states := []string{"starting", "running", "stopping", "stopped"}
	for _, s := range states {
		m.sendStatus(session.StatusEvent{ServerID: "s1", State: s})
	}

	for i, want := range states {
		got := drainState(t, m, "s1")
		if got != want {
			t.Fatalf("transition %d = %q, want %q", i, got, want)
		}
	}
}

// TestTeardownWithPendingCoalescedDoesNotHang: leaving a coalesced status pending
// (sink never drained) must not hang teardown. The dispatcher parks on the
// blocked send; the test goroutine returns without deadlock.
func TestTeardownWithPendingCoalescedDoesNotHang(t *testing.T) {
	done := make(chan struct{})
	go func() {
		defer close(done)
		m := newManager(t, &fakeDriver{}, nil)
		fillSink(t, m)
		for _, s := range []string{"starting", "running", "stopped"} {
			m.sendStatus(session.StatusEvent{ServerID: "s1", State: s})
		}
		// Intentionally do not drain: the dispatcher stays blocked, but
		// sendStatus and this goroutine must return.
	}()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("teardown hung with pending coalesced status")
	}
}
