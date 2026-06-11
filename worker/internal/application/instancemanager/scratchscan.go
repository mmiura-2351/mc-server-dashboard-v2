package instancemanager

import (
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/regionfsck"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// displacedPrefix is the dot-prefixed name prefix datatransfer uses for a displaced
// old working set kept aside for recovery (issue #906/#910). It is a sibling of the
// server-id scratch dirs and must be skipped by ScanHeldServers: it is not a held
// server, and scanning it would trigger a per-boot header fsck of a world-sized tree
// and emit a confusing server_id=.displaced-<id> corrupt warning. The constant is
// duplicated rather than imported to keep this application package off the adapter.
const displacedPrefix = ".displaced-"

// ScanHeldServers returns the working sets this Worker already holds in its
// persistent local scratch (issue #763): the immediate subdirectories of
// scratchDir that hold a NON-EMPTY working set, each tagged with the generation
// recorded in its marker file (issue #763, the store generation the set was last
// hydrated from or snapshotted to). The list is advertised on Register
// (held_servers) so the API skips the destructive hydrate on a same-worker restart
// ONLY when the held generation is fresh enough — a hydrate would unpack the last
// authoritative snapshot over the Worker's LIVE, newer working set and roll the
// world back, while a STALE held generation (e.g. an A->B->A leftover scratch) must
// still hydrate.
//
// A held set is structurally fsck'd before its generation is advertised (issue
// #834): a periodic running-id snapshot records the gen-N marker durably while the
// live world files are written by the Minecraft process and never fsynced by the
// Worker, so a power loss right after a snapshot can leave a durable gen-N marker
// next to a TORN local world. Advertising gen N would let the #767 skip gate boot
// that torn world even though the store holds a consistent copy. So a held set with
// a structurally corrupt region is advertised at generation 0 — held, but at an
// unknown generation the API treats as older than any published store generation,
// forcing the hydrate that recovers the consistent store copy. The fsck reads only
// the region headers (regionfsck), so it is bounded; it runs at most once per held
// set at registration. A fsck I/O error is best-effort (logged, the recorded
// generation stands): the API gate remains the correctness backstop, and the
// startup scan must not wedge on a read fault.
//
// A subdirectory whose only content is the generation marker is treated as EMPTY
// and SKIPPED: it holds no real working set, so the API must still hydrate (never
// silently boot a fresh/empty world). A missing or unreadable scratch root yields
// an empty list (the Worker reports holding nothing). The scan does not recurse and
// does not validate that a name is a server id — a non-server directory under
// scratch is harmless to report because the API only consults this for ids it has
// assigned to the Worker.
func ScanHeldServers(scratchDir string, log *slog.Logger) []session.HeldServer {
	entries, err := os.ReadDir(scratchDir)
	if err != nil {
		return nil
	}
	var held []session.HeldServer
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		// A .displaced-<id> sibling is a recovery copy a hydrate kept aside (issue
		// #906/#910), not a held server: skip it so it is never reported (and never
		// header-fsck'd per boot under a server_id=.displaced-<id> warning).
		if strings.HasPrefix(entry.Name(), displacedPrefix) {
			continue
		}
		workingDir := filepath.Join(scratchDir, entry.Name())
		if !hasWorkingSet(workingDir) {
			continue
		}
		held = append(held, session.HeldServer{
			ServerID:   entry.Name(),
			Generation: heldGeneration(workingDir, entry.Name(), log),
		})
	}
	return held
}

// heldGeneration returns the generation to advertise for a held working set: the
// recorded marker generation when the set is structurally sound, or 0 when a region
// fsck finds it torn (issue #834) — a 0 forces the API to hydrate, recovering the
// consistent store copy over the torn local world. A fsck I/O error leaves the
// recorded generation untouched (best-effort, logged): the API integrity gate is
// the correctness backstop, so the scan must not wedge on a read fault.
func heldGeneration(workingDir, serverID string, log *slog.Logger) uint64 {
	gen := readGeneration(workingDir)
	report, err := regionfsck.CheckWorkingSet(workingDir)
	if err != nil {
		if log != nil {
			log.Warn("held-set region fsck failed; advertising recorded generation",
				"server_id", serverID, "generation", gen, "error", err)
		}
		return gen
	}
	if !report.Healthy() {
		first := report.Corrupt[0]
		if log != nil {
			log.Warn("held set has a corrupt region; advertising generation 0 to force a hydrate",
				"server_id", serverID, "recorded_generation", gen,
				"corrupt", len(report.Corrupt), "scanned", report.Scanned,
				"example", filepath.Base(first.Path), "reason", first.Reason.String())
		}
		return 0
	}
	return gen
}

// hasWorkingSet reports whether workingDir holds a real working set: at least one
// child that is NOT the generation marker. A dir holding only the marker (or no
// children, or unreadable) holds no working set.
func hasWorkingSet(workingDir string) bool {
	children, err := os.ReadDir(workingDir)
	if err != nil {
		return false
	}
	for _, child := range children {
		if child.Name() != generationFile {
			return true
		}
	}
	return false
}
