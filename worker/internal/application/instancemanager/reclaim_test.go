package instancemanager

import (
	"context"
	"os"
	"path/filepath"
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
	_ = m.Handle(context.Background(), startCmd())
	dir := seedScratch(t, m, "s1")

	m.ReclaimDeletedScratches([]string{"s1"})
	// Give time for the goroutine.
	time.Sleep(50 * time.Millisecond)
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

	m.ReclaimDeletedScratches([]string{"../escaped", "", "."})
	time.Sleep(50 * time.Millisecond)
	if _, err := os.Stat(sibling); err != nil {
		t.Fatalf("traversal-unsafe id escaped the scratch root: %v", err)
	}
}

// ReclaimDeletedScratches is idempotent on a missing dir (no error).
func TestReclaimDeletedScratchesIdempotentOnMissingDir(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	// "no-such-server" has no scratch dir — the call should not panic.
	m.ReclaimDeletedScratches([]string{"no-such-server"})
	time.Sleep(50 * time.Millisecond)
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

	m.ReclaimDeletedScratches([]string{"s1"})
	time.Sleep(50 * time.Millisecond)
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("scratch dir removed for a reserved server: %v", err)
	}
	m.release("s1")
}

// Manager implements the session.ScratchReclaimer interface (compile check).
var _ session.ScratchReclaimer = (*Manager)(nil)
