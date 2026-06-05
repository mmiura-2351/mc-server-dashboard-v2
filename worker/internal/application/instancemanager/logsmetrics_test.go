package instancemanager

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// richInstance is a fakeInstance that also implements LogSource and StatsSource
// so the manager's log/metrics pumps engage (FR-MON-2, FR-MON-3).
type richInstance struct {
	serverID string
	events   chan execution.StatusEvent
	logs     chan execution.LogEvent

	mu        sync.Mutex
	sample    execution.MetricsSample
	sampleErr error
}

func newRichInstance(id string) *richInstance {
	i := &richInstance{
		serverID: id,
		events:   make(chan execution.StatusEvent, 8),
		logs:     make(chan execution.LogEvent, 8),
		sample:   execution.MetricsSample{ServerID: id, CPUMillis: 100, MemoryBytes: 2048},
	}
	i.events <- execution.StatusEvent{ServerID: id, State: execution.StateRunning}
	return i
}

func (i *richInstance) Stop(context.Context, bool) error {
	i.events <- execution.StatusEvent{ServerID: i.serverID, State: execution.StateStopped}
	// Terminal: close the streams so the pumps tear down (drivers do this in
	// supervise after the process/container exits).
	close(i.events)
	close(i.logs)
	return nil
}

func (i *richInstance) Status() execution.ServerState        { return execution.StateRunning }
func (i *richInstance) Events() <-chan execution.StatusEvent { return i.events }
func (i *richInstance) Logs() <-chan execution.LogEvent      { return i.logs }

func (i *richInstance) Sample(context.Context) (execution.MetricsSample, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.sample, i.sampleErr
}

// richDriver hands out a single richInstance.
type richDriver struct{ inst *richInstance }

func (d *richDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.inst = newRichInstance(spec.ServerID)
	return d.inst, nil
}

// fakeClock drives the metrics ticker deterministically: After returns a channel
// the test fires via tick().
type fakeClock struct {
	mu sync.Mutex
	// chs holds the After channels currently waited on by pump goroutines.
	chs []chan time.Time
	// registers counts every After registration ever made. A live pump increments
	// it each time it re-parks in its select; once it stops increasing, the pump
	// has exited (used by waitForPumpExit).
	registers int
}

func (c *fakeClock) Now() time.Time { return time.Unix(0, 0) }

func (c *fakeClock) After(time.Duration) <-chan time.Time {
	ch := make(chan time.Time, 1)
	c.mu.Lock()
	c.chs = append(c.chs, ch)
	c.registers++
	c.mu.Unlock()
	return ch
}

// NewTimer satisfies session.Clock. The metrics ticker uses only After, so this
// returns a timer whose channel never fires.
func (c *fakeClock) NewTimer(time.Duration) session.Timer { return noopTimer{} }

// noopTimer is a session.Timer that never fires; the metrics ticker does not use
// NewTimer.
type noopTimer struct{}

func (noopTimer) C() <-chan time.Time { return nil }
func (noopTimer) Reset(time.Duration) {}
func (noopTimer) Stop()               {}

// tick fires every pending After channel, advancing the metrics ticker one step.
func (c *fakeClock) tick() {
	c.mu.Lock()
	chs := c.chs
	c.chs = nil
	c.mu.Unlock()
	for _, ch := range chs {
		ch <- time.Unix(1, 0)
	}
}

func newRichManager(t *testing.T, d *richDriver, clk session.Clock) *Manager {
	t.Helper()
	return New(map[string]execution.ExecutionDriver{"host-process": d}, t.TempDir(),
		func(context.Context, string, string) (execution.ServerControl, error) { return nil, nil }).
		WithMetrics(clk, time.Hour)
}

// Captured log lines flow from the instance through the manager to the merged
// Logs() stream, mapped to session.LogEvent.
func TestManagerForwardsLogs(t *testing.T) {
	d := &richDriver{}
	m := newRichManager(t, d, &fakeClock{})

	res := m.Handle(context.Background(), startCmd())
	if !res.Success {
		t.Fatalf("start = %+v", res)
	}
	// drain the initial running status so the pump does not block.
	drainStatus(m)

	d.inst.logs <- execution.LogEvent{ServerID: "s1", Line: "boot", Stream: execution.LogStreamStderr}

	select {
	case ev := <-m.Logs():
		if ev.ServerID != "s1" || ev.Line != "boot" || ev.Stream != session.LogStreamStderr {
			t.Fatalf("log = %+v", ev)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for a forwarded log line")
	}
}

// The metrics pump samples on each clock tick and forwards a Metrics event.
func TestManagerEmitsMetricsOnCadence(t *testing.T) {
	d := &richDriver{}
	clk := &fakeClock{}
	m := newRichManager(t, d, clk)

	res := m.Handle(context.Background(), startCmd())
	if !res.Success {
		t.Fatalf("start = %+v", res)
	}
	drainStatus(m)

	// Wait until the metrics pump has registered its first After channel, then tick.
	waitFor(t, func() bool {
		clk.mu.Lock()
		defer clk.mu.Unlock()
		return len(clk.chs) > 0
	})
	clk.tick()

	select {
	case ev := <-m.Metrics():
		if ev.ServerID != "s1" || ev.CPUMillis != 100 || ev.MemoryBytes != 2048 {
			t.Fatalf("metrics = %+v", ev)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for a metrics event")
	}
}

// When the instance is not a StatsSource, the manager still emits an up-only
// sample on each tick (server id, zero stats) so the API learns it is running.
func TestManagerEmitsUpOnlyMetricsWithoutStatsSource(t *testing.T) {
	d := &fakeDriver{} // fakeInstance implements neither LogSource nor StatsSource
	clk := &fakeClock{}
	m := New(map[string]execution.ExecutionDriver{"host-process": d}, t.TempDir(),
		func(context.Context, string, string) (execution.ServerControl, error) { return nil, nil }).
		WithMetrics(clk, time.Hour)

	res := m.Handle(context.Background(), startCmd())
	if !res.Success {
		t.Fatalf("start = %+v", res)
	}
	drainStatus(m)

	waitFor(t, func() bool {
		clk.mu.Lock()
		defer clk.mu.Unlock()
		return len(clk.chs) > 0
	})
	clk.tick()

	select {
	case ev := <-m.Metrics():
		if ev.ServerID != "s1" || ev.CPUMillis != 0 || ev.MemoryBytes != 0 {
			t.Fatalf("up-only metrics = %+v, want zero stats", ev)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for an up-only metrics event")
	}
}

// Stopping the instance tears down the metrics pump: once the pump goroutine has
// exited, no further metrics are emitted on subsequent ticks.
func TestMetricsPumpStopsAfterInstanceExit(t *testing.T) {
	d := &richDriver{}
	clk := &fakeClock{}
	m := newRichManager(t, d, clk)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v", res)
	}
	drainStatus(m)

	// Wait until the pump is parked in its select loop (one After registered),
	// proving it is alive before we tear it down.
	waitFor(t, func() bool {
		clk.mu.Lock()
		defer clk.mu.Unlock()
		return len(clk.chs) > 0
	})

	stop := session.Command{CommandID: "c2", ServerID: "s1", Kind: "StopServer"}
	if res := m.Handle(context.Background(), stop); !res.Success {
		t.Fatalf("stop = %+v", res)
	}

	// Wait until the pump goroutine has actually exited, then assert it emits no
	// further metrics. Firing a tick that races the teardown select is
	// non-deterministic (the pump may legitimately emit one straggler sample as done
	// closes), so instead we observe exit directly through the fake clock: a live
	// pump always re-registers an After channel before parking in its select, a dead
	// one never does. We keep firing pending ticks (draining any straggler metrics)
	// until the registration counter is stable across a tick that produces no new
	// registration — at that point the goroutine has returned for good.
	waitForPumpExit(t, clk, m)

	// The pump has exited. A subsequent tick (no After channels exist, so it is a
	// no-op) must not produce any metrics.
	clk.tick()
	select {
	case ev := <-m.Metrics():
		t.Fatalf("unexpected metrics after exit: %+v", ev)
	case <-time.After(50 * time.Millisecond):
		// No metrics emitted — the pump stopped. Pass.
	}
}

// blockingStatsInstance is a richInstance whose Sample blocks until its context
// is cancelled, recording that it observed the cancellation. It exercises the
// hung-Engine-stats teardown: when the instance terminates, the metrics pump
// must cancel the in-flight Sample and exit promptly rather than leaking on the
// stuck call.
type blockingStatsInstance struct {
	*richInstance
	entered   chan struct{}
	sampleMu  sync.Mutex
	cancelled bool
}

func (i *blockingStatsInstance) Sample(ctx context.Context) (execution.MetricsSample, error) {
	select {
	case i.entered <- struct{}{}:
	default:
	}
	<-ctx.Done()
	i.sampleMu.Lock()
	i.cancelled = true
	i.sampleMu.Unlock()
	return execution.MetricsSample{}, ctx.Err()
}

// blockingDriver hands out a blockingStatsInstance.
type blockingDriver struct{ inst *blockingStatsInstance }

func (d *blockingDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.inst = &blockingStatsInstance{
		richInstance: newRichInstance(spec.ServerID),
		entered:      make(chan struct{}, 1),
	}
	return d.inst, nil
}

// A Sample that hangs is cancelled when the instance terminates: the metrics
// pump derives the sample context from its done signal, so teardown unblocks the
// stuck call instead of leaking the goroutine.
func TestMetricsSampleCancelledOnTeardown(t *testing.T) {
	d := &blockingDriver{}
	clk := &fakeClock{}
	m := New(map[string]execution.ExecutionDriver{"host-process": d}, t.TempDir(),
		func(context.Context, string, string) (execution.ServerControl, error) { return nil, nil }).
		WithMetrics(clk, time.Hour)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v", res)
	}
	drainStatus(m)

	// Drive one tick so the pump calls Sample, which then blocks on ctx.
	waitFor(t, func() bool {
		clk.mu.Lock()
		defer clk.mu.Unlock()
		return len(clk.chs) > 0
	})
	clk.tick()

	select {
	case <-d.inst.entered:
	case <-time.After(2 * time.Second):
		t.Fatal("Sample was never called")
	}

	// Terminate the instance: this closes the status pump's done channel, which
	// must cancel the in-flight Sample.
	stop := session.Command{CommandID: "c2", ServerID: "s1", Kind: "StopServer"}
	if res := m.Handle(context.Background(), stop); !res.Success {
		t.Fatalf("stop = %+v", res)
	}

	// Teardown must cancel the hung Sample: cancelled flips true only because the
	// pump derived the sample context from its done signal. Without the fix the
	// Sample would block forever and the pump goroutine would leak.
	waitFor(t, func() bool {
		d.inst.sampleMu.Lock()
		defer d.inst.sampleMu.Unlock()
		return d.inst.cancelled
	})
}

// drainStatus consumes the merged status stream so the status pump never blocks.
func drainStatus(m *Manager) {
	go func() {
		for range m.Events() { //nolint:revive // intentionally draining to channel close
		}
	}()
}

// waitForPumpExit blocks until the metrics pump goroutine has returned, draining
// any straggler metrics it emits while tearing down. It fires each pending tick
// and then confirms exit: a pump still in its loop re-registers an After channel
// (registers grows) before parking, whereas an exited pump never registers again.
// When a tick is consumed without a new registration appearing and no After
// channels remain, the goroutine has exited for good.
func waitForPumpExit(t *testing.T, clk *fakeClock, m *Manager) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		clk.mu.Lock()
		before := clk.registers
		pending := len(clk.chs)
		clk.mu.Unlock()

		if pending == 0 {
			// Pump is parked on done with no live tick, or already exited. Either
			// way it will take the done branch and never register again. Confirm by
			// checking the counter is stable after a short grace period.
			time.Sleep(2 * time.Millisecond)
			clk.mu.Lock()
			stable := clk.registers == before && len(clk.chs) == 0
			clk.mu.Unlock()
			if stable {
				return
			}
			continue
		}

		// Fire the pending tick(s); drain any straggler metrics so the sink never
		// blocks the pump on its way out.
		clk.tick()
		drainMetrics(m)
	}
	t.Fatal("metrics pump did not exit before deadline")
}

// drainMetrics non-blockingly removes any buffered metrics events.
func drainMetrics(m *Manager) {
	for {
		select {
		case <-m.Metrics():
		default:
			return
		}
	}
}

// waitFor polls cond until true or fails after a deadline.
func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("condition not met before deadline")
}
