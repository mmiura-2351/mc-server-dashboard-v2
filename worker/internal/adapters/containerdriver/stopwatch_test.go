package containerdriver

import (
	"context"
	"errors"
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
// window, poll and cap on a fake clock, and begins a graceful Stop of it.
func startStopRun(t *testing.T, docker *fakeDocker, ctrl execution.ServerControl, ctrlErr error, world *fakeWorld) *stopRun {
	t.Helper()
	d := New(docker, images(), func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
		return ctrl, ctrlErr
	}, Options{WorkerID: "w1", StopTimeout: testIdle, GameBindIP: "0.0.0.0", ReadinessTimeout: 20 * time.Millisecond})
	clock := newStopClock()
	d.now, d.after = clock.Now, clock.After
	if d.stopPoll != testPoll || d.stopCap != testCap {
		t.Fatalf("driver poll/cap = %v/%v, want the production %v/%v", d.stopPoll, d.stopCap, testPoll, testCap)
	}
	s := spec()
	s.WorldProgress = world.probe
	inst, err := d.Start(context.Background(), s)
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
	r := &stopRun{t: t, clock: clock, done: make(chan error, 1)}
	go func() { r.done <- inst.Stop(context.Background(), true) }()
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
