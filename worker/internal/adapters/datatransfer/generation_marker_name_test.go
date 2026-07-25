package datatransfer

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"testing"
)

// The generation marker's FILENAME is a cross-package contract (issue #2280).
// Two Worker packages must agree on it byte-for-byte, with nothing in the type
// system pinning them to each other:
//
//   - unpackAndSwap here WRITES the marker into the hydrate temp tree before the
//     swap-in rename (issue #917), using the local constant
//     generationMarkerFile.
//   - instancemanager.readGeneration READS it back, using its own generationFile
//     in worker/internal/application/instancemanager/generation.go.
//
// So this test and its twin —
// TestReadGenerationReadsTheSharedGenerationMarkerName in
// worker/internal/application/instancemanager — each assert the SAME hardcoded
// literal from their own side. Renaming either constant then fails CI here
// instead of degrading an invariant silently.
//
// The invariant is durability, not bookkeeping. If the two names drift, a
// freshly hydrated dir carries the marker under one name, readGeneration finds
// nothing under the other and reports generation 0, the API treats the working
// set as unknown and re-dispatches a hydrate — and that hydrate's displace step
// spends the recovery copy. That is exactly the data-loss window issue #917
// closed, reopened by a rename.
func TestHydrateWritesTheSharedGenerationMarkerName(t *testing.T) {
	const servedGen uint64 = 42
	body := tarOf(map[string]string{"server.properties": "new"})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set(generationHeader, strconv.FormatUint(servedGen, 10))
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	dest := filepath.Join(t.TempDir(), "server")
	if _, err := New(srv.Client()).Hydrate(context.Background(), srv.URL, "tok", dest); err != nil {
		t.Fatalf("Hydrate: %v", err)
	}

	// The name is hardcoded on purpose: reading it via generationMarkerFile would
	// make this test follow a rename rather than catch it.
	data, err := os.ReadFile(filepath.Join(dest, ".mcsd_generation"))
	if err != nil {
		t.Fatalf("read marker: %v: the marker must stay named %q, the name "+
			"instancemanager.readGeneration looks for (issues #917, #2280)",
			err, ".mcsd_generation")
	}
	if want := strconv.FormatUint(servedGen, 10); string(data) != want {
		t.Fatalf("marker contents = %q, want %q", data, want)
	}
}
