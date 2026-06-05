package session

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"time"
)

// discardLogger is a slog logger that writes nowhere, keeping test output clean.
func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// captureHandler is a slog.Handler that records the records it receives so tests
// can assert on emitted log lines (level and attributes).
type captureHandler struct {
	mu      sync.Mutex
	records []slog.Record
}

func (h *captureHandler) Enabled(context.Context, slog.Level) bool { return true }

func (h *captureHandler) Handle(_ context.Context, r slog.Record) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.records = append(h.records, r.Clone())
	return nil
}

func (h *captureHandler) WithAttrs([]slog.Attr) slog.Handler { return h }
func (h *captureHandler) WithGroup(string) slog.Handler      { return h }

// recordsAtLevel returns a copy of the captured records at the given level.
func (h *captureHandler) recordsAtLevel(level slog.Level) []slog.Record {
	h.mu.Lock()
	defer h.mu.Unlock()
	var out []slog.Record
	for _, rec := range h.records {
		if rec.Level == level {
			out = append(out, rec)
		}
	}
	return out
}

// captureLogger pairs a capturing handler with a logger built on it.
func captureLogger() (*slog.Logger, *captureHandler) {
	h := &captureHandler{}
	return slog.New(h), h
}

// fakeClock is a manually-advanced Clock. After returns a channel a test fires
// by calling fire(); Now is fixed (tests that need it advance manually).
type fakeClock struct {
	mu      sync.Mutex
	now     time.Time
	pending []chan time.Time
	timers  []*fakeTimer
}

func newFakeClock() *fakeClock {
	return &fakeClock{now: time.Unix(0, 0)}
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) After(time.Duration) <-chan time.Time {
	ch := make(chan time.Time, 1)
	c.mu.Lock()
	c.pending = append(c.pending, ch)
	c.mu.Unlock()
	return ch
}

// NewTimer registers a persistent fakeTimer whose channel survives Reset, so
// tests can model the heartbeat deadline as a single timer that is re-armed (not
// recreated) on each beat.
func (c *fakeClock) NewTimer(time.Duration) Timer {
	t := &fakeTimer{ch: make(chan time.Time, 1), now: c.Now}
	c.mu.Lock()
	c.timers = append(c.timers, t)
	c.mu.Unlock()
	return t
}

// fireNext fires the oldest pending timer, returning false if none is pending.
func (c *fakeClock) fireNext() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.pending) == 0 {
		return false
	}
	ch := c.pending[0]
	c.pending = c.pending[1:]
	ch <- c.now
	return true
}

// firstTimer returns the first registered persistent timer, or nil if the runner
// has not armed one yet.
func (c *fakeClock) firstTimer() *fakeTimer {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.timers) == 0 {
		return nil
	}
	return c.timers[0]
}

// fakeTimer is a persistent, resettable timer. Its channel is created once and
// reused across Reset, mirroring a real time.Timer reset (the heartbeat seam).
type fakeTimer struct {
	ch  chan time.Time
	now func() time.Time
}

func (t *fakeTimer) C() <-chan time.Time { return t.ch }

func (t *fakeTimer) Reset(time.Duration) {}

func (t *fakeTimer) Stop() {}

// fire delivers one tick on the timer's channel, mimicking the deadline elapsing.
func (t *fakeTimer) fire() { t.ch <- t.now() }

// errStreamClosed simulates the server dropping the stream.
var errStreamClosed = errors.New("fake: stream closed")

// fakeTransport is one in-memory Session stream. Inbound commands are queued on
// commands; recorded outputs are captured for assertions.
type fakeTransport struct {
	mu sync.Mutex

	ack RegisterAck

	registers   int
	heartbeats  int
	results     []CommandResult
	statuses    []StatusEvent
	logLines    []LogEvent
	metrics     []MetricsEvent
	closed      bool
	commands    chan Command
	recvErr     error // returned by RecvCommand once commands drains
	registerErr error // returned by SendRegister, if set
	ackErr      error // returned by RecvRegisterAck, if set
}

func newFakeTransport(ack RegisterAck) *fakeTransport {
	return &fakeTransport{
		ack:      ack,
		commands: make(chan Command, 8),
		recvErr:  errStreamClosed,
	}
}

func (t *fakeTransport) SendRegister(_ context.Context, _ Capabilities) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.registers++
	return t.registerErr
}

func (t *fakeTransport) RecvRegisterAck(_ context.Context) (RegisterAck, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.ackErr != nil {
		return RegisterAck{}, t.ackErr
	}
	return t.ack, nil
}

func (t *fakeTransport) SendHeartbeat(_ context.Context) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.heartbeats++
	return nil
}

func (t *fakeTransport) SendCommandResult(_ context.Context, result CommandResult) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.results = append(t.results, result)
	return nil
}

func (t *fakeTransport) SendStatusChange(_ context.Context, event StatusEvent) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.statuses = append(t.statuses, event)
	return nil
}

func (t *fakeTransport) SendLogLine(_ context.Context, event LogEvent) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.logLines = append(t.logLines, event)
	return nil
}

func (t *fakeTransport) SendMetrics(_ context.Context, event MetricsEvent) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.metrics = append(t.metrics, event)
	return nil
}

func (t *fakeTransport) RecvCommand(ctx context.Context) (Command, error) {
	select {
	case <-ctx.Done():
		return Command{}, ctx.Err()
	case cmd, ok := <-t.commands:
		if !ok {
			return Command{}, t.recvErr
		}
		return cmd, nil
	}
}

func (t *fakeTransport) Close() error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.closed = true
	return nil
}

func (t *fakeTransport) heartbeatCount() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.heartbeats
}

func (t *fakeTransport) resultsCopy() []CommandResult {
	t.mu.Lock()
	defer t.mu.Unlock()
	return append([]CommandResult(nil), t.results...)
}

func (t *fakeTransport) statusesCopy() []StatusEvent {
	t.mu.Lock()
	defer t.mu.Unlock()
	return append([]StatusEvent(nil), t.statuses...)
}

func (t *fakeTransport) logLinesCopy() []LogEvent {
	t.mu.Lock()
	defer t.mu.Unlock()
	return append([]LogEvent(nil), t.logLines...)
}

func (t *fakeTransport) metricsCopy() []MetricsEvent {
	t.mu.Lock()
	defer t.mu.Unlock()
	return append([]MetricsEvent(nil), t.metrics...)
}

// fakeHandler is an in-memory CommandHandler: it records dispatched commands,
// returns a canned result, and pushes status events on demand.
type fakeHandler struct {
	mu      sync.Mutex
	handled []Command
	result  CommandResult
	events  chan StatusEvent
	logs    chan LogEvent
	metrics chan MetricsEvent
}

func newFakeHandler(result CommandResult) *fakeHandler {
	return &fakeHandler{
		result:  result,
		events:  make(chan StatusEvent, 8),
		logs:    make(chan LogEvent, 8),
		metrics: make(chan MetricsEvent, 8),
	}
}

func (h *fakeHandler) Handle(_ context.Context, cmd Command) CommandResult {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.handled = append(h.handled, cmd)
	res := h.result
	res.CommandID = cmd.CommandID
	return res
}

func (h *fakeHandler) Events() <-chan StatusEvent   { return h.events }
func (h *fakeHandler) Logs() <-chan LogEvent        { return h.logs }
func (h *fakeHandler) Metrics() <-chan MetricsEvent { return h.metrics }

func (h *fakeHandler) handledCopy() []Command {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]Command(nil), h.handled...)
}

// fakeDialer hands out a queue of transports, one per Dial; once exhausted it
// returns dialErr (or the last transport repeatedly if loop is set).
type fakeDialer struct {
	mu         sync.Mutex
	transports []*fakeTransport
	calls      int
	dialErr    error
}

func (d *fakeDialer) Dial(_ context.Context) (Transport, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	idx := d.calls
	d.calls++
	if idx >= len(d.transports) {
		if d.dialErr != nil {
			return nil, d.dialErr
		}
		return nil, errStreamClosed
	}
	return d.transports[idx], nil
}

func (d *fakeDialer) dialCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.calls
}
