package instancemanager

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// fakeTransfer records hydrate/snapshot calls and returns a canned error. When
// seq is set, Snapshot appends a "transfer" marker to it so a test can assert the
// copy is ordered between the RCON save-off / save-on bracket (#694).
type fakeTransfer struct {
	mu        sync.Mutex
	hydrated  []string // workingDir args
	snapshots []string
	// snapshotHadWorkingSet records, per Snapshot call, whether the working dir
	// held a real working set at the moment the pack ran (issue #841): a graceful
	// stop must not GC the scratch before the post-stop final SnapshotTrigger packs
	// it, or the snapshot captures an empty/absent dir and is silently lost.
	snapshotHadWorkingSet []bool
	err                   error
	seq                   *[]string
	// gen is the store generation Hydrate/Snapshot report (issue #763); 0 by
	// default. The manager records it in the working set's generation marker.
	gen uint64
	// cancelDuringSnapshot, when set, is invoked at the start of Snapshot to model
	// the request context being cancelled mid-transfer; Snapshot then returns
	// context.Canceled. It proves the deferred save-on still runs (#694).
	cancelDuringSnapshot context.CancelFunc
	// snapshotBaseGenerations records, per Snapshot call, the base generation the
	// manager declared (issue #847): the store generation the set was hydrated from.
	snapshotBaseGenerations []uint64
	// snapshotWorkerIDs records, per Snapshot call, the worker id the manager
	// declared (issue #847 bug 3).
	snapshotWorkerIDs []string
	// blockUntilCtxDone, when set, makes Hydrate/Snapshot block until the passed
	// context is cancelled and then return ctx.Err(), modeling a stalled transfer
	// the per-transfer deadline (issue #874) must abort. gotCtxErr captures the
	// error the blocked transfer observed so a test can assert it was the deadline.
	blockUntilCtxDone bool
	gotCtxErr         error
}

func (f *fakeTransfer) Hydrate(ctx context.Context, _, _, workingDir string) (uint64, error) {
	f.mu.Lock()
	f.hydrated = append(f.hydrated, workingDir)
	block := f.blockUntilCtxDone
	f.mu.Unlock()
	if block {
		<-ctx.Done()
		f.mu.Lock()
		f.gotCtxErr = ctx.Err()
		f.mu.Unlock()
		return 0, ctx.Err()
	}
	return f.gen, f.err
}

func (f *fakeTransfer) Snapshot(ctx context.Context, _, _, workingDir string, baseGeneration uint64, workerID string) (uint64, error) {
	f.mu.Lock()
	if f.blockUntilCtxDone {
		f.snapshots = append(f.snapshots, workingDir)
		f.mu.Unlock()
		<-ctx.Done()
		f.mu.Lock()
		f.gotCtxErr = ctx.Err()
		f.mu.Unlock()
		return 0, ctx.Err()
	}
	f.snapshots = append(f.snapshots, workingDir)
	f.snapshotHadWorkingSet = append(f.snapshotHadWorkingSet, hasWorkingSet(workingDir))
	f.snapshotBaseGenerations = append(f.snapshotBaseGenerations, baseGeneration)
	f.snapshotWorkerIDs = append(f.snapshotWorkerIDs, workerID)
	if f.seq != nil {
		*f.seq = append(*f.seq, "transfer")
	}
	if f.cancelDuringSnapshot != nil {
		f.cancelDuringSnapshot()
		return 0, context.Canceled
	}
	return f.gen, f.err
}

// equalLines reports whether two RCON-line slices are element-wise equal.
func equalLines(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}

// containsLine reports whether line appears in lines.
func containsLine(lines []string, line string) bool {
	for _, l := range lines {
		if l == line {
			return true
		}
	}
	return false
}

func hydrateCmd() session.Command {
	return session.Command{
		CommandID: "h1", ServerID: "s1", Kind: "HydrateTrigger",
		TransferURL: "https://api/working-set", TransferToken: "tok",
	}
}

func snapshotCmd() session.Command {
	return session.Command{
		CommandID: "p1", ServerID: "s1", Kind: "SnapshotTrigger",
		TransferURL: "https://api/snapshot", TransferToken: "tok",
	}
}

func TestHydrateTriggerPullsIntoWorkingDir(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	res := m.Handle(context.Background(), hydrateCmd())
	if !res.Success {
		t.Fatalf("HydrateTrigger result = %+v, want success", res)
	}
	want := filepath.Join(m.scratchDir, "s1")
	if len(tr.hydrated) != 1 || tr.hydrated[0] != want {
		t.Fatalf("hydrated = %v, want [%q]", tr.hydrated, want)
	}
}

func TestHydrateTriggerOnRunningServerIsInvalidState(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start failed: %+v", res)
	}
	res := m.Handle(context.Background(), hydrateCmd())
	if res.Success || res.ErrorCode != session.CommandErrorInvalidState {
		t.Fatalf("HydrateTrigger on running server = %+v, want invalid-state failure", res)
	}
	if len(tr.hydrated) != 0 {
		t.Fatal("a running server must not be hydrated")
	}
}

func TestHydrateTriggerTransferFailureIsCoded(t *testing.T) {
	tr := &fakeTransfer{err: errors.New("boom")}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	res := m.Handle(context.Background(), hydrateCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("HydrateTrigger failure = %+v, want transfer-failed", res)
	}
}

func TestHydrateTriggerWithoutTransferClientFails(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil) // no WithTransfer
	res := m.Handle(context.Background(), hydrateCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("HydrateTrigger with no client = %+v, want transfer-failed", res)
	}
}

func TestSnapshotTriggerPacksWorkingDir(t *testing.T) {
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	res := m.Handle(context.Background(), snapshotCmd())
	if !res.Success {
		t.Fatalf("SnapshotTrigger result = %+v, want success", res)
	}
	want := filepath.Join(m.scratchDir, "s1")
	if len(tr.snapshots) != 1 || tr.snapshots[0] != want {
		t.Fatalf("snapshots = %v, want [%q]", tr.snapshots, want)
	}
}

func TestSnapshotTriggerTransferFailureIsCoded(t *testing.T) {
	tr := &fakeTransfer{err: errors.New("boom")}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)

	res := m.Handle(context.Background(), snapshotCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("SnapshotTrigger failure = %+v, want transfer-failed", res)
	}
}

// A running-server snapshot brackets the working-dir copy with save-off →
// save-all flush → save-on (#694/#907): save-off disables auto-save so a region
// file cannot be captured torn mid-copy, save-all flush blocks until the world is
// fully on disk (a plain non-blocking save-all returns before the async save
// completes, so the fsck would race in-flight writes — the #907 false positive),
// and save-on re-enables auto-save afterwards.
func TestSnapshotTriggerRunningServerBracketsCopyWithSaveOffOn(t *testing.T) {
	ctrl := &fakeControl{reply: "ok"}
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("SnapshotTrigger result = %+v, want success", res)
	}
	want := []string{"save-off", "save-all flush", "save-on"}
	if !equalLines(ctrl.lines, want) {
		t.Fatalf("rcon lines = %v, want %v", ctrl.lines, want)
	}
}

// save-off must precede the transfer (the world is quiesced before the copy
// starts) and save-on must follow it (auto-save is only re-enabled after the copy
// completes) — otherwise the bracket would not actually protect the copy window.
func TestSnapshotTriggerRunningServerSaveOffBracketsTheTransfer(t *testing.T) {
	var seq []string
	ctrl := &fakeControl{reply: "ok", seq: &seq}
	tr := &fakeTransfer{seq: &seq}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("SnapshotTrigger result = %+v, want success", res)
	}
	want := []string{"save-off", "save-all flush", "transfer", "save-on"}
	if !equalLines(seq, want) {
		t.Fatalf("operation order = %v, want %v", seq, want)
	}
}

// save-on MUST still run when the transfer itself fails: the deferred restore
// re-enables auto-save before the failed result is returned, so a transfer error
// never leaves the server with auto-save disabled (#694).
func TestSnapshotTriggerRunningServerSaveOnRunsOnTransferError(t *testing.T) {
	var seq []string
	ctrl := &fakeControl{reply: "ok", seq: &seq}
	tr := &fakeTransfer{seq: &seq, err: errors.New("boom")}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	res := m.Handle(context.Background(), snapshotCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("SnapshotTrigger = %+v, want transfer-failed", res)
	}
	want := []string{"save-off", "save-all flush", "transfer", "save-on"}
	if !equalLines(seq, want) {
		t.Fatalf("operation order = %v, want %v (save-on must run after a failed transfer)", seq, want)
	}
}

// save-on MUST still run when the request context is already cancelled: the
// deferred restore runs on a context detached from the request's, so a
// cancelled/timed-out snapshot still re-enables auto-save rather than leaving the
// server unable to persist (#694).
func TestSnapshotTriggerRunningServerSaveOnRunsWhenContextCancelled(t *testing.T) {
	// failOnCancelled makes the RCON Execute fail on a dead context, so save-on can
	// only succeed if the restore runs on a live, detached context.
	ctrl := &fakeControl{reply: "ok", failOnCancelled: true}
	m := newManager(t, &fakeDriver{}, ctrl)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}

	ctx, cancel := context.WithCancel(context.Background())
	// The transfer cancels the request context mid-copy (a cancelled/timed-out
	// snapshot), after save-off has already disabled auto-save.
	m.WithTransfer(&fakeTransfer{cancelDuringSnapshot: cancel})

	res := m.Handle(ctx, snapshotCmd())
	if res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want failure under cancelled context", res)
	}
	// save-on ran (it would have errored on the cancelled request context, so its
	// presence proves the restore used a live, detached context).
	if !containsLine(ctrl.lines, "save-on") {
		t.Fatalf("rcon lines = %v, want a save-on (restore must run on a detached context)", ctrl.lines)
	}
}

// When save-off fails the running world is NOT quiesced, so the periodic snapshot
// is refused fail-closed (quiesce_unavailable, #907) rather than packing the live
// world — that unquiesced pack is exactly what produced the 35/35 torn-read false
// positives. No upload happens, and no save-on is issued (auto-save was never
// disabled, so re-enabling it would be wrong). The next tick retries.
func TestSnapshotTriggerRunningServerSaveOffFailureRefusesQuiesceUnavailable(t *testing.T) {
	ctrl := &fakeControl{err: errors.New("rcon: read length: EOF")}
	tr := &fakeTransfer{}
	m := newManager(t, &fakeDriver{}, ctrl).WithTransfer(tr)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	res := m.Handle(context.Background(), snapshotCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("SnapshotTrigger result = %+v, want transfer-failed (quiesce_unavailable)", res)
	}
	if !strings.Contains(res.ErrorMessage, "quiesce_unavailable") {
		t.Fatalf("error message = %q, want it to name quiesce_unavailable", res.ErrorMessage)
	}
	if len(tr.snapshots) != 0 {
		t.Fatalf("unquiesced world must not be packed; snapshots = %v", tr.snapshots)
	}
	if containsLine(ctrl.lines, "save-on") {
		t.Fatalf("rcon lines = %v, want no save-on when save-off never succeeded", ctrl.lines)
	}
}

// A snapshot upload that exceeds the per-transfer deadline (issue #874) is
// aborted Worker-side: SetTransferDeadline bounds the transfer's context, so a
// stalled upload returns a deadline error rather than hanging the lane forever
// (the unbounded-upload case #869 recovers from API-side).
func TestSnapshotTriggerUploadExceedingDeadlineAborts(t *testing.T) {
	tr := &fakeTransfer{blockUntilCtxDone: true}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	m.SetTransferDeadline(20 * time.Millisecond)

	res := m.Handle(context.Background(), snapshotCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("SnapshotTrigger over deadline = %+v, want transfer-failed", res)
	}
	tr.mu.Lock()
	gotErr := tr.gotCtxErr
	tr.mu.Unlock()
	if !errors.Is(gotErr, context.DeadlineExceeded) {
		t.Fatalf("transfer ctx err = %v, want context.DeadlineExceeded", gotErr)
	}
}

// A hydrate download is bounded symmetrically (issue #874): the same
// per-transfer deadline aborts a stalled download.
func TestHydrateTriggerDownloadExceedingDeadlineAborts(t *testing.T) {
	tr := &fakeTransfer{blockUntilCtxDone: true}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	m.SetTransferDeadline(20 * time.Millisecond)

	res := m.Handle(context.Background(), hydrateCmd())
	if res.Success || res.ErrorCode != session.CommandErrorTransferFailed {
		t.Fatalf("HydrateTrigger over deadline = %+v, want transfer-failed", res)
	}
	tr.mu.Lock()
	gotErr := tr.gotCtxErr
	tr.mu.Unlock()
	if !errors.Is(gotErr, context.DeadlineExceeded) {
		t.Fatalf("transfer ctx err = %v, want context.DeadlineExceeded", gotErr)
	}
}

// With no transfer deadline set (an older API that omits the RegisterAck field,
// or before the first ack), a transfer runs unbounded — the prior behavior. The
// blocked transfer only returns when the request context itself is cancelled.
func TestSnapshotTriggerWithoutDeadlineRunsUnbounded(t *testing.T) {
	tr := &fakeTransfer{blockUntilCtxDone: true}
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(tr)
	// No SetTransferDeadline call: the bound is 0 (unbounded).

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan session.CommandResult, 1)
	go func() { done <- m.Handle(ctx, snapshotCmd()) }()

	// The transfer must still be blocked (no deadline fired); cancelling the
	// request context is the only thing that releases it.
	select {
	case res := <-done:
		t.Fatalf("snapshot returned %+v before the request context was cancelled; deadline must be unbounded", res)
	case <-time.After(50 * time.Millisecond):
	}
	cancel()
	res := <-done
	if res.Success {
		t.Fatalf("snapshot = %+v, want failure after request cancel", res)
	}
	tr.mu.Lock()
	gotErr := tr.gotCtxErr
	tr.mu.Unlock()
	if !errors.Is(gotErr, context.Canceled) {
		t.Fatalf("transfer ctx err = %v, want context.Canceled (request cancel, not a deadline)", gotErr)
	}
}
