package session

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"testing"
	"time"
)

func testCaps() Capabilities {
	return Capabilities{
		WorkerID:      "worker-1",
		WorkerVersion: "test",
		Drivers:       []string{"container"},
		MaxServers:    0,
	}
}

func acceptedAck() RegisterAck {
	return RegisterAck{Accepted: true, HeartbeatInterval: 5 * time.Second}
}

// waitFor polls cond until true or the deadline, keeping the fake-clock tests
// free of real sleeps in the assertion path.
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

func TestRegisterPrecedesHeartbeat(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	r := NewRunner(dialer, testCaps(), clock, discardLogger())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// The runner must register and arm the heartbeat timer before any beat.
	waitFor(t, func() bool {
		transport.mu.Lock()
		defer transport.mu.Unlock()
		return transport.registers == 1
	})
	var timer *fakeTimer
	waitFor(t, func() bool {
		timer = clock.firstTimer()
		return timer != nil
	})
	if transport.heartbeatCount() != 0 {
		t.Fatalf("heartbeat sent before timer fired: %d", transport.heartbeatCount())
	}

	// Fire the heartbeat timer once.
	timer.fire()
	waitFor(t, func() bool { return transport.heartbeatCount() == 1 })

	cancel()
	<-done
}

func TestHeartbeatCadence(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	r := NewRunner(dialer, testCaps(), clock, discardLogger())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	var timer *fakeTimer
	waitFor(t, func() bool {
		timer = clock.firstTimer()
		return timer != nil
	})
	for beat := 1; beat <= 3; beat++ {
		timer.fire()
		want := beat
		waitFor(t, func() bool { return transport.heartbeatCount() == want })
	}

	cancel()
	<-done
}

// TestHeartbeatNotStarvedByEventTraffic reproduces issue #341: with a steady
// stream of inbound events arriving at sub-interval spacing across several
// intervals, the runner must still send a heartbeat at every interval boundary.
// The previous code re-armed the heartbeat via clock.After on every select
// iteration, so a never-idle select never chose the heartbeat case — the worker
// starved its own heartbeat and the API marked it offline. The heartbeat
// deadline must be a persistent timer, reset only after a beat is sent, so its
// cadence is independent of event traffic.
func TestHeartbeatNotStarvedByEventTraffic(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{Success: true})
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// Wait for the runner to arm its heartbeat timer.
	var timer *fakeTimer
	waitFor(t, func() bool {
		timer = clock.firstTimer()
		return timer != nil
	})

	for beat := 1; beat <= 3; beat++ {
		// Deliver a burst of events at sub-interval spacing and confirm each is
		// processed, so the select keeps choosing event cases between beats.
		handler.logs <- LogEvent{}
		handler.metrics <- MetricsEvent{}
		handler.events <- StatusEvent{}
		want := beat
		waitFor(t, func() bool { return len(transport.logLinesCopy()) == want })
		waitFor(t, func() bool { return len(transport.metricsCopy()) == want })
		waitFor(t, func() bool { return len(transport.statusesCopy()) == want })

		// The interval boundary elapses: the persistent heartbeat timer fires.
		timer.fire()
		waitFor(t, func() bool { return transport.heartbeatCount() == want })
	}

	cancel()
	<-done
}

func TestRejectedRegistrationDoesNotReconnect(t *testing.T) {
	transport := newFakeTransport(RegisterAck{Accepted: false, RejectionReason: "bad credential"})
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	r := NewRunner(dialer, testCaps(), clock, discardLogger())

	err := r.Run(context.Background())
	if !errors.Is(err, ErrRejected) {
		t.Fatalf("Run() error = %v, want ErrRejected", err)
	}
	if dialer.dialCount() != 1 {
		t.Errorf("dialed %d times, want exactly 1 (no reconnect on reject)", dialer.dialCount())
	}
}

func TestTerminalErrorDoesNotReconnect(t *testing.T) {
	// The adapter wraps a non-retryable connection failure (e.g. the API aborts
	// the stream with UNAUTHENTICATED) as ErrTerminal; the run loop must stop.
	transport := newFakeTransport(RegisterAck{})
	transport.ackErr = fmt.Errorf("recv ack: %w", ErrTerminal)
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	r := NewRunner(dialer, testCaps(), clock, discardLogger())

	err := r.Run(context.Background())
	if !errors.Is(err, ErrTerminal) {
		t.Fatalf("Run() error = %v, want ErrTerminal", err)
	}
	if dialer.dialCount() != 1 {
		t.Errorf("dialed %d times, want exactly 1 (no reconnect on terminal error)", dialer.dialCount())
	}
}

func TestReconnectReRegisters(t *testing.T) {
	first := newFakeTransport(acceptedAck())
	second := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{first, second}}
	clock := newFakeClock()
	// Deterministic, zero jitter so the backoff timer fires immediately.
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithRandFloat(func() float64 { return 0 }))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// First connection registers.
	waitFor(t, func() bool {
		first.mu.Lock()
		defer first.mu.Unlock()
		return first.registers == 1
	})

	// Drop the first stream: closing commands makes RecvCommand return the
	// stream-closed error, ending serve and triggering a reconnect.
	close(first.commands)

	// Keep firing the pending backoff timer until the second connection
	// re-registers from scratch (CONTROL_PLANE.md Section 4.4).
	waitFor(t, func() bool {
		clock.fireNext()
		second.mu.Lock()
		defer second.mu.Unlock()
		return second.registers == 1
	})
	if dialer.dialCount() != 2 {
		t.Errorf("dialCount = %d, want 2", dialer.dialCount())
	}

	cancel()
	<-done
}

func TestUnsupportedCommandIsAcknowledged(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	r := NewRunner(dialer, testCaps(), clock, discardLogger())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	transport.commands <- Command{CommandID: "cmd-7", ServerID: "srv-1", Kind: "StartServer"}

	waitFor(t, func() bool { return len(transport.resultsCopy()) == 1 })
	got := transport.resultsCopy()[0]
	if got.CommandID != "cmd-7" {
		t.Errorf("result CommandID = %q, want cmd-7 (correlation)", got.CommandID)
	}
	if got.Success {
		t.Error("result Success = true, want false (unsupported)")
	}
	if got.ErrorCode != CommandErrorInternal {
		t.Errorf("result ErrorCode = %v, want CommandErrorInternal", got.ErrorCode)
	}
	if got.ErrorMessage == "" {
		t.Error("result ErrorMessage empty, want an explanation")
	}

	cancel()
	<-done
}

func TestLifecycleCommandDispatchedToHandler(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{Success: true, Output: "ok"})
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	transport.commands <- Command{CommandID: "cmd-1", ServerID: "srv-1", Kind: "StartServer"}

	waitFor(t, func() bool { return len(handler.handledCopy()) == 1 })
	waitFor(t, func() bool { return len(transport.resultsCopy()) == 1 })

	got := transport.resultsCopy()[0]
	if got.CommandID != "cmd-1" || !got.Success || got.Output != "ok" {
		t.Fatalf("dispatched result = %+v, want success cmd-1 output ok", got)
	}

	cancel()
	<-done
}

// recordAttr returns the value of the named attribute on a slog.Record as a
// string (via its Value), or "" if absent.
func recordAttr(rec slog.Record, key string) (string, bool) {
	var val string
	var found bool
	rec.Attrs(func(a slog.Attr) bool {
		if a.Key == key {
			val = a.Value.String()
			found = true
			return false
		}
		return true
	})
	return val, found
}

func TestFailedCommandResultIsLogged(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	logger, capture := captureLogger()
	handler := newFakeHandler(CommandResult{
		Success:      false,
		ErrorCode:    CommandErrorInvalidState,
		ErrorMessage: "instance already running",
	})
	r := NewRunner(dialer, testCaps(), clock, logger, WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	transport.commands <- Command{CommandID: "cmd-9", ServerID: "srv-1", Kind: "StartServer"}

	waitFor(t, func() bool { return len(capture.recordsAtLevel(slog.LevelWarn)) == 1 })
	rec := capture.recordsAtLevel(slog.LevelWarn)[0]

	if got, _ := recordAttr(rec, "command_id"); got != "cmd-9" {
		t.Errorf("warn command_id = %q, want cmd-9", got)
	}
	if got, _ := recordAttr(rec, "server_id"); got != "srv-1" {
		t.Errorf("warn server_id = %q, want srv-1", got)
	}
	if got, _ := recordAttr(rec, "kind"); got != "StartServer" {
		t.Errorf("warn kind = %q, want StartServer", got)
	}
	if got, _ := recordAttr(rec, "error_code"); got != CommandErrorInvalidState.String() {
		t.Errorf("warn error_code = %q, want %q", got, CommandErrorInvalidState.String())
	}
	if got, _ := recordAttr(rec, "error_message"); got != "instance already running" {
		t.Errorf("warn error_message = %q, want %q", got, "instance already running")
	}

	cancel()
	<-done
}

func TestSuccessfulCommandResultIsNotWarnLogged(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	logger, capture := captureLogger()
	handler := newFakeHandler(CommandResult{Success: true, Output: "ok"})
	r := NewRunner(dialer, testCaps(), clock, logger, WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	transport.commands <- Command{CommandID: "cmd-1", ServerID: "srv-1", Kind: "StartServer"}

	waitFor(t, func() bool { return len(transport.resultsCopy()) == 1 })
	if n := len(capture.recordsAtLevel(slog.LevelWarn)); n != 0 {
		t.Errorf("warn records = %d, want 0 for a successful command", n)
	}

	cancel()
	<-done
}

func TestUnknownCommandStillUnsupportedWithHandler(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{Success: true})
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// An unset/unknown command oneof (empty Kind) must stay unsupported and never
	// reach the handler, even with one wired.
	transport.commands <- Command{CommandID: "cmd-2", ServerID: "srv-1", Kind: ""}

	waitFor(t, func() bool { return len(transport.resultsCopy()) == 1 })
	got := transport.resultsCopy()[0]
	if got.Success {
		t.Fatal("an unknown command should remain unsupported even with a handler")
	}
	if len(handler.handledCopy()) != 0 {
		t.Fatal("an unknown command should not reach the handler")
	}

	cancel()
	<-done
}

func TestFileCommandDispatchedToHandler(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{Success: true, FileContent: []byte("data")})
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// ReadFile is small and stays inline on the receive loop; it reaches the
	// handler and its bytes ride the result (Section 7.2).
	transport.commands <- Command{CommandID: "cmd-3", ServerID: "srv-1", Kind: "ReadFile", Path: "server.properties"}

	waitFor(t, func() bool { return len(handler.handledCopy()) == 1 })
	waitFor(t, func() bool { return len(transport.resultsCopy()) == 1 })

	got := transport.resultsCopy()[0]
	if got.CommandID != "cmd-3" || !got.Success || string(got.FileContent) != "data" {
		t.Fatalf("dispatched result = %+v, want success cmd-3 content data", got)
	}

	cancel()
	<-done
}

func TestListFilesDispatchedToHandler(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{Success: true, FileListing: &FileListing{}})
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// ListFiles is small and stays inline on the receive loop; it must reach the
	// handler rather than be answered with the canned "unsupported" result
	// (issue #219).
	transport.commands <- Command{CommandID: "cmd-4", ServerID: "srv-1", Kind: "ListFiles", Path: "."}

	waitFor(t, func() bool { return len(handler.handledCopy()) == 1 })
	waitFor(t, func() bool { return len(transport.resultsCopy()) == 1 })

	got := transport.resultsCopy()[0]
	if got.CommandID != "cmd-4" || !got.Success {
		t.Fatalf("dispatched result = %+v, want success cmd-4", got)
	}

	cancel()
	<-done
}

// blockingHandler blocks SnapshotTrigger on a release channel so a test can hold
// one in flight while sending another command; all other commands return at once.
type blockingHandler struct {
	mu          sync.Mutex
	handled     []Command
	releaseSnap chan struct{}
	events      chan StatusEvent
}

func newBlockingHandler() *blockingHandler {
	return &blockingHandler{releaseSnap: make(chan struct{}), events: make(chan StatusEvent)}
}

func (h *blockingHandler) Handle(ctx context.Context, cmd Command) CommandResult {
	if cmd.Kind == "SnapshotTrigger" {
		select {
		case <-h.releaseSnap:
		case <-ctx.Done():
		}
	}
	h.mu.Lock()
	h.handled = append(h.handled, cmd)
	h.mu.Unlock()
	return CommandResult{CommandID: cmd.CommandID, Success: true}
}

func (h *blockingHandler) Events() <-chan StatusEvent   { return h.events }
func (h *blockingHandler) Logs() <-chan LogEvent        { return nil }
func (h *blockingHandler) Metrics() <-chan MetricsEvent { return nil }

// A slow snapshot for one server must not block a fast command (e.g. a stop of
// another server) — the long-running transfer runs off the serial receive loop
// (issue #95).
func TestSlowSnapshotDoesNotBlockOtherCommands(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newBlockingHandler()
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// A snapshot that blocks, then a stop for a different server.
	transport.commands <- Command{CommandID: "snap", ServerID: "s1", Kind: "SnapshotTrigger"}
	transport.commands <- Command{CommandID: "stop", ServerID: "s2", Kind: "StopServer"}

	// The stop must complete and answer while the snapshot is still blocked.
	waitFor(t, func() bool {
		for _, r := range transport.resultsCopy() {
			if r.CommandID == "stop" && r.Success {
				return true
			}
		}
		return false
	})

	// The snapshot has not answered yet (still blocked).
	for _, r := range transport.resultsCopy() {
		if r.CommandID == "snap" {
			t.Fatal("snapshot answered before release; it did not block as set up")
		}
	}

	// Release the snapshot; it now completes too.
	close(handler.releaseSnap)
	waitFor(t, func() bool {
		for _, r := range transport.resultsCopy() {
			if r.CommandID == "snap" && r.Success {
				return true
			}
		}
		return false
	})

	cancel()
	<-done
}

// laneHandler blocks any command (regardless of kind) whose ServerID matches
// blockServer until released, and records the order in which it handles each
// command per server. It models a slow graceful Stop on one server.
type laneHandler struct {
	mu          sync.Mutex
	handled     []Command
	blockServer string
	release     chan struct{}
}

func newLaneHandler(blockServer string) *laneHandler {
	return &laneHandler{blockServer: blockServer, release: make(chan struct{})}
}

func (h *laneHandler) Handle(ctx context.Context, cmd Command) CommandResult {
	if cmd.ServerID == h.blockServer {
		select {
		case <-h.release:
		case <-ctx.Done():
		}
	}
	h.mu.Lock()
	h.handled = append(h.handled, cmd)
	h.mu.Unlock()
	return CommandResult{CommandID: cmd.CommandID, Success: true}
}

func (h *laneHandler) Events() <-chan StatusEvent   { return nil }
func (h *laneHandler) Logs() <-chan LogEvent        { return nil }
func (h *laneHandler) Metrics() <-chan MetricsEvent { return nil }

func (h *laneHandler) handledIDs() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	ids := make([]string, len(h.handled))
	for i, c := range h.handled {
		ids[i] = c.CommandID
	}
	return ids
}

// A slow graceful Stop on s1 (a lifecycle command, handled inline before issue
// #95) must not delay a command for a different server s2: per-server lanes run
// concurrently across servers (issue #95).
func TestSlowStopDoesNotBlockOtherServer(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newLaneHandler("s1")
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// A slow stop on s1, then a stop for a different server s2.
	transport.commands <- Command{CommandID: "stop-s1", ServerID: "s1", Kind: "StopServer"}
	transport.commands <- Command{CommandID: "stop-s2", ServerID: "s2", Kind: "StopServer"}

	// s2 must complete while s1 is still blocked.
	waitFor(t, func() bool {
		for _, res := range transport.resultsCopy() {
			if res.CommandID == "stop-s2" && res.Success {
				return true
			}
		}
		return false
	})

	for _, res := range transport.resultsCopy() {
		if res.CommandID == "stop-s1" {
			t.Fatal("s1 stop answered before release; lanes did not run concurrently")
		}
	}

	close(handler.release)
	waitFor(t, func() bool {
		for _, res := range transport.resultsCopy() {
			if res.CommandID == "stop-s1" && res.Success {
				return true
			}
		}
		return false
	})

	cancel()
	<-done
}

// Commands for the SAME server stay strictly ordered even under a burst: a lane
// executes its server's commands serially in arrival order (issue #95).
func TestSameServerCommandsStayOrdered(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	// Block nothing; we only care about handling order.
	handler := newLaneHandler("")
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	const n = 20
	want := make([]string, n)
	for i := 0; i < n; i++ {
		id := fmt.Sprintf("c%02d", i)
		want[i] = id
		transport.commands <- Command{CommandID: id, ServerID: "s1", Kind: "ServerCommand", Line: id}
	}

	waitFor(t, func() bool { return len(handler.handledIDs()) == n })

	got := handler.handledIDs()
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("same-server handling order = %v, want %v", got, want)
		}
	}

	cancel()
	<-done
}

// After a server's lane goes idle, its goroutine and map entry are torn down so
// an ever-growing roster of servers does not leak goroutines (issue #95).
func TestIdleLaneIsTornDown(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newLaneHandler("")
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	transport.commands <- Command{CommandID: "c1", ServerID: "s1", Kind: "ServerCommand"}
	waitFor(t, func() bool { return len(transport.resultsCopy()) == 1 })

	// Once the command is answered the lane should drain and remove itself.
	waitFor(t, func() bool { return r.laneCount() == 0 })

	cancel()
	<-done
}

// gateHandler blocks every command whose Kind is in slowKinds until released,
// recording handling order. Commands whose Kind is not slow (e.g. ServerCommand)
// return immediately. It models long-running lane work (hydrate/stop) saturating
// the concurrency cap while an instant ServerCommand wants to run.
type gateHandler struct {
	mu        sync.Mutex
	handled   []Command
	inflight  int
	slowKinds map[string]bool
	release   chan struct{}
}

func newGateHandler(slowKinds ...string) *gateHandler {
	set := make(map[string]bool, len(slowKinds))
	for _, k := range slowKinds {
		set[k] = true
	}
	return &gateHandler{slowKinds: set, release: make(chan struct{})}
}

func (h *gateHandler) Handle(ctx context.Context, cmd Command) CommandResult {
	if h.slowKinds[cmd.Kind] {
		h.mu.Lock()
		h.inflight++
		h.mu.Unlock()
		select {
		case <-h.release:
		case <-ctx.Done():
		}
	}
	h.mu.Lock()
	h.handled = append(h.handled, cmd)
	h.mu.Unlock()
	return CommandResult{CommandID: cmd.CommandID, Success: true}
}

func (h *gateHandler) inflightCount() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.inflight
}

func (h *gateHandler) Events() <-chan StatusEvent   { return nil }
func (h *gateHandler) Logs() <-chan LogEvent        { return nil }
func (h *gateHandler) Metrics() <-chan MetricsEvent { return nil }

// A burst of slow long-running ops on maxConcurrentLanes distinct servers
// saturates the global concurrency cap. An instant ServerCommand for a further,
// otherwise-idle server must still complete promptly — the quick-command bypass
// lets it skip the cap (issue #169). Without the bypass it blocks on a lane slot
// and this test times out.
func TestQuickCommandBypassesSaturatedCap(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newGateHandler("HydrateTrigger")
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// Saturate the cap with one slow hydrate per distinct server.
	for i := 0; i < maxConcurrentLanes; i++ {
		id := fmt.Sprintf("slow-%d", i)
		transport.commands <- Command{CommandID: id, ServerID: id, Kind: "HydrateTrigger"}
	}

	// Wait until all slow ops are actually in-flight (each holding a cap slot)
	// before issuing the quick command, so the cap is genuinely saturated.
	waitFor(t, func() bool { return handler.inflightCount() == maxConcurrentLanes })

	// An instant ServerCommand for a different, idle server must still complete
	// while all slow lanes hold the cap.
	transport.commands <- Command{CommandID: "quick", ServerID: "quick-server", Kind: "ServerCommand"}

	waitFor(t, func() bool {
		for _, res := range transport.resultsCopy() {
			if res.CommandID == "quick" && res.Success {
				return true
			}
		}
		return false
	})

	// The slow hydrates must still be blocked: only the quick command got through.
	for _, res := range transport.resultsCopy() {
		if res.CommandID != "quick" {
			t.Fatalf("slow op %q answered before release; cap was not actually saturated", res.CommandID)
		}
	}

	close(handler.release)
	cancel()
	<-done
}

// A TunnelDial (a player join) must also bypass the saturated cap: a join must
// not queue behind a hydrate (issue #958, RELAY.md Section 5). This mirrors the
// ServerCommand bypass with a TunnelDial as the quick command.
func TestTunnelDialBypassesSaturatedCap(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newGateHandler("HydrateTrigger")
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// Saturate the cap with one slow hydrate per distinct server.
	for i := 0; i < maxConcurrentLanes; i++ {
		id := fmt.Sprintf("slow-%d", i)
		transport.commands <- Command{CommandID: id, ServerID: id, Kind: "HydrateTrigger"}
	}
	waitFor(t, func() bool { return handler.inflightCount() == maxConcurrentLanes })

	// A join (TunnelDial) for a different, idle server must still complete while all
	// slow lanes hold the cap.
	transport.commands <- Command{CommandID: "join", ServerID: "join-server", Kind: "TunnelDial"}

	waitFor(t, func() bool {
		for _, res := range transport.resultsCopy() {
			if res.CommandID == "join" && res.Success {
				return true
			}
		}
		return false
	})

	for _, res := range transport.resultsCopy() {
		if res.CommandID != "join" {
			t.Fatalf("slow op %q answered before release; cap was not actually saturated", res.CommandID)
		}
	}

	close(handler.release)
	cancel()
	<-done
}

// OpenBedrockTunnel must also bypass the saturated cap (issue #1546,
// docs/app/BEDROCK_TUNNEL.md): Open returns once the tunnel is registered, not
// once the handshake completes, so it must not queue behind a hydrate either.
// This mirrors TestTunnelDialBypassesSaturatedCap with OpenBedrockTunnel as the
// quick command.
func TestOpenBedrockTunnelBypassesSaturatedCap(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newGateHandler("HydrateTrigger")
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// Saturate the cap with one slow hydrate per distinct server.
	for i := 0; i < maxConcurrentLanes; i++ {
		id := fmt.Sprintf("slow-%d", i)
		transport.commands <- Command{CommandID: id, ServerID: id, Kind: "HydrateTrigger"}
	}
	waitFor(t, func() bool { return handler.inflightCount() == maxConcurrentLanes })

	// An OpenBedrockTunnel for a different, idle server must still complete while
	// all slow lanes hold the cap.
	transport.commands <- Command{CommandID: "open", ServerID: "bedrock-server", Kind: "OpenBedrockTunnel"}

	waitFor(t, func() bool {
		for _, res := range transport.resultsCopy() {
			if res.CommandID == "open" && res.Success {
				return true
			}
		}
		return false
	})

	for _, res := range transport.resultsCopy() {
		if res.CommandID != "open" {
			t.Fatalf("slow op %q answered before release; cap was not actually saturated", res.CommandID)
		}
	}

	close(handler.release)
	cancel()
	<-done
}

// The bypass must not break per-server FIFO/safety: a ServerCommand queued behind
// a same-server long-running op must still wait for that op, never racing ahead
// of it (issue #169). A command must not run against a server mid-hydrate.
func TestQuickCommandStillSerializesWithinServer(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newGateHandler("HydrateTrigger")
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// A slow hydrate then a ServerCommand for the SAME server.
	transport.commands <- Command{CommandID: "hydrate", ServerID: "s1", Kind: "HydrateTrigger"}
	transport.commands <- Command{CommandID: "cmd", ServerID: "s1", Kind: "ServerCommand"}

	// The same-server quick command must not bypass ahead of the in-flight
	// hydrate: while the hydrate is blocked, nothing for s1 may complete.
	waitFor(t, func() bool { return r.laneCount() == 1 })
	for _, res := range transport.resultsCopy() {
		if res.CommandID == "cmd" {
			t.Fatal("same-server ServerCommand bypassed an in-flight hydrate; FIFO broken")
		}
	}

	// Release the hydrate; both complete in FIFO order.
	close(handler.release)
	waitFor(t, func() bool { return len(transport.resultsCopy()) == 2 })
	got := transport.resultsCopy()
	if got[0].CommandID != "hydrate" || got[1].CommandID != "cmd" {
		t.Fatalf("same-server order = [%s %s], want [hydrate cmd]", got[0].CommandID, got[1].CommandID)
	}

	cancel()
	<-done
}

func TestStatusEventsForwardedAsStatusChange(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{})
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	waitFor(t, func() bool {
		transport.mu.Lock()
		defer transport.mu.Unlock()
		return transport.registers == 1
	})

	handler.events <- StatusEvent{ServerID: "srv-1", State: "running"}

	waitFor(t, func() bool { return len(transport.statusesCopy()) == 1 })
	got := transport.statusesCopy()[0]
	if got.ServerID != "srv-1" || got.State != "running" {
		t.Fatalf("forwarded status = %+v, want srv-1 running", got)
	}

	cancel()
	<-done
}

func TestLogEventsForwardedAsLogLine(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{})
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	waitFor(t, func() bool {
		transport.mu.Lock()
		defer transport.mu.Unlock()
		return transport.registers == 1
	})

	handler.logs <- LogEvent{ServerID: "srv-1", Line: "hello", Stream: LogStreamStderr}

	waitFor(t, func() bool { return len(transport.logLinesCopy()) == 1 })
	got := transport.logLinesCopy()[0]
	if got.ServerID != "srv-1" || got.Line != "hello" || got.Stream != LogStreamStderr {
		t.Fatalf("forwarded log = %+v, want srv-1 hello stderr", got)
	}

	cancel()
	<-done
}

func TestMetricsEventsForwardedAsMetrics(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{})
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	waitFor(t, func() bool {
		transport.mu.Lock()
		defer transport.mu.Unlock()
		return transport.registers == 1
	})

	handler.metrics <- MetricsEvent{ServerID: "srv-1", CPUMillis: 250, MemoryBytes: 4096}

	waitFor(t, func() bool { return len(transport.metricsCopy()) == 1 })
	got := transport.metricsCopy()[0]
	if got.ServerID != "srv-1" || got.CPUMillis != 250 || got.MemoryBytes != 4096 {
		t.Fatalf("forwarded metrics = %+v", got)
	}

	cancel()
	<-done
}

func TestCleanShutdownOnCancel(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	r := NewRunner(dialer, testCaps(), clock, discardLogger())

	ctx, cancel := context.WithCancel(context.Background())
	var (
		err error
		wg  sync.WaitGroup
	)
	wg.Add(1)
	go func() { defer wg.Done(); err = r.Run(ctx) }()

	waitFor(t, func() bool {
		transport.mu.Lock()
		defer transport.mu.Unlock()
		return transport.registers == 1
	})

	cancel()
	wg.Wait()

	if err != nil {
		t.Errorf("Run() on cancel returned %v, want nil (clean shutdown)", err)
	}
	transport.mu.Lock()
	closed := transport.closed
	transport.mu.Unlock()
	if !closed {
		t.Error("transport not closed on shutdown")
	}
}

// The RegisterAck's transfer_deadline is pushed onto a handler that implements
// the optional TransferDeadlineSetter after registration (issue #874), so the
// instance manager can bound its data-plane transfers from one source.
func TestRegisterAckTransferDeadlinePlumbedToHandler(t *testing.T) {
	ack := acceptedAck()
	ack.TransferDeadline = 11 * time.Minute
	transport := newFakeTransport(ack)
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{Success: true})
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	waitFor(t, func() bool {
		_, set := handler.transferDeadlineCopy()
		return set
	})
	got, _ := handler.transferDeadlineCopy()
	if got != 11*time.Minute {
		t.Fatalf("pushed transfer deadline = %v, want 11m", got)
	}

	cancel()
	<-done
}

// After a (re-)register the session asks a handler that implements the optional
// StatusResyncer to re-emit its live instances' state (issue #985), and those
// re-emitted events are forwarded as StatusChange on the freshly registered
// stream — so an API restart moves a still-running server out of observed=unknown
// within seconds instead of over the reconciler grace window.
func TestRegisterTriggersStatusResyncOnNewStream(t *testing.T) {
	first := newFakeTransport(acceptedAck())
	second := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{first, second}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{})
	// The handler re-emits a running server on every resync, modeling a live
	// instance the worker still holds across the control-plane reconnect.
	handler.resyncEmit = []StatusEvent{{ServerID: "srv-1", State: "running"}}
	r := NewRunner(dialer, testCaps(), clock, discardLogger(),
		WithCommandHandler(handler), WithRandFloat(func() float64 { return 0 }))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// First register fires the resync; the re-emitted running status is forwarded
	// on the first stream.
	waitFor(t, func() bool { return first.registerCount() == 1 })
	waitFor(t, func() bool { return len(first.statusesCopy()) == 1 })
	if got := first.statusesCopy()[0]; got.ServerID != "srv-1" || got.State != "running" {
		t.Fatalf("resync status on first stream = %+v, want srv-1 running", got)
	}

	// Drop the first stream; the worker reconnects and re-registers, and the
	// resync re-emits the running status onto the SECOND (new) stream.
	close(first.commands)
	waitFor(t, func() bool {
		clock.fireNext()
		return second.registerCount() == 1
	})
	waitFor(t, func() bool { return len(second.statusesCopy()) == 1 })
	if got := second.statusesCopy()[0]; got.ServerID != "srv-1" || got.State != "running" {
		t.Fatalf("resync status on second stream = %+v, want srv-1 running", got)
	}
	if calls := handler.resyncCallsCopy(); calls != 2 {
		t.Fatalf("ResyncStatus calls = %d, want 2 (one per register)", calls)
	}

	cancel()
	<-done
}

// A handler that re-emits nothing on resync (the empty-instance-map case, e.g. a
// fresh worker process) produces no spurious StatusChange after register.
func TestRegisterStatusResyncEmptyEmitsNothing(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{}) // resyncEmit is nil: a no-op resync
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	waitFor(t, func() bool { return handler.resyncCallsCopy() == 1 })

	// Drive a heartbeat so the serve loop has demonstrably run past register; no
	// status should have been forwarded.
	var timer *fakeTimer
	waitFor(t, func() bool {
		timer = clock.firstTimer()
		return timer != nil
	})
	timer.fire()
	waitFor(t, func() bool { return transport.heartbeatCount() >= 1 })
	if n := len(transport.statusesCopy()); n != 0 {
		t.Fatalf("statuses forwarded = %d, want 0 (empty resync is a no-op)", n)
	}

	cancel()
	<-done
}

// A handled-kind command with an empty ServerID must be rejected with
// CommandErrorServerNotFound and never reach the handler (issue #1618). Every
// handled kind is server-scoped by contract; an empty id bypasses the
// per-server lane machinery and would run inline on the receive goroutine.
func TestHandledKindWithEmptyServerIDRejected(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{Success: true})
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	handledKinds := []string{
		"StartServer", "StopServer", "RestartServer", "ServerCommand",
		"HydrateTrigger", "SnapshotTrigger", "ReadFile", "EditFile", "ListFiles",
		"TunnelDial", "OpenBedrockTunnel", "CloseBedrockTunnel",
	}
	for i, kind := range handledKinds {
		transport.commands <- Command{
			CommandID: fmt.Sprintf("cmd-%d", i),
			ServerID:  "",
			Kind:      kind,
		}
	}

	waitFor(t, func() bool { return len(transport.resultsCopy()) == len(handledKinds) })

	for i, res := range transport.resultsCopy() {
		if res.CommandID != fmt.Sprintf("cmd-%d", i) {
			t.Errorf("result[%d] CommandID = %q, want cmd-%d", i, res.CommandID, i)
		}
		if res.Success {
			t.Errorf("result[%d] (%s) Success = true, want false", i, handledKinds[i])
		}
		if res.ErrorCode != CommandErrorServerNotFound {
			t.Errorf("result[%d] (%s) ErrorCode = %v, want CommandErrorServerNotFound", i, handledKinds[i], res.ErrorCode)
		}
		if res.ErrorMessage == "" {
			t.Errorf("result[%d] (%s) ErrorMessage empty, want a description", i, handledKinds[i])
		}
	}

	if n := len(handler.handledCopy()); n != 0 {
		t.Errorf("handler invoked %d times, want 0 (empty ServerID must not reach handler)", n)
	}

	cancel()
	<-done
}

// An empty-ServerID handled-kind command must not block the receive loop: it
// is rejected instantly with CommandErrorServerNotFound, so a subsequent command
// for a real server proceeds without delay (issue #1618).
func TestEmptyServerIDCommandDoesNotBlockReceiveLoop(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newLaneHandler("")
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// Send an empty-ServerID handled-kind, then a real server command.
	transport.commands <- Command{CommandID: "hyd", ServerID: "", Kind: "HydrateTrigger"}
	transport.commands <- Command{CommandID: "stop-s2", ServerID: "s2", Kind: "StopServer"}

	// stop-s2 must complete successfully.
	waitFor(t, func() bool {
		for _, res := range transport.resultsCopy() {
			if res.CommandID == "stop-s2" && res.Success {
				return true
			}
		}
		return false
	})

	// hyd must have been rejected with CommandErrorServerNotFound.
	var hydResult *CommandResult
	for _, res := range transport.resultsCopy() {
		if res.CommandID == "hyd" {
			r := res
			hydResult = &r
		}
	}
	if hydResult == nil {
		t.Fatal("hyd result not found")
	}
	if hydResult.Success {
		t.Error("hyd Success = true, want false")
	}
	if hydResult.ErrorCode != CommandErrorServerNotFound {
		t.Errorf("hyd ErrorCode = %v, want CommandErrorServerNotFound", hydResult.ErrorCode)
	}

	// The handler must have processed only stop-s2, never hyd.
	ids := handler.handledIDs()
	if len(ids) != 1 || ids[0] != "stop-s2" {
		t.Errorf("handler processed %v, want only [stop-s2]", ids)
	}

	cancel()
	<-done
}

// An empty-ServerID command with an unknown (or empty) Kind must still get the
// canned "unsupported" result with CommandErrorInternal — the empty-ServerID
// guard only applies to handled kinds (issue #1618).
func TestEmptyServerIDUnknownKindStaysUnsupported(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{Success: true})
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	transport.commands <- Command{CommandID: "empty", ServerID: "", Kind: ""}
	transport.commands <- Command{CommandID: "fleet", ServerID: "", Kind: "SomeFutureFleetCommand"}

	waitFor(t, func() bool { return len(transport.resultsCopy()) == 2 })

	for _, res := range transport.resultsCopy() {
		if res.Success {
			t.Errorf("result %q Success = true, want false (unsupported)", res.CommandID)
		}
		if res.ErrorCode != CommandErrorInternal {
			t.Errorf("result %q ErrorCode = %v, want CommandErrorInternal", res.CommandID, res.ErrorCode)
		}
		if res.ErrorMessage == "" {
			t.Errorf("result %q ErrorMessage empty, want an explanation", res.CommandID)
		}
	}

	if n := len(handler.handledCopy()); n != 0 {
		t.Errorf("handler invoked %d times, want 0", n)
	}

	cancel()
	<-done
}
