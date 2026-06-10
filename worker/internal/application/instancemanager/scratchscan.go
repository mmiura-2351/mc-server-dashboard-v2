package instancemanager

import (
	"os"
	"path/filepath"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

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
// A subdirectory whose only content is the generation marker is treated as EMPTY
// and SKIPPED: it holds no real working set, so the API must still hydrate (never
// silently boot a fresh/empty world). A missing or unreadable scratch root yields
// an empty list (the Worker reports holding nothing). The scan does not recurse and
// does not validate that a name is a server id — a non-server directory under
// scratch is harmless to report because the API only consults this for ids it has
// assigned to the Worker.
func ScanHeldServers(scratchDir string) []session.HeldServer {
	entries, err := os.ReadDir(scratchDir)
	if err != nil {
		return nil
	}
	var held []session.HeldServer
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		workingDir := filepath.Join(scratchDir, entry.Name())
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
