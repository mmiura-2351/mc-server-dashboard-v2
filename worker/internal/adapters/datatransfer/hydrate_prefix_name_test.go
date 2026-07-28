package datatransfer

import (
	"path/filepath"
	"strings"
	"testing"
)

// The hydrate temp/aside name PREFIX is a cross-package contract (issue #2290),
// the same shape as the generation marker's filename contract (issue #2280).
// Two Worker packages must agree on it byte-for-byte, with nothing in the type
// system pinning them to each other:
//
//   - unpackAndSwap here CREATES the per-hydrate temp tree and the superseded-set
//     aside under the local hydrateTmpPrefix.
//   - instancemanager's held-set scans SKIP it, using their own hydratePrefix in
//     worker/internal/application/instancemanager/scratchscan.go.
//
// So this test and its twin — TestHeldScansSkipTheSharedHydrateTempPrefix in
// worker/internal/application/instancemanager — each assert the SAME hardcoded
// literal from their own side. Renaming either constant then fails CI here
// instead of degrading the invariant silently.
//
// If the two drift, a crash between the aside/unpack and the swap-in leaves a
// tree the scan no longer recognises: it is a full working set with a generation
// marker, so the boot scan enumerates it as a held server whose id is the literal
// directory name and region-fscks a world-sized tree that is about to be swept.
func TestHydrateTempTreesUseTheSharedHydratePrefix(t *testing.T) {
	// The prefix is hardcoded on purpose: deriving it from hydrateTmpPrefix would
	// make this test follow a rename rather than catch it.
	const shared = ".hydrate-"

	got := hydrateTmpPrefix(filepath.Join(t.TempDir(), "server"))
	if !strings.HasPrefix(got, shared) {
		t.Fatalf("hydrateTmpPrefix = %q, want the %q prefix instancemanager's held-set "+
			"scans skip (issue #2290)", got, shared)
	}
}
