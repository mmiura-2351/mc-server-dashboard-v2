//go:build e2e

// This file holds the e2e suite's TestMain, which enforces the same invariant on this binary that
// instancemanager's own TestMain enforces on the unit one.
package e2e

import (
	"os"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager/goroutineleak"
)

// Check for surviving Manager goroutines after all test cleanup has completed.
func TestMain(m *testing.M) {
	os.Exit(goroutineleak.FailIfSurvivors(m.Run()))
}
