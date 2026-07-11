package instancemanager

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// capturingSlogHandler is a minimal slog.Handler that captures log records for
// test assertions, keeping tests off real I/O and making WarnOrphanDisplacedTrees
// observable without depending on slog.Default() output.
type capturingSlogHandler struct {
	records []slog.Record
}

func (h *capturingSlogHandler) Enabled(_ context.Context, _ slog.Level) bool { return true }
func (h *capturingSlogHandler) Handle(_ context.Context, r slog.Record) error {
	h.records = append(h.records, r)
	return nil
}
func (h *capturingSlogHandler) WithAttrs(_ []slog.Attr) slog.Handler { return h }
func (h *capturingSlogHandler) WithGroup(_ string) slog.Handler      { return h }

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

	// A held set whose region is genuinely torn but whose marker still records gen 9.
	// Under the unified rule (issue #927) an unaligned size is no longer corrupt per
	// se; this fixture stays refused because healthyRegion()'s chunk declares a
	// sector-filling length (4092) whose byte extent (offset 8192 + 4 + 4092 = 12288)
	// now overruns the truncated size (12278) -> truncated_chunk.
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

// TestScanHeldServersLiveFormatScratchAdvertisesHeldGeneration verifies the boot
// scan advertises the RECORDED generation for a structurally-sound live-format
// scratch — the unpadded (non-4096-aligned) tail a crashed or non-gracefully-stopped
// 26.x server leaves behind (issue #927/#926 item 1). Under the unified region rule
// such a scratch is not corrupt, so its marker generation N must be advertised (not
// gen 0). Advertising N lets the #767 skip gate (held >= published) boot the held
// world directly, preserving the crashed server's progression instead of forcing a
// gen-0 recovery hydrate that would roll it back by up to a snapshot interval.
func TestScanHeldServersLiveFormatScratchAdvertisesHeldGeneration(t *testing.T) {
	scratch := t.TempDir()

	// A held set whose region is live-format: unaligned size, byte-precise-valid
	// trailing chunk (offset*4096 + 4 + length == size). Marker records gen 11.
	live := filepath.Join(scratch, "live-server", "region")
	if err := os.MkdirAll(live, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(live, "r.0.0.mca"), unalignedLiveRegion(), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := writeGeneration(filepath.Join(scratch, "live-server"), 11); err != nil {
		t.Fatal(err)
	}

	got := ScanHeldServers(scratch, nil)
	want := []session.HeldServer{{ServerID: "live-server", Generation: 11}}
	if len(got) != len(want) || got[0] != want[0] {
		t.Fatalf("held = %v, want %v", got, want)
	}
}

// TestWarnOrphanDisplacedTreesLogsUnassigned verifies WarnOrphanDisplacedTrees
// emits a WARN for each .displaced-<id> tree whose server id is NOT in heldServers
// (issue #911): the server was deleted or re-placed elsewhere and the displaced tree
// is now an orphan the operator must handle manually.
func TestWarnOrphanDisplacedTreesLogsUnassigned(t *testing.T) {
	scratch := t.TempDir()

	// .displaced-s1: server s1 is NOT held -> must produce a WARN.
	if err := os.MkdirAll(filepath.Join(scratch, ".displaced-s1"), 0o750); err != nil {
		t.Fatal(err)
	}
	// .displaced-s2: server s2 IS held -> must NOT produce a WARN.
	if err := os.MkdirAll(filepath.Join(scratch, ".displaced-s2"), 0o750); err != nil {
		t.Fatal(err)
	}
	// A plain server dir: ignored (not a displaced prefix).
	if err := os.MkdirAll(filepath.Join(scratch, "s3"), 0o750); err != nil {
		t.Fatal(err)
	}

	held := []session.HeldServer{{ServerID: "s2", Generation: 1}}
	h := &capturingSlogHandler{}
	log := slog.New(h)
	WarnOrphanDisplacedTrees(scratch, held, log)

	// Exactly one WARN: for .displaced-s1 (the unassigned orphan).
	if len(h.records) != 1 {
		t.Fatalf("got %d log records, want 1; records = %v", len(h.records), h.records)
	}
	if h.records[0].Level != slog.LevelWarn {
		t.Fatalf("log level = %v, want Warn", h.records[0].Level)
	}
	// Verify the record carries the displaced path and server id.
	foundPath, foundID := false, false
	h.records[0].Attrs(func(a slog.Attr) bool {
		switch a.Key {
		case "path":
			if a.Value.String() == filepath.Join(scratch, ".displaced-s1") {
				foundPath = true
			}
		case "server_id":
			if a.Value.String() == "s1" {
				foundID = true
			}
		}
		return true
	})
	if !foundPath {
		t.Fatalf("log record missing path=%q", filepath.Join(scratch, ".displaced-s1"))
	}
	if !foundID {
		t.Fatalf("log record missing server_id=s1")
	}
}

// TestWarnOrphanDisplacedTreesSilentWhenAllAssigned verifies no WARNs are emitted
// when every displaced tree belongs to a currently-held server (issue #911): those
// trees will be GC'd by sweepDisplaced on the next successful snapshot.
func TestWarnOrphanDisplacedTreesSilentWhenAllAssigned(t *testing.T) {
	scratch := t.TempDir()

	if err := os.MkdirAll(filepath.Join(scratch, ".displaced-s1"), 0o750); err != nil {
		t.Fatal(err)
	}

	held := []session.HeldServer{{ServerID: "s1", Generation: 3}}
	h := &capturingSlogHandler{}
	WarnOrphanDisplacedTrees(scratch, held, slog.New(h))

	if len(h.records) != 0 {
		t.Fatalf("got %d log records, want 0 (s1 is held, no orphan warn needed)", len(h.records))
	}
}

// TestWarnOrphanDisplacedTreesNilLoggerIsSafe verifies that WarnOrphanDisplacedTrees
// does not panic when log is nil (issue #911): callers that don't care about log
// output (or tests that pass nil) must not crash.
func TestWarnOrphanDisplacedTreesNilLoggerIsSafe(t *testing.T) {
	scratch := t.TempDir()
	if err := os.MkdirAll(filepath.Join(scratch, ".displaced-s1"), 0o750); err != nil {
		t.Fatal(err)
	}
	// Must not panic.
	WarnOrphanDisplacedTrees(scratch, nil, nil)
}

// TestWarnOrphanDisplacedTreesMissingScratchRootIsSafe verifies an absent scratch
// root does not panic or error (issue #911): a first-boot Worker has no scratch dir.
func TestWarnOrphanDisplacedTreesMissingScratchRootIsSafe(t *testing.T) {
	h := &capturingSlogHandler{}
	WarnOrphanDisplacedTrees(filepath.Join(t.TempDir(), "does-not-exist"), nil, slog.New(h))
	if len(h.records) != 0 {
		t.Fatalf("got %d records from absent scratch dir, want 0", len(h.records))
	}
}

// TestManagerHeldServersReadsCurrentGenerations verifies the Manager.HeldServers
// method returns the current generation for each held working set (issue #1711).
// Unlike the boot-time ScanHeldServers, HeldServers skips the region fsck: a
// torn region in the scratch keeps its recorded generation because the Worker
// is still running and the fsck is only needed to detect post-crash corruption.
func TestManagerHeldServersReadsCurrentGenerations(t *testing.T) {
	scratch := t.TempDir()

	// A non-empty server dir with a generation marker.
	srv := filepath.Join(scratch, "srv-a")
	if err := os.MkdirAll(srv, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(srv, "level.dat"), []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := writeGeneration(srv, 3); err != nil {
		t.Fatal(err)
	}

	m := New(nil, scratch, nil)

	got := m.HeldServers()
	if len(got) != 1 {
		t.Fatalf("held = %v, want 1 entry", got)
	}
	if got[0].ServerID != "srv-a" || got[0].Generation != 3 {
		t.Fatalf("held = %+v, want {srv-a gen 3}", got[0])
	}

	// Advance the generation and verify HeldServers reflects the new value.
	if err := writeGeneration(srv, 10); err != nil {
		t.Fatal(err)
	}
	got = m.HeldServers()
	if len(got) != 1 || got[0].Generation != 10 {
		t.Fatalf("after advance: held = %v, want generation 10", got)
	}
}
