package instancemanager

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
)

// Existing fixtures keep their short name; production uses scratchformat directly.
const generationFile = scratchformat.GenerationMarkerFile

// testGenerationKey is the key the managers newManager builds sign and verify their
// generation markers with (issue #3349), and the one the helpers below use. It is held
// in memory, so no test's scratch root holds a key file the test did not ask for.
var testGenerationKey = &GenerationKey{key: bytes.Repeat([]byte{0x5a}, generationKeySize)}

// markerServerID is the server whose marker a test tree holds: the one the tree is
// named after, under its own name or the .displaced-<id> one a hydrate parks it at.
func markerServerID(dir string) string {
	return strings.TrimPrefix(filepath.Base(dir), displacedPrefix)
}

// readMarker is readGenerationMarker for the server workingDir belongs to, under
// testGenerationKey.
func readMarker(workingDir string) (uint64, error) {
	return readGenerationMarker(testGenerationKey, workingDir, markerServerID(workingDir))
}

// readGeneration is the marker's value as the production readers act on it: a refused
// or unverified marker reads as 0, like an absent one.
func readGeneration(workingDir string) uint64 {
	return readGenerationOf(workingDir, markerServerID(workingDir))
}

// readGenerationOf is readGeneration for a tree under a name markerServerID cannot
// read the server from, such as .sweeping-<id>-*.
func readGenerationOf(dir, serverID string) uint64 {
	gen, _ := readGenerationMarker(testGenerationKey, dir, serverID)
	return gen
}

// stampGeneration is writeGeneration for the server workingDir belongs to, under
// testGenerationKey.
func stampGeneration(workingDir string, gen uint64) error {
	return writeGeneration(testGenerationKey, workingDir, markerServerID(workingDir), gen)
}

// seedHydrateShapedTree creates a directory holding a real working set (world
// content plus a generation marker at gen).
func seedHydrateShapedTree(t *testing.T, dir string, gen uint64) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, "world"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "world", "level.dat"), []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := stampGeneration(dir, gen); err != nil {
		t.Fatal(err)
	}
}
