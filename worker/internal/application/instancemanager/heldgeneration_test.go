package instancemanager

import (
	"context"
	"os"
	"testing"
)

// The three tests below pin the Worker-declared held generation a SnapshotTrigger
// puts on its CommandResult (issue #2481). The API mirrors that declaration into
// its held-working-set inventory, which gates the destructive skip-hydrate (#763)
// and the reconciler's short held-start grace (#999) — so the declaration must be
// the same fact as the on-disk marker the Worker re-advertises at registration,
// never a prediction of it.
//
// What makes that structural rather than careful: handleSnapshot's declaration is
// assigned in exactly ONE place, from recordGenerationIfUnchanged's report that
// the marker write landed. The stopped-id branch never calls it (it calls
// removeScratch instead), so it cannot produce a declaration at all.

// TestRunningSnapshotDeclaresTheGenerationItStamped is the positive direction: a
// running-id snapshot keeps its scratch and stamps the freshly published
// generation into the marker, so it declares that generation to the API.
func TestRunningSnapshotDeclaresTheGenerationItStamped(t *testing.T) {
	tr := &fakeTransfer{gen: 12}
	ctrl := &fakeControl{reply: "ok"}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
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

// TestStoppedSnapshotDeclaresNoHeldGeneration is the world-loss case issue #2481
// exists for. The stopped-id branch publishes and then removeScratch DELETES the
// working set (#762/#841), so the Worker holds nothing afterwards. Declaring the
// published generation here would let the API record held == store, take the short
// grace, and start with skip_hydrate into a freshly MkdirAll'd empty directory —
// a #696-class world rollback.
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

// TestRunningSnapshotDeclaresNothingWhenTheStampWasSkipped is the test that pins
// the declaration to the MARKER WRITE rather than to the branch. A running-id
// snapshot holds no per-id reservation (#829 item 4), so a new stream can re-place
// and hydrate the server while this (old, dropped) stream is still uploading; the
// #2284 identity guard then SKIPS the stamp and the marker keeps the hydrate's
// older generation. The scratch dir is still "retained" in the crude sense — it is
// right there on disk — but it is NOT the tree this snapshot published, so
// declaring the published generation would be exactly the marker-newer-than-tree
// error #2284 closed, re-introduced over the wire instead of on disk.
func TestRunningSnapshotDeclaresNothingWhenTheStampWasSkipped(t *testing.T) {
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
