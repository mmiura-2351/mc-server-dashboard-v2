package session

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/metrics"
)

type fakeReportClient struct {
	mu     sync.Mutex
	starts []apiclient.SessionStart
	ends   []apiclient.SessionEnd
	failN  int // fail the next N calls
}

func (f *fakeReportClient) ReportSessions(_ context.Context, starts []apiclient.SessionStart, ends []apiclient.SessionEnd) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failN > 0 {
		f.failN--
		return errors.New("transient")
	}
	f.starts = append(f.starts, starts...)
	f.ends = append(f.ends, ends...)
	return nil
}

func (f *fakeReportClient) counts() (int, int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.starts), len(f.ends)
}

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestReporterStartEndTracksActive(t *testing.T) {
	r := NewReporter(&fakeReportClient{}, discardLogger(), func() time.Time { return time.Unix(0, 0) }, nil)

	id := r.Start("srv", "amber", "1.2.3.4", "Steve", "uuid", apiclient.SourceJava)
	if got := r.ActiveSessionIDs(); len(got) != 1 || got[0] != id {
		t.Fatalf("active = %v, want [%s]", got, id)
	}
	r.End(id)
	if got := r.ActiveSessionIDs(); len(got) != 0 {
		t.Fatalf("active after End = %v, want empty", got)
	}
}

func TestReporterStartThreadsSource(t *testing.T) {
	r := NewReporter(&fakeReportClient{}, discardLogger(), func() time.Time { return time.Unix(0, 0) }, nil)

	r.Start("srv", "amber", "1.2.3.4", "Steve", "uuid", apiclient.SourceBedrock)

	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.pendStarts) != 1 {
		t.Fatalf("pendStarts = %d, want 1", len(r.pendStarts))
	}
	if r.pendStarts[0].Source != apiclient.SourceBedrock {
		t.Errorf("Source = %v, want SourceBedrock", r.pendStarts[0].Source)
	}
}

func TestReporterFlushDeliversBatch(t *testing.T) {
	fake := &fakeReportClient{}
	r := NewReporter(fake, discardLogger(), nil, nil)
	id := r.Start("srv", "amber", "1.2.3.4", "Steve", "", apiclient.SourceJava)
	r.End(id)

	r.flush(context.Background())
	starts, ends := fake.counts()
	if starts != 1 || ends != 1 {
		t.Errorf("delivered %d starts / %d ends, want 1/1", starts, ends)
	}
}

func TestReporterRetriesOnError(t *testing.T) {
	fake := &fakeReportClient{failN: 1}
	r := NewReporter(fake, discardLogger(), nil, nil)
	r.Start("srv", "amber", "1.2.3.4", "Steve", "", apiclient.SourceJava)

	// First flush fails; events are restored.
	r.flush(context.Background())
	if s, _ := fake.counts(); s != 0 {
		t.Fatalf("failed flush should deliver nothing, got %d", s)
	}
	// Second flush succeeds with the retained event.
	r.flush(context.Background())
	if s, _ := fake.counts(); s != 1 {
		t.Errorf("retry should deliver the retained event, got %d", s)
	}
}

// TestReporterRetryBufferBounded asserts the retry buffer is capped during a
// sustained outage: repeated failed flushes drop the oldest events rather than
// growing without bound, and the buffer never exceeds MaxBufferedEvents.
func TestReporterRetryBufferBounded(t *testing.T) {
	// Always-failing client so events are restored on every flush.
	fake := &fakeReportClient{failN: 1 << 30}
	r := NewReporter(fake, discardLogger(), nil, nil)

	// Buffer more starts than the cap, flushing between batches so capOldest runs
	// on the restore path.
	for i := 0; i < MaxBufferedEvents+500; i++ {
		r.Start("srv", "amber", "1.2.3.4", "Steve", "", apiclient.SourceJava)
		if i%100 == 0 {
			r.flush(context.Background())
		}
	}
	r.flush(context.Background())

	r.mu.Lock()
	got := len(r.pendStarts)
	r.mu.Unlock()
	if got > MaxBufferedEvents {
		t.Errorf("pendStarts = %d, exceeds cap %d", got, MaxBufferedEvents)
	}
}

func TestCapOldestDropsFront(t *testing.T) {
	buf := make([]int, MaxBufferedEvents+3)
	for i := range buf {
		buf[i] = i
	}
	out := capOldest(buf, "start", discardLogger())
	if len(out) != MaxBufferedEvents {
		t.Fatalf("len = %d, want %d", len(out), MaxBufferedEvents)
	}
	// Oldest (front) three were dropped; the newest is retained at the tail.
	if out[0] != 3 {
		t.Errorf("front element = %d, want 3 (oldest dropped)", out[0])
	}
	if out[len(out)-1] != MaxBufferedEvents+2 {
		t.Errorf("tail element = %d, want newest retained", out[len(out)-1])
	}
}

// TestReporterFlushFailureIncrementsMetric asserts a failed ReportSessions flush
// increments relay_session_report_flush_failures_total, and a successful flush
// leaves it untouched.
func TestReporterFlushFailureIncrementsMetric(t *testing.T) {
	reg := prometheus.NewRegistry()
	m := metrics.New(reg, "test")
	fake := &fakeReportClient{failN: 1}
	r := NewReporter(fake, discardLogger(), nil, m)
	r.Start("srv", "amber", "1.2.3.4", "Steve", "", apiclient.SourceJava)

	// First flush fails → counter goes to 1.
	r.flush(context.Background())
	assertFlushFailures(t, reg, 1)

	// Second flush succeeds → counter stays at 1.
	r.flush(context.Background())
	assertFlushFailures(t, reg, 1)
}

func assertFlushFailures(t *testing.T, reg *prometheus.Registry, want int) {
	t.Helper()
	expected := fmt.Sprintf(`
# HELP relay_session_report_flush_failures_total Failed ReportSessions flushes in the session reporter.
# TYPE relay_session_report_flush_failures_total counter
relay_session_report_flush_failures_total %d
`, want)
	if err := testutil.GatherAndCompare(reg, strings.NewReader(expected), "relay_session_report_flush_failures_total"); err != nil {
		t.Error(err)
	}
}

// blockingClient blocks until its context is cancelled, simulating an
// unreachable API.
type blockingClient struct{}

func (blockingClient) ReportSessions(ctx context.Context, _ []apiclient.SessionStart, _ []apiclient.SessionEnd) error {
	<-ctx.Done()
	return ctx.Err()
}

// TestReporterFlushBoundedByTimeout pins the per-flush RPC deadline (issue
// #1719): a black-holed API connection (an RPC that never returns) must not
// wedge flush — and with it the single Run goroutine — beyond flushTimeout.
// The timed-out batch must take the error-restore path so it is retried and
// capOldest can bound the buffer during the outage.
func TestReporterFlushBoundedByTimeout(t *testing.T) {
	r := NewReporter(blockingClient{}, discardLogger(), nil, nil)
	r.flushTimeout = 50 * time.Millisecond
	r.Start("srv", "amber", "1.2.3.4", "Steve", "", apiclient.SourceJava)

	done := make(chan struct{})
	go func() { r.flush(context.Background()); close(done) }()

	select {
	case <-done:
		// flush returned; the RPC was bounded.
	case <-time.After(2 * time.Second):
		t.Fatal("flush did not return; per-RPC deadline missing (black-holed API wedges the Run goroutine)")
	}

	r.mu.Lock()
	got := len(r.pendStarts)
	r.mu.Unlock()
	if got != 1 {
		t.Errorf("timed-out flush should restore the event for retry, pendStarts = %d, want 1", got)
	}
}

func TestReporterShutdownFlushTimesOut(t *testing.T) {
	r := NewReporter(blockingClient{}, discardLogger(), nil, nil)
	r.shutdownTimeout = 50 * time.Millisecond
	r.WithFlushInterval(time.Hour) // prevent periodic flushes
	r.Start("srv", "amber", "1.2.3.4", "Steve", "", apiclient.SourceJava)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { r.Run(ctx); close(done) }()
	cancel()

	select {
	case <-done:
		// Run returned; the shutdown flush was bounded.
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return within the shutdown flush timeout; would hang indefinitely")
	}
}

func TestReporterRunFlushesOnShutdown(t *testing.T) {
	fake := &fakeReportClient{}
	r := NewReporter(fake, discardLogger(), nil, nil)
	r.Start("srv", "amber", "1.2.3.4", "Steve", "", apiclient.SourceJava)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { r.Run(ctx); close(done) }()
	cancel()
	<-done

	if s, _ := fake.counts(); s != 1 {
		t.Errorf("shutdown flush should deliver the buffered start, got %d", s)
	}
}

// TestReporterRunDrainsPostShutdownEvents verifies that session events arriving
// after the shutdown signal (but before Run returns) are still flushed, not
// silently lost.
func TestReporterRunDrainsPostShutdownEvents(t *testing.T) {
	// slowClient blocks the first call (the primary shutdown flush) long enough
	// for a concurrent End to enqueue after shutdown.
	fake := &slowClient{delay: 50 * time.Millisecond, inner: &fakeReportClient{}}
	r := NewReporter(fake, discardLogger(), nil, nil)
	r.shutdownTimeout = 2 * time.Second
	r.WithFlushInterval(time.Hour) // no periodic flushes

	id := r.Start("srv", "amber", "1.2.3.4", "Steve", "", apiclient.SourceJava)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { r.Run(ctx); close(done) }()

	// Trigger shutdown; the primary flush will start draining the Start event.
	cancel()

	// While the primary flush is in flight (50ms delay), inject an End event.
	time.Sleep(10 * time.Millisecond)
	r.End(id)

	<-done

	inner := fake.inner
	s, e := inner.counts()
	if s != 1 || e != 1 {
		t.Errorf("post-shutdown drain: starts=%d ends=%d, want 1/1", s, e)
	}
}

// slowClient wraps a fakeReportClient and delays the first call.
type slowClient struct {
	delay time.Duration
	inner *fakeReportClient
	once  sync.Once
}

func (s *slowClient) ReportSessions(ctx context.Context, starts []apiclient.SessionStart, ends []apiclient.SessionEnd) error {
	s.once.Do(func() { time.Sleep(s.delay) })
	return s.inner.ReportSessions(ctx, starts, ends)
}

// gatedClient blocks inside ReportSessions until gate is closed, letting tests
// observe the flush-in-flight state.
type gatedClient struct {
	gate  chan struct{} // close to unblock
	inner *fakeReportClient
}

func (g *gatedClient) ReportSessions(ctx context.Context, starts []apiclient.SessionStart, ends []apiclient.SessionEnd) error {
	<-g.gate
	return g.inner.ReportSessions(ctx, starts, ends)
}

// TestSnapshotActiveExcludesFlush verifies the barrier: while SnapshotActive is
// held, a concurrent flush cannot run (the Start event stays buffered).
func TestSnapshotActiveExcludesFlush(t *testing.T) {
	fake := &fakeReportClient{}
	r := NewReporter(fake, discardLogger(), nil, nil)
	r.WithFlushInterval(time.Millisecond) // tiny interval

	// Hold the barrier.
	ids, release := r.SnapshotActive()
	if len(ids) != 0 {
		t.Fatalf("expected no active sessions, got %v", ids)
	}

	// Start a session while the barrier is held.
	r.Start("srv", "amber", "1.2.3.4", "Steve", "uuid", apiclient.SourceJava)

	// Run the reporter briefly; flushes should be blocked.
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { r.Run(ctx); close(done) }()
	time.Sleep(50 * time.Millisecond)

	s, _ := fake.counts()
	if s != 0 {
		t.Fatalf("flush ran while barrier held: starts=%d, want 0", s)
	}

	// Release the barrier; the flush should proceed.
	release()
	time.Sleep(50 * time.Millisecond)
	cancel()
	<-done

	s, _ = fake.counts()
	if s != 1 {
		t.Errorf("flush did not deliver after barrier released: starts=%d, want 1", s)
	}
}

// TestSnapshotActiveWaitsForInflightFlush verifies the barrier from the other
// direction: SnapshotActive blocks until an in-flight flush completes.
func TestSnapshotActiveWaitsForInflightFlush(t *testing.T) {
	gate := make(chan struct{})
	fake := &gatedClient{gate: gate, inner: &fakeReportClient{}}
	r := NewReporter(fake, discardLogger(), nil, nil)

	r.Start("srv", "amber", "1.2.3.4", "Steve", "uuid", apiclient.SourceJava)

	// Trigger a flush that will block inside ReportSessions.
	flushDone := make(chan struct{})
	go func() { r.flush(context.Background()); close(flushDone) }()
	time.Sleep(20 * time.Millisecond) // let flush enter the RPC

	// SnapshotActive should block because the flush holds flightMu.
	snapped := make(chan struct{})
	go func() {
		_, rel := r.SnapshotActive()
		rel()
		close(snapped)
	}()

	// Give SnapshotActive a chance to return (it shouldn't).
	select {
	case <-snapped:
		t.Fatal("SnapshotActive returned while flush was in flight")
	case <-time.After(50 * time.Millisecond):
		// expected: still blocked
	}

	// Unblock the flush.
	close(gate)
	<-flushDone

	select {
	case <-snapped:
		// SnapshotActive returned after flush completed — correct.
	case <-time.After(2 * time.Second):
		t.Fatal("SnapshotActive did not return after flush completed")
	}
}
