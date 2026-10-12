package instancemanager

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/serverproperties"
)

// A server.properties the file-edit path accepted must never be refused for its
// size by the Worker's own reader of that file: otherwise an edit made through
// the dashboard could leave a server that no longer starts, with nothing in the
// UI saying why (issue #3335). The reader's cap is defined apart from
// MaxFileBytes (an adapter cannot import this package), so the relationship is
// pinned here.
func TestServerPropertiesReaderAcceptsAnythingAnEditCanWrite(t *testing.T) {
	if serverproperties.MaxSize < MaxFileBytes {
		t.Fatalf("serverproperties.MaxSize = %d is below MaxFileBytes = %d", serverproperties.MaxSize, MaxFileBytes)
	}
}

// MaxFileBytes is the file cap the API holds too (MAX_EDIT_BYTES), and the one
// the control-plane message limit is sized to carry: all three are pinned to the
// shared contract file (issue #3360).
func TestMaxFileBytesIsTheOneTheAPIHolds(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "proto", "contract", "control_plane_limits.json"))
	if err != nil {
		t.Fatalf("read the limits contract: %v", err)
	}
	var limits struct {
		MaxFileBytes int `json:"max_file_bytes"`
	}
	if err := json.Unmarshal(data, &limits); err != nil {
		t.Fatalf("parse the limits contract: %v", err)
	}
	if MaxFileBytes != limits.MaxFileBytes {
		t.Fatalf("MaxFileBytes = %d, the contract says %d", MaxFileBytes, limits.MaxFileBytes)
	}
}
