package instancemanager

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager/goroutineleak"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// ReclaimDeletedScratches removes the scratch dir and hydrate leftovers for a deleted server id.
func TestReclaimDeletedScratchesRemovesScratchAndHydrateLeftovers(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	dir := seedScratch(t, m, "s1")
	leftover := filepath.Join(m.scratchDir, ".hydrate-s1-stale")
	if err := os.MkdirAll(leftover, 0o750); err != nil {
		t.Fatal(err)
	}

	// Use the synchronous body for per-ID ordering checks; Close can stop asynchronous reclaim before its first ID.
	m.reclaimDeletedScratches([]string{"s1"})
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("scratch dir not reclaimed for deleted server: stat err = %v", err)
	}
	if _, err := os.Stat(leftover); !os.IsNotExist(err) {
		t.Fatalf("hydrate leftover not reclaimed for deleted server: stat err = %v", err)
	}
}

// Gate cleanup at the scratch removal boundary to verify hydrate leftovers are already gone.
func TestReclaimSweepsHydrateLeftoversBeforeTheScratchDirGoes(t *testing.T) {
	h := newBlockingReclaimLogger()
	m := newManager(t, &fakeDriver{}, nil).WithLogger(slog.New(h))
	// Registered AFTER newManager's Close, so cleanups run it FIRST (see
	// TestCloseJoinsAnInFlightReclaim).
	t.Cleanup(h.unpark)
	seedScratch(t, m, "s1")
	leftover := filepath.Join(m.scratchDir, ".hydrate-s1-stale")
	if err := os.MkdirAll(leftover, 0o750); err != nil {
		t.Fatal(err)
	}

	m.ReclaimDeletedScratches([]string{"s1"})
	// The parked record is emitted by the scratch removal itself, so reaching it
	// means <scratch>/s1 is already gone — the exact instant after which no pass is
	// ever offered this id again.
	select {
	case <-h.entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the reclaim never reached its removal; the log record this test parks on has changed")
	}
	if _, err := os.Stat(leftover); !os.IsNotExist(err) {
		t.Fatalf("the hydrate leftover outlived the scratch dir that advertises the id, so nothing can reclaim it: stat err = %v", err)
	}
	h.unpark()
}

// ReclaimDeletedScratches MUST NOT remove.displaced-<id> trees.
func TestReclaimDeletedScratchesRetainsDisplacedTree(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	dir := seedScratch(t, m, "s1")
	displaced := seedDisplaced(t, m, "s1")

	// The synchronous body, so the scratch removal has provably run by the time the
	// .displaced tree is checked and its survival is a decision, not a race (see
	// TestReclaimDeletedScratchesRemovesScratchAndHydrateLeftovers).
	m.reclaimDeletedScratches([]string{"s1"})
	// Require scratch removal first so a no-op reclaim cannot pass the displaced-tree retention assertion.
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("scratch dir not reclaimed for deleted server: stat err = %v", err)
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
	control := seedScratch(t, m, "s2")
	_ = m.Handle(context.Background(), startCmd())

	// s2 is the positive control, listed AFTER the running id and asserted FIRST: a reclaim that does nothing at
	// all leaves s1 standing too, and one that aborts the call at s1 instead of skipping it never reaches s2.
	m.reclaimDeletedScratches([]string{"s1", "s2"})
	if _, err := os.Stat(control); !os.IsNotExist(err) {
		t.Fatalf("scratch dir not reclaimed for the deleted server listed after the running one: stat err = %v", err)
	}
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
	control := seedScratch(t, m, "s1")

	// s1 is the positive control, listed AFTER the unsafe ids and asserted FIRST, so the sibling's survival is a
	// refusal rather than a reclaim that did nothing, and a refusal that skips the id rather than aborting the
	// call.
	m.reclaimDeletedScratches([]string{"../escaped", "", ".", "s1"})
	if _, err := os.Stat(control); !os.IsNotExist(err) {
		t.Fatalf("scratch dir not reclaimed for the safe id listed after the unsafe ones: stat err = %v", err)
	}
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
	control := seedScratch(t, m, "s2")

	// Simulate s1 having an in-flight hydrate by reserving it.
	ok, _, _ := m.reserve("s1")
	if !ok {
		t.Fatal("could not reserve s1 for test setup")
	}

	// s2 is the positive control, listed AFTER the reserved id and asserted FIRST (see
	// TestReclaimDeletedScratchesSkipsRunningServer).
	m.reclaimDeletedScratches([]string{"s1", "s2"})
	if _, err := os.Stat(control); !os.IsNotExist(err) {
		t.Fatalf("scratch dir not reclaimed for the deleted server listed after the reserved one: stat err = %v", err)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("scratch dir removed for a reserved server: %v", err)
	}
	m.release("s1")
}

// Block at the reclaim log between scratch removal and reservation release to hold an in-flight cleanup
// deterministically.
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
// removing a scratch dir and before releasing the reservation.
func newBlockingReclaimLogger() *blockingReclaimLogger {
	return &blockingReclaimLogger{
		msg:     "reclaimed orphaned scratch for deleted server",
		entered: make(chan struct{}),
		release: make(chan struct{}),
	}
}

// Close JOINS a reclaim in flight. The reclaim was the one manager-owned goroutine spawned with a bare go, so
// Close neither waited for it nor cancelled it: parked here it has removed the scratch tree but has not yet
// released the reservation, and a Close that returned in that window lets the process exit inside it.
func TestCloseJoinsAnInFlightReclaim(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	h := newBlockingReclaimLogger()
	m := newManager(t, &fakeDriver{}, nil).WithLogger(slog.New(h))
	// Registered AFTER newManager's Close, so cleanups run it FIRST: a t.Fatal
	// below would otherwise leave Close joining a reclaim nothing ever releases,
	// and the package would hang instead of failing.
	t.Cleanup(h.unpark)
	dir := seedScratch(t, m, "s1")

	m.ReclaimDeletedScratches([]string{"s1"})
	select {
	case <-h.entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the reclaim never reached its removal; the log record this test parks on has changed")
	}
	// The status dispatcher and the reclaim. Counting the reclaim at all is what
	// pins its frame into goroutineleak's census, so the leak check can see it.
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
	// The work still ahead of the park is the release, so that is what proves Close
	// waited for the body rather than merely for the goroutine's last log record.
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("the joined reclaim did not remove the scratch dir: stat err = %v", err)
	}
	m.mu.Lock()
	stillReserved := len(m.reserved)
	m.mu.Unlock()
	if stillReserved != 0 {
		t.Fatalf("Close returned before the joined reclaim released its reservation: %d held", stillReserved)
	}
	awaitManagerGoroutines(t, 0)
}

// A reclaim requested AFTER Close is dropped whole: goBackground refuses on a closed manager, so no goroutine
// starts and no id is touched.
func TestReclaimDeletedScratchesAfterCloseIsDropped(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	m := newManager(t, &fakeDriver{}, nil)
	control := seedScratch(t, m, "s0")
	dir := seedScratch(t, m, "s1")

	// Verify this entry point reclaims before Close so the later no-op proves shutdown rejection.
	m.ReclaimDeletedScratches([]string{"s0"})
	deadline := time.Now().Add(5 * time.Second)
	for {
		_, err := os.Stat(control)
		if os.IsNotExist(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the reclaim requested before Close never removed the control's scratch dir: stat err = %v", err)
		}
		time.Sleep(time.Millisecond)
	}
	m.Close()

	m.ReclaimDeletedScratches([]string{"s1"})

	// Two assertions covering each other, because a spawn is asynchronous: one
	// still running is seen here, and one that already finished has removed the
	// scratch dir the next check demands.
	if live, stacks := goroutineleak.Settle(1, 200*time.Millisecond); live != 0 {
		t.Fatalf("a reclaim requested after Close started %d goroutine(s):\n%s", live, stacks)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("a reclaim requested after Close reclaimed the scratch dir anyway: %v", err)
	}
}

// Pause reclaim within one ID, begin Close, then require that ID to finish and later IDs to remain untouched.
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

// Skipped IDs remain advertised so the next registration retries their reclaim.
func TestStoppedReclaimLeavesSkippedIDsHeld(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	ids := []string{"s1", "s2"}
	for _, id := range ids {
		seedScratch(t, m, id)
	}
	seedScratch(t, m, "s0")

	// The positive control: on a closed manager the loop top stops at the first id, so no id in the stopped call
	// can be one it reclaims. Instead the same body on the same manager reclaims s0 before Close, which makes s1
	// and s2 staying held below the stop rather than a reclaim that does nothing.
	m.reclaimDeletedScratches([]string{"s0"})
	m.Close()

	m.reclaimDeletedScratches(ids)

	held := make(map[string]bool)
	for _, hs := range m.HeldServers() {
		held[hs.ServerID] = true
	}
	if held["s0"] {
		t.Fatalf("s0 is still advertised as held after the reclaim before Close, so the ids below staying held proves nothing about the stop: held = %v", held)
	}
	for _, id := range ids {
		if !held[id] {
			t.Fatalf("%s is no longer advertised as held after a stopped reclaim, so the next registration cannot re-offer it: held = %v", id, held)
		}
	}
}

// Manager implements the session.ScratchReclaimer interface (compile check).
var _ session.ScratchReclaimer = (*Manager)(nil)
