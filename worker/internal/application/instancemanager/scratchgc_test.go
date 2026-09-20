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
// hydrate/run leaves behind (at least one file under scratchDir/<id>), which
// since issue #2802 includes the GENERATION MARKER: a 200 hydrate embeds it in
// the tree before the swap-in (issue #917) and a 204 stamps it as its only write,
// so a marker-less dir is not a state any hydrate leaves — and it is the exact
// state launchReserved's guard refuses. Content 0 keeps readGeneration's answer
// what an unmarked dir gave before (an unknown/never-hydrated set).
func seedScratch(t *testing.T, m *Manager, serverID string) string {
	t.Helper()
	dir := filepath.Join(m.scratchDir, serverID)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "level.dat"), []byte("world"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, generationFile), []byte("0"), 0o640); err != nil {
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
	dir := seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())

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
	seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())

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
	dir := seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())

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
	dir := seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())

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
	dir := seedScratch(t, m, "s1") // before the start: launchReserved refuses a marker-less dir
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}

	res := m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: "s1", Kind: "StopServer"})
	if res.Success {
		t.Fatalf("first stop = %+v, want failure (driver could not confirm termination)", res)
	}
	// The failure must be the ORPHAN one, not a short-circuit. A stop that never
	// reached the driver refuses with SERVER_NOT_FOUND before attemptStop runs, and
	// the retention assertion below then holds vacuously — which is exactly how this
	// test passed for the wrong reason while the start was refused (issue #2828).
	if res.ErrorCode != session.CommandErrorInternal {
		t.Fatalf("first stop = %+v, want the unconfirmed-termination failure (INTERNAL); "+
			"a SERVER_NOT_FOUND here means no instance was registered and the orphan path never ran", res)
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
	dir := seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())
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

// The sweep in the post-upload tail is gated on the same working-dir identity pin as
// the marker stamp beside it (issue #2291, closing the window #917 item 3 named). With
// the working dir replaced mid-upload by a concurrent stream's hydrate, the tree at
// .displaced-<id> is that hydrate's recovery copy — the world as it stood before the
// swap, which this snapshot never published — so removing it would spend a copy this
// success does not supersede. The sweep is skipped instead, and the leaked tree is
// reclaimed by the next successful snapshot for the id (the #906 GC-on-success
// contract). The publish itself still succeeds: only the GC is declined.
func TestRunningSnapshotSkipsDisplacedSweepWhenWorkingDirReplaced(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())
	tr.duringUpload = func(workingDir string) { replaceWorkingDirLikeHydrate(t, workingDir, 7) }

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("running-id snapshot = %+v, want success (the publish succeeded; only the sweep is skipped)", res)
	}

	// seedScratch wrote this content, and replaceWorkingDirLikeHydrate renamed that very
	// directory to .displaced-s1 — so reading it back proves the surviving tree is the
	// racing hydrate's recovery copy and not some other leftover.
	got, err := os.ReadFile(filepath.Join(m.scratchDir, ".displaced-s1", "level.dat"))
	if err != nil {
		t.Fatalf("displaced recovery tree removed by a snapshot of a working dir it no longer "+
			"packed: the sweep is not gated on the identity pin (issue #2291): %v", err)
	}
	if string(got) != "world" {
		t.Fatalf("displaced tree content = %q, want %q (the interleaving did not happen, "+
			"so this test proves nothing)", got, "world")
	}
}

// A hydrate that lands WHILE a successful snapshot's sweep is still removing the
// displaced tree must find the slot empty, never the half-deleted tree (issue #2799).
// The removal is a traversal that takes seconds for a world-sized tree, and the
// hydrate's oldest-wins check (datatransfer.displacedSlotHoldsWorkingSet, the same
// "holds a working set" test as hasWorkingSet) reads the slot BY NAME: a tree still
// being traversed there reads as an occupied slot, so the hydrate retains it — while
// the traversal finishes deleting it — and drops the live set it displaces. The
// identity pin on the sweep (issue #2291) does not cover this: the working dir is still
// the packed tree when the sweep starts, and it is the HYDRATE that misreads the slot.
//
// The removal seam lands a racing hydrate part-way through the traversal, the one
// interleaving the fix has to hold for, rather than racing for it.
func TestHydrateDuringDisplacedSweepFindsTheSlotEmpty(t *testing.T) {
	tr := &fakeTransfer{}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	live := seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	slot := seedDisplaced(t, m, "s1")
	// A second entry, so removing one of them models a traversal already under way
	// with the rest of the world still on disk.
	if err := os.MkdirAll(filepath.Join(slot, "world", "region"), 0o750); err != nil {
		t.Fatal(err)
	}

	var interleaved, droppedLive bool
	restore := removeDisplacedTree
	removeDisplacedTree = func(path string) error {
		interleaved = true
		if err := os.Remove(filepath.Join(path, "level.dat")); err != nil {
			t.Fatalf("model the traversal's first unlink: %v", err)
		}
		// The racing hydrate's slot decision lands here, mid-traversal.
		if hasWorkingSet(slot) {
			droppedLive = true // oldest-wins: retain the slot, discard the live set
		} else {
			replaceWorkingDirLikeHydrate(t, live, 7) // ordinary displace: park the live set in the slot
		}
		return restore(path)
	}
	t.Cleanup(func() { removeDisplacedTree = restore })

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("running-id snapshot = %+v, want success", res)
	}

	if !interleaved {
		t.Fatal("the sweep never reached its removal, so no hydrate interleaved and this test proves nothing")
	}
	if droppedLive {
		t.Fatal("a hydrate landing mid-sweep found a half-deleted tree in the .displaced-s1 slot: " +
			"oldest-wins retains it (and the sweep then finishes deleting it) while the live set " +
			"it displaces is dropped (issue #2799)")
	}
	// seedScratch wrote "world" into the live set, so reading it back from the slot
	// proves the hydrate's recovery copy survived the rest of the sweep's traversal.
	got, err := os.ReadFile(filepath.Join(slot, "level.dat"))
	if err != nil || string(got) != "world" {
		t.Fatalf("slot level.dat = %q (err %v), want %q: the sweep removed the racing "+
			"hydrate's recovery copy along with the tree it was sweeping", got, err, "world")
	}
	// Only the hydrated working dir and the hydrate's recovery copy remain: the swept
	// tree is gone, under whatever name it was removed from.
	entries, err := os.ReadDir(m.scratchDir)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, e := range entries {
		names = append(names, e.Name())
	}
	if len(names) != 2 || names[0] != ".displaced-s1" || names[1] != "s1" {
		t.Fatalf("scratch root = %v, want [.displaced-s1 s1]: the swept tree must be removed in full", names)
	}
}

// A running-id sweep must not take a recovery copy a hydrate parked in the slot AFTER
// the caller's identity pin passed (issue #3118). The pin gates the sweep (issue #2291),
// but it keeps passing right up to the moment the racing hydrate renames the working dir
// aside — and what the sweep's rename takes out of the slot is whatever sits there at
// THAT instant, not what its Lstat saw. The shape is routine, not exotic: the slot held
// marker-only junk (a 204 hydrate leaves a world-less <scratch>/<id> that the next
// hydrate parks here by the ordinary displace path), the hydrate clears that junk and
// parks its live set directly in the slot as its recovery copy, and the sweep then
// renames that live set out and removes it — a tree this snapshot never published,
// holding the published state plus everything written since its pack.
//
// The rename seam lands the hydrate's park in that gap, the one interleaving the
// post-rename re-check has to hold for, rather than racing for it.
func TestDisplacedSweepKeepsARecoveryCopyParkedAfterThePinCheck(t *testing.T) {
	tr := &fakeTransfer{}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	live := seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	slot := filepath.Join(m.scratchDir, ".displaced-s1")
	if err := os.MkdirAll(slot, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := writeGeneration(slot, 3); err != nil {
		t.Fatal(err)
	}

	var interleaved bool
	restore := renameSweptTree
	renameSweptTree = func(from, to string) error {
		interleaved = true
		// The racing hydrate lands here: it clears the world-less junk
		// (datatransfer.displacedSlotHoldsWorkingSet) and takes the ordinary displace
		// path, which parks the live set DIRECTLY in the slot as its recovery copy.
		if err := os.RemoveAll(slot); err != nil {
			t.Fatalf("model the hydrate's junk clear: %v", err)
		}
		replaceWorkingDirLikeHydrate(t, live, 7)
		return restore(from, to)
	}
	t.Cleanup(func() { renameSweptTree = restore })

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("running-id snapshot = %+v, want success (the publish succeeded; only the GC is at stake)", res)
	}

	if !interleaved {
		t.Fatal("the sweep never reached its rename, so no hydrate interleaved and this test proves nothing")
	}
	// seedScratch wrote "world" into the live set and replaceWorkingDirLikeHydrate
	// renamed that very directory into the slot, so reading it back proves the surviving
	// tree is the hydrate's recovery copy — not the junk the sweep set out to remove.
	got, err := os.ReadFile(filepath.Join(slot, "level.dat"))
	if err != nil || string(got) != "world" {
		t.Fatalf("slot level.dat = %q (err %v), want %q: the sweep took a recovery copy the "+
			"hydrate parked after the identity pin passed (issue #3118)", got, err, "world")
	}
	// The hydrated tree is in place and nothing is left under a .sweeping-<id>-* name:
	// the sweep put back what it took rather than stranding it for the boot reclaim.
	assertScratchRoot(t, m, ".displaced-s1", "s1")
}

// assertScratchRoot fails unless the scratch root holds exactly want, in order. os.ReadDir
// sorts by name, so the wanted names are listed sorted.
func assertScratchRoot(t *testing.T, m *Manager, want ...string) {
	t.Helper()
	entries, err := os.ReadDir(m.scratchDir)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, e := range entries {
		names = append(names, e.Name())
	}
	if len(names) != len(want) {
		t.Fatalf("scratch root = %v, want %v", names, want)
	}
	for i := range want {
		if names[i] != want[i] {
			t.Fatalf("scratch root = %v, want %v", names, want)
		}
	}
}

// A tree the sweep cannot put back stays WHOLE under its .sweeping-<id>-* name for the
// boot reclaim (issue #3118). With the working dir replaced mid-sweep the tree may be a
// recovery copy the snapshot never published, and a slot that has filled again holds
// another one: renaming over that would spend a copy, and removing the tree would be a
// delete on a guess. The drop is deferred to ReclaimInterruptedDisplacedSweeps instead,
// which is no worse than the unconditional removal this replaced.
func TestDisplacedSweepLeavesATreeItCannotPutBack(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	slot := filepath.Join(m.scratchDir, ".displaced-s1")
	seedHydrateShapedTree(t, slot, 7)

	removed := false
	restoreRename, restoreRemove := renameSweptTree, removeDisplacedTree
	renameSweptTree = func(from, to string) error {
		if err := restoreRename(from, to); err != nil {
			return err
		}
		// A hydrate parks the working set it displaces in the now-empty slot.
		seedHydrateShapedTree(t, slot, 9)
		return nil
	}
	removeDisplacedTree = func(string) error { removed = true; return nil }
	t.Cleanup(func() { renameSweptTree, removeDisplacedTree = restoreRename, restoreRemove })

	m.sweepDisplaced("s1", func() (bool, string) { return false, "working_dir_replaced" })

	if removed {
		t.Fatal("the sweep removed the tree it had taken from the slot although the working dir " +
			"was replaced meanwhile: the success no longer proves the store supersedes it (issue #3118)")
	}
	if got := readGeneration(slot); got != 9 {
		t.Fatalf(".displaced-s1 generation = %d, want 9: the put-back renamed over the copy that "+
			"filled the slot instead of leaving the swept tree aside (issue #3118)", got)
	}
	left, err := filepath.Glob(filepath.Join(m.scratchDir, sweepingPrefix+"s1-*"))
	if err != nil || len(left) != 1 {
		t.Fatalf("renamed trees = %v (err %v), want exactly one for the boot reclaim to take", left, err)
	}
	if got, err := os.ReadFile(filepath.Join(left[0], "world", "level.dat")); err != nil || string(got) != "x" {
		t.Fatalf("%s holds level.dat %q (err %v), want the whole tree: %q",
			filepath.Base(left[0]), got, err, "x")
	}
}

// The put-back is fsynced too (issue #3118): it undoes a rename the sweep was about to
// make durable (issue #2799), and a power loss that rolled the put-back back would strand
// the tree under a .sweeping-<id>-* name the next boot reclaims — turning a recovery copy
// into garbage. No test can stage the power loss, so the sync and removal seams record
// the order instead: one sync, with the tree already back in the slot, and no traversal.
func TestDisplacedSweepSyncsTheTreeItPutsBack(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	slot := filepath.Join(m.scratchDir, ".displaced-s1")
	seedHydrateShapedTree(t, slot, 7)

	var syncedWithSlotBack []bool
	removed := false
	restoreSync, restoreRemove := syncSweepScratchRoot, removeDisplacedTree
	syncSweepScratchRoot = func(dir string) error {
		if dir != m.scratchDir {
			t.Errorf("synced %q, want the scratch root %q", dir, m.scratchDir)
		}
		_, err := os.Lstat(slot)
		syncedWithSlotBack = append(syncedWithSlotBack, err == nil)
		return nil
	}
	removeDisplacedTree = func(string) error { removed = true; return nil }
	t.Cleanup(func() { syncSweepScratchRoot, removeDisplacedTree = restoreSync, restoreRemove })

	m.sweepDisplaced("s1", func() (bool, string) { return false, "working_dir_absent" })

	if removed {
		t.Fatal("the sweep traversed a tree it had decided to put back (issue #3118)")
	}
	if len(syncedWithSlotBack) != 1 || !syncedWithSlotBack[0] {
		t.Fatalf("scratch-root syncs (slot back in place at each) = %v, want exactly one with the "+
			"tree already restored: an un-fsynced put-back can roll back into a .sweeping- name "+
			"the next boot reclaims (issue #3118)", syncedWithSlotBack)
	}
	assertScratchRoot(t, m, ".displaced-s1")
}

// interruptDisplacedSweep runs sweepDisplaced for serverID over a seeded displaced tree
// holding a full working set, with the removal stopped before it starts — the state a
// crash between the sweep's rename and its traversal leaves — and returns the path the
// tree was left under. The path is found by what appeared in the scratch root rather
// than built from the prefix, so the tests below follow what the sweep really leaves.
func interruptDisplacedSweep(t *testing.T, m *Manager, serverID string) string {
	t.Helper()
	seedHydrateShapedTree(t, filepath.Join(m.scratchDir, ".displaced-"+serverID), 7)
	before := map[string]bool{}
	entries, err := os.ReadDir(m.scratchDir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		before[e.Name()] = true
	}

	restore := removeDisplacedTree
	removeDisplacedTree = func(string) error { return nil }
	m.sweepDisplaced(serverID, nil)
	removeDisplacedTree = restore

	entries, err = os.ReadDir(m.scratchDir)
	if err != nil {
		t.Fatal(err)
	}
	var left []string
	for _, e := range entries {
		if !before[e.Name()] {
			left = append(left, e.Name())
		}
	}
	if len(left) != 1 {
		t.Fatalf("an interrupted displaced sweep left %v in the scratch root, want exactly one renamed tree", left)
	}
	return filepath.Join(m.scratchDir, left[0])
}

// The tree an interrupted sweep leaves is a full working set with a generation marker,
// under a name that is not a server id: the held-set scans must skip it exactly as they
// skip the .displaced- and .hydrate- siblings (issue #2799), or the Worker advertises a
// server id the API never assigned and region-fscks a world-sized tree at every boot
// until it is reclaimed.
func TestInterruptedDisplacedSweepIsNotAdvertisedAsHeld(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	seedHydrateShapedTree(t, filepath.Join(m.scratchDir, "s1"), 7)
	leftover := interruptDisplacedSweep(t, m, "s1")

	for _, scan := range []struct {
		name string
		got  []session.HeldServer
	}{
		{"ScanHeldServers", ScanHeldServers(m.scratchDir, nil)},
		{"HeldServers", m.HeldServers()},
	} {
		if len(scan.got) != 1 || scan.got[0].ServerID != "s1" {
			t.Fatalf("%s = %v, want only s1: the tree an interrupted displaced sweep left at %s "+
				"must never be enumerated as a held server (issue #2799)",
				scan.name, scan.got, filepath.Base(leftover))
		}
	}
}

// The tree an interrupted sweep leaves is reclaimed at the next Worker boot (issue
// #2799), and by nothing else: a crash inside the stopped-id GC (removeScratch) leaves it
// with the scratch dir already gone, so the id is never advertised as held again and the
// deleted-server reclaim is never offered it. Deleting it at boot is unconditional
// because nothing sweeps at boot and the sweep had already decided the tree was garbage.
// Everything else in the scratch root — a live scratch, another server's recovery tree,
// a crashed hydrate's leftover — is not this reclaim's to touch.
func TestBootReclaimsInterruptedDisplacedSweeps(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	leftover := interruptDisplacedSweep(t, m, "s1")
	hydrateLeftover := filepath.Join(m.scratchDir, ".hydrate-s1-123456")
	if err := os.MkdirAll(hydrateLeftover, 0o750); err != nil {
		t.Fatal(err)
	}
	keep := []string{seedScratch(t, m, "s1"), seedDisplaced(t, m, "s2"), hydrateLeftover}

	ReclaimInterruptedDisplacedSweeps(m.scratchDir)

	if _, err := os.Stat(leftover); !os.IsNotExist(err) {
		t.Fatalf("interrupted displaced sweep %s survived the boot reclaim (stat err = %v): "+
			"nothing else ever reclaims it (issue #2799)", filepath.Base(leftover), err)
	}
	for _, p := range keep {
		if _, err := os.Stat(p); err != nil {
			t.Fatalf("boot reclaim removed %s, which is not an interrupted displaced sweep: %v", filepath.Base(p), err)
		}
	}
}

// The sweep fsyncs the scratch root AFTER its rename and BEFORE its traversal (issue
// #2799). Without that barrier a power loss can persist the traversal's unlinks yet roll
// back the un-fsynced rename, putting a half-deleted tree back in the .displaced-<id>
// slot, where a later hydrate's oldest-wins check retains it over the live set. No test
// can stage the power loss, so the sync and removal seams record the order instead.
func TestDisplacedSweepSyncsTheRenameBeforeRemoving(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	slot := filepath.Join(m.scratchDir, ".displaced-s1")
	seedHydrateShapedTree(t, slot, 7)

	var calls []string
	restoreSync, restoreRemove := syncSweepScratchRoot, removeDisplacedTree
	syncSweepScratchRoot = func(dir string) error {
		calls = append(calls, "sync "+dir)
		if _, err := os.Lstat(slot); !os.IsNotExist(err) {
			t.Errorf("the scratch root was synced while the tree was still in the slot (lstat err = %v): "+
				"a sync before the rename makes nothing durable", err)
		}
		return nil
	}
	removeDisplacedTree = func(path string) error {
		calls = append(calls, "remove "+path)
		return nil
	}
	t.Cleanup(func() { syncSweepScratchRoot, removeDisplacedTree = restoreSync, restoreRemove })

	m.sweepDisplaced("s1", nil)

	if len(calls) != 2 || calls[0] != "sync "+m.scratchDir ||
		!strings.HasPrefix(calls[1], "remove "+filepath.Join(m.scratchDir, sweepingPrefix+"s1-")) {
		t.Fatalf("sweep calls = %q, want the scratch root synced and then the renamed tree removed: "+
			"a traversal the rename is not yet durable under can leave a half-deleted tree in the "+
			"slot after a power loss (issue #2799)", calls)
	}
}

// A failed sync stops the sweep before its traversal (issue #2799): removing a tree whose
// rename is not durable is exactly what a power loss can turn into a half-deleted tree
// back in the slot. The tree stays whole under its .sweeping-<id>-* name, off the slot,
// for the next boot's ReclaimInterruptedDisplacedSweeps to take.
func TestDisplacedSweepSyncFailureLeavesTheTreeWhole(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	slot := filepath.Join(m.scratchDir, ".displaced-s1")
	seedHydrateShapedTree(t, slot, 7)

	removed := false
	restoreSync, restoreRemove := syncSweepScratchRoot, removeDisplacedTree
	syncSweepScratchRoot = func(string) error { return errors.New("injected fsync failure") }
	removeDisplacedTree = func(path string) error {
		removed = true
		return restoreRemove(path)
	}
	t.Cleanup(func() { syncSweepScratchRoot, removeDisplacedTree = restoreSync, restoreRemove })

	m.sweepDisplaced("s1", nil)

	if removed {
		t.Fatal("the sweep removed the tree although the scratch root sync failed: its rename " +
			"may not be durable, so a power loss can put the half-deleted tree back in the slot (issue #2799)")
	}
	if _, err := os.Lstat(slot); !os.IsNotExist(err) {
		t.Fatalf("the .displaced-s1 slot survived the sweep's rename (lstat err = %v), want it empty", err)
	}
	left, err := filepath.Glob(filepath.Join(m.scratchDir, sweepingPrefix+"s1-*"))
	if err != nil || len(left) != 1 {
		t.Fatalf("renamed trees = %v (err %v), want exactly one for the boot reclaim to take", left, err)
	}
	got, err := os.ReadFile(filepath.Join(left[0], "world", "level.dat"))
	if err != nil || string(got) != "x" || readGeneration(left[0]) != 7 {
		t.Fatalf("%s holds level.dat %q (err %v) at generation %d, want the whole tree: %q at 7",
			filepath.Base(left[0]), got, err, readGeneration(left[0]), "x")
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
	m.sweepDisplaced("s2", nil)
	if _, err := os.Stat(displaced); err != nil {
		t.Fatalf("displaced tree for s1 removed by an s2 displaced sweep (wrong id): %v", err)
	}
}
