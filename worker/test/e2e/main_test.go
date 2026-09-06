//go:build e2e

// This file holds the e2e suite's TestMain, which enforces the same invariant
// on this binary that instancemanager's own TestMain enforces on the unit one
// (issues #2777, #2881).
package e2e

import (
	"os"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager/goroutineleak"
)

// TestMain fails the suite when a goroutine one of its Managers started is still
// running after every test has finished. The scenarios here build real Managers
// over a real Docker daemon (restart_e2e_test.go, forge_e2e_test.go), and each
// closes its own with a defer that runs before the container removals its
// t.Cleanups perform (issue #2875) — but nothing checked that they do, so a
// fourth construction added later could reintroduce the leak with no gate
// reddening. The census it asserts against is the one instancemanager's TestMain
// uses, so an entry added to that single list reaches both binaries at once and
// the two can never drift apart — a pump left out of it stays invisible to both.
//
// It runs after m.Run returns, i.e. after every test's cleanups, so it does not
// race the suite's own container teardown. The per-run reaper is invoked by the
// scenarios themselves, not from here, so this adds no setup they could fight.
func TestMain(m *testing.M) {
	os.Exit(goroutineleak.FailIfSurvivors(m.Run()))
}
