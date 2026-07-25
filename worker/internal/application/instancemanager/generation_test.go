package instancemanager

import (
	"context"
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
