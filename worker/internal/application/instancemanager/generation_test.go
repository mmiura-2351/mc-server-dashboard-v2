package instancemanager

import (
	"context"
	"os"
	"path/filepath"
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

// TestSnapshotDeclaresHeldGenerationAsBase proves a SnapshotTrigger declares the
// store generation the working set was hydrated from (the held marker) as the
// publish's base generation, so the API's publish-time generation guard can refuse
// a stale publish (issue #847).
func TestSnapshotDeclaresHeldGenerationAsBase(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
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
	_ = m.Handle(context.Background(), startCmd())
	dir := seedScratch(t, m, "s1")
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
	if held := ScanHeldServers(m.scratchDir); len(held) != 0 {
		t.Fatalf("held = %v after final snapshot, want none", held)
	}
}

// TestGenerationMarkerRetainedOnRestart proves a transient restart retains the
// generation marker (the same Worker keeps its live working set), so the held
// generation is re-reported on the next registration (issue #763).
func TestGenerationMarkerRetainedOnRestart(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())
	dir := seedScratch(t, m, "s1")
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
