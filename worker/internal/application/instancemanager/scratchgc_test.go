package instancemanager

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// seedScratch creates a non-empty scratch working dir for serverID so a test can
// assert whether a stop/restart removed or retained it. It mirrors what a real
// hydrate/run leaves behind (at least one file under scratchDir/<id>).
func seedScratch(t *testing.T, m *Manager, serverID string) string {
	t.Helper()
	dir := filepath.Join(m.scratchDir, serverID)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "level.dat"), []byte("world"), 0o640); err != nil {
		t.Fatal(err)
	}
	return dir
}

// A confirmed StopServer is an AUTHORITATIVE stop: every API path that sends a
// bare StopServer to the Worker (StopServer, redispatch_stop) clears the server's
// assignment on the confirmed stop. So the Worker GCs the local working set on a
// successful stop — leftover scratch only accumulates disk and widens the
// stale-leftover surface the #698 hydrate-skip reasons about (issue #762).
func TestStopRemovesScratch(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())
	dir := seedScratch(t, m, "s1")

	res := m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("scratch dir still present after authoritative stop: stat err = %v", err)
	}
}

// A crash mid-hydrate (datatransfer.unpackAndSwap, issue #772) leaves
// .hydrate-<id>-* temp/trash siblings in the scratch root. The next start's
// leftover sweep only clears them if the id is re-placed onto this Worker, so an
// authoritative stop (server delete / re-placed elsewhere) must sweep this id's
// siblings too — otherwise the world-sized orphan leaks permanently (issue #806).
func TestStopSweepsHydrateLeftovers(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())
	seedScratch(t, m, "s1")

	// A leftover temp/trash sibling for s1 from a crashed hydrate.
	leftover := filepath.Join(m.scratchDir, ".hydrate-s1-stale")
	if err := os.MkdirAll(leftover, 0o750); err != nil {
		t.Fatal(err)
	}
	// Another server's leftover must NOT be touched (exact-prefix match for s1 only).
	otherLeftover := filepath.Join(m.scratchDir, ".hydrate-s2-stale")
	if err := os.MkdirAll(otherLeftover, 0o750); err != nil {
		t.Fatal(err)
	}

	res := m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	if _, err := os.Stat(leftover); !os.IsNotExist(err) {
		t.Fatalf("s1 hydrate leftover still present after authoritative stop: stat err = %v", err)
	}
	if _, err := os.Stat(otherLeftover); err != nil {
		t.Fatalf("another server's hydrate leftover was swept (must match s1's prefix only): %v", err)
	}
}

// sweepHydrateLeftovers removes only the .hydrate-<id>-* siblings for the given id,
// leaving the server's own scratch dir and unrelated entries untouched (issue #806).
func TestSweepHydrateLeftovers(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)

	staleA := filepath.Join(m.scratchDir, ".hydrate-s1-stale")
	staleB := filepath.Join(m.scratchDir, ".hydrate-s1-other")
	if err := os.MkdirAll(staleA, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(staleB, 0o750); err != nil {
		t.Fatal(err)
	}
	keep := seedScratch(t, m, "s1") // the live working dir, must be retained
	otherID := filepath.Join(m.scratchDir, ".hydrate-s11-stale")
	if err := os.MkdirAll(otherID, 0o750); err != nil {
		t.Fatal(err)
	}

	m.sweepHydrateLeftovers("s1")

	for _, p := range []string{staleA, staleB} {
		if _, err := os.Stat(p); !os.IsNotExist(err) {
			t.Fatalf("leftover %s not removed: stat err = %v", p, err)
		}
	}
	if _, err := os.Stat(keep); err != nil {
		t.Fatalf("live scratch dir wrongly removed: %v", err)
	}
	if _, err := os.Stat(otherID); err != nil {
		t.Fatalf("different-id leftover (.hydrate-s11-) wrongly removed by s1 sweep: %v", err)
	}
}

// A RestartServer is a TRANSIENT restart: the API's RestartServer keeps the
// assignment (desired stays running) and the same Worker keeps its live working
// set so the #698 hydrate-skip still applies on the next start. The Worker must
// RETAIN the scratch — deleting it here would reintroduce the #696 rollback
// (a later hydrate would unpack the last snapshot over an empty dir) (issue #762).
func TestRestartRetainsScratch(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())
	dir := seedScratch(t, m, "s1")

	res := m.Handle(context.Background(), session.Command{CommandID: "restart", ServerID: "s1", Kind: "RestartServer"})
	if !res.Success {
		t.Fatalf("restart = %+v, want success", res)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("scratch dir removed by a transient restart (breaks #698 hydrate-skip): %v", err)
	}
}

// A failed-stop orphan may still be alive (the driver could not confirm
// termination, issue #251): the lingering process can still write the working
// set, so a failed StopServer must RETAIN the scratch. GC only on a CONFIRMED
// stop (issue #762).
func TestFailedStopRetainsScratch(t *testing.T) {
	d := &orphanDriver{stopAfter: 1} // first Stop fails, leaving an orphan
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())
	dir := seedScratch(t, m, "s1")

	res := m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: "s1", Kind: "StopServer"})
	if res.Success {
		t.Fatalf("first stop = %+v, want failure (driver could not confirm termination)", res)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("scratch dir removed on a failed stop (the orphan may still be writing it): %v", err)
	}
}
