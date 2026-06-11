package instancemanager

import (
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// TestScanHeldServersReportsNonEmptyDirsWithGeneration verifies the registration
// scan reports non-empty scratch subdirectories with the generation recorded in
// their marker file and SKIPS empty/absent ones and dirs holding only the marker
// (issue #763): an empty scratch holds no working set, so reporting it would let
// the API skip the hydrate and boot a fresh/empty world.
func TestScanHeldServersReportsNonEmptyDirsWithGeneration(t *testing.T) {
	scratch := t.TempDir()

	// A non-empty server dir with a generation marker -> reported with that gen.
	held := filepath.Join(scratch, "held-server")
	if err := os.MkdirAll(held, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(held, "level.dat"), []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := writeGeneration(held, 7); err != nil {
		t.Fatal(err)
	}

	// A non-empty server dir WITHOUT a marker (predates #763) -> reported at gen 0.
	heldNoMarker := filepath.Join(scratch, "held-nomarker")
	if err := os.MkdirAll(filepath.Join(heldNoMarker, "world"), 0o750); err != nil {
		t.Fatal(err)
	}

	// An empty server dir (scratch was wiped) -> skipped.
	if err := os.MkdirAll(filepath.Join(scratch, "empty-server"), 0o750); err != nil {
		t.Fatal(err)
	}

	// A server dir holding ONLY the generation marker -> skipped (no working set).
	markerOnly := filepath.Join(scratch, "marker-only")
	if err := os.MkdirAll(markerOnly, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := writeGeneration(markerOnly, 3); err != nil {
		t.Fatal(err)
	}

	// A stray regular file at the scratch root (e.g. a snapshot spool) -> skipped.
	if err := os.WriteFile(filepath.Join(scratch, "snapshot-1.tar"), []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}

	got := ScanHeldServers(scratch, nil)
	sort.Slice(got, func(i, j int) bool { return got[i].ServerID < got[j].ServerID })
	want := []session.HeldServer{
		{ServerID: "held-nomarker", Generation: 0},
		{ServerID: "held-server", Generation: 7},
	}
	if len(got) != len(want) {
		t.Fatalf("held = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("held = %v, want %v", got, want)
		}
	}
}

// TestScanHeldServersMissingScratchRoot verifies an absent scratch root yields an
// empty list (a fresh Worker holds nothing), not a panic or error.
func TestScanHeldServersMissingScratchRoot(t *testing.T) {
	got := ScanHeldServers(filepath.Join(t.TempDir(), "does-not-exist"), nil)
	if len(got) != 0 {
		t.Fatalf("held = %v, want empty", got)
	}
}

// TestScanHeldServersTornRegionForcesHydrate verifies a held set whose region file
// is structurally torn is advertised at generation 0 even though its marker records
// gen N (issue #834): a periodic running-id snapshot makes the gen-N marker durable
// while the live world is never fsynced, so a power loss can leave a durable gen-N
// marker next to a torn local world. Advertising gen N would let the #767 skip gate
// boot the torn world; advertising 0 forces a hydrate that recovers the consistent
// store copy.
func TestScanHeldServersTornRegionForcesHydrate(t *testing.T) {
	scratch := t.TempDir()

	// A held set whose region is torn (size not a 4096 multiple, the #703 shape) but
	// whose marker still records gen 9.
	torn := filepath.Join(scratch, "torn-server", "region")
	if err := os.MkdirAll(torn, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(torn, "r.0.0.mca"), healthyRegion()[:3*fsckSector-10], 0o640); err != nil {
		t.Fatal(err)
	}
	if err := writeGeneration(filepath.Join(scratch, "torn-server"), 9); err != nil {
		t.Fatal(err)
	}

	// A held set whose region is structurally sound, with marker gen 4: untouched.
	sound := filepath.Join(scratch, "sound-server", "region")
	if err := os.MkdirAll(sound, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sound, "r.0.0.mca"), healthyRegion(), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := writeGeneration(filepath.Join(scratch, "sound-server"), 4); err != nil {
		t.Fatal(err)
	}

	got := ScanHeldServers(scratch, nil)
	sort.Slice(got, func(i, j int) bool { return got[i].ServerID < got[j].ServerID })
	want := []session.HeldServer{
		{ServerID: "sound-server", Generation: 4},
		{ServerID: "torn-server", Generation: 0},
	}
	if len(got) != len(want) {
		t.Fatalf("held = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("held = %v, want %v", got, want)
		}
	}
}
