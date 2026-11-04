package instancemanager

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/regioncheck"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// These tests pin the snapshot command's deadline (issue #3511): the command
// answers at its deadline whatever its body is blocked on, the abandoned body
// keeps the id's claim until it exits, and it deletes nothing once abandoned.
// Each one stalls one call the way storage that stops answering would, waits for
// the body to reach it, and only then ends the deadline (snapshotExpiry), so no
// assertion depends on timing.

// stall blocks every caller until released and reports the first entry.
type stall struct {
	entered chan struct{}
	release chan struct{}
	enter   sync.Once
	open    sync.Once
}

// newStall returns a stall the test releases when it ends at the latest.
func newStall(t *testing.T) *stall {
	s := &stall{entered: make(chan struct{}), release: make(chan struct{})}
	t.Cleanup(s.unblock)
	return s
}

func (s *stall) wait() {
	s.enter.Do(func() { close(s.entered) })
	<-s.release
}

func (s *stall) unblock() { s.open.Do(func() { close(s.release) }) }

// awaitSnapshotBodyExit returns a channel that receives once per snapshot body
// that has exited and released its claim.
func awaitSnapshotBodyExit(m *Manager) <-chan struct{} {
	exited := make(chan struct{}, 8)
	m.snapshotBodyExited = func(string) { exited <- struct{}{} }
	return exited
}

// snapshotExpiry replaces the snapshot command's deadline with one the test ends:
// the returned call ends the deadline of the command in flight.
func snapshotExpiry(t *testing.T, m *Manager) func() {
	cancels := make(chan context.CancelFunc, 8)
	m.snapshotContextHook = func(ctx context.Context, _ bool) (context.Context, context.CancelFunc) {
		c, cancel := context.WithCancel(ctx)
		cancels <- cancel
		return c, cancel
	}
	return func() {
		select {
		case cancel := <-cancels:
			cancel()
		case <-time.After(5 * time.Second):
			t.Fatal("no snapshot command took a deadline")
		}
	}
}

// goHandle dispatches cmd on its own goroutine.
func goHandle(m *Manager, cmd session.Command) <-chan session.CommandResult {
	done := make(chan session.CommandResult, 1)
	go func() { done <- m.Handle(context.Background(), cmd) }()
	return done
}

// awaitAnswer fails the test unless the command answers within 5 s.
func awaitAnswer(t *testing.T, done <-chan session.CommandResult) session.CommandResult {
	t.Helper()
	select {
	case res := <-done:
		return res
	case <-time.After(5 * time.Second):
		t.Fatal("the snapshot command did not answer while its body was blocked")
		return session.CommandResult{}
	}
}

func assertFailed(t *testing.T, res session.CommandResult) {
	t.Helper()
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("SnapshotTrigger = %+v, want transfer-failed", res)
	}
}

// assertBusy fails unless every cmd is refused BUSY.
func assertBusy(t *testing.T, m *Manager, cmds ...session.Command) {
	t.Helper()
	for _, cmd := range cmds {
		if res := m.Handle(context.Background(), cmd); res.Success || res.ErrorCode != session.CommandErrorBusy {
			t.Fatalf("%s while the abandoned body is alive = %+v, want busy", cmd.Kind, res)
		}
	}
}

// stalledChecker is a region check stuck on storage: it ignores its context.
type stalledChecker struct{ s *stall }

func (c stalledChecker) CheckWorkingSet(context.Context, string) (regioncheck.Report, error) {
	c.s.wait()
	return regioncheck.Report{}, nil
}

// abandonStoppedSnapshot runs a stopped id's snapshot until st is entered, ends
// its deadline, and checks the command answered a failure while the body is held.
// The abandoned body then keeps the reservation: a start, a hydrate and another
// snapshot are refused BUSY until st is released and the body has exited. The
// failed final snapshot keeps its scratch.
func abandonStoppedSnapshot(t *testing.T, m *Manager, st *stall) {
	t.Helper()
	dir := seedScratch(t, m, s1)
	displaced := seedDisplaced(t, m, s1)
	exited := awaitSnapshotBodyExit(m)
	expire := snapshotExpiry(t, m)

	done := goHandle(m, snapshotCmd())
	awaitEnter(t, st.entered)
	expire()
	assertFailed(t, awaitAnswer(t, done))
	assertBusy(t, m, startCmd(), hydrateCmd(), snapshotCmd())

	st.unblock()
	awaitEnter(t, exited)
	for _, path := range []string{dir, displaced} {
		if _, err := os.Stat(path); err != nil {
			t.Fatalf("the abandoned snapshot deleted %s: %v", path, err)
		}
	}
	if res := m.Handle(context.Background(), hydrateCmd()); !res.Success {
		t.Fatalf("HydrateTrigger after the body exited = %+v, want the id free again", res)
	}
}

func TestStoppedSnapshotAnswersWhileListingTheWorkingDirIsStuck(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{})
	st := newStall(t)
	m.readWorkingDir = func(dir string) ([]os.DirEntry, error) {
		st.wait()
		return os.ReadDir(dir)
	}
	abandonStoppedSnapshot(t, m, st)
}

func TestStoppedSnapshotAnswersWhileReadingTheGenerationMarkerIsStuck(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{})
	st := newStall(t)
	m.readGeneration = func(dir, id string, log *slog.Logger) uint64 {
		st.wait()
		return recordedGeneration(dir, id, log)
	}
	abandonStoppedSnapshot(t, m, st)
}

func TestStoppedSnapshotAnswersWhileTheFsckIsStuck(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{})
	st := newStall(t)
	m.regions = stalledChecker{st}
	abandonStoppedSnapshot(t, m, st)
}

// The pack and upload are stuck — a spool create, the upload's open, the spool
// unlink — and once released the upload publishes. The body had been abandoned,
// so it skips the scratch GC: the scratch and the displaced tree stay.
func TestStoppedSnapshotAnswersWhileThePackAndUploadAreStuck(t *testing.T) {
	st := newStall(t)
	tr := &fakeTransfer{gen: 7, stallSnapshot: st.wait}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	abandonStoppedSnapshot(t, m, st)
	tr.mu.Lock()
	defer tr.mu.Unlock()
	if len(tr.snapshots) != 1 {
		t.Fatalf("snapshots = %v, want the one released after abandonment", tr.snapshots)
	}
}

// redialControl is a quiesce connection that reconnects with its own credentials
// (rcon.Client.Redial), handing out fresh controls the test can inspect.
type redialControl struct {
	*fakeControl
	mu       sync.Mutex
	redialed []*fakeControl
}

func (c *redialControl) Redial(context.Context) (execution.ServerControl, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	fresh := &fakeControl{reply: "ok"}
	c.redialed = append(c.redialed, fresh)
	return fresh, nil
}

// redialedLines returns the lines every redialed control received.
func (c *redialControl) redialedLines() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	var lines []string
	for _, f := range c.redialed {
		lines = append(lines, f.lines...)
	}
	return lines
}

// newRunningManager starts s1 on a manager whose RCON open hands out ctrl once and
// stalls any later open, as a read of server.properties on stuck storage would.
func newRunningManager(t *testing.T, ctrl *redialControl, tr *fakeTransfer) *Manager {
	t.Helper()
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)
	m.fsckRetryDelay = 0 // the restore's redial backoff
	startRunning(t, m)
	reopen := newStall(t)
	var mu sync.Mutex
	opens := 0
	m.openControl = func(context.Context, string, string, string) (execution.ServerControl, error) {
		mu.Lock()
		opens++
		n := opens
		mu.Unlock()
		if n > 1 {
			reopen.wait()
		}
		return ctrl, nil
	}
	return m
}

// abandonRunningSnapshot runs s1's snapshot until st is entered, ends its
// deadline, and checks that the command answered a failure with save-on restored
// over a redialed connection. While the body is held a second snapshot is BUSY and
// a stop still proceeds. Once released, the body stamps no marker and sweeps no
// displaced tree.
func abandonRunningSnapshot(t *testing.T, m *Manager, ctrl *redialControl, st *stall) {
	t.Helper()
	dir := filepath.Join(m.scratchDir, s1)
	displaced := seedDisplaced(t, m, s1)
	exited := awaitSnapshotBodyExit(m)
	expire := snapshotExpiry(t, m)

	done := goHandle(m, snapshotCmd())
	awaitEnter(t, st.entered)
	expire()
	assertFailed(t, awaitAnswer(t, done))
	if !containsLine(ctrl.redialedLines(), "save-on") {
		t.Fatalf("redialed rcon lines = %v, want save-on restored by the command", ctrl.redialedLines())
	}
	assertBusy(t, m, snapshotCmd())
	if res := m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: s1, Kind: "StopServer"}); !res.Success {
		t.Fatalf("StopServer over the abandoned snapshot = %+v, want it to proceed", res)
	}

	st.unblock()
	awaitEnter(t, exited)
	if _, err := os.Stat(displaced); err != nil {
		t.Fatalf("the abandoned snapshot swept the displaced tree: %v", err)
	}
	if got := recordedGeneration(dir, s1, m.logger); got != 0 {
		t.Fatalf("marker = %d after the abandoned snapshot, want it unstamped (0)", got)
	}
}

func TestRunningSnapshotAnswersWhileTheSettleScanIsStuck(t *testing.T) {
	ctrl := &redialControl{fakeControl: &fakeControl{reply: "ok"}}
	m := newRunningManager(t, ctrl, &fakeTransfer{gen: 7})
	st := newStall(t)
	m.scanRegion = func(root string) (regionState, error) {
		st.wait()
		return scanRegionState(root)
	}
	abandonRunningSnapshot(t, m, ctrl, st)
}

func TestRunningSnapshotAnswersWhileThePackIsStuck(t *testing.T) {
	ctrl := &redialControl{fakeControl: &fakeControl{reply: "ok"}}
	st := newStall(t)
	m := newRunningManager(t, ctrl, &fakeTransfer{gen: 7, stallPack: st.wait})
	abandonRunningSnapshot(t, m, ctrl, st)
}

func TestRunningSnapshotAnswersWhileTheUploadIsStuck(t *testing.T) {
	ctrl := &redialControl{fakeControl: &fakeControl{reply: "ok"}}
	st := newStall(t)
	m := newRunningManager(t, ctrl, &fakeTransfer{gen: 7, stallUpload: st.wait})
	abandonRunningSnapshot(t, m, ctrl, st)
}

func TestRunningSnapshotAnswersWhileTheSpoolUnlinkIsStuck(t *testing.T) {
	ctrl := &redialControl{fakeControl: &fakeControl{reply: "ok"}}
	st := newStall(t)
	m := newRunningManager(t, ctrl, &fakeTransfer{uploadErr: errors.New("upload boom"), stallCleanup: st.wait})
	abandonRunningSnapshot(t, m, ctrl, st)
}

// The save-on restore reconnects with the credentials the quiesce resolved, not
// by reading server.properties again (issue #3511): a stuck reread would hold the
// restore, and the command with it.
func TestRunningSnapshotRestoreDoesNotReopenFromTheWorkingDir(t *testing.T) {
	ctrl := &redialControl{fakeControl: &fakeControl{reply: "ok", failLines: map[string]error{"save-on": errors.New("save-on boom")}}}
	m := newRunningManager(t, ctrl, &fakeTransfer{gen: 7})
	res := awaitAnswer(t, goHandle(m, snapshotCmd()))
	if !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}
	if !containsLine(ctrl.redialedLines(), "save-on") {
		t.Fatalf("redialed rcon lines = %v, want save-on retried on a redialed connection", ctrl.redialedLines())
	}
}

// When the deadline passes after the upload published, the command answers
// success, declaring no generation (issue #2481), and the body finishes its tail
// under its claim. The marker stamp is held here on the working-set lock.
func TestRunningSnapshotCommittedBeforeTheDeadlineAnswersSuccess(t *testing.T) {
	ctrl := &redialControl{fakeControl: &fakeControl{reply: "ok"}}
	m := newRunningManager(t, ctrl, &fakeTransfer{gen: 7})
	contended := make(chan struct{}, 1)
	m.workingSetLockContended = func(string) { contended <- struct{}{} }
	lock := m.workingSetLock(s1)
	if !lock.TryLock() {
		t.Fatal("the working-set lock is already held")
	}
	exited := awaitSnapshotBodyExit(m)
	expire := snapshotExpiry(t, m)

	done := goHandle(m, snapshotCmd())
	awaitEnter(t, contended)
	expire()
	res := awaitAnswer(t, done)
	if !res.Success || res.HeldGeneration != nil {
		t.Fatalf("SnapshotTrigger = %+v, want success declaring no generation", res)
	}
	assertBusy(t, m, snapshotCmd())

	lock.Unlock()
	awaitEnter(t, exited)
	if got := recordedGeneration(filepath.Join(m.scratchDir, s1), s1, m.logger); got != 7 {
		t.Fatalf("marker = %d, want the committed body's stamp (7)", got)
	}
}

// With the real clock: a stopped id's stuck pack answers at the transfer deadline.
func TestStoppedSnapshotAnswersAtTheTransferDeadline(t *testing.T) {
	st := newStall(t)
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{stallSnapshot: st.wait})
	seedScratch(t, m, s1)
	exited := awaitSnapshotBodyExit(m)
	m.SetTransferDeadline(100 * time.Millisecond)
	assertFailed(t, awaitAnswer(t, goHandle(m, snapshotCmd())))
	st.unblock()
	awaitEnter(t, exited)
}

// With the real clock: a running snapshot's phases end restoreSaveTimeout before
// the transfer deadline, so the restore fits inside it and the command answers
// within the deadline, not restoreSaveTimeout after it.
func TestRunningSnapshotAnswersWithinTheTransferDeadline(t *testing.T) {
	ctrl := &redialControl{fakeControl: &fakeControl{reply: "ok"}}
	st := newStall(t)
	m := newRunningManager(t, ctrl, &fakeTransfer{stallPack: st.wait})
	exited := awaitSnapshotBodyExit(m)
	m.SetTransferDeadline(restoreSaveTimeout + 100*time.Millisecond)
	assertFailed(t, awaitAnswer(t, goHandle(m, snapshotCmd())))
	st.unblock()
	awaitEnter(t, exited)
}
