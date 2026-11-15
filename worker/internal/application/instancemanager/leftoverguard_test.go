package instancemanager

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// fakeGuard is a driver's execution.LeftoverGuard: what it answers, and a log of
// the calls it took.
type fakeGuard struct {
	mu      sync.Mutex
	endErr  error
	busyErr error
	calls   []string
	// onEnd, when set, runs inside EndLeftovers, so a test can look at the
	// working set at the moment the leftover is ended.
	onEnd func()
}

func (g *fakeGuard) EndLeftovers(_ context.Context, serverID, workingDir string) error {
	g.mu.Lock()
	g.calls = append(g.calls, "end "+serverID+" "+workingDir)
	hook, err := g.onEnd, g.endErr
	g.mu.Unlock()
	if hook != nil {
		hook()
	}
	return err
}

// Busy fails on a context that has ended, as the container driver's does: its
// List cannot answer on one.
func (g *fakeGuard) Busy(ctx context.Context, serverID string) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.calls = append(g.calls, "busy "+serverID)
	if err := ctx.Err(); err != nil {
		return err
	}
	return g.busyErr
}

func (g *fakeGuard) set(endErr, busyErr error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.endErr, g.busyErr = endErr, busyErr
}

func (g *fakeGuard) callLog() []string {
	g.mu.Lock()
	defer g.mu.Unlock()
	return append([]string(nil), g.calls...)
}

// guardedDriver is a driver that can leave a server's process running
// untracked: its Start is the wrapped driver's, and it answers LeftoverGuard.
type guardedDriver struct {
	execution.ExecutionDriver
	*fakeGuard
}

var errLeftover = errors.New("test: container container-1, which a failed start may have left running, is not removed")

func newGuardedManager(t *testing.T) (*Manager, *fakeGuard, *fakeTransfer) {
	t.Helper()
	g := &fakeGuard{}
	tr := &fakeTransfer{}
	m := newManager(t, guardedDriver{&fakeDriver{}, g}, nil).WithTransfer(tr)
	return m, g, tr
}

func hydrateCount(tr *fakeTransfer) int {
	tr.mu.Lock()
	defer tr.mu.Unlock()
	return len(tr.hydrated)
}

func packCount(tr *fakeTransfer) int {
	tr.mu.Lock()
	defer tr.mu.Unlock()
	return len(tr.packs)
}

// A hydrate over a stopped server first ends what the driver left running for
// it, then asks whether anything still may be, and only then writes the working
// set (issue #3672). A leftover's graceful stop saves onto the working set as it
// is then, so ending it after the hydrate would overwrite the fresh copy.
func TestHydrateEndsLeftoversBeforeItWritesTheWorkingSet(t *testing.T) {
	m, g, tr := newGuardedManager(t)
	dir := filepath.Join(m.scratchDir, s1)
	hydratedAtEnd := -1
	g.onEnd = func() { hydratedAtEnd = hydrateCount(tr) }

	if res := m.Handle(context.Background(), hydrateCmd()); !res.Success {
		t.Fatalf("hydrate = %+v, want success", res)
	}
	if want := []string{"end " + s1 + " " + dir, "busy " + s1}; !slices.Equal(g.callLog(), want) {
		t.Fatalf("guard calls = %v, want %v", g.callLog(), want)
	}
	if hydratedAtEnd != 0 || hydrateCount(tr) != 1 {
		t.Fatalf("hydrates when the leftover was ended = %d, after = %d; want 0 then 1", hydratedAtEnd, hydrateCount(tr))
	}
}

// A hydrate is refused BUSY, and writes nothing, while a container the manager
// does not track may be running against the working set: a leftover that could
// not be ended, a running container of the server the driver lists, or an answer
// the driver could not get (issue #3672). The reservation is released, so the
// retry goes ahead once the driver reports the server clear.
func TestHydrateRefusedBusyWhileAnUntrackedContainerMayRun(t *testing.T) {
	for _, tc := range []struct {
		name            string
		endErr, busyErr error
	}{
		// A leftover the driver failed to end refuses on its own, whatever Busy
		// would answer.
		{name: "leftover recorded and not ended", endErr: errLeftover},
		{name: "untracked container running", busyErr: errors.New("test: container /mcsd-s1 of the server is \"running\"; its working set is in use")},
		{name: "busy query fails", busyErr: errors.New("test: cannot establish that no container of the server is running: daemon down")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m, g, tr := newGuardedManager(t)
			g.set(tc.endErr, tc.busyErr)

			res := m.Handle(context.Background(), hydrateCmd())
			if res.Success || res.ErrorCode != session.CommandErrorBusy {
				t.Fatalf("hydrate = %+v, want BUSY", res)
			}
			want := tc.busyErr
			if tc.endErr != nil {
				want = tc.endErr
			}
			if !strings.Contains(res.ErrorMessage, want.Error()) {
				t.Fatalf("message = %q, want it to carry %q", res.ErrorMessage, want)
			}
			if n := hydrateCount(tr); n != 0 {
				t.Fatalf("hydrates = %d, want none while the working set may be in use", n)
			}

			g.set(nil, nil)
			if res := m.Handle(context.Background(), hydrateCmd()); !res.Success {
				t.Fatalf("retry = %+v, want success once the server is clear", res)
			}
		})
	}
}

// A stopped server's snapshot is refused BUSY, and packs nothing, while a
// container the manager does not track may be running against the working set
// (issue #3672). It does not end the leftover: the snapshot is no precondition
// of the start that ends it, and the leftover's own shutdown save would be what
// it packed. The reservation is released, so the retry goes ahead once the
// server is clear.
func TestStoppedSnapshotRefusedBusyWhileAnUntrackedContainerMayRun(t *testing.T) {
	for _, tc := range []struct {
		name    string
		busyErr error
	}{
		{name: "leftover recorded", busyErr: errLeftover},
		{name: "untracked container running", busyErr: errors.New("test: container /mcsd-s1 of the server is \"running\"; its working set is in use")},
		{name: "busy query fails", busyErr: errors.New("test: cannot establish that no container of the server is running: daemon down")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m, g, tr := newGuardedManager(t)
			seedScratch(t, m, s1)
			g.set(nil, tc.busyErr)

			res := m.Handle(context.Background(), snapshotCmd())
			if res.Success || res.ErrorCode != session.CommandErrorBusy ||
				!strings.Contains(res.ErrorMessage, tc.busyErr.Error()) {
				t.Fatalf("snapshot = %+v, want BUSY carrying %q", res, tc.busyErr)
			}
			if n := packCount(tr); n != 0 {
				t.Fatalf("packs = %d, want none while the working set may be in use", n)
			}
			if want := []string{"busy " + s1}; !slices.Equal(g.callLog(), want) {
				t.Fatalf("guard calls = %v, want %v: no leftover ended by a snapshot", g.callLog(), want)
			}

			g.set(nil, nil)
			if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
				t.Fatalf("retry = %+v, want success once the server is clear", res)
			}
		})
	}
}

// The running-server paths ask the guard nothing: a running instance already
// refuses the hydrate, and a running snapshot quiesces with its save-off
// bracket, so neither pays a daemon round-trip (issue #3672).
func TestRunningServerPathsAskTheGuardNothing(t *testing.T) {
	g := &fakeGuard{busyErr: errLeftover, endErr: errLeftover}
	tr := &fakeTransfer{}
	m := newManagerWithControls(t, guardedDriver{&fakeDriver{}, g}, &fakeControl{reply: "ok"}).WithTransfer(tr)
	startRunning(t, m)

	if res := m.Handle(context.Background(), hydrateCmd()); res.Success || res.ErrorCode != session.CommandErrorInvalidState {
		t.Fatalf("hydrate of a running server = %+v, want INVALID_STATE", res)
	}
	if res := m.Handle(context.Background(), snapshotCmd()); !res.Success {
		t.Fatalf("snapshot of a running server = %+v, want success", res)
	}
	if calls := g.callLog(); len(calls) != 0 {
		t.Fatalf("guard calls = %v, want none on the running-server paths", calls)
	}
}

// A launch a resync reported `starting` that fails while the driver may have
// left its container running reports `crashed`, not `stopped` (issue #3672): a
// container may still be running, and `stopped` would say none is. The
// reconciler redispatches the start either way, and that start ends the
// leftover. With the server clear it still reports `stopped`, a cancelled launch
// included: the question is not asked on the launch's own context.
func TestResyncReportedLaunchThatFailsReportsCrashedWhileALeftoverMayRun(t *testing.T) {
	for _, tc := range []struct {
		name      string
		cancelled bool
		busyErr   error
		wantState string
	}{
		{name: "leftover", busyErr: errLeftover, wantState: "crashed"},
		{name: "clear", wantState: "stopped"},
		{name: "cancelled and clear", cancelled: true, wantState: "stopped"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			g := &fakeGuard{busyErr: tc.busyErr}
			held := newHeldLaunchDriver(errors.New("test: launch failed"))
			if tc.cancelled {
				held = newHeldLaunchDriver(nil)
			}
			m := newManager(t, guardedDriver{held, g}, nil)
			seedScratch(t, m, s1)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan session.CommandResult, 1)
			go func() { done <- m.Handle(ctx, startCmd()) }()
			awaitEnter(t, held.entered)
			m.ResyncStatus()
			if states := statesOf(t, m, 1); states[s1] != "starting" {
				t.Fatalf("resync states = %v, want %s starting", states, s1)
			}

			if tc.cancelled {
				cancel()
			} else {
				close(held.release)
			}
			if res := <-done; res.Success {
				t.Fatalf("start = %+v, want the launch's failure", res)
			}

			got := drainStatuses(t, m, 1)[0]
			if got.ServerID != s1 || got.State != tc.wantState {
				t.Fatalf("status = %+v, want %s %s", got, s1, tc.wantState)
			}
			if tc.busyErr != nil && !strings.Contains(got.Detail, tc.busyErr.Error()) {
				t.Fatalf("detail = %q, want it to carry %q", got.Detail, tc.busyErr)
			}
			if want := []string{"busy " + s1}; !slices.Equal(g.callLog(), want) {
				t.Fatalf("guard calls = %v, want %v", g.callLog(), want)
			}
		})
	}
}

// A failed launch no resync reported says nothing and asks the guard nothing:
// only the corrective report needs the answer (issue #3672).
func TestUnreportedLaunchThatFailsAsksTheGuardNothing(t *testing.T) {
	g := &fakeGuard{busyErr: errLeftover}
	held := newHeldLaunchDriver(errors.New("test: launch failed"))
	m := newManager(t, guardedDriver{held, g}, nil)
	seedScratch(t, m, s1)
	close(held.release)

	if res := m.Handle(context.Background(), startCmd()); res.Success {
		t.Fatalf("start = %+v, want the launch's failure", res)
	}
	expectNoStatus(t, m)
	if calls := g.callLog(); len(calls) != 0 {
		t.Fatalf("guard calls = %v, want none", calls)
	}
}

// The reclaim of a deleted server's scratch first ends what the driver left
// running for it, with the working set still in place for the leftover's
// graceful stop to watch, then removes the scratch (issue #3672). Without it the
// leftover of a deleted server would run until the Worker's next boot.
func TestReclaimEndsADeletedServersLeftoversBeforeRemovingTheScratch(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	dir := seedScratch(t, m, s1)
	presentAtEnd := false
	g.onEnd = func() {
		_, err := os.Stat(dir)
		presentAtEnd = err == nil
	}

	m.reclaimDeletedScratches([]string{s1})

	if want := []string{"end " + s1 + " " + dir, "busy " + s1}; !slices.Equal(g.callLog(), want) {
		t.Fatalf("guard calls = %v, want %v", g.callLog(), want)
	}
	if !presentAtEnd {
		t.Fatal("the scratch was gone when the leftover was ended, want it in place")
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("scratch not reclaimed: stat err = %v", err)
	}
}

// A deleted server whose working set may still be in use keeps its scratch:
// removing it would pull the tree from under a running container. The id stays
// advertised, so the next registration offers it again, and the boot's sweep
// removes the container should nothing earlier end it (issue #3672). The
// reservation is released.
func TestReclaimSkipsADeletedServerWhoseWorkingSetMayBeInUse(t *testing.T) {
	for _, tc := range []struct {
		name            string
		endErr, busyErr error
	}{
		{name: "leftover not ended", endErr: errLeftover},
		{name: "untracked container running", busyErr: errors.New("test: its working set is in use")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m, g, _ := newGuardedManager(t)
			dir := seedScratch(t, m, s1)
			g.set(tc.endErr, tc.busyErr)

			m.reclaimDeletedScratches([]string{s1})

			if _, err := os.Stat(dir); err != nil {
				t.Fatalf("scratch stat err = %v, want it kept", err)
			}
			g.set(nil, nil)
			m.reclaimDeletedScratches([]string{s1})
			if _, err := os.Stat(dir); !os.IsNotExist(err) {
				t.Fatalf("scratch not reclaimed on the next offer: stat err = %v", err)
			}
		})
	}
}
