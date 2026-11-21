package regionfsck

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// The region check's depth bound is the one the API's member-name rule holds
// (issue #3973), pinned to the shared contract file.
func TestWalkDepthIsTheMemberNameRule(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "proto", "contract", "member_name_limits.json"))
	if err != nil {
		t.Fatalf("read the member-name contract: %v", err)
	}
	var limits struct {
		MaxDirectoryDepth int `json:"max_directory_depth"`
	}
	if err := json.Unmarshal(data, &limits); err != nil {
		t.Fatalf("parse the member-name contract: %v", err)
	}
	if maxWalkDepth != limits.MaxDirectoryDepth {
		t.Fatalf("maxWalkDepth = %d, the contract says %d", maxWalkDepth, limits.MaxDirectoryDepth)
	}
}
