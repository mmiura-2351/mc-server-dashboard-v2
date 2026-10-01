package instancemanager

import (
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/regionfsck"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// displacedPrefix is the dot-prefixed name prefix datatransfer uses for a displaced
// old working set kept aside for recovery (issue #906/#910). It is a sibling of the
// server-id scratch dirs and must be skipped by ScanHeldServers: it is not a held
// server, and scanning it would trigger a per-boot header fsck of a world-sized tree
// and emit a confusing server_id=.displaced-<id> corrupt warning. The constant is
// duplicated rather than imported to keep this application package off the adapter.
const displacedPrefix = ".displaced-"

// sweepingPrefix is the dot-prefixed name prefix sweepDisplaced renames a
// .displaced-<id> tree to (".sweeping-<id>-*") before removing it, so the slot is
// emptied atomically instead of being traversed in place (issue #2799). A tree still
// under this name is one whose removal did not finish — or, since issue #3118, one whose
// removal the sweep withdrew and left here, because the tree held no working set or the
// slot was not empty to put it back into. The boot reclaim takes it either way; the cases
// where such a tree was still worth something, and why none of them can be told apart
// here, are in putBackSweptTree and ReclaimInterruptedDisplacedSweeps. Creation, the
// held-set skip and the boot reclaim all live in this package and share this one constant.
const sweepingPrefix = ".sweeping-"

// isReservedScratchName reports whether a scratch-root entry name is one of the
// dot-prefixed siblings the Worker keeps next to the server-id scratch dirs rather
// than a working set it holds for an assigned server. Both held-set scans
// share it so they can never drift apart on what they enumerate.
func isReservedScratchName(name string) bool {
	return strings.HasPrefix(name, displacedPrefix) || strings.HasPrefix(name, scratchformat.HydratePrefix) ||
		strings.HasPrefix(name, sweepingPrefix)
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
// The verdict is PERSISTED: the set's marker is rewritten to 0 (issue #3178). The list
// this returns is not what reaches the API — the session replaces it with the in-session
// HeldServers() before every Register, the first included (issue #1711), and that scan
// runs no fsck — so a verdict kept only here would never leave the process. With the
// marker at 0, every later registration advertises 0 too, and the hydrate it dispatches
// clears it through its own marker write (handleHydrate's recordGeneration), with no
// verdict anywhere else that could go stale. A restart re-reads 0 as well, so even a
// later boot whose sweep fails still sends the hydrate. The costs, accepted: the boot
// scan writes to disk; the original generation survives only in the WARN (which a manual
// recovery from a displaced torn tree needs, STORAGE.md Section 4.6); and a false torn
// verdict is durable, costing one hydrate that parks the replaced tree aside as any
// hydrate does. A rewrite that fails is logged and degrades to advertising the recorded
// generation, the same best-effort class as a fsck I/O error.
//
// quiesced is the CALLER's statement that no container this Worker started can still
// be writing into a scratch tree — the same premise the boot reclaims above require,
// established by the container orphan sweep and by nothing else. The fsck is skipped
// when it is false, no marker is rewritten, and every held set is advertised at its
// RECORDED generation (issue #3171). A torn verdict is only meaningful on a quiesced set
// (regionfsck states that contract at its own site): after a failed sweep an orphan
// can still be running with <scratch>/<id> bind-mounted, and since nothing re-adopts
// containers the Worker does not even know the world is live, so the scan would read a
// world mid-write, judge a healthy set torn and advertise a 0 that dispatches a
// DESTRUCTIVE hydrate over it. Reporting the marker is the scan's honest answer in that state:
// the marker is on disk, and judging what it names is what quiescence buys.
//
// The costs of the two directions are not symmetric, which is what settles it. An
// unjudged genuinely-torn set is booted at its recorded generation for this one boot,
// with the consistent store copy still in place and the fsck arriving at the next boot
// whose sweep succeeds — the same "wait for a boot that can prove it" the reclaims
// above take. A judged live world is hydrated over while its server writes, and that
// loses the world's progression AND everything written after it (the descriptors keep
// writing into unlinked inodes). Skipping the scan entirely — the other candidate —
// advertises NOTHING held, so the API hydrates every server on this Worker, which is
// strictly worse than either.
//
// The fsck uses the single region rule set (issue #927/#926 item 1): a held scratch
// of a crashed or non-gracefully-stopped 26.x server is live-format (unaligned tails)
// and structurally sound, so it now PASSES and the worker advertises its held
// generation — no forced gen-0 recovery hydrate that would discard up to a full
// snapshot-interval of progression even though the scratch was fine. A genuinely
// torn scratch (a referenced chunk overrunning EOF, an entry past EOF, a severed
// prefix) still fails the byte-precise check and falls back to gen 0 as before. The
// fsck reads only the region headers (regionfsck), so it is bounded; it runs at most
// once per held set per boot. A fsck I/O error is best-effort (logged, the
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
func ScanHeldServers(scratchDir string, quiesced bool, log *slog.Logger) []session.HeldServer {
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
		// #906/#910), a .hydrate-<id>-* one is a crashed hydrate's leftover (issue
		// #2290) and a .sweeping-<id>-* one an interrupted displaced sweep's (issue
		// #2799), not held servers: skip them so they are never reported (and never
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
			Generation: heldGeneration(workingDir, entry.Name(), quiesced, log),
		})
	}
	return held
}

// rewriteTornMarker is the marker write heldGeneration persists a torn verdict with —
// writeGeneration, indirected through a package var (mirroring readSweptTree) so a test
// can fail it without a chmod fixture, which root ignores. Production always uses
// writeGeneration.
var rewriteTornMarker = writeGeneration

// persistTornVerdict rewrites a torn set's marker to 0 (issue #3178), the one place both
// judges — the boot scan (heldGeneration) and the launch fsck (tornAtLaunch, issue #3201)
// — persist a torn verdict. gen is the marker's current reading, and the write is skipped
// when it is already 0, which covers an absent marker: creating one would drop the launch
// guard's refusal (issue #2802).
func persistTornVerdict(workingDir string, gen uint64) error {
	if gen == 0 {
		return nil
	}
	return rewriteTornMarker(workingDir, 0)
}

// heldGeneration returns the generation to advertise for a held working set: the
// recorded marker generation when the set is structurally sound, or 0 when a region
// fsck finds it torn (issue #834) — a 0 forces the API to hydrate, recovering the
// consistent store copy over the torn local world. A torn set's marker is rewritten to
// 0 (skipped when it already reads 0, which covers an absent marker: creating one would
// drop the launch guard's refusal, issue #2802) so the in-session scan every Register
// sends carries the verdict (issue #3178); the WARN is then the only record of the
// recorded generation, which is why it names it. A fsck I/O error leaves the recorded
// generation untouched (best-effort, logged): the API integrity gate is the correctness
// backstop, so the scan must not wedge on a read fault. A failed rewrite is the same
// class: logged, and the marker's recorded generation is what registrations then carry.
//
// The fsck does not run at all unless the caller established quiescence (issue
// #3171): on a world a container may still be writing, a torn verdict is not a fact
// about the world but an artefact of the read, and the 0 it produces — persisted in the
// marker, at that — sends a destructive hydrate over a live server. The recorded
// generation is what the Worker can honestly say there, and the fsck resumes at the
// next boot that can prove the premise. ScanHeldServers' doc has the full argument,
// including why skipping the advertisement instead is worse.
func heldGeneration(workingDir, serverID string, quiesced bool, log *slog.Logger) uint64 {
	gen := readGeneration(workingDir)
	if !quiesced {
		if log != nil {
			log.Warn("skipping the held-set region fsck: the container orphan sweep did not "+
				"establish that no container is still writing, so a torn-looking region here may "+
				"be a running orphan's live world read mid-write; advertising the recorded "+
				"generation and re-checking at the next boot whose sweep succeeds (issue #3171)",
				"server_id", serverID, "generation", gen)
		}
		return gen
	}
	report, err := regionfsck.CheckWorkingSet(workingDir)
	if err != nil {
		if log != nil {
			log.Warn("held-set region fsck failed; advertising recorded generation",
				"server_id", serverID, "generation", gen, "error", err)
		}
		return gen
	}
	if !report.Healthy() {
		rewriteErr := persistTornVerdict(workingDir, gen)
		if log != nil {
			first := report.Corrupt[0]
			attrs := []any{"server_id", serverID, "recorded_generation", gen,
				"corrupt", len(report.Corrupt), "scanned", report.Scanned,
				"example", filepath.Base(first.Path), "reason", first.Reason.String()}
			if rewriteErr != nil {
				log.Warn("held set has a corrupt region but its generation marker could not be "+
					"rewritten to 0; registrations advertise the recorded generation until a "+
					"hydrate rewrites the marker or a later quiesced boot judges the set again",
					append(attrs, "error", rewriteErr)...)
			} else {
				log.Warn("held set has a corrupt region; its generation marker now reads 0, forcing "+
					"a hydrate (recorded_generation is the only surviving record of the original, "+
					"see STORAGE.md Section 4.6)", attrs...)
			}
		}
		return 0
	}
	return gen
}

// HeldServers returns the working sets this Worker currently holds in its local
// scratch, each tagged with its recorded generation (issue #1711). The session calls
// this before each registration, the first included, so the advertised held set
// reflects servers placed or generations advanced since boot — which makes this, not
// ScanHeldServers, what the API is told.
//
// It runs no region fsck, and it does not need one: the torn-region verdict (issue
// #834) is reached once, by the quiesced boot scan, and persisted in the very marker
// read here (issue #3178), so a torn set reads 0 until a hydrate replaces the tree and
// rewrites the marker. Re-judging here would pay a region walk per held world on every
// reconnect and, worse, read worlds this Worker is running mid-write.
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

// ReclaimInterruptedDisplacedSweeps removes every .sweeping-<id>-* tree in scratchDir
// (issue #2799): a displaced tree sweepDisplaced renamed out of its slot but did not
// finish removing, because the Worker crashed mid-traversal or the removal failed. It
// runs once at boot, where it is unconditional GIVEN ITS PRECONDITION: no sweep is in
// flight yet, and the sweep had already decided each such tree was garbage. The
// precondition is the CALLER's to establish — no container this Worker started is still
// writing into the tree, which only a SUCCESSFUL container orphan sweep proves, so run()
// skips this call (and its .hydrate- sibling below) when that sweep failed (PR #3170
// review round 2). Without that gate a tree a hydrate parked aside while an unswept orphan
// kept writing into it is deleted under a live server. It stays unconditional now that a sweep
// can also leave one behind by withdrawing its removal and having nowhere to put the tree
// back (issue #3118), and it is deliberately not taught to put trees back itself: the
// withdrawn case, the crash mid-traversal and a power loss inside the sweep's own
// put-back window leave the same state on disk, and only the last of the three is a tree
// the slot is missing. Restoring on that guess would resurrect trees a sweep was entitled
// to delete, which is the unbounded leak this reclaim exists to stop.
// Nothing else reclaims one — a crash
// inside the stopped-id GC leaves it after the scratch dir is gone, so the id is never
// advertised as held again and ReclaimDeletedScratches is never offered it. Best-effort:
// an unreadable scratch root or a failed removal is ignored and retried at the next boot.
func ReclaimInterruptedDisplacedSweeps(scratchDir string) {
	entries, err := os.ReadDir(scratchDir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), sweepingPrefix) {
			_ = os.RemoveAll(filepath.Join(scratchDir, e.Name()))
		}
	}
}

// ReclaimHydrateLeftovers removes every .hydrate-<id>-* tree in scratchDir (issue
// #3167) — the per-hydrate temp tree datatransfer.unpackAndSwap unpacks into, and the
// superseded working set it parks aside when oldest-wins keeps an older .displaced-<id>
// instead (issue #2278). It is the sibling of ReclaimInterruptedDisplacedSweeps above,
// runs at the same point in boot and for the same reason: nothing else reclaims one once
// the id's scratch dir is gone. Both held-set scans skip .hydrate- names
// (isReservedScratchName), so such a tree is never advertised on its own; the per-id
// sweeps (removeScratch, ReclaimDeletedScratches) are only ever offered an id the
// scratch dir still advertises; and datatransfer's own sweep runs only if the server is
// re-placed onto this Worker. A server deleted or re-placed elsewhere mid-hydrate
// therefore leaked a world-sized tree permanently.
//
// Unconditional at boot, and what that rests on:
//
//   - Nothing can be building one. The only creation site is unpackAndSwap, reached from
//     a HydrateTrigger, and the session that dispatches commands does not exist until
//     after this call — the same argument ReclaimInterruptedDisplacedSweeps makes for a
//     sweep in flight.
//   - Nothing can still be writing into one — and this one is a PRECONDITION the caller
//     must establish, not something this function can check. A .hydrate- tree is never
//     bind-mounted (a container gets <scratch>/<id>), but a container that held such a
//     tree under its old name across a park-aside keeps writing into it through the
//     inode, and only the container orphan sweep stops that. run() therefore calls this
//     ONLY when that sweep succeeded (PR #3170 review round 2): a failed sweep is
//     non-fatal by design, so quiescence has to be checked rather than assumed, and the
//     trees wait for a boot whose sweep succeeds.
//   - None of them is the copy worth keeping. The live set a hydrate displaces is parked
//     DIRECTLY at .displaced-<id> whenever that slot is free, precisely so the recovery
//     copy is never left under a name a sweep deletes (issue #910). What is left under a
//     .hydrate- name is the store's own copy being unpacked, or the set oldest-wins
//     elected to DROP — including the case where the post-swap drop declined and left it
//     "for the next leftover sweep" (issue #3112). Every existing sweeper already deletes
//     these three unconditionally, so this pass changes WHEN they go, not what goes.
//
// Best-effort: an unreadable scratch root or a failed removal is ignored and retried at
// the next boot.
func ReclaimHydrateLeftovers(scratchDir string) {
	entries, err := os.ReadDir(scratchDir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), scratchformat.HydratePrefix) {
			_ = os.RemoveAll(filepath.Join(scratchDir, e.Name()))
		}
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

// readSweptTree is the os.ReadDir sweptTreeHoldsWorkingSet lists a swept tree with,
// indirected through a package var (mirroring datatransfer.readDir, which the hydrate's
// slot rule reads through for the same reason) so a test can inject a read failure
// without a chmod fixture — a mode-000 directory is readable by root, so a chmod-based
// test silently stops asserting anything whenever the suite runs as root. Production
// always uses os.ReadDir.
var readSweptTree = os.ReadDir

// sweptTreeHoldsWorkingSet reports whether a tree the running-id sweep took out of the
// .displaced-<id> slot holds a world worth putting back (issue #3118).
//
// It is NOT hasWorkingSet, and the difference is the point. This is a durability
// decision about one specific tree, so it applies the rule the HYDRATE applies to that
// same slot (datatransfer.displacedSlotHoldsWorkingSet) — the two must agree, and
// TestSweptTreeClassifierMatchesTheHydrateSlotRule pins them to each other in the issue
// #2280 twin-test style, since the adapter deliberately imports nothing from here:
//
//   - TYPE-AWARE: Lstat plus IsDir, never a symlink-following read. hasWorkingSet lists
//     through a symlink, so it calls a link to a populated directory a working set while
//     the hydrate calls it junk. A sweep acting on that puts the link back into the slot,
//     where it occupies the slot against a concurrent sweep holding the real recovery
//     tree (PR #3121 review, round 2).
//   - ERROR-RETURNING: a read failure is returned, not folded into "no working set" the
//     way hasWorkingSet folds it. That fold is right for the held-set scans, which must
//     not ADVERTISE a set they cannot prove; it is wrong here, where the same answer
//     means "delete this at the next boot". The caller keeps the tree on uncertainty.
func sweptTreeHoldsWorkingSet(path string) (bool, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return false, err
	}
	if !info.IsDir() {
		return false, nil
	}
	entries, err := readSweptTree(path)
	if err != nil {
		return false, err
	}
	return holdsWorkingSet(entries), nil
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
		if !strings.HasPrefix(child.Name(), scratchformat.GenerationMarkerFile) {
			return true
		}
	}
	return false
}
