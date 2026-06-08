package instancemanager

import (
	"os"
	"path/filepath"
	"sort"
	"testing"
)

// TestScanHeldServerIDsReportsNonEmptyDirs verifies the registration scan reports
// ids for non-empty scratch subdirectories and SKIPS empty or absent ones (issue
// #696): an empty scratch dir holds no working set, so reporting it would let the
// API skip the hydrate and boot a fresh/empty world.
func TestScanHeldServerIDsReportsNonEmptyDirs(t *testing.T) {
	scratch := t.TempDir()

	// A non-empty server dir (holds a live working set) -> reported.
	held := filepath.Join(scratch, "held-server")
	if err := os.MkdirAll(held, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(held, "level.dat"), []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}

	// A second non-empty server dir whose content is itself a subdirectory ->
	// still reported (non-empty = at least one entry).
	heldNested := filepath.Join(scratch, "held-nested")
	if err := os.MkdirAll(filepath.Join(heldNested, "world"), 0o750); err != nil {
		t.Fatal(err)
	}

	// An empty server dir (scratch was wiped) -> skipped.
	if err := os.MkdirAll(filepath.Join(scratch, "empty-server"), 0o750); err != nil {
		t.Fatal(err)
	}

	// A stray regular file at the scratch root (e.g. a snapshot spool) -> skipped.
	if err := os.WriteFile(filepath.Join(scratch, "snapshot-1.tar"), []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}

	got := ScanHeldServerIDs(scratch)
	sort.Strings(got)
	want := []string{"held-nested", "held-server"}
	if len(got) != len(want) {
		t.Fatalf("held = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("held = %v, want %v", got, want)
		}
	}
}

// TestScanHeldServerIDsMissingScratchRoot verifies an absent scratch root yields
// an empty list (a fresh Worker holds nothing), not a panic or error.
func TestScanHeldServerIDsMissingScratchRoot(t *testing.T) {
	got := ScanHeldServerIDs(filepath.Join(t.TempDir(), "does-not-exist"))
	if len(got) != 0 {
		t.Fatalf("held = %v, want empty", got)
	}
}
