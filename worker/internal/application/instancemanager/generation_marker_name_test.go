package instancemanager

import (
	"os"
	"path/filepath"
	"testing"
)

// The generation marker's FILENAME is a cross-package contract (issue #2280).
// Two Worker packages must agree on it byte-for-byte, with nothing in the type
// system pinning them to each other:
//
//   - datatransfer.unpackAndSwap WRITES the marker into the hydrate temp tree
//     before the swap-in rename (issue #917), using its own local constant
//     generationMarkerFile in
//     worker/internal/adapters/datatransfer/datatransfer.go.
//   - readGeneration here READS it back, using generationFile in generation.go.
//
// So this test and its twin — TestHydrateWritesTheSharedGenerationMarkerName in
// worker/internal/adapters/datatransfer — each assert the SAME hardcoded
// literal from their own side. Renaming either constant then fails CI here
// instead of degrading an invariant silently.
//
// The invariant is durability, not bookkeeping. If the two names drift, a
// freshly hydrated dir carries the marker under one name, readGeneration finds
// nothing under the other and reports generation 0, the API treats the working
// set as unknown and re-dispatches a hydrate — and that hydrate's displace step
// spends the recovery copy. That is exactly the data-loss window issue #917
// closed, reopened by a rename.
func TestReadGenerationReadsTheSharedGenerationMarkerName(t *testing.T) {
	workingDir := t.TempDir()
	// The name is hardcoded on purpose: writing it via generationFile would make
	// this test follow a rename rather than catch it.
	if err := os.WriteFile(filepath.Join(workingDir, ".mcsd_generation"), []byte("42"), 0o640); err != nil {
		t.Fatalf("write marker: %v", err)
	}

	if gen := readGeneration(workingDir); gen != 42 {
		t.Fatalf("readGeneration = %d, want 42: the marker must stay named %q, "+
			"the name datatransfer.unpackAndSwap writes pre-swap (issues #917, #2280)",
			gen, ".mcsd_generation")
	}
}
