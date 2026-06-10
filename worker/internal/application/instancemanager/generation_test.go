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

// TestSnapshotRecordsNewGeneration proves a SnapshotTrigger records the NEW store
// generation the publish produced, so the held generation advances to match the
// scratch it pushed (issue #763).
func TestSnapshotRecordsNewGeneration(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	seedScratch(t, m, "s1")

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}
	if got := readGeneration(filepath.Join(m.scratchDir, "s1")); got != 12 {
		t.Fatalf("recorded generation = %d, want 12", got)
	}
}

// TestGenerationMarkerRemovedOnAuthoritativeStop proves the generation marker
// follows the scratch lifecycle: an authoritative stop GCs the scratch (issue
// #762), which drops the marker with it, so a GC'd server reports holding nothing
// and the API hydrates afresh (issue #763).
func TestGenerationMarkerRemovedOnAuthoritativeStop(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())
	dir := seedScratch(t, m, "s1")
	if err := writeGeneration(dir, 7); err != nil {
		t.Fatal(err)
	}

	res := m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: "s1", Kind: "StopServer"})
	if !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	if _, err := os.Stat(generationMarkerPath(m, "s1")); !os.IsNotExist(err) {
		t.Fatalf("generation marker survived an authoritative stop: stat err = %v", err)
	}
	if held := ScanHeldServers(m.scratchDir); len(held) != 0 {
		t.Fatalf("held = %v after authoritative stop, want none", held)
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
