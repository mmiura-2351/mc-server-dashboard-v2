package instancemanager

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager/goroutineleak"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// awaitManagerGoroutines asserts that exactly want manager-owned goroutines are
// live, allowing the same settling window the other waits in this package use.
func awaitManagerGoroutines(t *testing.T, want int) {
	t.Helper()
	if live, stacks := goroutineleak.Settle(want, 5*time.Second); live != want {
		t.Fatalf("%d manager background goroutine(s) live, want %d:\n%s", live, want, stacks)
	}
}

// TestMain runs the package and then hands the result to the shared census,
// which fails the run if any goroutine a Manager started is still there once
// every test has finished (issue #2777). The census lives in goroutineleak
// rather than here so worker/test/e2e — which builds Managers against a real
// Docker daemon and cannot import this file — asserts the same invariant off the
// same frame list (issue #2881).
func TestMain(m *testing.M) {
	os.Exit(goroutineleak.FailIfSurvivors(m.Run()))
}

// A running instance's status pump parks on the instance's event channel and the
// metrics pump runs off the clock until the status pump releases it. Nothing
// closed either: an instance that never reaches a terminal state — a server
// still up when the Worker goes down, and every fake in this package — left the
// pair (and the metrics pump's cancel watcher) parked for the life of the
// process, against a manager nobody owned any more (issue #2777). Close ends
// them.
func TestCloseEndsThePumpsOfALiveInstance(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	m := newManager(t, &fakeDriver{}, nil)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start failed: %+v", res)
	}
	// The dispatcher, the status pump, the metrics pump and its cancel watcher.
	awaitManagerGoroutines(t, 4)

	m.Close()

	awaitManagerGoroutines(t, 0)
}

// statusDispatcher is started by New and parks on statusNotify, which nothing
// ever closes — so every manager ever built left one behind, one per test in
// this package (issue #2777).
func TestCloseEndsTheStatusDispatcher(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	m := newManager(t, &fakeDriver{}, nil)
	awaitManagerGoroutines(t, 1)

	m.Close()

	awaitManagerGoroutines(t, 0)
}

// The dispatcher's send is BLOCKING by design: coalesced status is state, not a
// stream, so backpressure is absorbed rather than dropped (issue #96). By the
// time Close runs, nothing drains the merged stream any more — main.go closes
// the manager only after the session runner has returned — so a dispatcher
// observing the shutdown only between events would hold Close forever on a full
// sink. It observes it on the send too, and the parked status is dropped.
func TestCloseEndsAStatusDispatcherBlockedOnAFullSink(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	m := newManager(t, &fakeDriver{}, nil)

	// Fill the sink through the fast path, then park one more status behind it:
	// with events full, sendStatus routes through the pending slot and wakes the
	// dispatcher, which takes the entry and blocks on the send.
	for i := 0; i < cap(m.Events()); i++ {
		m.sendStatus(session.StatusEvent{ServerID: fmt.Sprintf("filler-%d", i), State: "running"})
	}
	m.sendStatus(session.StatusEvent{ServerID: "s1", State: "running"})
	waitFor(t, func() bool {
		m.statusMu.Lock()
		defer m.statusMu.Unlock()
		return m.coalescing["s1"] && len(m.dirtyStatus) == 0 && len(m.pendingStatus) == 0
	})

	closed := make(chan struct{})
	go func() { m.Close(); close(closed) }()
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not return: the dispatcher is still blocked sending into a sink nobody drains")
	}

	awaitManagerGoroutines(t, 0)
}

// A start that lands after Close registers its instance but starts no pumps.
// Close has already run the Wait, so a pump spawned afterwards is both a
// goroutine nobody joins and — an Add racing a Wait that has reached zero — a
// panic away. It mirrors recordOrphan's guard for convergers (issue #2493).
func TestStartAfterCloseSpawnsNoPumps(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	m := newManager(t, &fakeDriver{}, nil)
	seedScratch(t, m, "s1")
	m.Close()

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start failed: %+v", res)
	}

	// The guard itself, asserted directly: a spawn on a closed manager is REFUSED,
	// not merely short-lived. Watching the goroutines alone cannot see this — one
	// started here observes the already-cancelled shutdown and leaves within
	// microseconds — but the danger is not its lifetime, it is the Add: landing
	// beside a Wait still in progress, that is a panic in the Worker's shutdown
	// path, not a leak.
	if m.goBackground(func() {}) {
		t.Fatal("goBackground started a goroutine on a closed manager; Close has already run the Wait that counts it")
	}

	awaitManagerGoroutines(t, 0)
}
