package instancemanager

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// generationMarkerPath returns the path of the working-set generation marker for
// serverID under the manager's scratch root.
func generationMarkerPath(m *Manager, serverID string) string {
	return filepath.Join(m.scratchDir, serverID, generationFile)
}

// TestHydrateRecordsGeneration proves a HydrateTrigger records the store
// generation the API served in the working set's marker, so a later registration
// re-reports it (issue #763).
func TestHydrateRecordsGeneration(t *testing.T) {
	tr := &fakeTransfer{gen: 11}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	if res := m.Handle(context.Background(), hydrateCmd()); !res.Success {
		t.Fatalf("HydrateTrigger = %+v, want success", res)
	}
	if got := readGeneration(filepath.Join(m.scratchDir, "s1")); got != 11 {
		t.Fatalf("recorded generation = %d, want 11", got)
	}
}

// TestSnapshotRecordsNewGeneration proves a RUNNING-server SnapshotTrigger records
// the NEW store generation the publish produced, so the held generation advances to
// match the scratch it pushed (issue #763). The running case is the one that retains
// its scratch — a STOPPED-id snapshot is the post-stop final capture and GCs the
// scratch instead of recording a generation onto a dir it is about to delete (#841).
func TestSnapshotRecordsNewGeneration(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	seedScratch(t, m, "s1")

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}
	if got := readGeneration(filepath.Join(m.scratchDir, "s1")); got != 12 {
		t.Fatalf("recorded generation = %d, want 12", got)
	}
}

// replaceWorkingDirLikeHydrate replaces dir the way a concurrent stream's hydrate
// does, so a test's interleaving reproduces the real one. It MIRRORS
// datatransfer.unpackAndSwap: build a fresh tree under a temp sibling, write the
// marker into it before the swap (issue #917), rename the live dir aside to
// .displaced-<id>, then rename the temp tree in — see the
// os.Rename(destDir, asideAt) / swapRename(tmpDir, destDir) pair in
// internal/adapters/datatransfer/datatransfer.go.
//
// The rename is the whole point: it gives the path a DIFFERENT directory object. A
// hook that merely rewrote files inside dir would leave the identity unchanged and
// prove nothing about the guard. Keep this in step with unpackAndSwap.
func replaceWorkingDirLikeHydrate(t *testing.T, dir string, gen uint64) {
	t.Helper()
	parent, id := filepath.Dir(dir), filepath.Base(dir)
	tmp := filepath.Join(parent, ".hydrate-"+id+"-tmp")
	if err := os.MkdirAll(tmp, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(tmp, "level.dat"), []byte("hydrated"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := writeGeneration(tmp, gen); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(dir, filepath.Join(parent, ".displaced-"+id)); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(tmp, dir); err != nil {
		t.Fatal(err)
	}
}

// TestRunningSnapshotSkipsGenerationStampWhenWorkingDirReplaced is the regression
// test for issue #2284. A running-id snapshot takes no per-id reservation (issue
// #829 item 4), so an old dropped stream's post-upload tail can still be running
// after a NEW stream has re-placed the server here and hydrated it. Stamping the
// newly published generation onto that replaced tree would make the marker describe
// a world it does not hold — and because the marker would then be NEWER than the
// tree, the #767 skip-hydrate gate (skip_hydrate = held >= store, lifecycle.py)
// would skip the very hydrate that corrects it and boot the wrong generation,
// silently. The stamp must be skipped instead, leaving the hydrate's own generation
// in place so the API re-hydrates.
func TestRunningSnapshotSkipsGenerationStampWhenWorkingDirReplaced(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	dir := seedScratch(t, m, "s1")
	if err := writeGeneration(dir, 5); err != nil {
		t.Fatal(err)
	}
	tr.duringUpload = func(workingDir string) { replaceWorkingDirLikeHydrate(t, workingDir, 7) }

	res := m.Handle(context.Background(), snapshotCmd())

	// The publish itself succeeded and minted generation 12 server-side; a skipped
	// marker stamp must not turn a valid publish into a reported failure.
	if !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success (the publish succeeded; only the stamp is skipped)", res)
	}
	if got := readGeneration(dir); got != 7 {
		t.Fatalf("generation = %d, want 7: the stale snapshot stamped its own generation onto the tree "+
			"the concurrent hydrate swapped in, so the marker no longer describes the tree (issue #2284)", got)
	}
}

// TestWriteGenerationGuardedRefusesAndRemovesItsTemp pins the pre-rename guard itself
// (issue #2284): a guard that reports the working dir is no longer the pinned one must
// stop the marker from being published, report the distinct errWorkingDirReplaced so the
// caller can log a skip rather than a marker-write failure, and leave no temp behind.
// Without this the guard block can be deleted outright with the rest of the suite green,
// because every interleaving test trips the CALLER's earlier check and returns before
// writeGenerationGuarded is ever reached with a failing guard.
func TestWriteGenerationGuardedRefusesAndRemovesItsTemp(t *testing.T) {
	dir := t.TempDir()

	err := writeGenerationGuarded(dir, 9, func() bool { return false })

	if !errors.Is(err, errWorkingDirReplaced) {
		t.Fatalf("writeGenerationGuarded = %v, want errWorkingDirReplaced", err)
	}
	if _, statErr := os.Stat(filepath.Join(dir, generationFile)); !os.IsNotExist(statErr) {
		t.Fatalf("marker published despite a refusing guard: stat err = %v", statErr)
	}
	entries, readErr := os.ReadDir(dir)
	if readErr != nil {
		t.Fatal(readErr)
	}
	if len(entries) != 0 {
		var names []string
		for _, entry := range entries {
			names = append(names, entry.Name())
		}
		t.Fatalf("refused write left %v behind, want its temp removed", names)
	}
}

// TestWriteGenerationGuardedWritesWhenTheGuardHolds is the companion direction: a guard
// that reports the dir unchanged must not disturb the write at all. Together with the
// refusal test above it pins the guard as a decision point rather than a blanket veto.
func TestWriteGenerationGuardedWritesWhenTheGuardHolds(t *testing.T) {
	dir := t.TempDir()

	if err := writeGenerationGuarded(dir, 9, func() bool { return true }); err != nil {
		t.Fatal(err)
	}

	if got := readGeneration(dir); got != 9 {
		t.Fatalf("generation = %d, want 9", got)
	}
}

// TestRunningSnapshotSkipsGenerationStampWhenWorkingDirRemoved covers the other way
// the pinned directory stops being the tree that was packed: a new stream's final
// snapshot (removeScratch) or the deleted-scratch reclaim deletes it outright. The
// stamp must be skipped rather than let writeGeneration's MkdirAll RESURRECT the dir
// as a marker-only directory.
func TestRunningSnapshotSkipsGenerationStampWhenWorkingDirRemoved(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	dir := seedScratch(t, m, "s1")
	tr.duringUpload = func(workingDir string) {
		if err := os.RemoveAll(workingDir); err != nil {
			t.Fatal(err)
		}
	}

	res := m.Handle(context.Background(), snapshotCmd())

	if !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("working dir resurrected by the skipped stamp (stat err = %v): a marker-only dir "+
			"would advertise a generation for a world this Worker no longer holds", err)
	}
}

// TestRunningSnapshotSkipsStampWhenWorkingDirReplacedAfterTheCheck drives the ONE
// interleaving that the caller's pre-check cannot catch and the pre-rename guard must
// (issue #2284): the replacement lands AFTER recordGenerationIfUnchanged has checked and
// passed, but BEFORE writeGenerationGuarded creates the marker temp. The temp is then
// created inside the REPLACEMENT directory, so every later path resolves there
// consistently and the rename would publish generation 12 onto a tree this snapshot
// never packed. (Once the temp exists the window is closed by path semantics instead:
// the temp rides the pinned inode into .displaced-<id> and the rename fails ENOENT on
// its source. That is why this test has to strike before CreateTemp to be meaningful.)
//
// The interleaving is microseconds wide in production — it spans MkdirAll — so it is
// driven through statWorkingDirRef rather than raced for: the fake performs the swap
// after the pre-check has read the OLD identity but before it returns, then delegates.
//
// It also pins the classification: this must be logged as a SKIP with the structured
// reason, not as recordGeneration's marker-write error.
func TestRunningSnapshotSkipsStampWhenWorkingDirReplacedAfterTheCheck(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	ctrl := &fakeControl{reply: "ok"}
	h := &capturingSlogHandler{}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr).WithLogger(slog.New(h))
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	dir := seedScratch(t, m, "s1")
	if err := writeGeneration(dir, 5); err != nil {
		t.Fatal(err)
	}

	calls := 0
	restore := statWorkingDirRef
	statWorkingDirRef = func(name string) (os.FileInfo, error) {
		calls++
		info, err := restore(name)
		if calls == 1 {
			// The pre-check has already sampled the pinned identity; swap the tree in
			// underneath it and hand back the pre-swap answer, so the check passes and
			// the write proceeds into a directory that is no longer the pinned one.
			replaceWorkingDirLikeHydrate(t, dir, 7)
		}
		return info, err
	}
	t.Cleanup(func() { statWorkingDirRef = restore })

	res := m.Handle(context.Background(), snapshotCmd())

	if !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}
	if calls < 2 {
		t.Fatalf("identity was compared %d time(s), want the pre-check AND the pre-rename guard: "+
			"the guarded write was never reached, so this test proves nothing", calls)
	}
	if got := readGeneration(dir); got != 7 {
		t.Fatalf("generation = %d, want 7: the marker temp was created inside the tree the "+
			"concurrent hydrate swapped in, and the pre-rename guard did not stop it from being "+
			"published there (issue #2284)", got)
	}
	// The refused write must not strand its temp — here the temp IS reachable (it was
	// created in the replacement dir), so the cleanup is observable.
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), generationFile+"-") {
			t.Fatalf("refused write stranded its temp %q in the working dir", entry.Name())
		}
	}
	assertSkippedStampWarn(t, h, "working_dir_replaced")
}

// assertSkippedStampWarn fails unless the records hold a skipped-stamp WARN carrying
// reason, and no marker-write ERROR: a refused stamp must be classified as a skip
// (errWorkingDirReplaced) rather than surfacing as writeGeneration's failure log.
func assertSkippedStampWarn(t *testing.T, h *capturingSlogHandler, reason string) {
	t.Helper()
	found := false
	for _, rec := range h.records {
		if strings.HasPrefix(rec.Message, "could not record working-set generation") {
			t.Fatalf("refused stamp logged as a marker-write error: %q", rec.Message)
		}
		if !strings.HasPrefix(rec.Message, "skipped recording working-set generation") {
			continue
		}
		if rec.Level != slog.LevelWarn {
			t.Fatalf("skipped-stamp log level = %v, want Warn", rec.Level)
		}
		rec.Attrs(func(a slog.Attr) bool {
			if a.Key == "reason" && a.Value.String() == reason {
				found = true
			}
			return true
		})
	}
	if !found {
		t.Fatalf("no skipped-stamp WARN with reason=%q; records = %v", reason, h.records)
	}
}

// TestPinWorkingDirReportsAnAbsentDirAsAbsent pins the capture-time classification: a
// pin taken on a directory that is not there must say so, not blame an fd/permission
// problem. The direction is the same either way (skip the stamp), but the reason is what
// an operator acts on — a deleted scratch and an exhausted fd table are different
// problems, so the three reasons have to stay disjoint.
func TestPinWorkingDirReportsAnAbsentDirAsAbsent(t *testing.T) {
	ref := pinWorkingDir(filepath.Join(t.TempDir(), "never-created"))
	defer ref.close()

	if ok, reason := ref.current(); ok || reason != "working_dir_absent" {
		t.Fatalf("current() = (%v, %q), want (false, \"working_dir_absent\")", ok, reason)
	}
}

// TestRunningSnapshotStampsWhenWorkingDirUnchanged pins the other direction: the
// guard must not degenerate into "never stamp". It models the 204 hydrate path,
// which returns WITHOUT touching destDir (datatransfer.Hydrate) — the directory the
// snapshot packed is still the directory on disk, so the stamp is correct and must
// happen. TestSnapshotRecordsNewGeneration covers the no-interleaving case.
func TestRunningSnapshotStampsWhenWorkingDirUnchanged(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	dir := seedScratch(t, m, "s1")
	tr.duringUpload = func(workingDir string) {
		// A concurrent 204 hydrate records its generation into the SAME directory.
		if err := writeGeneration(workingDir, 0); err != nil {
			t.Fatal(err)
		}
	}

	res := m.Handle(context.Background(), snapshotCmd())

	if !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}
	if got := readGeneration(dir); got != 12 {
		t.Fatalf("generation = %d, want 12: the working dir was never replaced, so the guard must "+
			"still stamp the published generation", got)
	}
}

// TestRunningSnapshotSkipsStampWhenIdentityUnavailable pins the failure DIRECTION of
// the detector: when the identity cannot be captured at all (EMFILE, EACCES), the
// stamp is skipped rather than written blind. A false skip costs one extra hydrate; a
// false stamp is the silent wrong-generation boot, so every uncertainty must resolve
// this way.
func TestRunningSnapshotSkipsStampWhenIdentityUnavailable(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	dir := seedScratch(t, m, "s1")
	if err := writeGeneration(dir, 5); err != nil {
		t.Fatal(err)
	}
	restore := openWorkingDirRef
	openWorkingDirRef = func(string) (*os.File, error) { return nil, errors.New("test: cannot open working dir") }
	t.Cleanup(func() { openWorkingDirRef = restore })

	res := m.Handle(context.Background(), snapshotCmd())

	if !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}
	if got := readGeneration(dir); got != 5 {
		t.Fatalf("generation = %d, want 5: an unavailable identity must skip the stamp, not stamp blind", got)
	}
}

// TestHydrateStampIsUnconditional proves the hydrate's own stamp was not gated along
// with the snapshot's. handleHydrate holds a per-id reservation across the whole
// transfer AND is the writer that produced the tree, so its marker is always correct.
// Gating it would be a correctness regression: a permanently missing marker reads as
// generation 0, and the API could then never skip a hydrate. The fake Hydrate never
// creates the working dir, so only the stamp's own MkdirAll can produce it.
func TestHydrateStampIsUnconditional(t *testing.T) {
	tr := &fakeTransfer{gen: 11}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	dir := filepath.Join(m.scratchDir, "s1")
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("working dir exists before the hydrate (stat err = %v), want absent", err)
	}

	if res := m.Handle(context.Background(), hydrateCmd()); !res.Success {
		t.Fatalf("HydrateTrigger = %+v, want success", res)
	}

	if got := readGeneration(dir); got != 11 {
		t.Fatalf("generation = %d, want 11: the hydrate stamp must stay unconditional", got)
	}
}

// TestWorkingDirRefSurvivesUnlinkAndRejectsReplacement pins the two mechanics the
// guard rests on, so a later "simplify" cannot quietly remove either.
//
//  1. os.SameFile against the captured identity REJECTS a different directory object
//     at the same path — the shape a hydrate's swap produces.
//  2. The *os.File is held OPEN for the whole window, which is what makes inode ABA
//     impossible. With a bare Stat-at-capture / Stat-at-compare token, hydrate #1
//     could free the inode and hydrate #2's os.MkdirTemp be handed it straight back;
//     the token would then MATCH and the stale snapshot would stamp anyway — the
//     detector failing in the UNSAFE direction on exactly the double-hydrate case
//     handleSnapshot documents as reachable. Holding the fd defers the inode's
//     reclamation until close, so it can never be recycled inside the window.
func TestWorkingDirRefSurvivesUnlinkAndRejectsReplacement(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "s1")
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}

	ref := pinWorkingDir(dir)
	defer ref.close()
	if ok, reason := ref.current(); !ok {
		t.Fatalf("current() = (false, %q) on the pinned dir itself, want (true, \"\")", reason)
	}

	// The hydrate shape: the pinned dir is renamed aside and a different directory
	// object takes its place.
	aside := filepath.Join(root, ".displaced-s1")
	if err := os.Rename(dir, aside); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	if ok, reason := ref.current(); ok || reason != "working_dir_replaced" {
		t.Fatalf("current() = (%v, %q), want (false, \"working_dir_replaced\")", ok, reason)
	}

	// The pinned inode outlives the unlink: fstat through the held descriptor still
	// succeeds after the original directory is gone, so the inode cannot be handed to
	// a new directory while this ref is alive. Two Stat calls would not have this.
	if err := os.RemoveAll(aside); err != nil {
		t.Fatal(err)
	}
	if _, err := ref.file.Stat(); err != nil {
		t.Fatalf("fstat through the held descriptor = %v after the pinned dir was unlinked: the "+
			"inode is only SAMPLED, not pinned, which reopens the ABA hole", err)
	}
}

// TestSnapshotDeclaresHeldGenerationAsBase proves a SnapshotTrigger declares the
// store generation the working set was hydrated from (the held marker) as the
// publish's base generation, so the API's publish-time generation guard can refuse
// a stale publish (issue #847).
func TestSnapshotDeclaresHeldGenerationAsBase(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	dir := seedScratch(t, m, "s1")
	if err := writeGeneration(dir, 5); err != nil {
		t.Fatal(err)
	}

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}
	if len(tr.snapshotBaseGenerations) != 1 || tr.snapshotBaseGenerations[0] != 5 {
		t.Fatalf("declared base generations = %v, want [5]", tr.snapshotBaseGenerations)
	}
}

// TestSnapshotDeclaresWorkerIDAsPublisher proves a SnapshotTrigger declares this
// Worker's own id as the publisher, so the API's publish-time generation guard can
// tell a same-Worker re-publish (lost-response self-heal) from a different-Worker
// stale publish (issue #847 bug 3).
func TestSnapshotDeclaresWorkerIDAsPublisher(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr).WithWorkerID("worker-xyz")
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	seedScratch(t, m, "s1")

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}
	if len(tr.snapshotWorkerIDs) != 1 || tr.snapshotWorkerIDs[0] != "worker-xyz" {
		t.Fatalf("declared worker ids = %v, want [worker-xyz]", tr.snapshotWorkerIDs)
	}
}

// TestGenerationMarkerRemovedAfterFinalSnapshot proves the generation marker
// follows the scratch lifecycle: the post-stop final snapshot GCs the scratch
// (issue #762/#841), which drops the marker with it, so a reclaimed server reports
// holding nothing and the API hydrates afresh (issue #763). The stop itself now
// RETAINS the marker so the final snapshot can still pack the working set (#841).
func TestGenerationMarkerRemovedAfterFinalSnapshot(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	dir := seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())
	if err := writeGeneration(dir, 7); err != nil {
		t.Fatal(err)
	}

	if res := m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: "s1", Kind: "StopServer"}); !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	if _, err := os.Stat(generationMarkerPath(m, "s1")); err != nil {
		t.Fatalf("generation marker dropped by the stop itself (final snapshot would pack empty, #841): %v", err)
	}
	// Post-stop final snapshot publishes, then the scratch (and its marker) is GC'd.
	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("final snapshot = %+v, want success", res)
	}
	if _, err := os.Stat(generationMarkerPath(m, "s1")); !os.IsNotExist(err) {
		t.Fatalf("generation marker survived the post-stop final snapshot GC: stat err = %v", err)
	}
	if held := ScanHeldServers(m.scratchDir, nil); len(held) != 0 {
		t.Fatalf("held = %v after final snapshot, want none", held)
	}
}

// TestWriteGenerationSweepsStaleTempSiblings proves a successful marker write
// reclaims the ".mcsd_generation-XXXX" temp siblings a crashed earlier write left
// behind (issue #2283): the leftovers are removed, the marker itself holds the new
// generation, and real working-set content is untouched.
func TestWriteGenerationSweepsStaleTempSiblings(t *testing.T) {
	dir := t.TempDir()
	leftover := filepath.Join(dir, ".mcsd_generation-123456")
	if err := os.WriteFile(leftover, []byte("3"), 0o600); err != nil {
		t.Fatal(err)
	}
	content := filepath.Join(dir, "server.properties")
	if err := os.WriteFile(content, []byte("level-name=world"), 0o600); err != nil {
		t.Fatal(err)
	}

	if err := writeGeneration(dir, 9); err != nil {
		t.Fatal(err)
	}

	if _, err := os.Stat(leftover); !os.IsNotExist(err) {
		t.Fatalf("stale temp sibling survived the marker write: stat err = %v", err)
	}
	if got := readGeneration(dir); got != 9 {
		t.Fatalf("generation = %d, want 9 (the sweep must not remove the marker itself)", got)
	}
	if data, err := os.ReadFile(content); err != nil || string(data) != "level-name=world" {
		t.Fatalf("working-set content = %q (err %v), want it untouched", data, err)
	}
}

// TestWriteGenerationKeepsTempFormDirectory proves the sweep only reclaims FILES in
// the temp form (issue #2283): a directory whose name happens to carry the temp
// prefix is working-set content the scan already ignores, and deleting it would
// silently destroy data.
func TestWriteGenerationKeepsTempFormDirectory(t *testing.T) {
	dir := t.TempDir()
	sub := filepath.Join(dir, ".mcsd_generation-dir")
	if err := os.MkdirAll(sub, 0o750); err != nil {
		t.Fatal(err)
	}

	if err := writeGeneration(dir, 4); err != nil {
		t.Fatal(err)
	}

	if info, err := os.Stat(sub); err != nil || !info.IsDir() {
		t.Fatalf("temp-form directory removed by the sweep: stat err = %v", err)
	}
}

// TestStrandedGenerationTempMatchesTheMarkerTempPredicates proves the temp name
// writeGeneration actually creates is the one every consumer's marker-temp predicate
// matches (issue #2287). The creation site and the three predicates are coupled only
// by the string they share, so a rename of generationFile that misses the creation
// site would strand temps no consumer recognises: hasWorkingSet (issue #2279) and the
// snapshot pack (issue #834) would read a leftover as working-set content — the Worker
// then advertises holding a world it does not hold — and sweepGenerationTemps (issue
// #2283) would stop reclaiming them.
//
// So the expectations here are DERIVED from generationFile rather than hardcoded: a
// rename that carries the creation site with it keeps this test green, while one that
// leaves the creation site behind turns it red. That is the opposite of the literal
// pin in generation_marker_name_test.go, which guards the marker's cross-package NAME;
// this guards the temp PATTERN against the constant it is built from.
//
// The temp is stranded through the real creation site: a directory sitting at the
// marker path makes the rename fail, leaving behind exactly what a crash between the
// temp write and the rename leaves behind.
func TestStrandedGenerationTempMatchesTheMarkerTempPredicates(t *testing.T) {
	dir := t.TempDir()
	// The directory makes the rename fail with EISDIR, and writeGeneration's
	// rename-error path returns WITHOUT unlinking its temp (unlike its write, sync and
	// close paths, which do) — that omission is what leaves the temp here to inspect.
	// Adding an inline unlink there would empty this dir and fail the test at
	// strandedGenerationTemp, far from anything to do with drift.
	if err := os.MkdirAll(filepath.Join(dir, generationFile), 0o750); err != nil {
		t.Fatal(err)
	}

	if err := writeGeneration(dir, 5); err == nil {
		t.Fatal("writeGeneration = nil, want the rename onto a directory to fail so its temp is stranded")
	}

	stranded := strandedGenerationTemp(t, dir)
	if !strings.HasPrefix(stranded, generationFile+"-") {
		t.Fatalf("stranded temp %q does not carry the %q prefix: the creation site no longer "+
			"derives its pattern from generationFile, so sweepGenerationTemps (issue #2283) "+
			"cannot reclaim it", stranded, generationFile+"-")
	}
	if hasWorkingSet(dir) {
		t.Fatalf("a dir holding only the stranded temp %q reads as a working set: the Worker "+
			"would advertise holding a world it does not hold (issue #2279)", stranded)
	}

	sweepGenerationTemps(dir)

	if _, err := os.Stat(filepath.Join(dir, stranded)); !os.IsNotExist(err) {
		t.Fatalf("stranded temp %q survived the sweep: stat err = %v", stranded, err)
	}
}

// strandedGenerationTemp returns the name of the single entry in dir that is not the
// marker path itself, failing the test when there is not exactly one.
func strandedGenerationTemp(t *testing.T, dir string) string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, entry := range entries {
		if entry.Name() != generationFile {
			names = append(names, entry.Name())
		}
	}
	if len(names) != 1 {
		t.Fatalf("entries besides the marker path = %v, want exactly the stranded temp", names)
	}
	return names[0]
}

// TestGenerationMarkerRetainedOnRestart proves a transient restart retains the
// generation marker (the same Worker keeps its live working set), so the held
// generation is re-reported on the next registration (issue #763).
func TestGenerationMarkerRetainedOnRestart(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	dir := seedScratch(t, m, "s1")
	_ = m.Handle(context.Background(), startCmd())
	if err := writeGeneration(dir, 7); err != nil {
		t.Fatal(err)
	}

	res := m.Handle(context.Background(), session.Command{CommandID: "restart", ServerID: "s1", Kind: "RestartServer"})
	if !res.Success {
		t.Fatalf("restart = %+v, want success", res)
	}
	if got := readGeneration(dir); got != 7 {
		t.Fatalf("generation after restart = %d, want 7 (marker must survive a transient restart)", got)
	}
}
