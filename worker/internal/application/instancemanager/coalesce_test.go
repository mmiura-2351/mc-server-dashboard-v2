package instancemanager

import (
	"strconv"
	"strings"
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

// seqOf parses the sequence number from a "v<N>" state label produced by the
// cross-boundary test. It fails the test on a malformed label.
func seqOf(t *testing.T, state string) int {
	t.Helper()
	n, err := strconv.Atoi(strings.TrimPrefix(state, "v"))
	if err != nil {
		t.Fatalf("unparsable state %q: %v", state, err)
	}
	return n
}

// TestCoalesceAcrossFastToStalledBoundary exercises one server crossing from the
// fast path into coalescing and back out: a few transitions flow directly onto a
// drained sink, then the sink fills mid-burst (coalescing engages) and the
// remaining transitions collapse to the latest. It asserts strict per-server
// ordering across the boundary — every delivered sequence is newer than the last
// (no older event after a newer one) — and that the final delivery is the latest
// sequence. States are labelled "v<N>" so ordering is checkable by sequence.
func TestCoalesceAcrossFastToStalledBoundary(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)

	// Phase 1 (fast path): the sink is empty and untouched by any consumer, so
	// these non-blocking sends land directly in the buffer, in order.
	const fastCount = 4
	seq := 0
	for ; seq < fastCount; seq++ {
		m.sendStatus(session.StatusEvent{ServerID: "s1", State: "v" + strconv.Itoa(seq)})
	}

	// Boundary: fill the remaining buffer capacity. The next s1 send finds the
	// sink full and flips s1 into coalescing. The filler count is unneeded here:
	// the consumer below skips filler dynamically.
	_ = fillSink(t, m)

	// Phase 2 (coalescing): a rapid burst that must collapse to the latest.
	for ; seq < fastCount+5; seq++ {
		m.sendStatus(session.StatusEvent{ServerID: "s1", State: "v" + strconv.Itoa(seq)})
	}
	latest := seq - 1

	// Controllable consumer: drain one event at a time. Fast-path s1 events sit
	// ahead of the filler in FIFO order; freeing a filler slot lets the
	// dispatcher's blocked send deposit the coalesced latest. Collect s1
	// deliveries in order until we observe the latest sequence.
	var delivered []int
	deadline := time.After(2 * time.Second)
	for {
		select {
		case ev := <-m.events:
			if ev.ServerID != "s1" {
				continue // filler
			}
			delivered = append(delivered, seqOf(t, ev.State))
			if delivered[len(delivered)-1] == latest {
				goto check
			}
		case <-deadline:
			t.Fatalf("did not observe latest v%d; delivered %v", latest, delivered)
		}
	}

check:
	// Strict per-server ordering across the boundary: never an older sequence
	// after a newer one.
	for i := 1; i < len(delivered); i++ {
		if delivered[i] <= delivered[i-1] {
			t.Fatalf("out-of-order delivery: %v (index %d not newer than %d)", delivered, i, i-1)
		}
	}
	// The final delivery is the latest.
	if got := delivered[len(delivered)-1]; got != latest {
		t.Fatalf("final delivered = v%d, want latest v%d", got, latest)
	}

	// No stray s1 event remains: intermediates past the boundary coalesced away.
	select {
	case ev := <-m.events:
		if ev.ServerID == "s1" {
			t.Fatalf("unexpected extra s1 event %+v after latest", ev)
		}
	case <-time.After(100 * time.Millisecond):
	}
}
