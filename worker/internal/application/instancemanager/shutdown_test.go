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

// Run the shared goroutine census after test cleanup so unit and E2E binaries enforce the same Manager lifetime.
func TestMain(m *testing.M) {
	os.Exit(goroutineleak.FailIfSurvivors(m.Run()))
}

// Keep an instance non-terminal and require Close to end its status and metrics pumps and teardown watcher.
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

// statusDispatcher is started by New and parks on statusNotify, which nothing ever closes, so every manager ever
// built left one behind, one per test in this package.
func TestCloseEndsTheStatusDispatcher(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	m := newManager(t, &fakeDriver{}, nil)
	awaitManagerGoroutines(t, 1)

	m.Close()

	awaitManagerGoroutines(t, 0)
}

// Fill the status sink so the dispatcher blocks sending; shutdown must release it without a consumer.
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

// A start that lands after Close registers its instance but starts no pumps. Close has already run the Wait, so
// a pump spawned afterwards is both a goroutine nobody joins and, an Add racing a Wait that has reached zero, a
// panic away. It mirrors recordOrphan's guard for convergers.
func TestStartAfterCloseSpawnsNoPumps(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	m := newManager(t, &fakeDriver{}, nil)
	seedScratch(t, m, "s1")
	m.Close()

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start failed: %+v", res)
	}

	// Assert the spawn rejection directly: a briefly started goroutine could hide a WaitGroup Add/Wait race.
	if m.goBackground(func() {}) {
		t.Fatal("goBackground started a goroutine on a closed manager; Close has already run the Wait that counts it")
	}

	awaitManagerGoroutines(t, 0)
}
