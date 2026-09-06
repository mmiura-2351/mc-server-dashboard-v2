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

	// The SYNCHRONOUS body, as the rest of this file's per-id tests use. The
	// asynchronous entry point removes the scratch dir first and sweeps the hydrate
	// leftover after, so the leftover check would race that goroutine on its own
	// (issue #1888), and the only barrier available is Close — which since issue
	// #2933 also STOPS the reclaim at its loop top, so a spawn that has not reached
	// its first id yet reclaims nothing. TestCloseJoinsAnInFlightReclaim covers the
	// goroutine and its join; this test is about what one id's reclaim removes.
	m.reclaimDeletedScratches([]string{"s1"})
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

	// The synchronous body, so the scratch removal has provably run by the time the
	// .displaced tree is checked and its survival is a decision, not a race (see
	// TestReclaimDeletedScratchesRemovesScratchAndHydrateLeftovers).
	m.reclaimDeletedScratches([]string{"s1"})
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
	msg         string
	entered     chan struct{}
	release     chan struct{}
	enterOnce   sync.Once
	releaseOnce sync.Once
}

func (h *blockingReclaimLogger) Enabled(context.Context, slog.Level) bool { return true }

func (h *blockingReclaimLogger) Handle(_ context.Context, r slog.Record) error {
	if r.Message == h.msg {
		h.enterOnce.Do(func() { close(h.entered) })
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
func (h *blockingReclaimLogger) unpark() { h.releaseOnce.Do(func() { close(h.release) }) }

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

// Close stops a reclaim at the next id boundary (issue #2933). The reclaim reads
// the shutdown at the TOP of the per-id loop — after the previous id's release,
// before the next id's reserve — where it holds no reservation and no half-done
// removal, so stopping there is safe by the same reasoning that makes the join
// safe. What it buys is the bound: Close pays the filesystem work of the id
// already in flight, not of every id still on the list. The id in flight still
// finishes; the window from reserve to release stays uninterruptible on purpose
// (issue #2878), which is what leaves no id with a removed tree and a held
// reservation.
func TestCloseStopsTheReclaimAtTheNextIDBoundary(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	h := newBlockingReclaimLogger()
	m := newManager(t, &fakeDriver{}, nil).WithLogger(slog.New(h))
	// Registered AFTER newManager's Close, so cleanups run it FIRST (see
	// TestCloseJoinsAnInFlightReclaim).
	t.Cleanup(h.unpark)

	ids := []string{"s1", "s2", "s3", "s4", "s5"}
	dirs := make(map[string]string, len(ids))
	for _, id := range ids {
		dirs[id] = seedScratch(t, m, id)
	}
	leftover := filepath.Join(m.scratchDir, ".hydrate-s1-stale")
	if err := os.MkdirAll(leftover, 0o750); err != nil {
		t.Fatal(err)
	}

	m.ReclaimDeletedScratches(ids)
	select {
	case <-h.entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the reclaim never reached its removal; the log record this test parks on has changed")
	}
	// The status dispatcher and the reclaim, parked inside s1.
	awaitManagerGoroutines(t, 2)

	closed := make(chan struct{})
	go func() { m.Close(); close(closed) }()
	// The dispatcher is gone, so Close is past stopBackground and the shutdown the
	// loop top reads is already cancelled: unparking now resumes the reclaim into a
	// manager that is shutting down, which is the state under test rather than a
	// scheduling coincidence.
	awaitManagerGoroutines(t, 1)
	h.unpark()
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not return after the reclaim it joined had finished")
	}

	// s1 was past the loop top when the shutdown landed, so it completes whole.
	if _, err := os.Stat(dirs["s1"]); !os.IsNotExist(err) {
		t.Fatalf("the id in flight was left half-reclaimed: stat err = %v", err)
	}
	if _, err := os.Stat(leftover); !os.IsNotExist(err) {
		t.Fatalf("the id in flight kept its hydrate leftovers: stat err = %v", err)
	}
	// Every id after it is untouched: Close did not pay their filesystem work.
	for _, id := range ids[1:] {
		if _, err := os.Stat(dirs[id]); err != nil {
			t.Fatalf("Close paid %s's reclaim after the shutdown was signalled: %v", id, err)
		}
	}
	// The loop top sits after a release, so the stopped reclaim holds nothing.
	m.mu.Lock()
	stillReserved := len(m.reserved)
	m.mu.Unlock()
	if stillReserved != 0 {
		t.Fatalf("the stopped reclaim left %d reservation(s) held", stillReserved)
	}
	awaitManagerGoroutines(t, 0)
}

// The ids a stopped reclaim skips are re-offered, not lost (issue #2933). Their
// scratch dirs are still on disk, so HeldServers() keeps advertising them and the
// next registration re-derives the unknown subset from that advertisement — the
// ack-vs-advertised intersection pinned in the session package
// (TestRegisterAckUnknownHeldServerIDsPlumbedToReclaimer) over a held set the
// runner re-reads per registration (TestReRegistrationRefreshesHeldServers, issue
// #1711). This is the Worker-side half those two compose with: a skipped id is
// still HELD.
//
// It also pins the low end of the bound. On a manager that is already shutting
// down the loop top stops the reclaim at the FIRST id, so "at most one id's
// filesystem work" includes none at all.
func TestStoppedReclaimLeavesSkippedIDsHeld(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	ids := []string{"s1", "s2"}
	for _, id := range ids {
		seedScratch(t, m, id)
	}
	m.Close()

	m.reclaimDeletedScratches(ids)

	held := make(map[string]bool)
	for _, hs := range m.HeldServers() {
		held[hs.ServerID] = true
	}
	for _, id := range ids {
		if !held[id] {
			t.Fatalf("%s is no longer advertised as held after a stopped reclaim, so the next registration cannot re-offer it: held = %v", id, held)
		}
	}
}

// Manager implements the session.ScratchReclaimer interface (compile check).
var _ session.ScratchReclaimer = (*Manager)(nil)
