package instancemanager

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// Declare a hydrate generation only after its local marker lands; the API uses it to decide whether to skip
// future hydrates.

// TestSuccessfulHydrateDeclaresTheGenerationItServed is the positive direction: a hydrate pulls the store's
// working set at the served generation and stamps it into the marker, so it declares that generation to the API,
// which then need not read the store itself before dispatching, a read that can only understate.
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

// Force marker-write failure while hydrate succeeds; no generation may be declared without the on-disk stamp.
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
