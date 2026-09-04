package instancemanager

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// ReclaimDeletedScratches removes the scratch dir and hydrate leftovers for a
// deleted server id (issue #924).
func TestReclaimDeletedScratchesRemovesScratchAndHydrateLeftovers(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	dir := seedScratch(t, m, "s1")
	leftover := filepath.Join(m.scratchDir, ".hydrate-s1-stale")
	if err := os.MkdirAll(leftover, 0o750); err != nil {
		t.Fatal(err)
	}

	m.ReclaimDeletedScratches([]string{"s1"})
	// ReclaimDeletedScratches runs on a goroutine that removes the scratch dir
	// first, then sweeps the hydrate leftover. Wait for BOTH to be gone before
	// asserting, otherwise the leftover check races the goroutine (issue #1888).
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		_, dirErr := os.Stat(dir)
		_, leftoverErr := os.Stat(leftover)
		if os.IsNotExist(dirErr) && os.IsNotExist(leftoverErr) {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("scratch dir not reclaimed for deleted server: stat err = %v", err)
	}
	if _, err := os.Stat(leftover); !os.IsNotExist(err) {
		t.Fatalf("hydrate leftover not reclaimed for deleted server: stat err = %v", err)
	}
}

// ReclaimDeletedScratches MUST NOT remove .displaced-<id> trees (issue #911).
func TestReclaimDeletedScratchesRetainsDisplacedTree(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	seedScratch(t, m, "s1")
	displaced := seedDisplaced(t, m, "s1")

	m.ReclaimDeletedScratches([]string{"s1"})
	// Wait for the goroutine to complete the scratch removal.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(filepath.Join(m.scratchDir, "s1")); os.IsNotExist(err) {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := os.Stat(displaced); err != nil {
		t.Fatalf(".displaced-s1 tree removed by ReclaimDeletedScratches (must be retained, issue #911): %v", err)
	}
}

// ReclaimDeletedScratches skips a running/reserved/orphaned id.
func TestReclaimDeletedScratchesSkipsRunningServer(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	dir := seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())

	m.reclaimDeletedScratches([]string{"s1"})
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("scratch dir removed for a running server: %v", err)
	}
}

// ReclaimDeletedScratches refuses an id with a path separator (defense in depth).
func TestReclaimDeletedScratchesRefusesUnsafeID(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	// Create a sibling dir that a traversal would hit.
	sibling := filepath.Join(m.scratchDir, "..", "escaped")
	if err := os.MkdirAll(sibling, 0o750); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = os.RemoveAll(sibling) }()

	m.reclaimDeletedScratches([]string{"../escaped", "", "."})
	if _, err := os.Stat(sibling); err != nil {
		t.Fatalf("traversal-unsafe id escaped the scratch root: %v", err)
	}
}

// ReclaimDeletedScratches is idempotent on a missing dir (no error).
func TestReclaimDeletedScratchesIdempotentOnMissingDir(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	// "no-such-server" has no scratch dir — the call should not panic.
	m.reclaimDeletedScratches([]string{"no-such-server"})
	// Reaching here without a panic is the assertion.
}

// ReclaimDeletedScratches skips a reserved id.
func TestReclaimDeletedScratchesSkipsReservedServer(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	dir := seedScratch(t, m, "s1")

	// Simulate s1 having an in-flight hydrate by reserving it.
	ok, _, _ := m.reserve("s1")
	if !ok {
		t.Fatal("could not reserve s1 for test setup")
	}

	m.reclaimDeletedScratches([]string{"s1"})
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("scratch dir removed for a reserved server: %v", err)
	}
	m.release("s1")
}

// blockingReclaimLogger parks the reclaim goroutine on one of the manager's own
// log records and lets it go again on demand. The manager's logger is the only
// seam INSIDE the reclaim body, and the record parked on sits between the scratch
// removal and the reservation release — the exact window issue #2878 is about —
// so this is what turns "a reclaim in flight" into a state a test can hold still.
// Records it does not park on pass straight through.
type blockingReclaimLogger struct {
	msg     string
	entered chan struct{}
	release chan struct{}
	once    sync.Once
}

func (h *blockingReclaimLogger) Enabled(context.Context, slog.Level) bool { return true }

func (h *blockingReclaimLogger) Handle(_ context.Context, r slog.Record) error {
	if r.Message == h.msg {
		close(h.entered)
		<-h.release
	}
	return nil
}

func (h *blockingReclaimLogger) WithAttrs([]slog.Attr) slog.Handler { return h }
func (h *blockingReclaimLogger) WithGroup(string) slog.Handler      { return h }

// unpark releases a parked reclaim. It is idempotent so the test can both release
// it deliberately and register the release as a cleanup, which keeps a t.Fatal
// before the deliberate one from leaving the goroutine parked for the rest of the
// package run.
func (h *blockingReclaimLogger) unpark() { h.once.Do(func() { close(h.release) }) }

// newBlockingReclaimLogger parks on the record reclaimDeletedScratches emits after
// removing a scratch dir and before sweeping the hydrate leftovers.
func newBlockingReclaimLogger() *blockingReclaimLogger {
	return &blockingReclaimLogger{
		msg:     "reclaimed orphaned scratch for deleted server",
		entered: make(chan struct{}),
		release: make(chan struct{}),
	}
}

// Close JOINS a reclaim in flight (issue #2878). The reclaim was the one
// manager-owned goroutine spawned with a bare go, so Close neither waited for it
// nor cancelled it: parked here it has removed the scratch tree but has not yet
// swept the hydrate leftovers or released the reservation, and a Close that
// returned in that window lets the process exit inside it.
func TestCloseJoinsAnInFlightReclaim(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	h := newBlockingReclaimLogger()
	m := newManager(t, &fakeDriver{}, nil).WithLogger(slog.New(h))
	// Registered AFTER newManager's Close, so cleanups run it FIRST: a t.Fatal
	// below would otherwise leave Close joining a reclaim nothing ever releases,
	// and the package would hang instead of failing.
	t.Cleanup(h.unpark)
	seedScratch(t, m, "s1")
	leftover := filepath.Join(m.scratchDir, ".hydrate-s1-stale")
	if err := os.MkdirAll(leftover, 0o750); err != nil {
		t.Fatal(err)
	}

	m.ReclaimDeletedScratches([]string{"s1"})
	select {
	case <-h.entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the reclaim never reached its removal; the log record this test parks on has changed")
	}
	// The status dispatcher and the reclaim. Counting the reclaim at all is what
	// pins its frame into managerFrames, so the package's leak check can see it.
	awaitManagerGoroutines(t, 2)

	closed := make(chan struct{})
	go func() { m.Close(); close(closed) }()
	// The dispatcher is gone, so Close is past stopBackground and inside its Wait:
	// the parked reclaim is the only thing that Wait can still be waiting on, and
	// the window below therefore reads a decision rather than a scheduling delay.
	awaitManagerGoroutines(t, 1)
	select {
	case <-closed:
		t.Fatal("Close returned with a reclaim parked between its scratch removal and its reservation release; nothing joined it")
	case <-time.After(100 * time.Millisecond):
	}

	h.unpark()
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not return after the reclaim it joined had finished")
	}
	if _, err := os.Stat(leftover); !os.IsNotExist(err) {
		t.Fatalf("Close returned before the joined reclaim finished its sweep: stat err = %v", err)
	}
	awaitManagerGoroutines(t, 0)
}

// A reclaim requested AFTER Close is dropped whole: goBackground refuses on a
// closed manager, so no goroutine starts and no id is touched (issue #2878).
func TestReclaimDeletedScratchesAfterCloseIsDropped(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	m := newManager(t, &fakeDriver{}, nil)
	dir := seedScratch(t, m, "s1")
	m.Close()

	m.ReclaimDeletedScratches([]string{"s1"})

	// Two assertions covering each other, because a spawn is asynchronous: one
	// still running is seen here, and one that already finished has removed the
	// scratch dir the next check demands.
	if live, stacks := settleManagerGoroutines(1, 200*time.Millisecond); live != 0 {
		t.Fatalf("a reclaim requested after Close started %d goroutine(s):\n%s", live, stacks)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("a reclaim requested after Close reclaimed the scratch dir anyway: %v", err)
	}
}

// Manager implements the session.ScratchReclaimer interface (compile check).
var _ session.ScratchReclaimer = (*Manager)(nil)
