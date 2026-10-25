package containerdriver

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// stopClock is a fake clock for a stop's wait (issue #3436): After registers a
// waiter that fires only when the test advances the clock past it.
type stopClock struct {
	mu      sync.Mutex
	now     time.Time
	waiters []stopClockWaiter
}

type stopClockWaiter struct {
	at time.Time
	ch chan time.Time
}

func newStopClock() *stopClock {
	return &stopClock{now: time.Date(2026, 10, 18, 0, 0, 0, 0, time.UTC)}
}

func (c *stopClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *stopClock) After(d time.Duration) <-chan time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	ch := make(chan time.Time, 1)
	c.waiters = append(c.waiters, stopClockWaiter{at: c.now.Add(d), ch: ch})
	return ch
}

// waitingFor reports whether a waiter is due at exactly at.
func (c *stopClock) waitingFor(at time.Time) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, w := range c.waiters {
		if w.at.Equal(at) {
			return true
		}
	}
	return false
}

func (c *stopClock) pending() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.waiters)
}

func (c *stopClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
	kept := c.waiters[:0]
	for _, w := range c.waiters {
		if w.at.After(c.now) {
			kept = append(kept, w)
			continue
		}
		w.ch <- c.now
	}
	c.waiters = kept
}

// stopRun is one Stop driven on the fake clock: step advances the clock by one
// poll and reports whether Stop has returned.
type stopRun struct {
	t     *testing.T
	d     *Driver
	clock *stopClock
	done  chan error
	err   error
}

// awaitWaiting blocks until Stop is parked on the fake clock or has returned,
// reporting whether it returned.
func (r *stopRun) awaitWaiting() bool {
	r.t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case r.err = <-r.done:
			return true
		default:
		}
		if r.clock.pending() > 0 {
			return false
		}
		time.Sleep(time.Millisecond)
	}
	r.t.Fatal("Stop neither waited on the clock nor returned")
	return false
}

// awaitReturn waits for Stop to return without advancing the clock: the exit
// alone must end a wait that is parked on a longer timer.
func (r *stopRun) awaitReturn() {
	r.t.Helper()
	select {
	case r.err = <-r.done:
	case <-time.After(10 * time.Second):
		r.t.Fatal("Stop still waiting after the server exited")
	}
}

func (r *stopRun) step(d time.Duration) bool {
	r.t.Helper()
	r.clock.advance(d)
	return r.awaitWaiting()
}

// fakeWorld is a WorldProgress probe the test writes to.
type fakeWorld struct {
	writes atomic.Int64
	fail   atomic.Bool
}

func (w *fakeWorld) write() { w.writes.Add(1) }

func (w *fakeWorld) probe() (execution.WorldProgress, error) {
	if w.fail.Load() {
		return execution.WorldProgress{}, errors.New("scan could not finish")
	}
	n := w.writes.Load()
	return execution.WorldProgress{Files: 1, Bytes: n, Newest: time.Unix(n, 0)}, nil
}

const (
	testIdle = 30 * time.Second
	testPoll = 5 * time.Second
	testCap  = 360 * time.Second
)

// startStopRun starts a running instance on a driver with the production idle
// window, poll and cap on a fake clock, and begins a graceful Stop of it. Scans
// run inline, so each poll sees its own scan's result deterministically.
func startStopRun(t *testing.T, docker *fakeDocker, ctrl execution.ServerControl, ctrlErr error, world *fakeWorld) *stopRun {
	t.Helper()
	return startStopRunWith(t, docker, ctrl, ctrlErr, world.probe, func(f func()) { f() }, nil)
}

// startStopRunWith is startStopRun with the probe and the scan runner given.
func startStopRunWith(t *testing.T, docker *fakeDocker, ctrl execution.ServerControl, ctrlErr error,
	probe func() (execution.WorldProgress, error), runScan func(func()), flush func(context.Context) bool) *stopRun {
	t.Helper()
	return startStopRunOn(t, docker, ctrl, ctrlErr, probe, runScan, flush)
}

// startStopRunOn is startStopRunWith over any dockerAPI.
func startStopRunOn(t *testing.T, docker dockerAPI, ctrl execution.ServerControl, ctrlErr error,
	probe func() (execution.WorldProgress, error), runScan func(func()), flush func(context.Context) bool) *stopRun {
	t.Helper()
	d := New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return ctrl, ctrlErr
	}, Options{WorkerID: "w1", StopTimeout: testIdle, GameBindIP: "0.0.0.0", ReadinessTimeout: 20 * time.Millisecond})
	clock := newStopClock()
	d.now, d.after, d.runScan = clock.Now, clock.After, runScan
	if d.stopPoll != testPoll || d.stopCap != testCap {
		t.Fatalf("driver poll/cap = %v/%v, want the production %v/%v", d.stopPoll, d.stopCap, testPoll, testCap)
	}
	s := spec()
	s.WorldProgress = probe
	inst, err := d.Start(context.Background(), s)
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
	r := &stopRun{t: t, d: d, clock: clock, done: make(chan error, 1)}
	go func() { r.done <- inst.Stop(context.Background(), true, flush) }()
	return r
}

// A server whose world keeps being written after SIGTERM is saving, so the stop
// keeps waiting far past the idle window and never kills it; it exits by itself.
// Before issue #3436 the daemon SIGKILLed it 30 s after SIGTERM, mid-save.
func TestSignalPhaseWaitsWhileTheWorldIsWritten(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true // SIGTERM starts a long shutdown save
	world := &fakeWorld{}
	r := startStopRun(t, docker, nil, errors.New("rcon unavailable"), world)

	if r.awaitWaiting() {
		t.Fatalf("Stop returned before waiting: %v", r.err)
	}
	if !docker.stopWasCalled() {
		t.Fatal("no SIGTERM was sent with RCON unavailable")
	}
	for elapsed := testPoll; elapsed <= 4*testIdle; elapsed += testPoll {
		world.write()
		if r.step(testPoll) {
			t.Fatalf("Stop returned %v after %v of writes, want it still waiting", r.err, elapsed)
		}
	}
	if docker.killWasCalled() {
		t.Fatal("the stop killed a server that was still writing its world")
	}

	docker.exit(143, nil) // the save completes and the JVM exits
	if !r.awaitWaiting() && !r.step(testPoll) {
		t.Fatal("Stop still waiting after the server exited")
	}
	if r.err != nil || docker.killWasCalled() {
		t.Fatalf("Stop = %v, kill = %v; want a clean stop with no kill", r.err, docker.killWasCalled())
	}
}

// A server that writes nothing after SIGTERM is killed once the idle window has
// passed since its last write: a hung server is escalated as fast as before.
func TestSignalPhaseKillsAfterTheIdleWindow(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true
	world := &fakeWorld{}
	r := startStopRun(t, docker, nil, errors.New("rcon unavailable"), world)
	r.awaitWaiting()

	// Two writes, then silence: the idle window runs from the last write.
	world.write()
	r.step(testPoll)
	world.write()
	r.step(testPoll)
	for idle := testPoll; idle < testIdle; idle += testPoll {
		if r.step(testPoll) {
			t.Fatalf("Stop returned %v after %v idle, want it waiting the full %v", r.err, idle, testIdle)
		}
	}
	if !r.step(testPoll) {
		t.Fatalf("Stop still waiting %v after the last write, want the kill", testIdle)
	}
	if r.err != nil || !docker.killWasCalled() {
		t.Fatalf("Stop = %v, kill = %v; want the idle server killed", r.err, docker.killWasCalled())
	}
}

// A server that never stops writing is killed at the cap, measured from the start
// of the escalation, however recent its last write.
func TestStopKillsAtTheCap(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true
	world := &fakeWorld{}
	r := startStopRun(t, docker, nil, errors.New("rcon unavailable"), world)
	r.awaitWaiting()

	for elapsed := testPoll; elapsed < testCap; elapsed += testPoll {
		world.write()
		if r.step(testPoll) {
			t.Fatalf("Stop returned %v after %v, want it waiting until the %v cap", r.err, elapsed, testCap)
		}
	}
	world.write()
	if !r.step(testPoll) {
		t.Fatalf("Stop still waiting at the %v cap", testCap)
	}
	if r.err != nil || !docker.killWasCalled() {
		t.Fatalf("Stop = %v, kill = %v; want the kill at the cap", r.err, docker.killWasCalled())
	}
}

// A scan that cannot tell counts as a write: the stop does not kill a save it
// cannot see, and the cap still bounds it.
func TestStopCountsAFailedScanAsProgress(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true
	world := &fakeWorld{}
	world.fail.Store(true)
	r := startStopRun(t, docker, nil, errors.New("rcon unavailable"), world)
	r.awaitWaiting()

	for elapsed := testPoll; elapsed <= 2*testIdle; elapsed += testPoll {
		if r.step(testPoll) {
			t.Fatalf("Stop returned %v after %v of failed scans, want it waiting", r.err, elapsed)
		}
	}
	docker.exit(143, nil)
	r.step(testPoll)
	if docker.killWasCalled() {
		t.Fatal("the stop killed a server whose world it could not scan before the cap")
	}
}

// The wait after RCON "stop" follows the same rule: while the world is written no
// SIGTERM is sent, and once it has gone the idle window without a write the stop
// escalates to SIGTERM (not straight to a kill), which here ends the server.
func TestRCONPhaseWaitsWhileTheWorldIsWrittenThenSendsSIGTERM(t *testing.T) {
	docker := newFakeDocker()
	ctrl := &fakeControl{}
	world := &fakeWorld{}
	r := startStopRun(t, docker, ctrl, nil, world)
	if r.awaitWaiting() {
		t.Fatalf("Stop returned before waiting: %v", r.err)
	}
	if !ctrl.stopCalled {
		t.Fatal("RCON stop was not sent")
	}

	for elapsed := testPoll; elapsed <= 3*testIdle; elapsed += testPoll {
		world.write()
		if r.step(testPoll) {
			t.Fatalf("Stop returned %v after %v of writes, want it waiting", r.err, elapsed)
		}
		if docker.stopWasCalled() {
			t.Fatalf("SIGTERM sent after %v while the world was still being written", elapsed)
		}
	}
	for idle := testPoll; idle < testIdle; idle += testPoll {
		if r.step(testPoll) || docker.stopWasCalled() {
			t.Fatalf("escalated after %v idle, want the full %v", idle, testIdle)
		}
	}
	returned := r.step(testPoll)
	if !docker.stopWasCalled() {
		t.Fatalf("no SIGTERM after %v idle following RCON stop", testIdle)
	}
	if !returned {
		t.Fatal("Stop still waiting after SIGTERM ended the server")
	}
	if r.err != nil || docker.killWasCalled() {
		t.Fatalf("Stop = %v, kill = %v; want SIGTERM to have ended the server", r.err, docker.killWasCalled())
	}
}

// stalledProbe is a probe whose calls from the n-th on block until the test ends,
// modelling a scan wedged on the filesystem (issue #3436 review).
func stalledProbe(t *testing.T, from int) func() (execution.WorldProgress, error) {
	t.Helper()
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	var calls atomic.Int64
	return func() (execution.WorldProgress, error) {
		if calls.Add(1) >= int64(from) {
			<-release
		}
		return execution.WorldProgress{}, nil
	}
}

// runToCap steps the clock until Stop returns and checks it was killed at the cap,
// at most one scan timeout late.
func runToCap(t *testing.T, r *stopRun, docker *fakeDocker) {
	t.Helper()
	elapsed := time.Duration(0)
	for !r.step(testPoll) {
		elapsed += testPoll
		if elapsed > 2*testCap {
			t.Fatalf("Stop still waiting %v into the stop, past the %v cap", elapsed, testCap)
		}
	}
	elapsed += testPoll
	if elapsed < testCap || elapsed > testCap+testPoll {
		t.Fatalf("Stop returned %v into the stop, want the %v cap (at most one scan timeout late)", elapsed, testCap)
	}
	if r.err != nil || !docker.killWasCalled() {
		t.Fatalf("Stop = %v, kill = %v; want the kill at the cap", r.err, docker.killWasCalled())
	}
}

// A baseline scan that never answers does not hold back the stop signal, and the
// stop is still killed at the cap: an unanswered scan counts as a write, so it
// keeps the wait alive, but only until the cap.
func TestStalledBaselineScanStillSignalsAndKillsAtTheCap(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true
	r := startStopRunWith(t, docker, nil, errors.New("rcon unavailable"), stalledProbe(t, 1), func(f func()) { go f() }, nil)

	if r.awaitWaiting() {
		t.Fatalf("Stop returned before waiting: %v", r.err)
	}
	if !docker.terminateCalled() {
		t.Fatal("no SIGTERM while the baseline scan is outstanding")
	}
	runToCap(t, r, docker)
}

// A later scan that stalls does not stall the wait: the stop is still killed at
// the cap rather than hanging with no daemon-side SIGKILL to end it.
func TestStalledLaterScanStillKillsAtTheCap(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true
	r := startStopRunWith(t, docker, nil, errors.New("rcon unavailable"), stalledProbe(t, 2), func(f func()) { go f() }, nil)

	if r.awaitWaiting() {
		t.Fatalf("Stop returned before waiting: %v", r.err)
	}
	runToCap(t, r, docker)
}

// The exit is still seen while a scan is outstanding: the stop ends cleanly at
// once, with no kill.
func TestExitDuringAStalledScanEndsTheStop(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true
	r := startStopRunWith(t, docker, nil, errors.New("rcon unavailable"), stalledProbe(t, 1), func(f func()) { go f() }, nil)
	r.awaitWaiting()
	if r.step(testPoll) {
		t.Fatalf("Stop returned %v with the server still running", r.err)
	}

	// The clock is not advanced: the exit alone must end the wait on the scan.
	docker.exit(143, nil)
	select {
	case r.err = <-r.done:
	case <-time.After(10 * time.Second):
		t.Fatal("Stop still waiting on a stalled scan after the server exited")
	}
	if r.err != nil || docker.killWasCalled() {
		t.Fatalf("Stop = %v, kill = %v; want a clean stop", r.err, docker.killWasCalled())
	}
}

// An instance stop sends SIGTERM through the kill endpoint and never through
// `docker stop`, whose daemon-side grace would SIGKILL a save the driver is still
// watching (issue #3436).
func TestInstanceStopSignalsWithoutDockerStop(t *testing.T) {
	docker := newFakeDocker()
	world := &fakeWorld{}
	r := startStopRun(t, docker, nil, errors.New("rcon unavailable"), world)
	if !r.awaitWaiting() && !r.step(testPoll) {
		t.Fatal("Stop still waiting after SIGTERM ended the server")
	}
	if r.err != nil {
		t.Fatalf("Stop: %v", r.err)
	}
	if !docker.terminateCalled() || docker.daemonStopCalled() {
		t.Fatalf("terminate = %v, docker stop = %v; want SIGTERM via the kill endpoint only",
			docker.terminateCalled(), docker.daemonStopCalled())
	}
}

// A Worker that shuts down in the middle of a stop hands the stop to the daemon
// (issue #3436 review): `docker stop` with the grace the Worker's own stop had
// left, so the daemon still SIGKILLs a server that never finishes even after the
// Worker has exited. Before this, the kill lived only in the Worker process.
func TestShutdownMidStopHandsTheKillToTheDaemon(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true
	world := &fakeWorld{}
	r := startStopRun(t, docker, nil, errors.New("rcon unavailable"), world)
	r.awaitWaiting()
	for range 3 {
		world.write()
		if r.step(testPoll) {
			t.Fatalf("Stop returned %v while the world was being written", r.err)
		}
	}

	if err := r.d.HandOffStops(context.Background()); err != nil {
		t.Fatalf("HandOffStops: %v", err)
	}
	want := []handOffCall{{id: "container-1", grace: testCap - 3*testPoll}}
	if got := docker.handOffCalls(); len(got) != 1 || got[0] != want[0] {
		t.Fatalf("hand-offs = %+v, want %+v (the cap's remaining grace)", got, want)
	}

	// The Worker's own stop keeps running while the process does.
	docker.exit(143, nil)
	if !r.awaitWaiting() && !r.step(testPoll) {
		t.Fatal("Stop still waiting after the server exited")
	}
	if r.err != nil || docker.killWasCalled() {
		t.Fatalf("Stop = %v, kill = %v; want a clean stop", r.err, docker.killWasCalled())
	}
}

// awaitHandOffs waits for n hand-offs: a stop that starts after the shutdown hands
// itself off in the background.
func awaitHandOffs(t *testing.T, docker *fakeDocker, n int) []handOffCall {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if got := docker.handOffCalls(); len(got) >= n {
			return got
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("hand-offs = %+v, want %d", docker.handOffCalls(), n)
	return nil
}

// A hand-off that comes after the stop has committed to killing its flushed world
// kills too, through the daemon, and sends no SIGTERM: the world is on disk, and a
// shutdown save started now would be cut short by the Worker's own kill.
func TestHandOffAfterTheKillCommitKillsInsteadOfSignalling(t *testing.T) {
	docker := newFakeDocker()
	flush := func(context.Context) bool { return true }
	d := New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("rcon unavailable")
	}, Options{WorkerID: "w1", StopTimeout: 50 * time.Millisecond, GameBindIP: "0.0.0.0", ReadinessTimeout: 20 * time.Millisecond})
	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
	docker.beforeKill = func() {
		if err := d.HandOffStops(context.Background()); err != nil {
			t.Errorf("HandOffStops: %v", err)
		}
	}

	if err := inst.Stop(context.Background(), true, flush); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if got := docker.handOffCalls(); len(got) != 0 {
		t.Fatalf("hand-offs = %+v, want none: the stop had committed to the kill", got)
	}
	if docker.terminateCalled() || docker.killCount() != 2 {
		t.Fatalf("terminate = %v, kills = %d; want no SIGTERM and the hand-off's kill beside the Worker's",
			docker.terminateCalled(), docker.killCount())
	}
}

// Once the escalation starts, the hand-off grace narrows to the cap from there:
// the flush's unspent budget is no longer something the Worker's stop could use.
func TestHandOffAfterTheFlushNarrowsToTheCap(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true
	release := make(chan struct{})
	flushing := make(chan struct{})
	flush := func(context.Context) bool {
		close(flushing)
		<-release
		return false // the flush could not settle; the stop escalates
	}
	r := startStopRunWith(t, docker, nil, errors.New("rcon unavailable"), (&fakeWorld{}).probe,
		func(f func()) { f() }, flush)
	<-flushing
	r.clock.advance(30 * time.Second) // the flush spends 30 s of its 90 s budget
	close(release)
	if r.awaitWaiting() {
		t.Fatalf("Stop returned before waiting: %v", r.err)
	}

	if err := r.d.HandOffStops(context.Background()); err != nil {
		t.Fatalf("HandOffStops: %v", err)
	}
	want := handOffCall{id: "container-1", grace: testCap}
	if got := docker.handOffCalls(); len(got) != 1 || got[0] != want {
		t.Fatalf("hand-offs = %+v, want %+v (the cap from the escalation's start)", got, want)
	}
}

// A SIGTERM request that errors with no hand-off and a world nobody writes is
// escalated after the idle window, not at once: the signal may have landed even
// though the request failed. This is a deliberate change from the fixed-grace
// stop, which killed as soon as its `docker stop` call errored.
func TestSIGTERMErrorKillsAfterTheIdleWindow(t *testing.T) {
	docker := newFakeDocker()
	docker.stopErr = errors.New("daemon hiccup")
	r := startStopRun(t, docker, nil, errors.New("rcon unavailable"), &fakeWorld{})
	if r.awaitWaiting() {
		t.Fatalf("Stop returned %v at once after the SIGTERM error, kill = %v; want the idle window first",
			r.err, docker.killWasCalled())
	}
	for idle := testPoll; idle < testIdle; idle += testPoll {
		if r.step(testPoll) {
			t.Fatalf("Stop returned %v after %v idle, want the full %v", r.err, idle, testIdle)
		}
	}
	if !r.step(testPoll) {
		t.Fatalf("Stop still waiting %v after the SIGTERM error", testIdle)
	}
	if r.err != nil || !docker.killWasCalled() {
		t.Fatalf("Stop = %v, kill = %v; want the kill after the idle window", r.err, docker.killWasCalled())
	}
}

// A shutdown that comes while the pre-stop flush is still running hands the stop
// off with the flush's budget on top of the cap — the Worker's own stop could
// still have run that long — and once the flush settles the Worker leaves the
// stop to the daemon: no immediate kill and no signal of its own, only the wait
// for the exit (issue #3436 review, one owner per kill).
func TestHandOffDuringTheFlushLeavesTheStopToTheDaemon(t *testing.T) {
	docker := newFakeDocker()
	ctrl := &fakeControl{}
	release := make(chan struct{})
	flushing := make(chan struct{})
	flush := func(context.Context) bool {
		close(flushing)
		<-release
		return true // the flush settled
	}
	r := startStopRunWith(t, docker, ctrl, nil, (&fakeWorld{}).probe, func(f func()) { f() }, flush)
	<-flushing
	if err := r.d.HandOffStops(context.Background()); err != nil {
		t.Fatalf("HandOffStops: %v", err)
	}
	want := handOffCall{id: "container-1", grace: defaultFlushTimeout + testCap}
	if got := docker.handOffCalls(); len(got) != 1 || got[0] != want {
		t.Fatalf("hand-offs = %+v, want %+v", got, want)
	}
	close(release)

	if r.awaitWaiting() {
		t.Fatalf("Stop returned %v, kill = %v; want it waiting for the daemon's stop", r.err, docker.killWasCalled())
	}
	for range 4 {
		if r.step(testIdle) {
			t.Fatalf("Stop returned %v while the daemon owns the stop", r.err)
		}
	}
	if docker.killWasCalled() || docker.terminateCalled() || ctrl.stopCalled {
		t.Fatalf("kill = %v, SIGTERM = %v, RCON stop = %v; want no signal from the Worker after the hand-off",
			docker.killWasCalled(), docker.terminateCalled(), ctrl.stopCalled)
	}
	docker.exit(143, nil) // the daemon's SIGTERM ends the server
	r.awaitReturn()
	if r.err != nil || docker.killWasCalled() {
		t.Fatalf("Stop = %v, kill = %v; want a clean stop", r.err, docker.killWasCalled())
	}
}

// A hand-off late in the idle window that follows a failed SIGTERM request takes
// the stop for good: the daemon's `docker stop` carries the cap's remaining grace,
// and the Worker no longer kills when its idle window runs out — that window
// started before the daemon's SIGTERM, which may only now have started the save
// (issue #3436 review).
func TestLateHandOffAfterAFailedSIGTERMLeavesTheKillToTheDaemon(t *testing.T) {
	docker := newFakeDocker()
	docker.stopErr = errors.New("daemon hiccup") // the Worker's SIGTERM never lands
	r := startStopRun(t, docker, nil, errors.New("rcon unavailable"), &fakeWorld{})
	r.awaitWaiting()
	for range 5 {
		if r.step(testPoll) {
			t.Fatalf("Stop returned %v inside the idle window", r.err)
		}
	}

	if err := r.d.HandOffStops(context.Background()); err != nil {
		t.Fatalf("HandOffStops: %v", err)
	}
	want := handOffCall{id: "container-1", grace: testCap - 5*testPoll}
	if got := docker.handOffCalls(); len(got) != 1 || got[0] != want {
		t.Fatalf("hand-offs = %+v, want %+v", got, want)
	}
	for elapsed := 6 * testPoll; elapsed <= 4*testIdle; elapsed += testPoll {
		if r.step(testPoll) {
			t.Fatalf("Stop returned %v at %v, after the hand-off", r.err, elapsed)
		}
	}
	if docker.killWasCalled() {
		t.Fatal("the Worker killed a stop it had handed to the daemon")
	}
	docker.exit(137, nil) // the daemon's kill, or the server's own exit
	r.awaitReturn()
	if r.err != nil || docker.killWasCalled() {
		t.Fatalf("Stop = %v, kill = %v; want the stop to end on the exit alone", r.err, docker.killWasCalled())
	}
}

// A hand-off that lands just as the Worker kills after its idle window matches
// that kill and sends no `docker stop`: the Worker committed to the kill first, so
// exactly one owner acts, and the daemon's part is the same kill, never a SIGTERM
// that would start a save (issue #3436 review).
func TestHandOffAtTheIdleKillMatchesTheKill(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true
	r := startStopRun(t, docker, nil, errors.New("rcon unavailable"), &fakeWorld{})
	docker.mu.Lock()
	docker.beforeKill = func() {
		if err := r.d.HandOffStops(context.Background()); err != nil {
			t.Errorf("HandOffStops: %v", err)
		}
	}
	docker.mu.Unlock()
	r.awaitWaiting()
	elapsed := time.Duration(0)
	for !r.step(testPoll) {
		if elapsed += testPoll; elapsed > 2*testIdle {
			t.Fatal("Stop did not kill after its idle window")
		}
	}
	if r.err != nil {
		t.Fatalf("Stop: %v", r.err)
	}
	if got := docker.handOffCalls(); len(got) != 0 {
		t.Fatalf("hand-offs = %+v, want no docker stop after the Worker committed to its kill", got)
	}
	if docker.killCount() != 2 {
		t.Fatalf("kills = %d, want the Worker's and the hand-off's matching one", docker.killCount())
	}
}

// A stop that starts after the shutdown hands itself off at once: the daemon owns
// it from the start, so the Worker sends no signal and only waits for the exit.
// The request goes out in the background, so a slow daemon does not hold the stop
// and its budget (MaxStopDuration, CloseBound) is unchanged.
func TestStopStartedAfterShutdownHandsItselfOff(t *testing.T) {
	docker := newFakeDocker()
	block := make(chan struct{})
	docker.handOffBlock = block
	d := New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return nil, errors.New("rcon unavailable")
	}, Options{WorkerID: "w1", StopTimeout: 50 * time.Millisecond, GameBindIP: "0.0.0.0", ReadinessTimeout: 20 * time.Millisecond})
	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
	if err := d.HandOffStops(context.Background()); err != nil {
		t.Fatalf("HandOffStops: %v", err)
	}
	if got := docker.handOffCalls(); len(got) != 0 {
		t.Fatalf("hand-offs with no stop in flight = %+v, want none", got)
	}

	done := make(chan error, 1)
	go func() { done <- inst.Stop(context.Background(), true) }()
	time.Sleep(200 * time.Millisecond) // four idle windows: a Worker that owned the stop would have signalled
	if docker.terminateCalled() || docker.killWasCalled() {
		t.Fatalf("SIGTERM = %v, kill = %v; want no signal from the Worker on a handed-off stop",
			docker.terminateCalled(), docker.killWasCalled())
	}
	docker.exit(143, nil) // the daemon has not even answered yet
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Stop: %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Stop waited on a hand-off the daemon has not taken yet")
	}
	close(block)
	got := awaitHandOffs(t, docker, 1)
	if got[0].id != "container-1" || got[0].grace < defaultStopCap-time.Second || got[0].grace > defaultStopCap {
		t.Fatalf("hand-offs = %+v, want one for container-1 with about the %v cap", got, defaultStopCap)
	}
}

// awaitCondition polls cond in real time; the stop's goroutines run concurrently
// with the test, which advances the fake clock only once they have parked.
func awaitCondition(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// handOffAsync runs HandOffStops with a short real-time bound, as Close does with
// its own, and reports whether it returned within a generous one: a failed or
// stuck first request must not hold the caller.
func handOffAsync(t *testing.T, d *Driver) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	done := make(chan struct{})
	go func() {
		_ = d.HandOffStops(ctx)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("HandOffStops held its caller past its context")
	}
}

// A hand-off is irreversible (issue #3436 review): one whose request fails after
// the stop is already waiting on the daemon is retried in the background against
// the same due time, and the Worker never signals the server again — the failed
// request may well have reached the daemon. The stop ends when the server exits.
func TestFailedHandOffIsRetriedAndTheWorkerNeverKills(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true
	release := make(chan struct{})
	docker.handOffHook = func(n int) error {
		if n == 0 {
			<-release
			return errors.New("daemon hiccup")
		}
		return nil
	}
	r := startStopRun(t, docker, nil, errors.New("rcon unavailable"), &fakeWorld{})
	r.awaitWaiting()
	t0 := r.clock.Now()
	r.step(testPoll)
	r.step(testPoll) // t = 10 s: the hand-off claims, due at t = 360 s
	handOffAsync(t, r.d)
	for range 4 {
		r.step(testPoll) // t = 30 s: the idle window ends; the daemon owns the kill
	}
	due := t0.Add(testCap)
	awaitCondition(t, "the stop waiting on the daemon", func() bool {
		return r.clock.waitingFor(due.Add(stopDeadlineGrace))
	})

	close(release) // the first request fails now
	awaitCondition(t, "the retry's pause", func() bool { return r.clock.pending() >= 2 })
	r.clock.advance(stopHandOffRetryInterval)
	got := awaitHandOffs(t, docker, 1)
	if want := (handOffCall{id: "container-1", grace: due.Sub(r.clock.Now())}); got[0] != want {
		t.Fatalf("retried hand-off = %+v, want %+v (the same due time)", got[0], want)
	}
	if docker.killWasCalled() || docker.terminateCount() != 1 {
		t.Fatalf("kill = %v, SIGTERMs = %d; want no Worker signal after the hand-off",
			docker.killWasCalled(), docker.terminateCount())
	}
	docker.exit(143, nil)
	r.awaitReturn()
	if r.err != nil || docker.killWasCalled() {
		t.Fatalf("Stop = %v, kill = %v; want a clean stop", r.err, docker.killWasCalled())
	}
}

// When no hand-off request ever reaches the daemon, the stop still never comes
// back to the Worker: it fails when the daemon's kill is overdue, and the Worker
// sends no kill — the next boot's Sweep owns the container.
func TestHandOffThatNeverLandsFailsTheStopAtItsDueTime(t *testing.T) {
	docker := newFakeDocker()
	docker.stopNoExit = true
	docker.handOffHook = func(int) error { return errors.New("daemon unreachable") }
	r := startStopRun(t, docker, nil, errors.New("rcon unavailable"), &fakeWorld{})
	r.awaitWaiting()
	t0 := r.clock.Now()
	r.step(testPoll)
	r.step(testPoll) // t = 10 s: the hand-off claims, due at t = 360 s
	handOffAsync(t, r.d)
	failAt := t0.Add(testCap + stopDeadlineGrace)
	for r.clock.Now().Before(t0.Add(30 * time.Second)) {
		r.clock.advance(testPoll)
		time.Sleep(time.Millisecond)
	}
	awaitCondition(t, "the stop waiting on the daemon", func() bool { return r.clock.waitingFor(failAt) })

	r.clock.advance(failAt.Sub(r.clock.Now()) - testPoll)
	select {
	case err := <-r.done:
		t.Fatalf("Stop returned %v before the daemon's kill was overdue", err)
	case <-time.After(100 * time.Millisecond):
	}
	r.clock.advance(testPoll)
	r.awaitReturn()
	if r.err == nil {
		t.Fatal("Stop = nil, want a failed stop: the container was never confirmed gone")
	}
	if docker.killWasCalled() {
		t.Fatal("the Worker killed a stop it had handed off")
	}
	if n := docker.handOffAttemptCount(); n < 2 {
		t.Fatalf("hand-off attempts = %d, want the failed request retried", n)
	}
}

// delayedTransport holds every request before sending it, modelling a daemon
// connection slow enough that the hand-off's own timeout passes first.
type delayedTransport struct {
	inner http.RoundTripper
	delay time.Duration
}

func (d delayedTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	time.Sleep(d.delay)
	return d.inner.RoundTrip(r)
}

// engineHandOff is the fake docker with HandOff going through a real Engine client.
type engineHandOff struct {
	*fakeDocker
	engine *EngineClient
}

func (e engineHandOff) HandOff(ctx context.Context, id string, grace time.Duration) error {
	return e.engine.HandOff(ctx, id, grace)
}

// A hand-off request that times out may still reach the daemon, and here it does,
// after the flush has completed: had the timeout handed the stop back, the
// flushed path would SIGKILL the save that late `docker stop` starts. Uncertain
// delivery counts as delivered, so the Worker never kills, and the request the
// daemon gets carries the grace of the original due time (issue #3436 review).
func TestTimedOutHandOffThatLandsLateIsNeverKilled(t *testing.T) {
	saved := stopHandOffCallTimeout
	stopHandOffCallTimeout = 50 * time.Millisecond
	t.Cleanup(func() { stopHandOffCallTimeout = saved })

	got := make(chan string, 4)
	daemon := startFakeDaemon(t, func(w http.ResponseWriter, r *http.Request) {
		got <- r.URL.Path + "?" + r.URL.RawQuery
		w.WriteHeader(http.StatusNoContent)
	})
	engine := daemon.client(t)
	engine.http.Transport = delayedTransport{inner: engine.http.Transport, delay: 300 * time.Millisecond}
	docker := newFakeDocker()
	release := make(chan struct{})
	flushing := make(chan struct{})
	flush := func(context.Context) bool {
		close(flushing)
		<-release
		return true // the flush settled
	}
	r := startStopRunOn(t, engineHandOff{fakeDocker: docker, engine: engine}, nil, errors.New("rcon unavailable"),
		(&fakeWorld{}).probe, func(f func()) { f() }, flush)
	<-flushing
	handOffAsync(t, r.d) // the first request times out, still in flight
	close(release)       // the flush completes before the request lands

	select {
	case req := <-got:
		if want := fmt.Sprintf("/containers/container-1/stop?t=%d", int((defaultFlushTimeout + testCap).Seconds())); !strings.HasSuffix(req, want) {
			t.Fatalf("daemon got %q, want a request ending %q", req, want)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the late hand-off never reached the daemon")
	}
	if r.awaitWaiting() {
		t.Fatalf("Stop returned %v, kill = %v; want it waiting on the daemon", r.err, docker.killWasCalled())
	}
	if docker.killWasCalled() {
		t.Fatal("the Worker killed a flushed stop whose hand-off was only uncertain")
	}
	docker.exit(143, nil)
	r.awaitReturn()
	if r.err != nil || docker.killWasCalled() {
		t.Fatalf("Stop = %v, kill = %v; want a clean stop", r.err, docker.killWasCalled())
	}
}

// startDeliver claims a stop due in 5 s on a fake clock and runs its delivery,
// returning the clock, the first attempt's result and the delivery's end.
func startDeliver(t *testing.T, docker *fakeDocker) (*stopClock, chan error, chan struct{}) {
	t.Helper()
	d := New(docker, images(), nil, Options{WorkerID: "w1", GameBindIP: "0.0.0.0"})
	clock := newStopClock()
	d.now, d.after = clock.Now, clock.After
	s := &inflightStop{serverID: "s1", containerID: "container-1"}
	s.setKillBy(clock.Now().Add(5 * time.Second))
	kill, _ := s.claim(clock.Now())
	first, done := make(chan error, 1), make(chan struct{})
	go func() {
		d.handOffs.deliver(s, kill, first)
		close(done)
	}()
	return clock, first, done
}

func isClosed(ch chan struct{}) bool {
	select {
	case <-ch:
		return true
	default:
		return false
	}
}

// A hand-off request that fails after the daemon's kill came due starts no other:
// the due time is read after the call, not before it (issue #3436 review). A late
// `docker stop` would only lengthen what the stop has already given up on.
func TestHandOffSendsNothingAfterTheDueTimePassedDuringACall(t *testing.T) {
	docker := newFakeDocker()
	release := make(chan struct{})
	docker.handOffHook = func(n int) error {
		if n == 0 {
			<-release
		}
		return errors.New("daemon hiccup")
	}
	clock, first, done := startDeliver(t, docker)
	awaitCondition(t, "the first request", func() bool { return docker.handOffAttemptCount() == 1 })
	clock.advance(10 * time.Second) // the call outlasts the due time
	close(release)
	if err := <-first; err == nil {
		t.Fatal("first attempt = nil, want its error")
	}
	awaitCondition(t, "the delivery to end or pause", func() bool { return isClosed(done) || clock.pending() > 0 })
	clock.advance(stopHandOffRetryInterval)
	awaitCondition(t, "the delivery to end", func() bool { return isClosed(done) })
	if n := docker.handOffAttemptCount(); n != 1 {
		t.Fatalf("hand-off attempts = %d, want 1: none may start once the kill is due", n)
	}
}

// Nor does one whose retry pause runs past the due time: the time is read again
// before each request.
func TestHandOffSendsNothingAfterTheDueTimePassedDuringThePause(t *testing.T) {
	docker := newFakeDocker()
	docker.handOffHook = func(int) error { return errors.New("daemon unreachable") }
	clock, first, done := startDeliver(t, docker)
	if err := <-first; err == nil {
		t.Fatal("first attempt = nil, want its error")
	}
	awaitCondition(t, "the retry pause", func() bool { return clock.pending() > 0 })
	clock.advance(10 * time.Second) // the pause ends after the due time
	awaitCondition(t, "the delivery to end", func() bool { return isClosed(done) })
	if n := docker.handOffAttemptCount(); n != 1 {
		t.Fatalf("hand-off attempts = %d, want 1: none may start once the kill is due", n)
	}
}
