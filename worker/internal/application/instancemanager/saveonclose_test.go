package instancemanager

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// saveOnRecorder is a concurrency-safe openControl. fakeControl cannot stand in
// here: the shutdown restore dials on its own goroutine while the stop that
// opened the bracket is still inside the driver, so two brackets append RCON
// lines at once and an unsynchronised recorder is a data race the -race build
// fails on rather than a fixture. failLines maps a command line to the error
// Execute returns for it, so a test can fail one step of the flush.
type saveOnRecorder struct {
	mu        sync.Mutex
	lines     []string
	byServer  map[string][]string
	failLines map[string]error
}

func (r *saveOnRecorder) open(_ context.Context, serverID, _, _ string) (execution.ServerControl, error) {
	return recordedControl{rec: r, serverID: serverID}, nil
}

type recordedControl struct {
	rec      *saveOnRecorder
	serverID string
}

func (c recordedControl) Execute(_ context.Context, line string) (string, error) {
	c.rec.mu.Lock()
	c.rec.lines = append(c.rec.lines, line)
	if c.rec.byServer == nil {
		c.rec.byServer = map[string][]string{}
	}
	c.rec.byServer[c.serverID] = append(c.rec.byServer[c.serverID], line)
	err := c.rec.failLines[line]
	c.rec.mu.Unlock()
	if err != nil {
		return "", err
	}
	return "ok", nil
}

func (c recordedControl) Close() error { return nil }

// hasFor / allFor scope the recorder to one server, for the tests that drive two at
// once (a parked converger holding Close in its join while an operator stop arms a
// bracket beside it).
func (r *saveOnRecorder) hasFor(serverID, line string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, l := range r.byServer[serverID] {
		if l == line {
			return true
		}
	}
	return false
}

func (r *saveOnRecorder) allFor(serverID string) []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.byServer[serverID]...)
}

func (r *saveOnRecorder) count(line string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := 0
	for _, l := range r.lines {
		if l == line {
			n++
		}
	}
	return n
}

func (r *saveOnRecorder) has(line string) bool { return r.count(line) > 0 }

// all copies the recorded lines so a failure message names the sequence that was
// actually issued.
func (r *saveOnRecorder) all() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.lines...)
}

// awaitLine waits for the recorder to observe line, reporting what was issued
// instead when it does not arrive. waitFor's own message ("condition not met")
// cannot say which bracket is missing.
func awaitLine(t *testing.T, rec *saveOnRecorder, line, why string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if rec.has(line) {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("rcon lines = %v, want %q: %s", rec.all(), line, why)
}

// flushGatedInstance runs the pre-stop flush and then PARKS inside the driver
// Stop, holding the manager in exactly the window this issue is about: the
// flush's save-off has disabled auto-save on a server that is still running, and
// nothing re-enables it until the escalation resolves one way or the other. In
// production that window is the containerdriver's escalation — the kill call plus
// the post-kill exit confirmation, up to 60 s after a flush that succeeded and
// ~160 s after one that did not.
type flushGatedInstance struct {
	*fakeInstance
	stopEntered chan struct{}
	release     chan struct{}
	releaseOnce sync.Once
	// stopErr is what Stop returns once released: an error models the survived-kill
	// orphan a retry keeps working, nil the stop that finally confirmed termination.
	stopErr error
}

func newFlushGatedInstance(id string, stopErr error) *flushGatedInstance {
	return &flushGatedInstance{
		fakeInstance: newFakeInstance(id),
		stopEntered:  make(chan struct{}, 1),
		release:      make(chan struct{}),
		stopErr:      stopErr,
	}
}

func (i *flushGatedInstance) Stop(ctx context.Context, graceful bool, preFallback ...func(context.Context) bool) error {
	// Run the flush before the terminate, exactly as the real containerdriver does
	// on the graceful path (#1007) — it is what issues the save-off.
	if graceful && len(preFallback) > 0 && preFallback[0] != nil {
		_ = preFallback[0](ctx)
	}
	select {
	case i.stopEntered <- struct{}{}:
	default:
	}
	<-i.release
	if i.stopErr != nil {
		return i.stopErr
	}
	return i.fakeInstance.Stop(ctx, graceful)
}

// releaseStop lets the parked Stop finish. It is idempotent so a test can
// register it as a cleanup AND call it in the body: a t.Fatal with the stop still
// parked would otherwise hang the cleanup's Close on a goroutine nothing releases.
func (i *flushGatedInstance) releaseStop() { i.releaseOnce.Do(func() { close(i.release) }) }

// flushGatedDriver hands out one flushGatedInstance, so the OPERATOR stop lane can
// be parked in the same window the converger's retry is.
type flushGatedDriver struct{ inst *flushGatedInstance }

func (d *flushGatedDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.inst = newFlushGatedInstance(spec.ServerID, errors.New("driver: process survived kill"))
	return d.inst, nil
}

// gatedDialControl is an openControl whose dial can be ARMED to block, modeling a
// server that TCP-accepts but never finishes the RCON handshake. A fixture that
// answers instantly cannot show whether Close waited for the restore to land, so
// the slow dial is the whole point. It honours ctx exactly as rcon.Dial does
// (DialContext plus a handshake deadline), which is also what lets a BOUNDED
// restore give up on its own.
//
// Dials before arm() answer instantly, so the stop's own flush is never gated, and
// onlyServer (when set) confines the gate to one id so a second server's flush can
// run to completion beside a blocked one.
type gatedDialControl struct {
	rec         *saveOnRecorder
	gate        atomic.Bool
	onlyServer  string
	dialEntered chan struct{}
	release     chan struct{}
	releaseOnce sync.Once
}

func newGatedDialControl(rec *saveOnRecorder) *gatedDialControl {
	return &gatedDialControl{rec: rec, dialEntered: make(chan struct{}, 1), release: make(chan struct{})}
}

func (g *gatedDialControl) arm() { g.gate.Store(true) }

func (g *gatedDialControl) open(ctx context.Context, serverID, _, _ string) (execution.ServerControl, error) {
	answer := recordedControl{rec: g.rec, serverID: serverID}
	if !g.gate.Load() || (g.onlyServer != "" && serverID != g.onlyServer) {
		return answer, nil
	}
	select {
	case g.dialEntered <- struct{}{}:
	default:
	}
	select {
	case <-g.release:
		return answer, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (g *gatedDialControl) releaseDial() { g.releaseOnce.Do(func() { close(g.release) }) }

// newSaveOnManager builds a manager whose RCON goes through open. It registers no
// Close: every test here closes explicitly and has to release its parked stop
// first, so the cleanup is per-test.
func newSaveOnManager(t *testing.T, d execution.ExecutionDriver, open controlFunc) *Manager {
	t.Helper()
	m := New(map[string]execution.ExecutionDriver{"container": d}, t.TempDir(), open)
	m.settlePollInterval = 0
	return m
}

// A retry stop quiesces the world with save-off and re-enables it only once the
// escalation has resolved (restoreSaveOnAfterFailedStop). A Worker that went down
// inside that window left a SURVIVING Minecraft container with auto-save off: the
// container is not a Compose service, so nothing stops it on the way out, and the
// repair arrived only at the next Worker boot (containerdriver.sweepSaveOn,
// #1710) — which an explicit stop or `docker compose down` never brings. Close
// closes the bracket itself now, at the moment the shutdown starts (issue #3166).
//
// The assertion is made while the stop is STILL PARKED, and that is what makes it
// a pin on the timing rather than merely on the call: the parked escalation is
// what Close is waiting for, so a restore issued after that Wait could not appear
// here.
func TestCloseRestoresSaveOnForAConvergerStopStillInFlight(t *testing.T) {
	rec := &saveOnRecorder{}
	m := newSaveOnManager(t, &fakeDriver{}, rec.open)
	shrinkOrphanConverger(m)
	seedScratch(t, m, "s1")
	inst := newFlushGatedInstance("s1", errors.New("driver: process survived kill"))
	t.Cleanup(func() { inst.releaseStop(); m.Close() })

	m.recordOrphan("s1", inst, "container", "1.21")
	awaitEnter(t, inst.stopEntered)
	if !rec.has("save-off") {
		t.Fatalf("rcon lines = %v, want the retry stop's flush to have issued save-off", rec.all())
	}
	if rec.has("save-on") {
		t.Fatalf("rcon lines = %v, want no save-on before the shutdown", rec.all())
	}

	closed := make(chan struct{})
	go func() { m.Close(); close(closed) }()

	awaitLine(t, rec, "save-on",
		"the shutdown must re-enable auto-save while the stop is still escalating, not after it")

	inst.releaseStop()
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not return once the parked retry stop finished")
	}
}

// The same window on the OPERATOR lane, which is where a timer cannot reach it:
// Runner.serve joins no command lane (#3168), so an in-flight StopServer is
// abandoned the moment run() returns and its save-off was never restored at all —
// not by the escalation (the process exits under it) and not by compose's
// stop_grace_period (nothing waits for that lane). Close's drain is keyed on the
// outstanding save-off rather than on which lane issued it, so this lane is
// covered by the same code (issue #3166).
func TestCloseRestoresSaveOnForAnOperatorStopStillInFlight(t *testing.T) {
	rec := &saveOnRecorder{}
	d := &flushGatedDriver{}
	m := newSaveOnManager(t, d, rec.open)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	inst := d.inst
	t.Cleanup(func() { inst.releaseStop(); m.Close() })

	stopped := make(chan session.CommandResult, 1)
	go func() {
		stopped <- m.Handle(context.Background(),
			session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})
	}()
	awaitEnter(t, inst.stopEntered)
	if !rec.has("save-off") {
		t.Fatalf("rcon lines = %v, want the operator stop's flush to have issued save-off", rec.all())
	}
	if rec.has("save-on") {
		t.Fatalf("rcon lines = %v, want no save-on before the shutdown", rec.all())
	}

	closed := make(chan struct{})
	go func() { m.Close(); close(closed) }()

	awaitLine(t, rec, "save-on",
		"the shutdown must re-enable auto-save for a stop on a lane it does not join")

	inst.releaseStop()
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("the operator stop did not return once released")
	}
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not return")
	}
}

// Close must not merely ISSUE the restore, it must not return until the save-on
// has landed: the process exits the moment run() returns (main.go), which kills a
// restore still in flight — so a shutdown that left it unjoined would have moved
// the loss rather than fixed it.
//
// A fixture that answers instantly cannot show this, which is why the dial is
// gated here: the test proves Close is still inside itself while the restore is
// parked in the dial, then releases it and requires the save-on to be recorded by
// the time Close returns. The stop runs on the operator lane deliberately — Close
// joins nothing there, so the restore is the ONLY thing that can be holding it.
func TestCloseWaitsForTheShutdownRestoreToLand(t *testing.T) {
	rec := &saveOnRecorder{}
	gate := newGatedDialControl(rec)
	d := &flushGatedDriver{}
	m := newSaveOnManager(t, d, gate.open)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	inst := d.inst
	t.Cleanup(func() { gate.releaseDial(); inst.releaseStop(); m.Close() })

	stopped := make(chan session.CommandResult, 1)
	go func() {
		stopped <- m.Handle(context.Background(),
			session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})
	}()
	awaitEnter(t, inst.stopEntered)
	// From here every dial blocks: the flush already has its connection, so the next
	// one is the shutdown restore's.
	gate.arm()

	closed := make(chan struct{})
	go func() { m.Close(); close(closed) }()
	awaitEnter(t, gate.dialEntered)

	select {
	case <-closed:
		t.Fatal("Close returned while its save-on was still dialing: the process exit that follows kills the restore")
	case <-time.After(50 * time.Millisecond):
	}

	gate.releaseDial()
	inst.releaseStop()
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not return once the restore completed")
	}
	if !rec.has("save-on") {
		t.Fatalf("rcon lines = %v, want save-on to have landed before Close returned", rec.all())
	}
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("the operator stop did not return once released")
	}
}

// ...but the wait is BOUNDED. The restore is one dial plus one command against a
// server on the same docker network, sub-second whenever that server answers at
// all; a server that does not answer within the bound is, in this exact window, a
// server whose stop is escalating BECAUSE it is not answering, and holding the
// Worker's shutdown open for it buys nothing the next boot's sweepSaveOn does not
// already cover. An unbounded wait here would instead put the failure path's
// generous restoreSaveTimeout on a shutdown leg that nothing overlaps.
//
// The gate is never released, so the only thing that can end this Close is the
// bound.
func TestCloseBoundsTheWaitForAnUnreachableRestore(t *testing.T) {
	// The relationship is the requirement, not the number: the shutdown restore has
	// no escalation to hide behind, so it cannot carry the budget sized for one that
	// does.
	if closingSaveOnTimeout >= restoreSaveTimeout {
		t.Fatalf("closingSaveOnTimeout = %s, want it well under restoreSaveTimeout (%s): nothing overlaps the shutdown restore",
			closingSaveOnTimeout, restoreSaveTimeout)
	}

	rec := &saveOnRecorder{}
	gate := newGatedDialControl(rec)
	d := &flushGatedDriver{}
	m := newSaveOnManager(t, d, gate.open)
	m.closingSaveOnTimeout = time.Millisecond
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	inst := d.inst
	t.Cleanup(func() { gate.releaseDial(); inst.releaseStop(); m.Close() })

	stopped := make(chan session.CommandResult, 1)
	go func() {
		stopped <- m.Handle(context.Background(),
			session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})
	}()
	awaitEnter(t, inst.stopEntered)
	gate.arm()

	closed := make(chan struct{})
	go func() { m.Close(); close(closed) }()
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close never returned: the shutdown restore's wait is unbounded")
	}
	// Nothing landed, and that is the point: the bound is what ended this Close, and
	// the unreachable server is left to the next boot's sweepSaveOn. Asserted before
	// the release below, because the stop's OWN failure-path restore issues a save-on
	// once the dial answers.
	if rec.has("save-on") {
		t.Fatalf("rcon lines = %v, want no save-on: the gated dial never answered", rec.all())
	}

	// The failure-path restore that follows the stop carries the generous
	// restoreSaveTimeout, so the dial is released first rather than making the test
	// wait that budget out.
	gate.releaseDial()
	inst.releaseStop()
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("the operator stop did not return once released")
	}
}

// A stop that CONFIRMED termination leaves nothing to restore: the container is
// gone, so a save-on at shutdown would dial a server that no longer exists. The
// outstanding save-off has to be forgotten when the stop resolves, not held until
// Close.
func TestCloseIssuesNoSaveOnOnceTheStopResolved(t *testing.T) {
	rec := &saveOnRecorder{}
	d := &flushOrphanDriver{stopAfter: 0} // the first Stop confirms termination
	m := newSaveOnManager(t, d, rec.open)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	if res := m.Handle(context.Background(),
		session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"}); !res.Success {
		t.Fatalf("stop = %+v, want success (confirmed termination)", res)
	}
	if !rec.has("save-off") {
		t.Fatalf("rcon lines = %v, want the stop's flush to have issued save-off", rec.all())
	}

	m.Close()

	if rec.has("save-on") {
		t.Fatalf("rcon lines = %v: the shutdown restored auto-save on a server whose stop confirmed termination", rec.all())
	}
}

// A save-off whose ROUND TRIP failed may still have disabled auto-save: rcon.Execute
// writes the command and only then waits for a reply, so a timeout says nothing
// about whether Minecraft ran it. The debt is therefore recorded before the command
// goes on the wire, and the shutdown restores auto-save for a server whose save-off
// was merely reported as failing — the direction that costs one idempotent save-on
// when wrong, instead of a surviving world that saves nothing.
//
// The parked stop CONFIRMS termination once released, so the failure-path restore
// never runs: the save-on this test sees can only be the shutdown's. And the
// release waits for the shutdown context to be cancelled, which happens after
// Close's drain has already read this id — without that anchor the stop could
// resolve first and forget the debt.
func TestCloseRestoresSaveOnWhenTheFlushSaveOffReportedFailure(t *testing.T) {
	rec := &saveOnRecorder{failLines: map[string]error{"save-off": errors.New("rcon read timeout")}}
	m := newSaveOnManager(t, &fakeDriver{}, rec.open)
	shrinkOrphanConverger(m)
	seedScratch(t, m, "s1")
	inst := newFlushGatedInstance("s1", nil) // the released Stop confirms termination
	t.Cleanup(func() { inst.releaseStop(); m.Close() })

	m.recordOrphan("s1", inst, "container", "1.21")
	awaitEnter(t, inst.stopEntered)

	closed := make(chan struct{})
	go func() { m.Close(); close(closed) }()
	select {
	case <-m.shutdown.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not cancel the shutdown context")
	}
	inst.releaseStop()
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not return once the parked retry stop finished")
	}

	if !rec.has("save-on") {
		t.Fatalf("rcon lines = %v, want save-on: a save-off whose reply timed out may still have landed", rec.all())
	}
}

// The debt must outlive the driver Stop's RETURN, all the way past the restore that
// the failure calls for. Forgetting it in between is not a cosmetic ordering: Close
// joins no command lane, so a drain landing in that window finds an empty map and
// the process exits under a survivor with auto-save still off.
//
// Asserted from inside the restore's own dial, which is the one instant where the
// ordering is observable without a race.
func TestFailedStopKeepsTheDebtUntilItsRestoreRan(t *testing.T) {
	rec := &saveOnRecorder{}
	d := &flushOrphanDriver{stopAfter: 1} // the stop fails: the restore path runs
	var m *Manager
	dials := 0
	var sawRestoreDial, debtAtRestoreDial bool
	m = newSaveOnManager(t, d, func(context.Context, string, string, string) (execution.ServerControl, error) {
		dials++
		if dials == 2 { // dial 1 is the flush's; dial 2 is the failed-stop restore's
			sawRestoreDial = true
			m.mu.Lock()
			_, debtAtRestoreDial = m.pendingSaveOn["s1"]
			m.mu.Unlock()
		}
		return recordedControl{rec: rec}, nil
	})
	closeWithTest(t, m)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}

	if res := m.Handle(context.Background(),
		session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"}); res.Success {
		t.Fatalf("stop = %+v, want failure (driver could not confirm termination)", res)
	}

	if !sawRestoreDial {
		t.Fatalf("rcon lines = %v, want the failed-stop restore to have dialed", rec.all())
	}
	if !debtAtRestoreDial {
		t.Fatal("the outstanding save-off was forgotten before its restore ran: a Close draining in that window exits under a survivor with auto-save off")
	}
	m.mu.Lock()
	_, stillOutstanding := m.pendingSaveOn["s1"]
	m.mu.Unlock()
	if stillOutstanding {
		t.Fatal("the outstanding save-off outlived the stop that settled it; a later Close would dial a server whose stop is long resolved")
	}
}

// THE LEDGER IS READ MORE THAN ONCE, because a stop already dispatched can open its
// bracket AFTER the first read. The flush's own RCON dial stands between the command
// and its save-off, and that dial is a TCP connect plus an AUTH handshake — up to
// rcon's 30 s ceiling, not an instant — so a stop that was in flight when the
// shutdown began can land its save-off well after Close drained. Close outlives that
// by however long its joined work takes and then returns: the operator lane it
// belongs to is never joined (#3168), so the process exited under a survivor with
// auto-save off.
//
// The window is reproduced exactly: Close starts BEFORE the save-off, with a parked
// converger holding it inside its join so the late bracket has somewhere to land.
func TestCloseRestoresSaveOnForADebtArmedAfterItsFirstDrain(t *testing.T) {
	rec := &saveOnRecorder{}
	gate := newGatedDialControl(rec)
	gate.onlyServer = "s1" // only the operator stop's dial is held; the converger's runs
	gate.arm()
	d := &flushGatedDriver{}
	m := newSaveOnManager(t, d, gate.open)
	shrinkOrphanConverger(m)
	seedScratch(t, m, "s1")
	seedScratch(t, m, "s2")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	operator := d.inst
	converger := newFlushGatedInstance("s2", errors.New("driver: process survived kill"))
	t.Cleanup(func() { gate.releaseDial(); operator.releaseStop(); converger.releaseStop(); m.Close() })

	// The converger's retry runs its flush and parks in the escalation, which is what
	// Close will be joining.
	m.recordOrphan("s2", converger, "container", "1.21")
	awaitEnter(t, converger.stopEntered)

	// The operator stop is dispatched now and parks in its flush's DIAL, before any
	// save-off — the state the window starts from.
	stopped := make(chan session.CommandResult, 1)
	go func() {
		stopped <- m.Handle(context.Background(),
			session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})
	}()
	awaitEnter(t, gate.dialEntered)
	if rec.hasFor("s1", "save-off") {
		t.Fatalf("s1 rcon lines = %v, want the flush to still be dialing", rec.allFor("s1"))
	}

	closed := make(chan struct{})
	go func() { m.Close(); close(closed) }()
	// Close has now read the ledger once and is inside its join, held by the parked
	// converger.
	select {
	case <-m.shutdown.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not cancel the shutdown context")
	}

	// Only now does the operator flush get its connection and disable auto-save: the
	// bracket is opened after the first drain.
	gate.releaseDial()
	awaitEnter(t, operator.stopEntered)
	if !rec.hasFor("s1", "save-off") {
		t.Fatalf("s1 rcon lines = %v, want the late flush to have issued save-off", rec.allFor("s1"))
	}

	// Let the join finish. Close must not return before settling the late bracket.
	converger.releaseStop()
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not return once the parked converger finished")
	}
	if !rec.hasFor("s1", "save-on") {
		t.Fatalf("s1 rcon lines = %v, want save-on: the Worker exited leaving a survivor with auto-save off", rec.allFor("s1"))
	}

	operator.releaseStop()
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("the operator stop did not return once released")
	}
}

// ...and once the ledger is SEALED, the flush must not open a bracket at all. That is
// what makes the read above the LAST one, and so what makes Close terminate: after
// the seal no debt can be created, so no third pass is needed and no drain loop can
// spin against a lane that keeps re-arming.
//
// Skipping the save-off costs this stop its quiesce — save-all and the settle still
// run, exactly as when a save-off fails (#1038) — and that cost is confined to a lane
// whose escalation cannot complete anyway: the seal is set after Close has joined
// everything it joins, so nothing reaching this branch has a stop the Worker will see
// through.
func TestFlushSkipsSaveOffOnceTheSaveOnLedgerIsSealed(t *testing.T) {
	rec := &saveOnRecorder{}
	gate := newGatedDialControl(rec)
	gate.onlyServer = "s1"
	gate.arm()
	d := &flushGatedDriver{}
	m := newSaveOnManager(t, d, gate.open)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}
	inst := d.inst
	t.Cleanup(func() { gate.releaseDial(); inst.releaseStop(); m.Close() })

	stopped := make(chan session.CommandResult, 1)
	go func() {
		stopped <- m.Handle(context.Background(),
			session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})
	}()
	awaitEnter(t, gate.dialEntered)

	// Nothing holds this Close: it joins its pumps and seals the ledger.
	closed := make(chan struct{})
	go func() { m.Close(); close(closed) }()
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not return")
	}

	// The flush gets its connection only now, with the ledger sealed.
	gate.releaseDial()
	awaitEnter(t, inst.stopEntered)
	if rec.hasFor("s1", "save-off") {
		t.Fatalf("s1 rcon lines = %v: a bracket was opened after the ledger was sealed, and nothing is left to close it", rec.allFor("s1"))
	}
	if !rec.hasFor("s1", "save-all") {
		t.Fatalf("s1 rcon lines = %v, want save-all: skipping the quiesce must not skip the flush", rec.allFor("s1"))
	}

	inst.releaseStop()
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("the operator stop did not return once released")
	}
}
