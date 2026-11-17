package scratchformat_test

import (
	"fmt"
	"os"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/testsupport/tmproot"
)

// TestMain puts the package's temp dirs on a tmpfs before any test (issue
// #3805): the Hydrate they drive fsyncs every file and directory it writes.
func TestMain(m *testing.M) {
	if err := tmproot.Use(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	os.Exit(m.Run())
}

func TestTempDirsLieUnderTheSelectedRoot(t *testing.T) {
	tmproot.RequireTempDirUnderRoot(t)
}
