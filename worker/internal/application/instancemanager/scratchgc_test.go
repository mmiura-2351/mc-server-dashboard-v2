package instancemanager

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
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

// A confirmed StopServer is an AUTHORITATIVE stop. The Worker must NOT GC the
// local working set on the stop itself: the API sends the final SnapshotTrigger
// for FR-DATA-7 only AFTER the stop's CommandResult (StopServer.__call__,
// lifecycle.py), so a stop-time GC would leave that snapshot to pack an empty
// dir and lose the world progressed since the last periodic snapshot (issue
// #841). The scratch is GC'd only AFTER the post-stop final snapshot publishes
// (TestStoppedSnapshotRemovesScratchAfterPublish); #762's anti-accumulation goal
// is preserved by reclaiming it then (and, for a snapshot that never arrives, at
// the next start's hydrate or worker-restart scan).
func TestStopRetainsScratchForFinalSnapshot(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())
	dir := seedScratch(t, m, "s1")

	res := m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("scratch dir removed by the stop itself (the post-stop final snapshot would pack an empty dir, issue #841): %v", err)
	}
}

// The bug fixed by #841: the API drives a graceful stop, then sends the final
// SnapshotTrigger for the same (now stopped, unassigned) id. The working set must
// still be present when that snapshot packs it. Before the fix, handleStop GC'd
// the scratch before returning, so the snapshot captured an empty dir and the
// world progressed since the last periodic snapshot was silently lost.
func TestStopThenFinalSnapshotPacksWorkingSet(t *testing.T) {
	tr := &fakeTransfer{}
	d := &fakeDriver{}
	m := newManager(t, d, nil).WithTransfer(tr)
	_ = m.Handle(context.Background(), startCmd())
	seedScratch(t, m, "s1")

	if res := m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: "s1", Kind: "StopServer"}); !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	// API ordering: final SnapshotTrigger AFTER the stop CommandResult.
	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("post-stop final snapshot = %+v, want success", res)
	}
	if len(tr.snapshotHadWorkingSet) != 1 || !tr.snapshotHadWorkingSet[0] {
		t.Fatalf("post-stop final snapshot packed an empty/absent working dir (world silently lost, issue #841): hadWorkingSet=%v", tr.snapshotHadWorkingSet)
	}
}

// #762's anti-accumulation goal, repositioned by #841: the scratch IS reclaimed,
// but AFTER the post-stop final snapshot has published it — not before. Once the
// stopped-id snapshot succeeds the working set is captured authoritatively and the
// API has unassigned the Worker, so the local copy is safe to GC.
func TestStoppedSnapshotRemovesScratchAfterPublish(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	dir := seedScratch(t, m, "s1") // stopped id: no running instance

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("stopped-id snapshot = %+v, want success", res)
	}
	if len(tr.snapshotHadWorkingSet) != 1 || !tr.snapshotHadWorkingSet[0] {
		t.Fatalf("snapshot did not see the working set before GC: %v", tr.snapshotHadWorkingSet)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("scratch dir not reclaimed after a successful stopped-id snapshot (breaks #762): stat err = %v", err)
	}
}

// A duplicate stopped-id SnapshotTrigger arriving AFTER the scratch was GC'd —
// the final snapshot published, removeScratch ran, but the CommandResult was lost
// on a dropped stream so the API re-dispatched — must be refused WITHOUT a
// transfer (issue #1713). Packing the absent dir uploads an empty tar with the
// base-generation guard disabled (readGeneration(absent) is 0, so the header is
// omitted), leaving the API-side empty-staging refusal as the only defense. The
// refusal is SERVER_NOT_FOUND, not TRANSFER_FAILED: no working set is held for
// the id, and no retry can succeed without a hydrate — a terminal condition, not
// a transient transfer failure.
func TestStoppedSnapshotAbsentWorkingDirRefusedWithoutTransfer(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	seedScratch(t, m, "s1") // stopped id: no running instance

	// The first final snapshot publishes and GCs the scratch.
	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("first stopped-id snapshot = %+v, want success", res)
	}
	// The duplicate re-dispatch finds the working dir absent and must refuse.
	res := m.Handle(context.Background(), snapshotCmd())
	if res.Success || res.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("duplicate stopped-id snapshot after GC = %+v, want server-not-found refusal", res)
	}
	// The phrase is load-bearing (issue #1790): the API's final-snapshot path
	// matches it (with the SERVER_NOT_FOUND code) to downgrade this refusal from
	// its data-loss ERROR to a benign-duplicate INFO — _WORKING_SET_ABSENT_MARKER
	// in api/src/mc_server_dashboard_api/servers/application/lifecycle.py. A
	// reword here silently re-arms the false alarm unless done together.
	if !strings.Contains(res.ErrorMessage, "working dir absent") {
		t.Fatalf("refusal message = %q, want the API-pinned phrase \"working dir absent\"", res.ErrorMessage)
	}
	if len(tr.snapshots) != 1 {
		t.Fatalf("the duplicate must not pack/upload the absent dir; snapshots = %v", tr.snapshots)
	}
}

// A FAILED stopped-id snapshot must RETAIN the scratch: the working set was not
// captured, so GC-ing it would lose the world exactly as the stop-time GC did
// (issue #841). The retained scratch is reclaimed on a later retry or at startup.
func TestStoppedSnapshotFailureRetainsScratch(t *testing.T) {
	tr := &fakeTransfer{err: errors.New("boom")}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	dir := seedScratch(t, m, "s1")

	if res := m.Handle(context.Background(), snapshotCmd()); res.Success {
		t.Fatalf("snapshot = %+v, want failure", res)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("scratch dir removed after a FAILED snapshot (world would be lost, issue #841): %v", err)
	}
}

// A RUNNING-id snapshot (the periodic FR-DATA-7 path) must NOT GC the scratch:
// the server is live and still owns its working set. Only the stopped-id snapshot
// — the post-stop final capture — reclaims it.
func TestRunningSnapshotRetainsScratch(t *testing.T) {
	tr := &fakeTransfer{}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	_ = m.Handle(context.Background(), startCmd())
	dir := seedScratch(t, m, "s1")

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("running-id snapshot = %+v, want success", res)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("scratch dir removed after a running-server snapshot: %v", err)
	}
}

// A crash mid-hydrate (datatransfer.unpackAndSwap, issue #772) leaves
// .hydrate-<id>-* temp/trash siblings in the scratch root. The next start's
// leftover sweep only clears them if the id is re-placed onto this Worker, so the
// authoritative reclamation (server delete / re-placed elsewhere) must sweep this
// id's siblings too — otherwise the world-sized orphan leaks permanently (issue
// #806). Since #841 that reclamation runs on the stopped-id final snapshot, not on
// the stop itself: the scratch dir and this id's leftovers are reclaimed together.
func TestFinalSnapshotSweepsHydrateLeftovers(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	dir := seedScratch(t, m, "s1") // stopped id: no running instance

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

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("stopped-id snapshot = %+v, want success", res)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("scratch dir not reclaimed after a successful stopped-id snapshot (breaks #762): stat err = %v", err)
	}
	if _, err := os.Stat(leftover); !os.IsNotExist(err) {
		t.Fatalf("s1 hydrate leftover still present after the final snapshot: stat err = %v", err)
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

// seedDisplaced creates a .displaced-<id> tree, as a prior hydrate would have left
// when it moved a retained-for-recovery scratch aside (issue #906). Returns the path
// so a test can assert whether a snapshot reclaimed it.
func seedDisplaced(t *testing.T, m *Manager, serverID string) string {
	t.Helper()
	dir := filepath.Join(m.scratchDir, ".displaced-"+serverID)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "level.dat"), []byte("recoverable"), 0o640); err != nil {
		t.Fatal(err)
	}
	return dir
}

// A successful STOPPED-id snapshot proves the store now supersedes this server's
// world, so the .displaced-<id> recovery tree a prior hydrate kept aside (issue #906)
// is reclaimed alongside the scratch — mirroring the #845 GC-on-success pattern.
func TestStoppedSnapshotGCsDisplacedTree(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	seedScratch(t, m, "s1") // stopped id: no running instance
	displaced := seedDisplaced(t, m, "s1")

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("stopped-id snapshot = %+v, want success", res)
	}
	if _, err := os.Stat(displaced); !os.IsNotExist(err) {
		t.Fatalf("displaced tree not reclaimed after a successful stopped-id snapshot (issue #906): stat err = %v", err)
	}
}

// A successful RUNNING-id snapshot also supersedes any displaced recovery tree (the
// store now holds the live world), so it GCs .displaced-<id> too (issue #906). The
// live scratch dir itself is retained — the server still owns it.
func TestRunningSnapshotGCsDisplacedTree(t *testing.T) {
	tr := &fakeTransfer{}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	_ = m.Handle(context.Background(), startCmd())
	dir := seedScratch(t, m, "s1")
	displaced := seedDisplaced(t, m, "s1")

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("running-id snapshot = %+v, want success", res)
	}
	if _, err := os.Stat(displaced); !os.IsNotExist(err) {
		t.Fatalf("displaced tree not reclaimed after a successful running-id snapshot (issue #906): stat err = %v", err)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("live scratch dir wrongly removed by a running-server snapshot: %v", err)
	}
}

// A FAILED snapshot must RETAIN the displaced recovery tree: the store did not
// capture the world, so the .displaced-<id> copy is still the only one — GC-ing it
// would defeat the recovery insurance entirely (issue #906).
func TestSnapshotFailureRetainsDisplacedTree(t *testing.T) {
	tr := &fakeTransfer{err: errors.New("boom")}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	seedScratch(t, m, "s1")
	displaced := seedDisplaced(t, m, "s1")

	if res := m.Handle(context.Background(), snapshotCmd()); res.Success {
		t.Fatalf("snapshot = %+v, want failure", res)
	}
	if _, err := os.Stat(displaced); err != nil {
		t.Fatalf("displaced tree removed after a FAILED snapshot (recovery copy lost, issue #906): %v", err)
	}
}

// A .displaced-<id> tree must never be treated as a LIVE scratch: it is dot-prefixed
// so it cannot collide with a server-id scratch dir, and the id-scoped sweeps touch
// only their own server's siblings (issue #906). ScanHeldServers must SKIP the
// .displaced- prefix entirely (issue #910): reporting it triggers a per-boot header
// fsck of a world-sized recovery tree and a confusing server_id=.displaced-<id>
// corrupt warning.
func TestDisplacedTreeNotTreatedAsLiveScratch(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	displaced := seedDisplaced(t, m, "s1")

	// A held-server scan never reports the displaced tree at all: neither under the
	// assigned id "s1" nor under its on-disk ".displaced-s1" name.
	for _, h := range ScanHeldServers(m.scratchDir, nil) {
		if h.ServerID == "s1" || strings.HasPrefix(h.ServerID, ".displaced-") {
			t.Fatalf("displaced tree reported as held server id %q (issue #910: must be skipped)", h.ServerID)
		}
	}
	// The hydrate-leftover sweep for s1 must not remove the displaced tree (different
	// prefix), and a displaced sweep for a DIFFERENT id must not touch s1's displaced.
	m.sweepHydrateLeftovers("s1")
	if _, err := os.Stat(displaced); err != nil {
		t.Fatalf("displaced tree removed by the s1 hydrate-leftover sweep: %v", err)
	}
	m.sweepDisplaced("s2")
	if _, err := os.Stat(displaced); err != nil {
		t.Fatalf("displaced tree for s1 removed by an s2 displaced sweep (wrong id): %v", err)
	}
}
