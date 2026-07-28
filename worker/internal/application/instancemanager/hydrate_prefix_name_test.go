package instancemanager

import (
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// The hydrate temp/aside name PREFIX is a cross-package contract (issue #2290),
// the same shape as the generation marker's filename contract (issue #2280).
// Two Worker packages must agree on it byte-for-byte, with nothing in the type
// system pinning them to each other:
//
//   - datatransfer.unpackAndSwap CREATES the per-hydrate temp tree and the
//     superseded-set aside under it, using its own local hydrateTmpPrefix in
//     worker/internal/adapters/datatransfer/datatransfer.go.
//   - the held-set scans here SKIP it, using hydratePrefix in scratchscan.go.
//
// So this test and its twin — TestHydrateTempTreesUseTheSharedHydratePrefix in
// worker/internal/adapters/datatransfer — each assert the SAME hardcoded
// literal from their own side. Renaming either constant then fails CI here
// instead of degrading the invariant silently.
//
// A crash between a hydrate's aside/unpack and its swap-in leaves such a tree in
// the scratch root as a FULL working set carrying a generation marker (the
// unpacked tree, or since issue #2278 the superseded live set). Without the skip
// the boot scan enumerates it as a held server whose id is the literal directory
// name, pays a region fsck over a world-sized tree that is about to be swept, and
// names a directory rather than a server id in exactly the incident logs someone
// is reading during a crashed hydrate.
func TestHeldScansSkipTheSharedHydrateTempPrefix(t *testing.T) {
	scratch := t.TempDir()

	// A genuine server working set: still reported, at its recorded generation.
	seedHydrateShapedTree(t, filepath.Join(scratch, "s1"), 7)

	// The names are hardcoded on purpose: building them from hydratePrefix would
	// make this test follow a rename rather than catch it. Both crash-left forms
	// are full working sets with a generation marker, indistinguishable from a
	// server dir except by name.
	seedHydrateShapedTree(t, filepath.Join(scratch, ".hydrate-s1-123456"), 7)
	seedHydrateShapedTree(t, filepath.Join(scratch, ".hydrate-s1-superseded-654321"), 6)

	want := []session.HeldServer{{ServerID: "s1", Generation: 7}}
	assertHeld(t, "ScanHeldServers", ScanHeldServers(scratch, nil), want)
	assertHeld(t, "HeldServers", New(nil, scratch, nil).HeldServers(), want)
}

// seedHydrateShapedTree creates a directory holding a real working set (world content
// plus a generation marker at gen), as both a live scratch dir and a crash-left
// hydrate tree carry.
func seedHydrateShapedTree(t *testing.T, dir string, gen uint64) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, "world"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "world", "level.dat"), []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := writeGeneration(dir, gen); err != nil {
		t.Fatal(err)
	}
}

func assertHeld(t *testing.T, scan string, got, want []session.HeldServer) {
	t.Helper()
	sort.Slice(got, func(i, j int) bool { return got[i].ServerID < got[j].ServerID })
	if len(got) != len(want) {
		t.Fatalf("%s = %v, want %v: a crash-left .hydrate-<id>-* tree must never be "+
			"enumerated as a held server (issue #2290)", scan, got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("%s = %v, want %v", scan, got, want)
		}
	}
}
