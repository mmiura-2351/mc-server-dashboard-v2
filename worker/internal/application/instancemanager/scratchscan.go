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

// hydratePrefix is the dot-prefixed name prefix datatransfer builds its per-hydrate
// temp tree and superseded-set aside from (hydrateTmpPrefix, ".hydrate-<id>-*"). A
// crash between a hydrate's aside/unpack and its swap-in leaves such a sibling of the
// server-id scratch dirs behind, and it is a FULL working set carrying a generation
// marker — so the held-set scans must skip it for the same reasons they skip
// .displaced- (issue #2290): it is not a held server, and enumerating it advertises a
// server id the API never assigned, pays a per-boot header fsck of a world-sized tree
// that is about to be swept anyway, and names a directory rather than a server id in
// exactly the incident diagnostics someone reads after a crashed hydrate. The constant
// is duplicated rather than imported to keep this application package off the adapter,
// and pinned to its creation site by the twin tests in hydrate_prefix_name_test.go.
const hydratePrefix = ".hydrate-"

// isReservedScratchName reports whether a scratch-root entry name is one of the
// dot-prefixed siblings datatransfer keeps next to the server-id scratch dirs rather
// than a working set the Worker holds for an assigned server. Both held-set scans
// share it so they can never drift apart on what they enumerate.
func isReservedScratchName(name string) bool {
	return strings.HasPrefix(name, displacedPrefix) || strings.HasPrefix(name, hydratePrefix)
}

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
// forcing the hydrate that recovers the consistent store copy.
//
// The fsck uses the single region rule set (issue #927/#926 item 1): a held scratch
// of a crashed or non-gracefully-stopped 26.x server is live-format (unaligned tails)
// and structurally sound, so it now PASSES and the worker advertises its held
// generation — no forced gen-0 recovery hydrate that would discard up to a full
// snapshot-interval of progression even though the scratch was fine. A genuinely
// torn scratch (a referenced chunk overrunning EOF, an entry past EOF, a severed
// prefix) still fails the byte-precise check and falls back to gen 0 as before. The
// fsck reads only the region headers (regionfsck), so it is bounded; it runs at most
// once per held set at registration. A fsck I/O error is best-effort (logged, the
// recorded generation stands): the API gate remains the correctness backstop, and
// the startup scan must not wedge on a read fault.
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
		// #906/#910) and a .hydrate-<id>-* one is a crashed hydrate's leftover (issue
		// #2290), not held servers: skip them so they are never reported (and never
		// header-fsck'd per boot under a server_id=.displaced-<id> warning).
		if isReservedScratchName(entry.Name()) {
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

// HeldServers returns the working sets this Worker currently holds in its local
// scratch, each tagged with its recorded generation (issue #1711). Unlike the
// boot-time ScanHeldServers, it skips the region fsck: the Worker is still
// running, so the torn-region recovery path (issue #834) is not needed for
// freshness — only the recorded generation marker matters. The session calls
// this before each (re-)registration so the advertised held set reflects
// servers placed or generations advanced since boot.
func (m *Manager) HeldServers() []session.HeldServer {
	entries, err := os.ReadDir(m.scratchDir)
	if err != nil {
		return nil
	}
	var held []session.HeldServer
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		if isReservedScratchName(entry.Name()) {
			continue
		}
		workingDir := filepath.Join(m.scratchDir, entry.Name())
		if !hasWorkingSet(workingDir) {
			continue
		}
		held = append(held, session.HeldServer{
			ServerID:   entry.Name(),
			Generation: readGeneration(workingDir),
		})
	}
	return held
}

// WarnOrphanDisplacedTrees logs a WARN for each .displaced-<id> tree in scratchDir
// whose server id is NOT in heldServers (issue #911). A .displaced-<id> tree is a
// last-resort recovery copy kept aside by a hydrate when a server's final stop
// snapshot failed (STORAGE.md Section 4.6). When the server id is in heldServers
// the tree will be GC'd on the next successful snapshot (sweepDisplaced); when it
// is NOT in heldServers the server was deleted or re-placed elsewhere and the tree
// is an orphan the operator should be aware of.
//
// One WARN line is logged per orphaned tree. The function is best-effort: a scan
// error is ignored (the scratchDir may not yet exist on a first boot). If log is
// nil, logging is suppressed (tests that don't care about log output).
func WarnOrphanDisplacedTrees(scratchDir string, held []session.HeldServer, log *slog.Logger) {
	if log == nil {
		return
	}
	heldIDs := make(map[string]bool, len(held))
	for _, h := range held {
		heldIDs[h.ServerID] = true
	}
	entries, err := os.ReadDir(scratchDir)
	if err != nil {
		return
	}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		name := entry.Name()
		if !strings.HasPrefix(name, displacedPrefix) {
			continue
		}
		serverID := strings.TrimPrefix(name, displacedPrefix)
		if heldIDs[serverID] {
			// This server still has a held working set; sweepDisplaced will reclaim
			// the tree on its next successful snapshot. No WARN needed.
			continue
		}
		log.Warn("displaced recovery tree for unknown/unassigned server found at boot; "+
			"manual cleanup or recovery may be needed (see STORAGE.md Section 4.6)",
			"path", filepath.Join(scratchDir, name), "server_id", serverID)
	}
}

// hasWorkingSet reports whether workingDir holds a real working set: at least one
// child that is NOT the generation marker. A dir holding only the marker (or no
// children, or unreadable) holds no working set.
//
// The marker match is by PREFIX, not exact name (issue #2279), the same convention
// the snapshot pack path applies for the same reason (issue #834): writeGeneration
// writes the marker atomically via a ".mcsd_generation-XXXX" temp sibling + rename,
// so a crash before the rename leaves such a temp in the scratch dir. An exact-name
// comparison would read that leftover as a working set and make the Worker advertise
// holding a world it does not hold. Only the marker and its temp siblings live
// directly under workingDir, so the prefix cannot mask a real world file (the scan
// does not recurse).
func hasWorkingSet(workingDir string) bool {
	children, err := os.ReadDir(workingDir)
	if err != nil {
		return false
	}
	return holdsWorkingSet(children)
}

// holdsWorkingSet is hasWorkingSet's decision over entries the caller has already
// read. Split out so a caller that must tell "holds no working set" from "cannot be
// read" applies the SAME predicate without inheriting the swallow above
// (handleSnapshot's stopped-id guard, PR #2840 review): there the false-on-error
// answer would classify an EACCES/EMFILE/EIO as the benign refusal the API reads as
// "nothing was lost". The swallow stays right for the scans above, which ADVERTISE
// held sets — a set the Worker cannot prove it holds must not be advertised.
func holdsWorkingSet(children []os.DirEntry) bool {
	for _, child := range children {
		if !strings.HasPrefix(child.Name(), generationFile) {
			return true
		}
	}
	return false
}
