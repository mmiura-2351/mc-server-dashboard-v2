package instancemanager

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager/goroutineleak"
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
	// leftovers are the server ids the driver records a leftover for. Asking is a
	// plain query, so it is not logged in calls.
	leftovers map[string]bool
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

func (g *fakeGuard) HasLeftovers(serverID string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.leftovers[serverID]
}

func (g *fakeGuard) recordLeftover(serverID string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.leftovers == nil {
		g.leftovers = map[string]bool{}
	}
	g.leftovers[serverID] = true
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

// A hydrate is refused UNAVAILABLE, and writes nothing, while a container the
// manager does not track may be running against the working set: a leftover that
// could not be ended, a running container of the server the driver lists, or an
// answer the driver could not get (issue #3672). It is not BUSY: no other command
// holds the id, so the API backs its retries off (issue #3678). The reservation is
// released, so the retry goes ahead once the driver reports the server clear.
func TestHydrateRefusedUnavailableWhileAnUntrackedContainerMayRun(t *testing.T) {
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
			if res.Success || res.ErrorCode != session.CommandErrorUnavailable {
				t.Fatalf("hydrate = %+v, want UNAVAILABLE", res)
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

// A stopped server's snapshot is refused UNAVAILABLE, and packs nothing, while a
// container the manager does not track may be running against the working set
// (issues #3672, #3678). It does not end the leftover: the snapshot is no precondition
// of the start that ends it, and the leftover's own shutdown save would be what
// it packed. The reservation is released, so the retry goes ahead once the
// server is clear.
func TestStoppedSnapshotRefusedUnavailableWhileAnUntrackedContainerMayRun(t *testing.T) {
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
			if res.Success || res.ErrorCode != session.CommandErrorUnavailable ||
				!strings.Contains(res.ErrorMessage, tc.busyErr.Error()) {
				t.Fatalf("snapshot = %+v, want UNAVAILABLE carrying %q", res, tc.busyErr)
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

// A guard answer the command's own cancellation cut short still refuses the
// hydrate and the stopped snapshot UNAVAILABLE (issue #3678): nothing was ruled
// out, and the answer of a cancelled command reaches no caller, so it is not
// worth a code of its own.
func TestGuardRefusalOnCallerCancelIsStillUnavailable(t *testing.T) {
	for _, tc := range []struct {
		name string
		cmd  session.Command
	}{
		{"hydrate", hydrateCmd()},
		{"stopped snapshot", snapshotCmd()},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m, _, _ := newGuardedManager(t)
			seedScratch(t, m, s1)
			ctx, cancel := context.WithCancel(context.Background())
			cancel()

			res := m.Handle(ctx, tc.cmd)
			if res.Success || res.ErrorCode != session.CommandErrorUnavailable ||
				!strings.Contains(res.ErrorMessage, context.Canceled.Error()) {
				t.Fatalf("%s = %+v, want UNAVAILABLE carrying the cancellation", tc.name, res)
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

// A connected Worker reclaims a deleted server when the API reports the delete,
// without waiting for its next registration (issue #3679). The command hands the
// id to the same reclaim a registration runs: it ends what the driver left
// running for the server, with the working set still in place, then removes the
// hydrate leftovers and the scratch. The reclaim runs in the background, so the
// test waits for the removal.
func TestReclaimDeletedServerCommandReclaimsWithoutReconnecting(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	dir := seedScratch(t, m, s1)
	leftover := filepath.Join(m.scratchDir, ".hydrate-"+s1+"-stale")
	if err := os.MkdirAll(leftover, 0o750); err != nil {
		t.Fatal(err)
	}
	var presentAtEnd atomic.Bool
	g.onEnd = func() {
		_, err := os.Stat(dir)
		presentAtEnd.Store(err == nil)
	}

	res := m.Handle(context.Background(), session.Command{CommandID: "reclaim", ServerID: s1, Kind: "ReclaimDeletedServer"})
	if !res.Success {
		t.Fatalf("ReclaimDeletedServer = %+v, want success", res)
	}

	deadline := time.Now().Add(5 * time.Second)
	for {
		_, err := os.Stat(dir)
		if os.IsNotExist(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the reclaim never removed the scratch dir: stat err = %v", err)
		}
		time.Sleep(time.Millisecond)
	}
	if want := []string{"end " + s1 + " " + dir, "busy " + s1}; !slices.Equal(g.callLog(), want) {
		t.Fatalf("guard calls = %v, want %v", g.callLog(), want)
	}
	if !presentAtEnd.Load() {
		t.Fatal("the scratch was gone when the leftover was ended, want it in place")
	}
	if _, err := os.Stat(leftover); !os.IsNotExist(err) {
		t.Fatalf("hydrate leftover not reclaimed: stat err = %v", err)
	}
}

// Delete reports reclaim single-flight per Worker (issue #3679): a report that
// arrives while the reclaim runs is queued for it, and the running reclaim takes
// every queued id in one more pass. So a burst of deletes costs one goroutine and
// two passes, never one goroutine per report, and no reported id is dropped.
func TestReclaimDeletedServerReportsDuringAReclaimJoinItsNextPass(t *testing.T) {
	h := &syncSlogHandler{}
	m, g, _ := newGuardedManager(t)
	m.WithLogger(slog.New(h))
	first := seedScratch(t, m, s1)
	var later []string
	for i := range 4 {
		later = append(later, fmt.Sprintf("53000000-0000-4000-8000-%012d", i))
	}
	dirs := []string{first}
	for _, id := range later {
		dirs = append(dirs, seedScratch(t, m, id))
	}

	var mu sync.Mutex
	inFlight, maxInFlight := 0, 0
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	g.onEnd = func() {
		mu.Lock()
		inFlight++
		maxInFlight = max(maxInFlight, inFlight)
		mu.Unlock()
		select {
		case entered <- struct{}{}:
		default:
		}
		<-release
		mu.Lock()
		inFlight--
		mu.Unlock()
	}
	report := func(id string) {
		t.Helper()
		if res := m.Handle(context.Background(), session.Command{CommandID: "reclaim-" + id, ServerID: id, Kind: "ReclaimDeletedServer"}); !res.Success {
			t.Fatalf("ReclaimDeletedServer(%s) = %+v, want success", id, res)
		}
	}

	report(s1)
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the first reclaim never asked the driver to end the server's leftovers")
	}
	for _, id := range later {
		report(id)
	}
	// A reclaim started per report would be inside the driver beside the first
	// one by now.
	time.Sleep(100 * time.Millisecond)
	close(release)

	deadline := time.Now().Add(5 * time.Second)
	for _, dir := range dirs {
		for {
			_, err := os.Stat(dir)
			if os.IsNotExist(err) {
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("a reported scratch was never reclaimed: %s: stat err = %v", dir, err)
			}
			time.Sleep(time.Millisecond)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if maxInFlight != 1 {
		t.Fatalf("%d reclaims ran at once, want 1", maxInFlight)
	}
	if passes := logged(h, slog.LevelDebug, reclaimPassMsg); passes != 2 {
		t.Fatalf("the reclaim made %d passes over the reported ids, want 2", passes)
	}
}

// A report that arrives after the Worker's previous reclaim has ended starts a
// new one (issue #3679). The single-flight flag is released when a reclaim ends;
// one left set would queue every later report's id for a reclaim that never runs,
// which is this issue's symptom again for every delete after the first burst.
func TestReclaimDeletedServerReportAfterAReclaimEndedStartsAnother(t *testing.T) {
	m, _, _ := newGuardedManager(t)
	first := seedScratch(t, m, s1)
	second := seedScratch(t, m, s2)
	report := func(id string) {
		t.Helper()
		if res := m.Handle(context.Background(), session.Command{CommandID: "reclaim-" + id, ServerID: id, Kind: "ReclaimDeletedServer"}); !res.Success {
			t.Fatalf("ReclaimDeletedServer(%s) = %+v, want success", id, res)
		}
	}

	report(s1)
	// Wait for the reclaim to end, not only for s1's scratch to go: a report that
	// lands before the reclaim's last, empty pass is taken by the same reclaim.
	deadline := time.Now().Add(5 * time.Second)
	for {
		m.mu.Lock()
		running := m.reclaimingReported
		m.mu.Unlock()
		_, err := os.Stat(first)
		if !running && os.IsNotExist(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the first reclaim never ended: still running = %v, scratch stat err = %v", running, err)
		}
		time.Sleep(time.Millisecond)
	}

	report(s2)
	deadline = time.Now().Add(5 * time.Second)
	for {
		_, err := os.Stat(second)
		if os.IsNotExist(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the scratch reported after the first reclaim ended was never reclaimed: stat err = %v", err)
		}
		time.Sleep(time.Millisecond)
	}
}

// A server that never published anything hydrates to a scratch holding only its
// generation marker, which the launch guard accepts, so a start the daemon did not
// answer can leave a container recorded for an id whose scratch holds no working
// set (issue #3679). A delete report still ends that container and removes the
// dir: the report reclaims an id the driver records a leftover for, as well as a
// held one.
func TestReclaimDeletedServerCommandEndsALeftoverOfAnUnheldScratch(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	dir := filepath.Join(m.scratchDir, s1)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, generationFile), testGenerationKey.seal(markerServerID(dir), 0), 0o640); err != nil {
		t.Fatal(err)
	}
	g.recordLeftover(s1)

	if res := m.Handle(context.Background(), session.Command{CommandID: "reclaim", ServerID: s1, Kind: "ReclaimDeletedServer"}); !res.Success {
		t.Fatalf("ReclaimDeletedServer = %+v, want success", res)
	}

	deadline := time.Now().Add(5 * time.Second)
	for {
		_, err := os.Stat(dir)
		if os.IsNotExist(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the reclaim never removed the marker-only scratch: stat err = %v; guard calls = %v", err, g.callLog())
		}
		time.Sleep(time.Millisecond)
	}
	if want := []string{"end " + s1 + " " + dir, "busy " + s1}; !slices.Equal(g.callLog(), want) {
		t.Fatalf("guard calls = %v, want %v", g.callLog(), want)
	}
}

// seedMarkerOnly makes the scratch dir a server that never published anything
// hydrates to: the generation marker and nothing else.
func seedMarkerOnly(t *testing.T, m *Manager, serverID string) string {
	t.Helper()
	dir := filepath.Join(m.scratchDir, serverID)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, generationFile), testGenerationKey.seal(markerServerID(dir), 0), 0o640); err != nil {
		t.Fatal(err)
	}
	return dir
}

// seedEmptyScratch makes the scratch dir of a server with nothing in it, not even
// the generation marker.
func seedEmptyScratch(t *testing.T, m *Manager, serverID string) string {
	t.Helper()
	dir := filepath.Join(m.scratchDir, serverID)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	return dir
}

// s3 is a third server id, for the tests that need one beside s1 and s2.
const s3 = "53000000-0000-4000-8000-000000000003"

// The manager lists as unheld every server-id scratch dir that holds no working
// set, empty or marker-only, whether or not a driver records a leftover for it
// (issue #3794). A held dir is advertised as held instead, never as both, and a
// dir whose name is not a server id is not listed at all.
func TestUnheldScratchServerIDsListsEveryServerIDDirWithNoWorkingSet(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	seedMarkerOnly(t, m, s1)
	g.recordLeftover(s1)
	seedMarkerOnly(t, m, s2)
	seedEmptyScratch(t, m, s3)
	seedScratch(t, m, ghost)
	g.recordLeftover(ghost)
	seedMarkerOnly(t, m, "not-a-server-id")

	if got, want := m.UnheldScratchServerIDs(), []string{s1, s2, s3}; !slices.Equal(got, want) {
		t.Fatalf("UnheldScratchServerIDs = %v, want %v", got, want)
	}
	held := m.HeldServers()
	if len(held) != 1 || held[0].ServerID != ghost {
		t.Fatalf("HeldServers = %+v, want only %s", held, ghost)
	}
	if len(g.callLog()) != 0 {
		t.Fatalf("guard calls = %v, want none: listing asks the backend nothing", g.callLog())
	}
}

// registrationAPI stands in for the API's side of a Worker's registration: it
// answers a Register with the ids it listed, held or unheld, that the API has
// deleted, as the API's servicer does, and then sends nothing.
type registrationAPI struct {
	deleted map[string]bool

	mu   sync.Mutex
	caps session.Capabilities
}

func (a *registrationAPI) Dial(context.Context) (session.Transport, error) { return a, nil }

func (a *registrationAPI) SendRegister(_ context.Context, caps session.Capabilities) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.caps = caps
	return nil
}

func (a *registrationAPI) RecvRegisterAck(context.Context) (session.RegisterAck, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	listed := a.caps.UnheldScratchServerIDs
	for _, hs := range a.caps.HeldServers {
		listed = append(listed, hs.ServerID)
	}
	ack := session.RegisterAck{HeartbeatInterval: time.Hour}
	for _, id := range listed {
		if a.deleted[id] {
			ack.UnknownHeldServerIDs = append(ack.UnknownHeldServerIDs, id)
		}
	}
	return ack, nil
}

func (a *registrationAPI) RecvCommand(ctx context.Context) (session.Command, error) {
	<-ctx.Done()
	return session.Command{}, ctx.Err()
}

func (a *registrationAPI) SendHeartbeat(context.Context) error                            { return nil }
func (a *registrationAPI) SendCommandResult(context.Context, session.CommandResult) error { return nil }
func (a *registrationAPI) SendStatusChange(context.Context, session.StatusEvent) error    { return nil }
func (a *registrationAPI) SendLogLine(context.Context, session.LogEvent) error            { return nil }
func (a *registrationAPI) SendMetrics(context.Context, session.MetricsEvent) error        { return nil }
func (a *registrationAPI) Close() error                                                   { return nil }

// A deleted server whose scratch holds only its generation marker, and whose
// failed start left a container the driver records, is reclaimed when the Worker
// registers (issue #3794), not left until its next boot. The manager lists the id
// as unheld, the API answers it deleted, and the session hands it to the reclaim,
// which ends the container and then removes the dir.
func TestRegistrationReclaimsALeftoverOfAnUnheldScratch(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	dir := seedMarkerOnly(t, m, s1)
	g.recordLeftover(s1)
	api := &registrationAPI{deleted: map[string]bool{s1: true}}
	r := session.NewRunner(api, session.Capabilities{WorkerID: "w1"}, systemClock{},
		slog.New(slog.DiscardHandler), session.WithCommandHandler(m))

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()
	defer func() { cancel(); <-done }()

	deadline := time.Now().Add(5 * time.Second)
	for {
		_, err := os.Stat(dir)
		if os.IsNotExist(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the registration never reclaimed the marker-only scratch: stat err = %v; guard calls = %v", err, g.callLog())
		}
		time.Sleep(time.Millisecond)
	}
	if want := []string{"end " + s1 + " " + dir, "busy " + s1}; !slices.Equal(g.callLog(), want) {
		t.Fatalf("guard calls = %v, want %v", g.callLog(), want)
	}
}

// A deleted server whose scratch dir holds no working set, empty or marker-only,
// is reclaimed when the Worker registers even with no leftover recorded (issue
// #3794): the dir is removed, after the leftover check every reclaimed id gets. A
// live server's marker-only dir is listed too, but the API does not answer it
// deleted, so it is left as it is.
func TestRegistrationReclaimsADeletedServersUnheldScratch(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	markerOnly := seedMarkerOnly(t, m, s1)
	empty := seedEmptyScratch(t, m, s2)
	live := seedMarkerOnly(t, m, ghost)
	api := &registrationAPI{deleted: map[string]bool{s1: true, s2: true}}
	r := session.NewRunner(api, session.Capabilities{WorkerID: "w1"}, systemClock{},
		slog.New(slog.DiscardHandler), session.WithCommandHandler(m))

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()
	defer func() { cancel(); <-done }()

	// One reclaim takes the acked ids in order, so s2's removal means s1 has had
	// its turn.
	deadline := time.Now().Add(5 * time.Second)
	for {
		_, err := os.Stat(empty)
		if os.IsNotExist(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the registration never reclaimed the empty scratch: stat err = %v; guard calls = %v", err, g.callLog())
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := os.Stat(markerOnly); !os.IsNotExist(err) {
		t.Fatalf("the marker-only scratch of a deleted server: stat err = %v, want it removed", err)
	}
	if _, err := os.Stat(filepath.Join(live, generationFile)); err != nil {
		t.Fatalf("the live server's marker-only scratch: stat err = %v, want it untouched", err)
	}
	want := []string{"end " + s1 + " " + markerOnly, "busy " + s1, "end " + s2 + " " + empty, "busy " + s2}
	if !slices.Equal(g.callLog(), want) {
		t.Fatalf("guard calls = %v, want %v", g.callLog(), want)
	}
}

// A delete report reclaims a scratch dir that holds no working set, empty or
// marker-only, with no leftover recorded (issue #3794), as a registration does:
// any scratch dir of the deleted server admits the id, not only a held one.
func TestReclaimDeletedServerCommandRemovesAnUnheldScratchWithNoLeftover(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	markerOnly := seedMarkerOnly(t, m, s1)
	empty := seedEmptyScratch(t, m, s2)

	for _, id := range []string{s1, s2} {
		if res := m.Handle(context.Background(), session.Command{CommandID: "reclaim-" + id, ServerID: id, Kind: "ReclaimDeletedServer"}); !res.Success {
			t.Fatalf("ReclaimDeletedServer(%s) = %+v, want success", id, res)
		}
	}

	deadline := time.Now().Add(5 * time.Second)
	for {
		_, err := os.Stat(empty)
		if os.IsNotExist(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the report never reclaimed the empty scratch: stat err = %v; guard calls = %v", err, g.callLog())
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := os.Stat(markerOnly); !os.IsNotExist(err) {
		t.Fatalf("the marker-only scratch: stat err = %v, want it removed", err)
	}
	want := []string{"end " + s1 + " " + markerOnly, "busy " + s1, "end " + s2 + " " + empty, "busy " + s2}
	if !slices.Equal(g.callLog(), want) {
		t.Fatalf("guard calls = %v, want %v", g.callLog(), want)
	}
}

// A delete report ends a recorded leftover even when the server's scratch dir is
// gone (issue #3794): the record alone admits the id, and the reclaim then has no
// dir to remove.
func TestReclaimDeletedServerCommandEndsALeftoverWithNoScratchDir(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	g.recordLeftover(s1)
	dir := filepath.Join(m.scratchDir, s1)

	if res := m.Handle(context.Background(), session.Command{CommandID: "reclaim", ServerID: s1, Kind: "ReclaimDeletedServer"}); !res.Success {
		t.Fatalf("ReclaimDeletedServer = %+v, want success", res)
	}

	want := []string{"end " + s1 + " " + dir, "busy " + s1}
	deadline := time.Now().Add(5 * time.Second)
	for !slices.Equal(g.callLog(), want) {
		if time.Now().After(deadline) {
			t.Fatalf("guard calls = %v, want %v", g.callLog(), want)
		}
		time.Sleep(time.Millisecond)
	}
}

// A symlink named like a server id is not a scratch dir: a delete report does not
// admit it, just as neither registration listing lists it (issue #3794). Read
// through the link, it would be a dir, so the gate must not follow it. s2,
// reported after it, is the positive control.
func TestReclaimDeletedServerCommandDoesNotAdmitASymlinkedScratch(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	target := t.TempDir()
	link := filepath.Join(m.scratchDir, s1)
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	control := seedMarkerOnly(t, m, s2)

	for _, id := range []string{s1, s2} {
		if res := m.Handle(context.Background(), session.Command{CommandID: "reclaim-" + id, ServerID: id, Kind: "ReclaimDeletedServer"}); !res.Success {
			t.Fatalf("ReclaimDeletedServer(%s) = %+v, want success", id, res)
		}
	}

	deadline := time.Now().Add(5 * time.Second)
	for {
		_, err := os.Stat(control)
		if os.IsNotExist(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the control scratch was never reclaimed: stat err = %v", err)
		}
		time.Sleep(time.Millisecond)
	}
	if want := []string{"end " + s2 + " " + control, "busy " + s2}; !slices.Equal(g.callLog(), want) {
		t.Fatalf("guard calls = %v, want only the control's %v", g.callLog(), want)
	}
	if info, err := os.Lstat(link); err != nil || info.Mode()&os.ModeSymlink == 0 {
		t.Fatalf("the link: lstat = %v, %v; want it left as a symlink", info, err)
	}
	if info, err := os.Stat(target); err != nil || !info.IsDir() {
		t.Fatalf("the link's target: stat = %v, %v; want it untouched", info, err)
	}
}

// Widening the report's gate to any scratch dir does not widen what the reclaim
// touches (issue #3794): a running id and a reserved one, each with a marker-only
// dir, are queued but skipped at reserve(), before the leftover check, and keep
// their dirs. s3, reported after them, is the positive control.
func TestReclaimDeletedServerCommandLeavesARunningOrReservedUnheldScratch(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	running := seedMarkerOnly(t, m, s1)
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("StartServer = %+v, want success", res)
	}
	reserved := seedMarkerOnly(t, m, s2)
	if ok, _, _ := m.reserve(s2, legNone); !ok {
		t.Fatal("could not reserve s2 for test setup")
	}
	defer m.release(s2)
	control := seedMarkerOnly(t, m, s3)
	before := len(g.callLog())

	for _, id := range []string{s1, s2, s3} {
		if res := m.Handle(context.Background(), session.Command{CommandID: "reclaim-" + id, ServerID: id, Kind: "ReclaimDeletedServer"}); !res.Success {
			t.Fatalf("ReclaimDeletedServer(%s) = %+v, want success", id, res)
		}
	}

	deadline := time.Now().Add(5 * time.Second)
	for {
		_, err := os.Stat(control)
		if os.IsNotExist(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the control scratch was never reclaimed: stat err = %v", err)
		}
		time.Sleep(time.Millisecond)
	}
	for _, dir := range []string{running, reserved} {
		if _, err := os.Stat(filepath.Join(dir, generationFile)); err != nil {
			t.Fatalf("%s: stat err = %v, want it untouched", dir, err)
		}
	}
	if got, want := g.callLog()[before:], []string{"end " + s3 + " " + control, "busy " + s3}; !slices.Equal(got, want) {
		t.Fatalf("guard calls = %v, want only the control's %v", got, want)
	}
}

// A Worker reclaims on a delete report only an id it has a scratch dir or a
// recorded leftover for (issues #3679, #3794). The API reports a delete to every
// connected Worker, so one with neither for the id does nothing for it, not even
// ask the driver.
func TestReclaimDeletedServerCommandLeavesAnIDThisWorkerDoesNotHold(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	held := seedScratch(t, m, s2)

	for _, id := range []string{s1, s2} {
		if res := m.Handle(context.Background(), session.Command{CommandID: "reclaim-" + id, ServerID: id, Kind: "ReclaimDeletedServer"}); !res.Success {
			t.Fatalf("ReclaimDeletedServer(%s) = %+v, want success", id, res)
		}
	}

	// One reclaim takes the reported ids in order, so s2's removal means s1 has
	// had its turn.
	deadline := time.Now().Add(5 * time.Second)
	for {
		_, err := os.Stat(held)
		if os.IsNotExist(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the held scratch was never reclaimed: stat err = %v", err)
		}
		time.Sleep(time.Millisecond)
	}
	if want := []string{"end " + s2 + " " + held, "busy " + s2}; !slices.Equal(g.callLog(), want) {
		t.Fatalf("guard calls = %v, want only the held id's %v", g.callLog(), want)
	}
}

// A closed manager starts no reclaim, so it does not answer a delete report as if
// it had (issue #3679). The scratch stays, and the next registration offers it.
func TestReclaimDeletedServerCommandRefusedOnAClosedManager(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	dir := seedScratch(t, m, s1)
	m.Close()

	res := m.Handle(context.Background(), session.Command{CommandID: "reclaim", ServerID: s1, Kind: "ReclaimDeletedServer"})
	if res.Success || res.ErrorCode != session.CommandErrorInternal {
		t.Fatalf("ReclaimDeletedServer on a closed manager = %+v, want an internal error", res)
	}
	if live, stacks := goroutineleak.Settle(0, 200*time.Millisecond); live != 0 {
		t.Fatalf("a delete report on a closed manager started %d goroutine(s):\n%s", live, stacks)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("scratch stat err = %v, want it kept", err)
	}
	if calls := g.callLog(); len(calls) != 0 {
		t.Fatalf("guard calls = %v, want none", calls)
	}
}

// The delete report keeps the reclaim's fencing (issue #3679): a deleted server
// whose working set a container the Worker does not track may still use keeps its
// scratch, as at registration (issue #3672), so the next registration offers it
// again.
func TestReclaimDeletedServerCommandKeepsAWorkingSetThatMayBeInUse(t *testing.T) {
	m, g, _ := newGuardedManager(t)
	dir := seedScratch(t, m, s1)
	g.set(errLeftover, nil)

	res := m.Handle(context.Background(), session.Command{CommandID: "reclaim", ServerID: s1, Kind: "ReclaimDeletedServer"})
	if !res.Success {
		t.Fatalf("ReclaimDeletedServer = %+v, want success", res)
	}

	// The reclaim runs in the background. It asks the guard while it holds the
	// id and releases the id last, so both together mean it has finished.
	deadline := time.Now().Add(5 * time.Second)
	for {
		m.mu.Lock()
		held := len(m.reserved)
		m.mu.Unlock()
		if len(g.callLog()) > 0 && held == 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the reclaim never finished: guard calls = %v, reserved ids = %d", g.callLog(), held)
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("scratch stat err = %v, want it kept while a container may run against it", err)
	}
}

// The delete report's reclaim is the manager's background work, not the lane's
// (issue #3679): the command answers at once, and Close joins the reclaim it
// started, as it joins a registration's (issue #2878). A session lane is joined
// by nothing, so a reclaim run on it could be cut off between its scratch removal
// and its reservation release.
func TestReclaimDeletedServerCommandAnswersAtOnceAndCloseJoinsItsReclaim(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	h := newBlockingReclaimLogger()
	m := newManager(t, &fakeDriver{}, nil).WithLogger(slog.New(h))
	// Registered AFTER newManager's Close, so cleanups run it FIRST (see
	// TestCloseJoinsAnInFlightReclaim).
	t.Cleanup(h.unpark)
	dir := seedScratch(t, m, s1)

	answered := make(chan session.CommandResult, 1)
	go func() {
		answered <- m.Handle(context.Background(), session.Command{CommandID: "reclaim", ServerID: s1, Kind: "ReclaimDeletedServer"})
	}()
	select {
	case <-h.entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the reclaim never reached its removal; the log record this test parks on has changed")
	}
	select {
	case res := <-answered:
		if !res.Success {
			t.Fatalf("ReclaimDeletedServer = %+v, want success", res)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("ReclaimDeletedServer did not answer while its reclaim was still running; it ran the reclaim on the lane")
	}
	// The status dispatcher and the reclaim.
	awaitManagerGoroutines(t, 2)

	closed := make(chan struct{})
	go func() { m.Close(); close(closed) }()
	// The dispatcher is gone, so Close is inside its Wait (see
	// TestCloseJoinsAnInFlightReclaim).
	awaitManagerGoroutines(t, 1)
	select {
	case <-closed:
		t.Fatal("Close returned with the reported delete's reclaim parked between its scratch removal and its reservation release")
	case <-time.After(100 * time.Millisecond):
	}

	h.unpark()
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not return after the reclaim it joined had finished")
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("the joined reclaim did not remove the scratch dir: stat err = %v", err)
	}
	awaitManagerGoroutines(t, 0)
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
