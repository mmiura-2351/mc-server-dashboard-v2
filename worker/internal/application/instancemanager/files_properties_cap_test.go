package instancemanager

import (
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
