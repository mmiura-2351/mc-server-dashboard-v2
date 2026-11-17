package instancemanager

import (
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/testsupport/tmproot"
)

// TestMain puts the package's temp dirs on a tmpfs before any test (issue
// #3783): its tests fsync for real. The tests that pin an fsync's order swap the
// sync seam (syncEditDir, syncSweepScratchRoot), so they hold on any filesystem.
func TestTempDirsLieUnderTheSelectedRoot(t *testing.T) {
	tmproot.RequireTempDirUnderRoot(t)
}
