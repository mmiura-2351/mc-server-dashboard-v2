package instancemanager

import (
	"context"
	"os"
	"testing"
)

// Snapshot declarations must match successfully published local markers; stopped scratch removal declares
// nothing.

// TestRunningSnapshotDeclaresTheGenerationItStamped is the positive direction: a
// running-id snapshot keeps its scratch and stamps the freshly published
// generation into the marker, so it declares that generation to the API.
func TestRunningSnapshotDeclaresTheGenerationItStamped(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	seedScratch(t, m, "s1")

	res := m.Handle(context.Background(), snapshotCmd())

	if !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}
	if res.HeldGeneration == nil {
		t.Fatalf("running-id snapshot declared no held generation; the scratch is retained and "+
			"stamped at %d, so the API is left hydrating unnecessarily (issue #2481)", tr.gen)
	}
	if *res.HeldGeneration != 12 {
		t.Fatalf("declared held generation = %d, want 12 (the generation the publish minted and "+
			"the stamp wrote)", *res.HeldGeneration)
	}
	// The declaration must agree with what a re-registration would advertise.
	if got := readGeneration(m.scratchDir + "/s1"); got != *res.HeldGeneration {
		t.Fatalf("declared %d but the on-disk marker reads %d: the declaration is not the marker",
			*res.HeldGeneration, got)
	}
}

// Stopped snapshots delete scratch after publication, so they must declare no held generation.
func TestStoppedSnapshotDeclaresNoHeldGeneration(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	dir := seedScratch(t, m, "s1") // stopped id: no running instance

	res := m.Handle(context.Background(), snapshotCmd())

	if !res.Success {
		t.Fatalf("stopped-id SnapshotTrigger = %+v, want success", res)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("scratch not GC'd, so this test is not exercising the deleting branch: stat err = %v", err)
	}
	if res.HeldGeneration != nil {
		t.Fatalf("stopped-id snapshot declared held generation %d for a scratch it just DELETED: "+
			"the API would skip the hydrate and boot an empty world (issue #2481)", *res.HeldGeneration)
	}
}

// A replacement tree is still held but was not packed by this snapshot; a skipped stamp must suppress its
// declaration.
func TestRunningSnapshotDeclaresNothingWhenTheStampWasSkipped(t *testing.T) {
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

	if !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success (the publish succeeded; only the stamp is skipped)", res)
	}
	if got := readGeneration(dir); got != 7 {
		t.Fatalf("marker = %d, want the concurrent hydrate's 7: the interleaving did not happen, "+
			"so this test proves nothing", got)
	}
	if res.HeldGeneration != nil {
		t.Fatalf("declared held generation %d while the stamp was SKIPPED and the marker reads 7: "+
			"the declaration is computed alongside the write instead of from it (issue #2481)",
			*res.HeldGeneration)
	}
}
