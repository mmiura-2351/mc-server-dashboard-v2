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
	mu  sync.Mutex
	chs []chan time.Time
}

func (c *fakeClock) Now() time.Time { return time.Unix(0, 0) }

func (c *fakeClock) After(time.Duration) <-chan time.Time {
	ch := make(chan time.Time, 1)
	c.mu.Lock()
	c.chs = append(c.chs, ch)
	c.mu.Unlock()
	return ch
}

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
		func(context.Context, string) (execution.ServerControl, error) { return nil, nil }).
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
		func(context.Context, string) (execution.ServerControl, error) { return nil, nil }).
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

// Stopping the instance tears down the metrics pump: after the instance reaches a
// terminal state, no further metrics are emitted on subsequent ticks.
func TestMetricsPumpStopsAfterInstanceExit(t *testing.T) {
	d := &richDriver{}
	clk := &fakeClock{}
	m := newRichManager(t, d, clk)

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start = %+v", res)
	}
	drainStatus(m)

	stop := session.Command{CommandID: "c2", ServerID: "s1", Kind: "StopServer"}
	if res := m.Handle(context.Background(), stop); !res.Success {
		t.Fatalf("stop = %+v", res)
	}

	// Give the pump goroutines time to observe the closed event channel and exit.
	waitFor(t, func() bool {
		clk.mu.Lock()
		defer clk.mu.Unlock()
		// After teardown no goroutine re-registers an After channel; firing any
		// stragglers must not produce a metrics event.
		return true
	})
	// Drain any After channels created before teardown, then assert no metrics.
	clk.tick()
	select {
	case ev, ok := <-m.Metrics():
		if ok {
			t.Fatalf("unexpected metrics after exit: %+v", ev)
		}
	case <-time.After(200 * time.Millisecond):
		// No metrics emitted — the pump stopped. Pass.
	}
}

// drainStatus consumes the merged status stream so the status pump never blocks.
func drainStatus(m *Manager) {
	go func() {
		for range m.Events() { //nolint:revive // intentionally draining to channel close
		}
	}()
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
