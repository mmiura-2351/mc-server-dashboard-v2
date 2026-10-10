package instancemanager

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// seedScratch provides content and a generation-zero marker, matching hydrated scratch while keeping the
// generation unknown.
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

// Keep scratch through confirmed stop because the API's final snapshot runs afterwards.
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

// Drive stop then final snapshot and require the world to remain present when packing begins.
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

// Reclaim stopped scratch only after successful final publication.
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

// A duplicate snapshot after successful cleanup must refuse absent scratch before packing, with
// SERVER_NOT_FOUND.
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
	// The phrase is load-bearing: the API's final-snapshot path matches it (with the SERVER_NOT_FOUND code) to
	// downgrade this refusal from its data-loss ERROR to a benign-duplicate INFO, _WORKING_SET_ABSENT_MARKER in
	// api/src/mc_server_dashboard_api/servers/application/lifecycle.py. A reword here silently re-arms the false
	// alarm unless done together.
	if !strings.Contains(res.ErrorMessage, "working dir absent") {
		t.Fatalf("refusal message = %q, want the API-pinned phrase \"working dir absent\"", res.ErrorMessage)
	}
	if len(tr.snapshots) != 1 {
		t.Fatalf("the duplicate must not pack/upload the absent dir; snapshots = %v", tr.snapshots)
	}
}

// A FAILED stopped-id snapshot must RETAIN the scratch: the working set was not captured, so GC-ing it would
// lose the world exactly as the stop-time GC did. The retained scratch is reclaimed on a later retry or at
// startup.
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

// Reclaim this ID's hydrate leftovers with stopped scratch, including servers never placed here again.
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

// Gate final-snapshot cleanup at scratch removal; hydrate leftovers must already be gone for interruption-safe
// retry.
func TestStoppedIDGCSweepsHydrateLeftoversBeforeTheScratchDirGoes(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	dir := seedScratch(t, m, "s1")
	leftover := filepath.Join(m.scratchDir, ".hydrate-s1-stale")
	if err := os.MkdirAll(leftover, 0o750); err != nil {
		t.Fatal(err)
	}

	// The removal seam IS the instant after which no per-id pass is ever offered this id
	// again, so it is where the leftover has to be gone already. The reclaim path pins
	// the same ordering on the log record its own removal emits; this one emits none on
	// its success path, so the seam stands in for that record.
	removed := false
	restore := removeScratchTree
	removeScratchTree = func(path string) error {
		removed = true
		if _, err := os.Stat(leftover); !os.IsNotExist(err) {
			t.Errorf("the s1 hydrate leftover was still there when the scratch dir was removed "+
				"(stat err = %v): an interruption in that window strands a world-sized tree no "+
				"per-id pass can reach again (issue #3167)", err)
		}
		return restore(path)
	}
	t.Cleanup(func() { removeScratchTree = restore })

	m.removeScratch("s1")

	if !removed {
		t.Fatal("removeScratch did not remove the scratch dir through the seam this test " +
			"observes the order through; the ordering is no longer pinned")
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("scratch dir not reclaimed after the final snapshot: stat err = %v", err)
	}
	if _, err := os.Stat(leftover); !os.IsNotExist(err) {
		t.Fatalf("s1 hydrate leftover not reclaimed: stat err = %v", err)
	}
}

// sweepHydrateLeftovers removes only the.hydrate-<id>-* siblings for the given id, leaving the server's own
// scratch dir and unrelated entries untouched.
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

// Restart keeps assignment and scratch so relaunch uses the current local world.
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

// A failed-stop orphan may still be alive (the driver could not confirm termination): the lingering process can
// still write the working set, so a failed StopServer must RETAIN the scratch. GC only on a CONFIRMED stop.
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
	// The failure must be the ORPHAN one, not a short-circuit. A stop that never reached the driver refuses with
	// SERVER_NOT_FOUND before attemptStop runs, and the retention assertion below then holds vacuously, which is
	// exactly how this test passed for the wrong reason while the start was refused.
	if res.ErrorCode != session.CommandErrorInternal {
		t.Fatalf("first stop = %+v, want the unconfirmed-termination failure (INTERNAL); "+
			"a SERVER_NOT_FOUND here means no instance was registered and the orphan path never ran", res)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("scratch dir removed on a failed stop (the orphan may still be writing it): %v", err)
	}
}

// seedDisplaced creates a.displaced-<id> tree, as a prior hydrate would have left when it moved a
// retained-for-recovery scratch aside. Returns the path so a test can assert whether a snapshot reclaimed it.
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

// Successful stopped publication makes the displaced recovery tree redundant.
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

// A successful RUNNING-id snapshot also supersedes any displaced recovery tree (the store now holds the live
// world), so it GCs.displaced-<id> too. The live scratch dir itself is retained, the server still owns it.
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

// The sweep in the post-upload tail is gated on the same working-dir identity pin as the marker stamp beside it
// (closing the window item 3 named). With the working dir replaced mid-upload by a concurrent stream's hydrate,
// the tree at.displaced-<id> is that hydrate's recovery copy, the world as it stood before the swap, which this
// snapshot never published, so removing it would spend a copy this success does not supersede. The sweep is
// skipped instead, and the leaked tree is reclaimed by the next successful snapshot for the id (the
// GC-on-success contract). The publish itself still succeeds: only the GC is declined.
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

// Pause recursive removal after rename so hydrate sees an empty recovery slot, never a half-deleted tree.
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

// Park a recovery copy after the caller's pin check; the sweep must recheck after rename and put the copy back.
func TestDisplacedSweepKeepsARecoveryCopyParkedAfterThePinCheck(t *testing.T) {
	tr := &fakeTransfer{}
	ctrl := &fakeControl{reply: "ok"}
	h := &capturingSlogHandler{}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr).WithLogger(slog.New(h))
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
	assertSweepInfo(t, h, "put back the displaced tree", "retained", slot)
}

// assertSweepInfo fails unless the records hold an INFO whose message starts with prefix
// and names path under key: the two outcomes of the sweep's re-check decide what the next
// boot deletes, so STORAGE.md Section 4.6 documents both lines for the operator.
func assertSweepInfo(t *testing.T, h *capturingSlogHandler, prefix, key, path string) {
	t.Helper()
	for _, rec := range h.records {
		if rec.Level != slog.LevelInfo || !strings.HasPrefix(rec.Message, prefix) {
			continue
		}
		named := false
		rec.Attrs(func(a slog.Attr) bool {
			if a.Key == key && a.Value.String() == path {
				named = true
			}
			return true
		})
		if named {
			return
		}
	}
	t.Fatalf("no INFO %q naming %s=%s; records = %v", prefix, key, path, h.records)
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

// Overlap two sweeps and a hydrate to prove the slot claim prevents one put-back from stranding another recovery
// copy.
func TestOverlappingDisplacedSweepsKeepTheRecoveryCopy(t *testing.T) {
	for _, tc := range []struct {
		name       string
		unreadable bool
	}{
		{"the other sweep holds junk it can classify", false},
		{"the other sweep holds a tree it cannot read", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := newManager(t, &fakeDriver{}, nil)
			live := seedScratch(t, m, "s1")
			slot := filepath.Join(m.scratchDir, ".displaced-s1")
			if err := os.MkdirAll(slot, 0o750); err != nil {
				t.Fatal(err)
			}
			if err := writeGeneration(slot, 3); err != nil {
				t.Fatal(err)
			}
			replaced := func() (bool, string) { return false, "working_dir_replaced" }

			// aRenamed fires only if the second sweep gets into its own window; aDone fires
			// either way, so the first sweep waits on both and this never deadlocks on the
			// sweep that is supposed to decline.
			aRenamed, aDone, bDecided := make(chan struct{}), make(chan struct{}), make(chan struct{})
			var mu sync.Mutex
			renames := 0
			aStarted := false
			bTrash := ""
			restoreRename, restoreRead := renameSweptTree, readSweptTree
			renameSweptTree = func(from, to string) error {
				mu.Lock()
				renames++
				n := renames
				mu.Unlock()
				if err := restoreRename(from, to); err != nil {
					return err
				}
				if n > 1 {
					close(aRenamed)
					return nil
				}
				bTrash = to
				// B has emptied the slot. The hydrate finds it empty and parks its live set
				// there by the ordinary displace path, and sweep A starts on it.
				replaceWorkingDirLikeHydrate(t, live, 7)
				aStarted = true
				go func() {
					m.sweepDisplaced("s1", replaced)
					close(aDone)
				}()
				select {
				case <-aRenamed: // A reached its own window: the overlap this test is about
				case <-aDone: // A declined: there is no overlap to wait for
				}
				return nil
			}
			readSweptTree = func(path string) ([]os.DirEntry, error) {
				if path == bTrash {
					if tc.unreadable {
						return nil, errors.New("injected read failure")
					}
					return restoreRead(path)
				}
				// A's own classification lands after B has decided, so B's put-back is the one
				// that gets the empty slot — the ordering the loss needs.
				<-bDecided
				return restoreRead(path)
			}
			t.Cleanup(func() { renameSweptTree, readSweptTree = restoreRename, restoreRead })

			m.sweepDisplaced("s1", replaced) // sweep B, holding what was in the slot
			close(bDecided)
			if aStarted {
				// Skipped when the first sweep never reached its rename, so a sweep that
				// stops renaming fails the assertions below instead of parking here.
				<-aDone
			}

			// seedScratch wrote "world" into the live set and replaceWorkingDirLikeHydrate
			// renamed that very directory into the slot, so reading it back proves the
			// hydrate's recovery copy is what survived.
			got, err := os.ReadFile(filepath.Join(slot, "level.dat"))
			if err != nil || string(got) != "world" {
				t.Fatalf("slot level.dat = %q (err %v), want %q: the other sweep put the tree it was "+
					"holding back into the slot, so the sweep holding the hydrate's recovery copy had "+
					"to leave it under .sweeping- for the next boot to delete (issue #3118)",
					got, err, "world")
			}
			mu.Lock()
			n := renames
			mu.Unlock()
			if n != 1 {
				t.Fatalf("sweep renames = %d, want 1: the second sweep took this id's slot while "+
					"another sweep was still deciding what to do with it", n)
			}
			// The first sweep's tree is where the boot reclaim expects it, and it is the only
			// thing left there.
			left, err := filepath.Glob(filepath.Join(m.scratchDir, sweepingPrefix+"s1-*"))
			if err != nil || len(left) != 1 {
				t.Fatalf("renamed trees = %v (err %v), want exactly one", left, err)
			}
			if hasWorkingSet(left[0]) {
				t.Fatalf("%s holds a working set, want the world-less junk: the sweeps swapped which "+
					"tree was left for the boot reclaim", filepath.Base(left[0]))
			}
		})
	}
}

// Use the same fixtures as the hydrate slot tests so both layers classify recovery trees identically.
func TestSweptTreeClassifierMatchesTheHydrateSlotRule(t *testing.T) {
	populated := filepath.Join(t.TempDir(), "elsewhere")
	seedHydrateShapedTree(t, populated, 7)

	cases := []struct {
		name  string
		build func(t *testing.T, path string)
		want  bool
	}{
		{"working set", func(t *testing.T, path string) { seedHydrateShapedTree(t, path, 7) }, true},
		{"empty dir", func(t *testing.T, path string) {
			if err := os.MkdirAll(path, 0o750); err != nil {
				t.Fatal(err)
			}
		}, false},
		{"regular file", func(t *testing.T, path string) {
			if err := os.WriteFile(path, []byte("leftover"), 0o640); err != nil {
				t.Fatal(err)
			}
		}, false},
		{"marker only", func(t *testing.T, path string) {
			if err := os.MkdirAll(path, 0o750); err != nil {
				t.Fatal(err)
			}
			if err := writeGeneration(path, 7); err != nil {
				t.Fatal(err)
			}
		}, false},
		{"marker temp sibling only", func(t *testing.T, path string) {
			if err := os.MkdirAll(path, 0o750); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(path, generationFile+"-abc123"), []byte("7"), 0o640); err != nil {
				t.Fatal(err)
			}
		}, false},
		{"symlink to a populated dir", func(t *testing.T, path string) {
			if err := os.Symlink(populated, path); err != nil {
				t.Fatal(err)
			}
		}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), ".sweeping-s1-123")
			tc.build(t, path)

			holds, err := sweptTreeHoldsWorkingSet(path)
			if err != nil {
				t.Fatalf("classifying %s = error %v, want a decision", tc.name, err)
			}
			if holds != tc.want {
				t.Fatalf("%s holds a working set = %v, want %v: the sweep's rule has drifted from "+
					"the hydrate's slot rule (datatransfer.displacedSlotHoldsWorkingSet)", tc.name, holds, tc.want)
			}
		})
	}
}

// A symlink in the slot is junk by that shared rule, so the sweep must not put one back:
// it would occupy the slot against a concurrent sweep holding the real recovery tree,
// which then has nowhere to restore it and leaves it for the boot reclaim to delete.
func TestDisplacedSweepDoesNotPutBackASymlinkSlot(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	populated := filepath.Join(t.TempDir(), "elsewhere")
	seedHydrateShapedTree(t, populated, 7)
	slot := filepath.Join(m.scratchDir, ".displaced-s1")
	if err := os.Symlink(populated, slot); err != nil {
		t.Fatal(err)
	}

	m.sweepDisplaced("s1", func() (bool, string) { return false, "working_dir_replaced" })

	if _, err := os.Lstat(slot); !os.IsNotExist(err) {
		t.Fatalf(".displaced-s1 exists after the sweep (lstat err = %v), want the slot left empty: "+
			"the sweep read THROUGH the symlink, called it a working set and put it back (issue #3118)", err)
	}
	left, err := filepath.Glob(filepath.Join(m.scratchDir, sweepingPrefix+"s1-*"))
	if err != nil || len(left) != 1 {
		t.Fatalf("renamed entries = %v (err %v), want exactly one (the symlink)", left, err)
	}
	// Inject a swept-tree read error; retain the uncertain recovery copy rather than leave it for boot deletion.
	if info, lerr := os.Lstat(left[0]); lerr != nil || info.Mode()&os.ModeSymlink == 0 {
		t.Fatalf("%s mode = %v (err %v), want a symlink", filepath.Base(left[0]), info, lerr)
	}
	if got, rerr := os.ReadFile(filepath.Join(populated, "world", "level.dat")); rerr != nil || string(got) != "x" {
		t.Fatalf("symlink target level.dat = %q (err %v), want %q untouched", got, rerr, "x")
	}
}

// A tree the sweep cannot READ is kept, not dropped (review, round 2). The classification answers "is this copy
// worth keeping", so a transient EACCES/EMFILE/EIO must not silently become "world-less junk, leave it for the
// boot reclaim to delete", the same direction datatransfer takes on its side (TestUnreadableDisplacedSlotFails-
// HydrateWithoutDiscarding). Putting an unclassifiable tree back costs at worst an occupied slot; leaving it
// costs the only copy of the unpublished delta.
//
// The failure is injected through a seam rather than a chmod fixture: a mode-000 dir is readable by root, so a
// chmod-based test silently stops asserting anything as root.
func TestDisplacedSweepKeepsATreeItCannotClassify(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	slot := filepath.Join(m.scratchDir, ".displaced-s1")
	seedHydrateShapedTree(t, slot, 7)

	restore := readSweptTree
	readSweptTree = func(string) ([]os.DirEntry, error) { return nil, errors.New("injected read failure") }
	t.Cleanup(func() { readSweptTree = restore })

	m.sweepDisplaced("s1", func() (bool, string) { return false, "working_dir_replaced" })

	if got, err := os.ReadFile(filepath.Join(slot, "world", "level.dat")); err != nil || string(got) != "x" {
		t.Fatalf("slot world/level.dat = %q (err %v), want %q: a tree the sweep could not read was "+
			"classified as junk and left under .sweeping- for the next boot to delete (issue #3118)", got, err, "x")
	}
	assertScratchRoot(t, m, ".displaced-s1")
}

// If the slot has refilled, preserve the swept tree whole under its temporary name rather than overwrite another
// copy.
func TestDisplacedSweepLeavesATreeItCannotPutBack(t *testing.T) {
	h := &capturingSlogHandler{}
	m := newManager(t, &fakeDriver{}, nil).WithLogger(slog.New(h))
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
	// Without this line the tree under .sweeping- reads as an ordinary interrupted
	// sweep, which is exactly what it is not.
	assertSweepInfo(t, h, "left a swept displaced tree", "swept_to", left[0])
}

// The put-back is fsynced too: it undoes a rename the sweep was about to make durable, and a power loss that
// rolled the put-back back would strand the tree under a.sweeping-<id>-* name the next boot reclaims, turning a
// recovery copy into garbage. No test can stage the power loss, so the sync and removal seams record the order
// instead: one sync, with the tree already back in the slot, and no traversal.
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

// The tree an interrupted sweep leaves is a full working set with a generation marker, under a name that is not
// a server id: the held-set scans must skip it exactly as they skip the.displaced- and.hydrate- siblings, or the
// Worker advertises a server id the API never assigned and region-fscks a world-sized tree at every boot until
// it is reclaimed.
func TestInterruptedDisplacedSweepIsNotAdvertisedAsHeld(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	seedHydrateShapedTree(t, filepath.Join(m.scratchDir, "s1"), 7)
	leftover := interruptDisplacedSweep(t, m, "s1")

	for _, scan := range []struct {
		name string
		got  []session.HeldServer
	}{
		{"ScanHeldServers", ScanHeldServers(m.scratchDir, true, nil)},
		{"HeldServers", m.HeldServers()},
	} {
		if len(scan.got) != 1 || scan.got[0].ServerID != "s1" {
			t.Fatalf("%s = %v, want only s1: the tree an interrupted displaced sweep left at %s "+
				"must never be enumerated as a held server (issue #2799)",
				scan.name, scan.got, filepath.Base(leftover))
		}
	}
}

// Boot reclaim must remove interrupted sweep trees even when server scratch no longer advertises their ID.
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

// Boot cleanup must reclaim hydrate leftovers whose server scratch no longer exists to advertise per-ID cleanup.
func TestBootReclaimsHydrateLeftovers(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	// Both crash-left forms: the per-hydrate temp tree and the superseded live set. Each
	// is a FULL working set carrying a generation marker, indistinguishable from a server
	// dir except by name.
	leftovers := []string{
		filepath.Join(m.scratchDir, scratchformat.HydratePrefix+"s1-123456"),
		filepath.Join(m.scratchDir, scratchformat.HydratePrefix+"s1-superseded-654321"),
	}
	for _, dir := range leftovers {
		seedHydrateShapedTree(t, dir, 7)
	}
	keep := []string{seedScratch(t, m, "s1"), seedDisplaced(t, m, "s2"), interruptDisplacedSweep(t, m, "s3")}

	ReclaimHydrateLeftovers(m.scratchDir)

	for _, dir := range leftovers {
		if _, err := os.Stat(dir); !os.IsNotExist(err) {
			t.Fatalf("hydrate leftover %s survived the boot reclaim (stat err = %v): nothing "+
				"else ever reclaims one once the id's scratch dir is gone (issue #3167)",
				filepath.Base(dir), err)
		}
	}
	for _, p := range keep {
		if _, err := os.Stat(p); err != nil {
			t.Fatalf("boot reclaim removed %s, which is not a hydrate leftover: %v", filepath.Base(p), err)
		}
	}
}

// The sweep fsyncs the scratch root AFTER its rename and BEFORE its traversal. Without that barrier a power loss
// can persist the traversal's unlinks yet roll back the un-fsynced rename, putting a half-deleted tree back in
// the.displaced-<id> slot, where a later hydrate's oldest-wins check retains it over the live set. No test can
// stage the power loss, so the sync and removal seams record the order instead.
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

// A failed sync stops the sweep before its traversal: removing a tree whose rename is not durable is exactly
// what a power loss can turn into a half-deleted tree back in the slot. The tree stays whole under
// its.sweeping-<id>-* name, off the slot, for the next boot's ReclaimInterruptedDisplacedSweeps to take.
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

// A FAILED snapshot must RETAIN the displaced recovery tree: the store did not capture the world, so
// the.displaced-<id> copy is still the only one, GC-ing it would defeat the recovery insurance entirely.
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

// Exclude displaced copies from held-set scans and isolate per-ID sweeps so recovery siblings are never treated
// as live scratch.
func TestDisplacedTreeNotTreatedAsLiveScratch(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	displaced := seedDisplaced(t, m, "s1")

	// A held-server scan never reports the displaced tree at all: neither under the
	// assigned id "s1" nor under its on-disk ".displaced-s1" name.
	for _, h := range ScanHeldServers(m.scratchDir, true, nil) {
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
