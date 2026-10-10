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

// Skip displaced recovery siblings in held-set scans; keep this prefix aligned with datatransfer without an
// adapter import.
const displacedPrefix = ".displaced-"

// Rename displaced trees to this prefix before removal; boot reclaim removes interrupted sweeps under these
// names.
const sweepingPrefix = ".sweeping-"

// Both held-set scans must skip the same recovery, hydrate, and sweeping sibling names.
func isReservedScratchName(name string) bool {
	return strings.HasPrefix(name, displacedPrefix) || strings.HasPrefix(name, scratchformat.HydratePrefix) ||
		strings.HasPrefix(name, sweepingPrefix)
}

// ScanHeldServers advertises content-bearing scratch with its generation, skipping reserved names.
// Run fsck only when quiesced; persist torn verdicts as zero because registration re-reads markers without fsck.
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
		// Recovery and incomplete-transfer siblings are not live held sets.
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

// Inject marker-write failure without chmod fixtures, which root ignores.
var rewriteTornMarker = writeGeneration

// heldGeneration persists zero only after quiesced fsck proves corruption.
// Fsck I/O errors keep the recorded generation; failed marker writes log and leave the disk marker unchanged.
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
		var rewriteErr error
		if gen != 0 {
			rewriteErr = rewriteTornMarker(workingDir, 0)
		}
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

// HeldServers refreshes inventory before every registration without scanning live regions.
// Boot fsck persists torn verdicts in the markers this scan reads; hydrate clears them by replacing the tree.
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

// WarnOrphanDisplacedTrees reports recovery copies with no held server or guaranteed future per-ID cleanup.
// Operators must inspect and recover or remove these copies manually.
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

// ReclaimInterruptedDisplacedSweeps removes .sweeping- leftovers only at boot after orphan writers are stopped.
// It must run before any sweep or hydrate can own these trees.
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

// ReclaimHydrateLeftovers removes incomplete unpack and superseded trees before the session starts.
// Call only after orphan writers are stopped; retain .displaced- recovery copies.
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

// A working set needs content beyond generation-marker files and temps; unreadable directories are not
// advertised.
func hasWorkingSet(workingDir string) bool {
	children, err := os.ReadDir(workingDir)
	if err != nil {
		return false
	}
	return holdsWorkingSet(children)
}

// Inject recovery-tree read failures without chmod fixtures, which root ignores.
var readSweptTree = os.ReadDir

// Classify recovery trees with the same type-aware rule as hydrate, without following symlinks.
// Return read errors so the caller retains uncertain data rather than scheduling it for deletion.
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

// Share the content predicate without swallowing read errors needed for durability decisions.
// Held-set advertisement treats unreadable as absent; final snapshot must propagate the failure.
func holdsWorkingSet(children []os.DirEntry) bool {
	for _, child := range children {
		if !strings.HasPrefix(child.Name(), scratchformat.GenerationMarkerFile) {
			return true
		}
	}
	return false
}
