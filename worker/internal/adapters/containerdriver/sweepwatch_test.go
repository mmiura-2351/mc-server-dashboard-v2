package containerdriver

import (
	"context"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// sweepDocker models several orphans for the startup Sweep (issue #3445): each
// listed container exits only when the test says so or when it is killed, and
// every signal is recorded with the fake clock's time. The embedded fakeDocker
// serves the endpoints the sweep never calls.
type sweepDocker struct {
	*fakeDocker
	clock *stopClock

	mu          sync.Mutex
	list        []Container
	exited      map[string]chan struct{}
	terminated  []string
	daemonStops []string
	kills       map[string]time.Time
	removed     []string
}

func newSweepDocker(clock *stopClock, list ...Container) *sweepDocker {
	f := &sweepDocker{fakeDocker: newFakeDocker(), clock: clock, list: list,
		exited: map[string]chan struct{}{}, kills: map[string]time.Time{}}
	for _, c := range list {
		f.exited[c.ID] = make(chan struct{})
	}
	return f
}

func (f *sweepDocker) List(context.Context, string, string) ([]Container, error) {
	return f.list, nil
}

// Terminate is SIGTERM: the orphan starts its shutdown save and keeps running.
func (f *sweepDocker) Terminate(_ context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.terminated = append(f.terminated, id)
	return nil
}

// Stop is `docker stop`: the daemon ends the container within its grace.
func (f *sweepDocker) Stop(_ context.Context, id string, _ time.Duration) error {
	f.mu.Lock()
	f.daemonStops = append(f.daemonStops, id)
	f.mu.Unlock()
	f.exit(id)
	return nil
}

func (f *sweepDocker) Kill(_ context.Context, id string) error {
	f.mu.Lock()
	f.kills[id] = f.clock.Now()
	f.mu.Unlock()
	f.exit(id)
	return nil
}

func (f *sweepDocker) Wait(ctx context.Context, id string) (int64, error) {
	select {
	case <-f.exited[id]:
		return 0, nil
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

func (f *sweepDocker) Remove(_ context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removed = append(f.removed, id)
	return nil
}

// exit ends the orphan's process: its shutdown save finished.
func (f *sweepDocker) exit(id string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	select {
	case <-f.exited[id]:
	default:
		close(f.exited[id])
	}
}

func (f *sweepDocker) killed() map[string]time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := map[string]time.Time{}
	for id, at := range f.kills {
		out[id] = at
	}
	return out
}

// sweepRun is one Sweep driven on the fake clock.
type sweepRun struct {
	t      *testing.T
	clock  *stopClock
	docker *sweepDocker
	done   chan error
	err    error
	// cancel cancels the Sweep's context: the Worker is shutting down.
	cancel context.CancelFunc
	// watched is how many orphans the run waits on: a step is over once each of
	// them is parked on the clock again.
	watched int
}

// startSweep runs Sweep on a driver with the production idle window, poll and
// cap on a fake clock, scans running inline, and a WorldProgress that hands out
// the world the test registered for each working set.
func startSweep(t *testing.T, docker *sweepDocker, worlds map[string]*fakeWorld, logger *slog.Logger) *sweepRun {
	t.Helper()
	d := New(docker, images(), nil, Options{
		WorkerID: "w1",
		WorldProgress: func(workingDir string) func() (execution.WorldProgress, error) {
			w, ok := worlds[workingDir]
			if !ok {
				t.Errorf("WorldProgress asked for %q, which no orphan mounts", workingDir)
				return nil
			}
			return w.probe
		},
		Logger: logger,
	})
	d.now, d.after, d.runScan = docker.clock.Now, docker.clock.After, func(f func()) { f() }
	if d.stopTimeout != testIdle || d.stopPoll != testPoll || d.stopCap != testCap {
		t.Fatalf("driver idle/poll/cap = %v/%v/%v, want the production %v/%v/%v",
			d.stopTimeout, d.stopPoll, d.stopCap, testIdle, testPoll, testCap)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	r := &sweepRun{t: t, clock: docker.clock, docker: docker, done: make(chan error, 1), cancel: cancel, watched: len(worlds)}
	go func() { r.done <- d.Sweep(ctx) }()
	return r
}

// awaitParked blocks until every watched orphan is waiting on the clock or Sweep
// has returned, reporting whether it returned.
func (r *sweepRun) awaitParked() bool {
	r.t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case r.err = <-r.done:
			return true
		default:
		}
		if r.clock.pending() >= r.watched {
			return false
		}
		time.Sleep(time.Millisecond)
	}
	r.t.Fatal("Sweep neither waited on the clock nor returned")
	return false
}

func (r *sweepRun) step() bool {
	r.t.Helper()
	r.clock.advance(testPoll)
	return r.awaitParked()
}

func (r *sweepRun) awaitReturn() {
	r.t.Helper()
	select {
	case r.err = <-r.done:
	case <-time.After(10 * time.Second):
		r.t.Fatal("Sweep did not return")
	}
}

func orphan(id, workingDir string) Container {
	return Container{ID: id, Name: "/mcsd-" + id, State: containerStateRunning, WorkingDir: workingDir}
}

// An orphan still writing its shutdown save keeps its grace far past the idle
// window, and exits by itself without a kill. Before issue #3445 the Sweep's
// `docker stop` SIGKILLed it 30 s after SIGTERM, mid-save.
func TestSweepWaitsWhileTheOrphansWorldIsWritten(t *testing.T) {
	clock := newStopClock()
	docker := newSweepDocker(clock, orphan("s1", "/scratch/s1"))
	world := &fakeWorld{}
	r := startSweep(t, docker, map[string]*fakeWorld{"/scratch/s1": world}, nil)
	start := clock.Now()

	if r.awaitParked() {
		t.Fatalf("Sweep returned before waiting: %v", r.err)
	}
	for clock.Now().Sub(start) < 4*testIdle {
		world.write()
		if r.step() {
			t.Fatalf("Sweep returned while the world was still written, at %v: %v", clock.Now().Sub(start), r.err)
		}
	}
	docker.exit("s1")
	r.awaitReturn()

	if r.err != nil {
		t.Fatalf("Sweep: %v", r.err)
	}
	if len(docker.killed()) != 0 {
		t.Fatalf("kills = %v, want none: the orphan finished its save and exited", docker.killed())
	}
	if len(docker.daemonStops) != 0 || len(docker.terminated) != 1 || docker.terminated[0] != "s1" {
		t.Fatalf("daemon stops = %v, SIGTERMs = %v; want one SIGTERM and no fixed-grace `docker stop`",
			docker.daemonStops, docker.terminated)
	}
	if len(docker.removed) != 1 || docker.removed[0] != "s1" {
		t.Fatalf("removed = %v, want [s1]", docker.removed)
	}
}

// Once the orphan's world has gone the idle window without a write, the Sweep
// kills it: exactly 30 s after the last write, however long the save ran before.
func TestSweepKillsTheOrphanOnceItsWorldGoesIdle(t *testing.T) {
	clock := newStopClock()
	docker := newSweepDocker(clock, orphan("s1", "/scratch/s1"))
	world := &fakeWorld{}
	r := startSweep(t, docker, map[string]*fakeWorld{"/scratch/s1": world}, nil)
	start := clock.Now()

	if r.awaitParked() {
		t.Fatalf("Sweep returned before waiting: %v", r.err)
	}
	for clock.Now().Sub(start) < 2*testIdle {
		world.write()
		if r.step() {
			t.Fatalf("Sweep returned while the world was still written: %v", r.err)
		}
	}
	lastWrite := clock.Now()
	for !r.step() {
		if clock.Now().Sub(lastWrite) > testIdle {
			t.Fatalf("no kill %v after the last write", clock.Now().Sub(lastWrite))
		}
	}

	at, ok := docker.killed()["s1"]
	if !ok {
		t.Fatalf("the idle orphan was not killed (err %v)", r.err)
	}
	if got := at.Sub(lastWrite); got != testIdle {
		t.Fatalf("killed %v after the last write, want the idle window %v", got, testIdle)
	}
	if len(docker.removed) != 1 || docker.removed[0] != "s1" {
		t.Fatalf("removed = %v, want [s1]", docker.removed)
	}
}

// Orphans are stopped side by side, so one that writes without end and another
// that does the same are both killed at the one cap: boot waits one cap, not one
// per orphan.
func TestSweepStopsOrphansConcurrentlyUnderOneCap(t *testing.T) {
	clock := newStopClock()
	docker := newSweepDocker(clock, orphan("s1", "/scratch/s1"), orphan("s2", "/scratch/s2"))
	w1, w2 := &fakeWorld{}, &fakeWorld{}
	r := startSweep(t, docker, map[string]*fakeWorld{"/scratch/s1": w1, "/scratch/s2": w2}, nil)
	start := clock.Now()

	if r.awaitParked() {
		t.Fatalf("Sweep returned before waiting: %v", r.err)
	}
	for {
		w1.write()
		w2.write()
		if r.step() {
			break
		}
		if len(docker.killed()) == 2 {
			r.awaitReturn()
			break
		}
		if clock.Now().Sub(start) > 2*testCap {
			t.Fatal("the orphans were not killed by twice the cap")
		}
	}

	kills := docker.killed()
	for _, id := range []string{"s1", "s2"} {
		at, ok := kills[id]
		if !ok {
			t.Fatalf("%s was not killed (kills %v, err %v)", id, kills, r.err)
		}
		if got := at.Sub(start); got != testCap {
			t.Fatalf("%s killed %v after the sweep began, want the one cap %v", id, got, testCap)
		}
	}
	if len(docker.removed) != 2 {
		t.Fatalf("removed = %v, want both orphans", docker.removed)
	}
}

// A Worker told to shut down mid-sweep cancels the Sweep's context, and the
// sweep abandons the orphan's stop: no kill is sent, so the orphan keeps running
// until the next boot's sweep. The WARN says so; before issue #3515 it claimed a
// kill with reason=deadline.
func TestSweepCancelledMidWaitAbandonsTheStopWithoutAKill(t *testing.T) {
	clock := newStopClock()
	docker := newSweepDocker(clock, orphan("s1", "/scratch/s1"))
	world := &fakeWorld{}
	logged := &syncBuffer{}
	r := startSweep(t, docker, map[string]*fakeWorld{"/scratch/s1": world}, slog.New(slog.NewTextHandler(logged, nil)))

	if r.awaitParked() {
		t.Fatalf("Sweep returned before waiting: %v", r.err)
	}
	world.write()
	if r.step() {
		t.Fatalf("Sweep returned while the world was still written: %v", r.err)
	}
	r.cancel()
	r.awaitReturn()

	if r.err == nil {
		t.Fatal("Sweep = nil, want an error: the abandoned orphan was never proven stopped")
	}
	if kills := docker.killed(); len(kills) != 0 {
		t.Fatalf("kills = %v, want none: the Worker is shutting down", kills)
	}
	out := logged.String()
	if strings.Contains(out, "killing it") || strings.Contains(out, "reason=deadline") {
		t.Fatalf("log = %q, want no kill claimed and no reason=deadline", out)
	}
	if !strings.Contains(out, "level=WARN") || !strings.Contains(out, "shutting down") ||
		!strings.Contains(out, "mcsd-s1") {
		t.Fatalf("log = %q, want a WARN that the stop of mcsd-s1 was abandoned because the Worker is shutting down", out)
	}
}

// The cap's kill WARN gives the time the orphan's world was last written, the
// newest mtime its scans saw (issue #3515). Before, it gave the last poll that saw
// a change, so an orphan writing every 3 s, which every 5 s poll sees, read a
// since_last_write of ~0.
func TestSweepCapKillLogsTheLastObservedWrite(t *testing.T) {
	clock := newStopClock()
	docker := newSweepDocker(clock, orphan("s1", "/scratch/s1"))
	world := &fakeWorld{}
	logged := &syncBuffer{}
	r := startSweep(t, docker, map[string]*fakeWorld{"/scratch/s1": world}, slog.New(slog.NewTextHandler(logged, nil)))
	start := clock.Now()

	if r.awaitParked() {
		t.Fatalf("Sweep returned before waiting: %v", r.err)
	}
	// Writes every 3 s, off the poll's beat, until the kill.
	const writeEvery = 3 * time.Second
	last := time.Time{}
	next := start.Add(time.Second)
	for {
		for poll := clock.Now().Add(testPoll); !next.After(poll); next = next.Add(writeEvery) {
			world.writeAt(next)
			last = next
		}
		if r.step() {
			break
		}
		if clock.Now().Sub(start) > 2*testCap {
			t.Fatal("the orphan was not killed by twice the cap")
		}
	}

	at, ok := docker.killed()["s1"]
	if !ok || at.Sub(start) != testCap {
		t.Fatalf("kills = %v, want s1 killed at the %v cap", docker.killed(), testCap)
	}
	since := loggedSinceLastWrite(t, logged.String(), "killing it")
	if want := at.Sub(last); since != want || since <= 0 || since > writeEvery {
		t.Fatalf("since_last_write = %v, want %v: the time since the last write, within one %v write interval",
			since, want, writeEvery)
	}
}

// An orphan whose working set cannot be found (no bind mount at the container's
// working directory) has no world to watch, so the Sweep falls back to the fixed
// `docker stop` grace and says so at WARN.
func TestSweepFallsBackToTheFixedStopWithoutAWorkingSet(t *testing.T) {
	clock := newStopClock()
	docker := newSweepDocker(clock, orphan("s1", ""))
	logged := &syncBuffer{}
	r := startSweep(t, docker, map[string]*fakeWorld{}, slog.New(slog.NewTextHandler(logged, nil)))
	r.awaitReturn()

	if r.err != nil {
		t.Fatalf("Sweep: %v", r.err)
	}
	if len(docker.daemonStops) != 1 || docker.daemonStops[0] != "s1" || len(docker.terminated) != 0 {
		t.Fatalf("daemon stops = %v, SIGTERMs = %v; want the fixed-grace `docker stop` alone",
			docker.daemonStops, docker.terminated)
	}
	out := logged.String()
	if !strings.Contains(out, "level=WARN") || !strings.Contains(out, "working set") ||
		!strings.Contains(out, "mcsd-s1") {
		t.Fatalf("log = %q, want a WARN naming the orphan and its missing working set", out)
	}
	if len(docker.removed) != 1 || docker.removed[0] != "s1" {
		t.Fatalf("removed = %v, want [s1]", docker.removed)
	}
}
