package instancemanager

import (
	"context"
	"errors"
	"sync"
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
	failLines map[string]error
}

func (r *saveOnRecorder) open(context.Context, string, string, string) (execution.ServerControl, error) {
	return recordedControl{rec: r}, nil
}

type recordedControl struct{ rec *saveOnRecorder }

func (c recordedControl) Execute(_ context.Context, line string) (string, error) {
	c.rec.mu.Lock()
	c.rec.lines = append(c.rec.lines, line)
	err := c.rec.failLines[line]
	c.rec.mu.Unlock()
	if err != nil {
		return "", err
	}
	return "ok", nil
}

func (c recordedControl) Close() error { return nil }

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

// newSaveOnManager builds a manager whose RCON goes through rec. It registers no
// Close: every test here closes explicitly and has to release its parked stop
// first, so the cleanup is per-test.
func newSaveOnManager(t *testing.T, d execution.ExecutionDriver, rec *saveOnRecorder) *Manager {
	t.Helper()
	m := New(map[string]execution.ExecutionDriver{"container": d}, t.TempDir(), rec.open)
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
// here. It is also the "no added shutdown latency" property — the restore runs
// beside the join, not in front of it.
func TestCloseRestoresSaveOnForAConvergerStopStillInFlight(t *testing.T) {
	rec := &saveOnRecorder{}
	m := newSaveOnManager(t, &fakeDriver{}, rec)
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
// not by the escalation (the process exits under it) and not by
// compose's stop_grace_period (nothing waits for that lane). Close's drain is
// keyed on the outstanding save-off rather than on which lane issued it, so this
// lane is covered by the same code (issue #3166).
func TestCloseRestoresSaveOnForAnOperatorStopStillInFlight(t *testing.T) {
	rec := &saveOnRecorder{}
	d := &flushGatedDriver{}
	m := newSaveOnManager(t, d, rec)
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

// A stop that CONFIRMED termination leaves nothing to restore: the container is
// gone, so a save-on at shutdown would dial a server that no longer exists. The
// outstanding save-off has to be forgotten when the stop resolves, not held until
// Close.
func TestCloseIssuesNoSaveOnOnceTheStopResolved(t *testing.T) {
	rec := &saveOnRecorder{}
	d := &flushOrphanDriver{stopAfter: 0} // the first Stop confirms termination
	m := newSaveOnManager(t, d, rec)
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

// A flush whose save-off never landed disabled nothing, so the shutdown has
// nothing to restore — the rule TestForcedFailedStopSkipsSaveOn already holds for
// the forced path and quiesceRunning's own saveOff flag for the snapshot bracket.
//
// The parked stop CONFIRMS termination once released, so the only save-on that
// could appear is the shutdown's: attemptStop's failure restore never runs. And
// the release waits for the shutdown to be cancelled, which happens after Close's
// drain has already decided about this id — without that anchor the stop could
// resolve first and forget the entry, and a drain that ignored the failed save-off
// would still pass.
func TestCloseIssuesNoSaveOnWhenTheStopFlushNeverDisabledAutoSave(t *testing.T) {
	rec := &saveOnRecorder{failLines: map[string]error{"save-off": errors.New("rcon down")}}
	m := newSaveOnManager(t, &fakeDriver{}, rec)
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

	if rec.has("save-on") {
		t.Fatalf("rcon lines = %v: the shutdown restored auto-save on a server whose save-off never landed", rec.all())
	}
}
