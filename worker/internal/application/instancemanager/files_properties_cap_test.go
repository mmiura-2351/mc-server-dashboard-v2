package instancemanager

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/serverproperties"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
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

// The Forge install log is how an operator diagnoses a failed install, read
// through the files API like any other file: the bound the driver keeps it under
// must stay below the read cap, or the log is refused exactly when it is needed
// (issue #3282).
func TestForgeInstallLogFitsTheFileReadCap(t *testing.T) {
	if execution.ForgeInstallLogMaxBytes >= MaxFileBytes {
		t.Fatalf("execution.ForgeInstallLogMaxBytes = %d is not below MaxFileBytes = %d",
			execution.ForgeInstallLogMaxBytes, MaxFileBytes)
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

// MaxConsoleLineBytes is the console-line cap the API holds too
// (MAX_CONSOLE_LINE_BYTES): both are pinned to the shared contract file (issue
// #3363).
func TestMaxConsoleLineBytesIsTheOneTheAPIHolds(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "proto", "contract", "control_plane_limits.json"))
	if err != nil {
		t.Fatalf("read the limits contract: %v", err)
	}
	var limits struct {
		MaxConsoleLineBytes int `json:"max_console_line_bytes"`
	}
	if err := json.Unmarshal(data, &limits); err != nil {
		t.Fatalf("parse the limits contract: %v", err)
	}
	if MaxConsoleLineBytes != limits.MaxConsoleLineBytes {
		t.Fatalf("MaxConsoleLineBytes = %d, the contract says %d", MaxConsoleLineBytes, limits.MaxConsoleLineBytes)
	}
}
