package instancemanager

import (
	"os"
	"path/filepath"
)

// ScanHeldServerIDs returns the ids of the servers whose working set this Worker
// already holds in its persistent local scratch (issue #696): the immediate
// subdirectories of scratchDir that are NON-EMPTY. The list is advertised on
// Register (held_server_ids) so the API skips the destructive hydrate on a
// same-worker restart — a hydrate would unpack the last authoritative snapshot
// over the Worker's LIVE, newer working set and roll the world back.
//
// An empty subdirectory is SKIPPED: an empty scratch holds no working set, so
// the API must still hydrate (never silently boot a fresh/empty world). A
// missing or unreadable scratch root yields an empty list (the Worker simply
// reports holding nothing). The scan does not recurse and does not validate that
// a name is a server id — a non-server directory under scratch is harmless to
// report because the API only consults this for ids it has assigned to the
// Worker.
func ScanHeldServerIDs(scratchDir string) []string {
	entries, err := os.ReadDir(scratchDir)
	if err != nil {
		return nil
	}
	var held []string
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		children, err := os.ReadDir(filepath.Join(scratchDir, entry.Name()))
		if err != nil || len(children) == 0 {
			continue
		}
		held = append(held, entry.Name())
	}
	return held
}
