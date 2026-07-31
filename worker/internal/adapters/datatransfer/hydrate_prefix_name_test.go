package datatransfer

import (
	"os"
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
//   - both leftover SWEEPS RECLAIM it — sweepHydrateLeftovers here and
//     Manager.sweepHydrateLeftovers there (issue #2409).
//
// So this test, its sweep companion below, and their twins in
// worker/internal/application/instancemanager each assert the SAME hardcoded
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

// The leftover SWEEP side of the same contract (issue #2409). unpackAndSwap sweeps
// before it allocates new temp names, so a sweep matching a stale literal would let
// every crashed hydrate's tree accumulate in the scratch root across re-hydrates —
// and, since the constant it drifted from is the one the creation site uses, a
// coordinated rename that updates the creation site, the scan constant and the
// prefix tests above would not notice.
func TestHydrateLeftoverSweepMatchesTheSharedHydratePrefix(t *testing.T) {
	// Hardcoded on purpose, exactly as above: deriving these names from
	// hydrateTmpPrefix would make the test follow a rename rather than catch it.
	const shared = ".hydrate-"

	parent := t.TempDir()
	leftovers := []string{
		filepath.Join(parent, ".hydrate-server-123456"),
		filepath.Join(parent, ".hydrate-server-superseded-654321"),
	}
	for _, dir := range leftovers {
		if err := os.MkdirAll(dir, 0o750); err != nil {
			t.Fatal(err)
		}
	}

	sweepHydrateLeftovers(parent, "server")

	for _, dir := range leftovers {
		if _, err := os.Stat(dir); !os.IsNotExist(err) {
			t.Fatalf("sweepHydrateLeftovers left %q behind: stat err = %v; the sweep must "+
				"match the same %q prefix hydrateTmpPrefix creates these trees under "+
				"(issue #2409)", filepath.Base(dir), err, shared)
		}
	}
}
