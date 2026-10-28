package instancemanager

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// The launch hand-over proves a multiply-linked file is the working set's own by
// counting its names, which only holds while no name moves under the walk. The
// Worker's own writers publish by renaming a temporary name into place, so each
// of them holds the server's working-set lock from before its temp exists until
// after the rename, and the driver is handed that same lock to hold while it
// walks (issue #3371).

// heldWhenTempDrawn replaces a temp-name seam with one that also records whether
// serverID's working-set lock is held at that point: after the writer began, and
// before it creates the temp it will rename.
func heldWhenTempDrawn(t *testing.T, m *Manager, serverID string, seam *func() string) *[]bool {
	t.Helper()
	orig := *seam
	t.Cleanup(func() { *seam = orig })
	lock := m.workingSetLock(serverID)
	var held []bool
	*seam = func() string {
		free := lock.TryLock()
		if free {
			lock.Unlock()
		}
		held = append(held, !free)
		return orig()
	}
	return &held
}

// assertHeldThenReleased fails unless the lock was held at every temp draw, at
// least one happened, and the lock is free again now.
func assertHeldThenReleased(t *testing.T, m *Manager, serverID string, held []bool) {
	t.Helper()
	if len(held) == 0 {
		t.Fatal("no temp name was drawn; the write under test did not run")
	}
	for i, h := range held {
		if !h {
			t.Fatalf("temp draw %d ran without the working-set lock: a hand-over could walk the "+
				"working set between this temp's creation and its rename", i)
		}
	}
	lock := m.workingSetLock(serverID)
	if !lock.TryLock() {
		t.Fatal("the working-set lock is still held after the command returned")
	}
	lock.Unlock()
}

func TestEditFileHoldsTheWorkingSetLockWhileItPublishes(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	target := writeWorkingFile(t, m, s1, "server.properties", []byte("motd=old"))
	held := heldWhenTempDrawn(t, m, s1, &editTempSuffix)

	res := handleWithin(t, m, session.Command{
		CommandID: "c1", ServerID: s1, Kind: "EditFile", Path: "server.properties", Content: []byte("motd=new"),
	})

	if !res.Success {
		t.Fatalf("EditFile = %+v, want success", res)
	}
	assertHeldThenReleased(t, m, s1, *held)
	if got, err := os.ReadFile(target); err != nil || string(got) != "motd=new" {
		t.Fatalf("the target reads %q (%v), want the edit published", got, err)
	}
}

// With nothing holding the lock an edit does not wait, so it does not consult its
// context either: it writes as it did before the lock existed.
func TestEditFileWritesWithoutWaitingWhenTheLockIsFree(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	target := writeWorkingFile(t, m, s1, "server.properties", []byte("motd=old"))
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	if res := m.Handle(ctx, editMotd()); !res.Success {
		t.Fatalf("EditFile = %+v, want success", res)
	}
	if got, err := os.ReadFile(target); err != nil || string(got) != "motd=new" {
		t.Fatalf("the target reads %q (%v), want the edit published", got, err)
	}
}

// A refused edit releases the lock too: here the target is a directory.
func TestEditFileReleasesTheWorkingSetLockWhenItFails(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	writeWorkingFile(t, m, s1, "world/level.dat", []byte("x"))

	res := handleWithin(t, m, session.Command{
		CommandID: "c1", ServerID: s1, Kind: "EditFile", Path: "world", Content: []byte("x"),
	})

	if res.Success {
		t.Fatalf("EditFile = %+v, want the directory refused", res)
	}
	lock := m.workingSetLock(s1)
	if !lock.TryLock() {
		t.Fatal("the working-set lock is still held after the refused edit returned")
	}
	lock.Unlock()
}

func TestHydrateStampHoldsTheWorkingSetLock(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{gen: 11})
	held := heldWhenTempDrawn(t, m, s1, &generationTempSuffix)

	if res := m.Handle(context.Background(), hydrateCmd()); !res.Success {
		t.Fatalf("HydrateTrigger = %+v, want success", res)
	}

	assertHeldThenReleased(t, m, s1, *held)
}

func TestRunningSnapshotStampHoldsTheWorkingSetLock(t *testing.T) {
	m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{gen: 12})
	seedScratch(t, m, s1)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	held := heldWhenTempDrawn(t, m, s1, &generationTempSuffix)

	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("SnapshotTrigger = %+v, want success", res)
	}

	assertHeldThenReleased(t, m, s1, *held)
}

// The driver walks the working set in places the manager cannot see — a Forge
// install's supervisor creates the launch container long after Start returned —
// so the lock travels to it in the spec, and it is the one the manager's own
// writers take for that server and no other.
func TestStartHandsTheDriverTheServersWorkingSetLock(t *testing.T) {
	d := &fakeDriver{}
	m := newManager(t, d, nil)
	seedScratch(t, m, s1)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}

	d.mu.Lock()
	got := d.started[0].WorkingSetLock
	d.mu.Unlock()
	if got != m.workingSetLock(s1) {
		t.Fatalf("spec.WorkingSetLock = %p, want server s1's working-set lock %p", got, m.workingSetLock(s1))
	}
	if m.workingSetLock(s1) == m.workingSetLock(s2) {
		t.Fatal("two servers share one working-set lock; a hand-over of one would hold up edits to the other")
	}
}

// --- waiting for a hand-over in progress -------------------------------------
//
// The tests below hold s1's lock the way a hand-over walk does. The manager's
// contention seam tells them a command has found the lock held and is about to
// wait, so nothing here sleeps to let a goroutine get somewhere.

// walkInProgress takes s1's working-set lock and returns it with a channel that
// receives once per command that found it held.
func walkInProgress(t *testing.T, m *Manager) (*execution.WorkingSetLock, <-chan struct{}) {
	t.Helper()
	lock := m.workingSetLock(s1)
	if !lock.TryLock() {
		t.Fatal("s1's working-set lock is already held")
	}
	waiting := make(chan struct{}, 8)
	m.workingSetLockContended = func(string) { waiting <- struct{}{} }
	return lock, waiting
}

// handleInBackground starts m.Handle(ctx, cmd) and returns where its result
// arrives. ctx must derive from t.Context(), so a test that fails while the walk
// still holds the lock ends the command's wait; a cleanup, registered after the
// manager's Close and so run before it, waits for the command to return (issue
// #3510).
func handleInBackground(ctx context.Context, t *testing.T, m *Manager, cmd session.Command) <-chan session.CommandResult {
	t.Helper()
	done := make(chan session.CommandResult, 1)
	var running sync.WaitGroup
	running.Go(func() { done <- m.Handle(ctx, cmd) })
	t.Cleanup(running.Wait)
	return done
}

// receiveWithin returns what ch delivers, and fails the test when nothing has
// arrived in time. Nothing here depends on how fast, only that it happens, so the
// budget is handleWithin's.
func receiveWithin[T any](t *testing.T, what string, ch <-chan T) T {
	t.Helper()
	select {
	case v := <-ch:
		return v
	case <-time.After(30 * time.Second):
		t.Fatalf("%s did not happen in time", what)
		var zero T
		return zero
	}
}

func editMotd() session.Command {
	return session.Command{
		CommandID: "c1", ServerID: s1, Kind: "EditFile", Path: "server.properties", Content: []byte("motd=new"),
	}
}

// assertEditNotWritten fails if the edit has touched its directory at all.
func assertEditNotWritten(t *testing.T, m *Manager, target string) {
	t.Helper()
	if got, err := os.ReadFile(target); err != nil || string(got) != "motd=old" {
		t.Fatalf("the target reads %q (%v), want it untouched", got, err)
	}
	assertNoEditTemp(t, m, filepath.Dir(target), "")
}

// An edit that meets a walk waits for it — creating nothing meanwhile — and
// publishes once the walk is done.
func TestEditFileWaitsForAHandOverAndThenPublishes(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	target := writeWorkingFile(t, m, s1, "server.properties", []byte("motd=old"))
	lock, waiting := walkInProgress(t, m)
	done := handleInBackground(t.Context(), t, m, editMotd())

	receiveWithin(t, "the edit's wait", waiting)
	assertEditNotWritten(t, m, target)
	lock.Unlock()

	if res := receiveWithin(t, "the edit's answer", done); !res.Success {
		t.Fatalf("EditFile = %+v, want success once the walk was done", res)
	}
	if got, err := os.ReadFile(target); err != nil || string(got) != "motd=new" {
		t.Fatalf("the target reads %q (%v), want the edit published", got, err)
	}
}

// An edit whose command goes away while it waits — the stream dropped — returns
// then, and writes nothing, then or later.
func TestEditFileGivesUpWaitingWhenItsCommandIsCancelled(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	target := writeWorkingFile(t, m, s1, "server.properties", []byte("motd=old"))
	lock, waiting := walkInProgress(t, m)
	ctx, cancel := context.WithCancel(t.Context())
	done := handleInBackground(ctx, t, m, editMotd())

	receiveWithin(t, "the edit's wait", waiting)
	cancel()

	res := receiveWithin(t, "the edit's answer", done)
	if res.Success || res.ErrorCode != session.CommandErrorInternal || !strings.Contains(res.ErrorMessage, context.Canceled.Error()) {
		t.Fatalf("EditFile = %+v, want it failed for the cancelled wait", res)
	}
	if lock.TryLock() {
		t.Fatal("the lock came free although the walk still holds it")
	}
	lock.Unlock()
	assertEditNotWritten(t, m, target)
}

// A walk that outlasts the wait does not keep the edit, or the lane behind it,
// for as long as it runs: the edit gives up, and the Stop queued after it — the
// command that ends a supervisor's walk — is handled.
func TestEditFileWaitIsBoundedSoAStopBehindItIsHandled(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	seedScratch(t, m, s1)
	target := writeWorkingFile(t, m, s1, "server.properties", []byte("motd=old"))
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	walkInProgress(t, m)
	m.workingSetLockWait = time.Millisecond

	res := handleWithin(t, m, editMotd())

	if res.Success || !strings.Contains(res.ErrorMessage, context.DeadlineExceeded.Error()) {
		t.Fatalf("EditFile = %+v, want it failed once the wait ran out", res)
	}
	assertEditNotWritten(t, m, target)
	stop := handleWithin(t, m, session.Command{CommandID: "stop", ServerID: s1, Kind: "StopServer"})
	if !stop.Success {
		t.Fatalf("StopServer after the edit = %+v, want it handled", stop)
	}
}

// A marker stamp that cannot get the lock is not written, and no generation is
// declared for it: the marker then reads older than the store, which costs one
// extra hydrate.
func TestHydrateStampIsSkippedWhenItsCommandIsCancelledWhileWaiting(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{gen: 11})
	_, waiting := walkInProgress(t, m)
	ctx, cancel := context.WithCancel(t.Context())
	done := handleInBackground(ctx, t, m, hydrateCmd())

	receiveWithin(t, "the stamp's wait", waiting)
	cancel()

	if res := receiveWithin(t, "the hydrate's answer", done); !res.Success || res.HeldGeneration != nil {
		t.Fatalf("HydrateTrigger = %+v, want success declaring no generation", res)
	}
	if _, err := os.Lstat(generationMarkerPath(m, s1)); !os.IsNotExist(err) {
		t.Fatalf("the marker: lstat err = %v, want it never written", err)
	}
}

func TestRunningSnapshotStampIsSkippedWhenItsCommandIsCancelledWhileWaiting(t *testing.T) {
	m := newManager(t, &fakeDriver{}, &fakeControl{reply: "ok"}).WithTransfer(&fakeTransfer{gen: 12})
	dir := seedScratch(t, m, s1)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v, want success", res)
	}
	_, waiting := walkInProgress(t, m)
	ctx, cancel := context.WithCancel(t.Context())
	done := handleInBackground(ctx, t, m, snapshotCmd())

	receiveWithin(t, "the stamp's wait", waiting)
	cancel()

	if res := receiveWithin(t, "the snapshot's answer", done); !res.Success || res.HeldGeneration != nil {
		t.Fatalf("SnapshotTrigger = %+v, want success declaring no generation", res)
	}
	if got := readGeneration(dir); got != 0 {
		t.Fatalf("recorded generation = %d, want the marker left at 0", got)
	}
}
