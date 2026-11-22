package datatransfer

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// The snapshot walk's depth and path bounds are the ones the API's member-name
// rule holds (issue #3973): both are pinned to the shared contract file, so a
// working set the API lets a user create is one the Worker can snapshot.
func TestPackBoundsAreTheMemberNameRule(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "proto", "contract", "member_name_limits.json"))
	if err != nil {
		t.Fatalf("read the member-name contract: %v", err)
	}
	var limits struct {
		MaxDirectoryDepth int `json:"max_directory_depth"`
		MaxPathBytes      int `json:"max_path_bytes"`
	}
	if err := json.Unmarshal(data, &limits); err != nil {
		t.Fatalf("parse the member-name contract: %v", err)
	}
	if maxPackDepth != limits.MaxDirectoryDepth {
		t.Fatalf("maxPackDepth = %d, the contract says %d", maxPackDepth, limits.MaxDirectoryDepth)
	}
	if maxMemberPath != limits.MaxPathBytes {
		t.Fatalf("maxMemberPath = %d, the contract says %d", maxMemberPath, limits.MaxPathBytes)
	}
}
