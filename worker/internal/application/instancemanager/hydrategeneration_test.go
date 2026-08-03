package instancemanager

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// The two tests below pin the Worker-declared held generation a HydrateTrigger puts
// on its CommandResult (issue #2500). They are the hydrate-path twin of the snapshot
// declaration in heldgeneration_test.go: the API mirrors the declaration into the
// held-working-set inventory its skip-hydrate gate (#763) and the reconciler's short
// held-start grace (#999) read, so the declaration must be the same fact as the
// on-disk marker the Worker re-advertises at registration, never a prediction of it.
//
// What makes that structural rather than careful: handleHydrate's declaration is
// assigned in exactly ONE place, from recordGeneration's report that the marker write
// landed, and it takes &gen — the same variable handed to the write. handleHydrate's
// case is simpler than the snapshot's: it holds a per-id reservation across the whole
// transfer AND is the writer that produced the tree, so recordGeneration is
// unconditional (no #2284 identity guard) and the only way to declare nothing is for
// the marker write itself to fail.

// TestSuccessfulHydrateDeclaresTheGenerationItServed is the positive direction: a
// hydrate pulls the store's working set at the served generation and stamps it into
// the marker, so it declares that generation to the API — which then need not read
// the store itself before dispatching (issue #2500), a read that can only understate.
func TestSuccessfulHydrateDeclaresTheGenerationItServed(t *testing.T) {
	tr := &fakeTransfer{gen: 9}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	res := m.Handle(context.Background(), hydrateCmd())

	if !res.Success {
		t.Fatalf("HydrateTrigger = %+v, want success", res)
	}
	if res.HeldGeneration == nil {
		t.Fatalf("successful hydrate declared no held generation; the scratch was just served " +
			"and stamped, so the API is left reading the store to guess it (issue #2500)")
	}
	if *res.HeldGeneration != 9 {
		t.Fatalf("declared held generation = %d, want 9 (the generation the hydrate served and "+
			"the stamp wrote)", *res.HeldGeneration)
	}
	// The declaration must agree with what a re-registration would advertise.
	if got := readGeneration(filepath.Join(m.scratchDir, "s1")); got != *res.HeldGeneration {
		t.Fatalf("declared %d but the on-disk marker reads %d: the declaration is not the marker",
			*res.HeldGeneration, got)
	}
}

// TestHydrateDeclaresNothingWhenTheMarkerWriteFailed pins the declaration to the
// marker WRITE rather than to the transfer's success. recordGeneration is best-effort:
// a marker it fails to write is one OLDER than the tree, costing an extra hydrate and
// nothing else, so the hydrate still succeeds. But the API must then fall back to its
// pre-dispatch read (issue #2500) rather than trust a declaration the marker does not
// back — otherwise a hydrate whose stamp was lost would let a later start skip the
// hydrate that would repair the tree. Absence of the marker is forced by seeding a
// regular file where the working dir would go, so writeGeneration's MkdirAll fails.
func TestHydrateDeclaresNothingWhenTheMarkerWriteFailed(t *testing.T) {
	tr := &fakeTransfer{gen: 9}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	// A regular file at the working-dir path makes writeGeneration's MkdirAll fail with
	// ENOTDIR, so the marker is never stamped. The fake transfer does not touch disk, so
	// this file survives the (no-op) hydrate.
	workingDir := filepath.Join(m.scratchDir, "s1")
	if err := os.WriteFile(workingDir, []byte("not a dir"), 0o600); err != nil {
		t.Fatal(err)
	}

	res := m.Handle(context.Background(), hydrateCmd())

	if !res.Success {
		t.Fatalf("HydrateTrigger = %+v, want success (the transfer succeeded; only the marker "+
			"write failed, which is best-effort)", res)
	}
	if res.HeldGeneration != nil {
		t.Fatalf("declared held generation %d while the marker write FAILED: the declaration is "+
			"computed alongside the transfer instead of from the write (issue #2500)",
			*res.HeldGeneration)
	}
}
