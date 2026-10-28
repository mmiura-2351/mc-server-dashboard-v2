package instancemanager

import (
	"context"
	"errors"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/regioncheck"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// stuckChecker is a region check stuck past the snapshot command's deadline: it
// returns only when its context is done, as the regioncheck.Checker contract
// requires of a check whose walk is stuck (issue #3511).
type stuckChecker struct {
	mu     sync.Mutex
	gotErr error
}

func (c *stuckChecker) CheckWorkingSet(ctx context.Context, _ string) (regioncheck.Report, error) {
	<-ctx.Done()
	c.mu.Lock()
	defer c.mu.Unlock()
	c.gotErr = ctx.Err()
	return regioncheck.Report{}, ctx.Err()
}

// snapshotWithin runs cmd and fails the test unless it answers within limit. On
// failure the request context is cancelled so the handler does not outlive the test.
func snapshotWithin(t *testing.T, m *Manager, cmd session.Command, limit time.Duration) session.CommandResult {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan session.CommandResult, 1)
	go func() { done <- m.Handle(ctx, cmd) }()
	select {
	case res := <-done:
		cancel()
		return res
	case <-time.After(limit):
		cancel()
		<-done
		t.Fatalf("%s did not answer within %s", cmd.Kind, limit)
		return session.CommandResult{}
	}
}

// The pre-pack fsck runs under the snapshot command's deadline (issue #3511): a
// check stuck past it fails the command at the deadline, nothing is packed, and a
// stopped server's scratch is kept as on any failed snapshot.
func TestSnapshotFsckStuckPastTheDeadlineFailsTheCommand(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	checker := &stuckChecker{}
	m.regions = checker
	m.SetTransferDeadline(100 * time.Millisecond)
	dir := seedScratch(t, m, s1)

	res := snapshotWithin(t, m, snapshotCmd(), 5*time.Second)
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("SnapshotTrigger = %+v, want transfer-failed", res)
	}
	checker.mu.Lock()
	gotErr := checker.gotErr
	checker.mu.Unlock()
	if !errors.Is(gotErr, context.DeadlineExceeded) {
		t.Fatalf("fsck ctx err = %v, want context.DeadlineExceeded", gotErr)
	}
	tr.mu.Lock()
	packed := len(tr.snapshots) + len(tr.packs)
	tr.mu.Unlock()
	if packed != 0 {
		t.Fatalf("a snapshot whose fsck ran out the deadline was packed")
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("the failed snapshot's scratch is gone: %v", err)
	}
}

// A running server's pack stuck past the snapshot command's deadline (issue #3511)
// fails the command, and auto-save is still re-enabled. The deadline keeps the
// save-on restore's budget in hand, so the phases before it end that much earlier
// and the command answers within the deadline restore included.
func TestSnapshotPackStuckPastTheDeadlineFailsARunningSnapshot(t *testing.T) {
	ctrl := &fakeControl{reply: "ok"}
	tr := &fakeTransfer{blockPack: true}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	seedScratch(t, m, s1)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	m.SetTransferDeadline(restoreSaveTimeout + 100*time.Millisecond)

	res := snapshotWithin(t, m, snapshotCmd(), 5*time.Second)
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("SnapshotTrigger = %+v, want transfer-failed", res)
	}
	tr.mu.Lock()
	gotErr, uploads := tr.gotCtxErr, len(tr.uploads)
	tr.mu.Unlock()
	if !errors.Is(gotErr, context.DeadlineExceeded) {
		t.Fatalf("pack ctx err = %v, want context.DeadlineExceeded", gotErr)
	}
	if uploads != 0 {
		t.Fatalf("uploads = %d, want none after a failed pack", uploads)
	}
	if !containsLine(ctrl.lines, "save-on") {
		t.Fatalf("rcon lines = %v, want save-on after the pack ran out the deadline", ctrl.lines)
	}
}
