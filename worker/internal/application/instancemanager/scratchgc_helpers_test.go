package instancemanager

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
)

// Existing fixtures keep their short name; production uses scratchformat directly.
const generationFile = scratchformat.GenerationMarkerFile

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
	if err := writeGeneration(dir, gen); err != nil {
		t.Fatal(err)
	}
}
