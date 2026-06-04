package session

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

func testCaps() Capabilities {
	return Capabilities{
		WorkerID:      "worker-1",
		WorkerVersion: "test",
		Drivers:       []string{"host-process"},
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
	if transport.heartbeatCount() != 0 {
		t.Fatalf("heartbeat sent before timer fired: %d", transport.heartbeatCount())
	}

	// Fire the heartbeat timer once.
	waitFor(t, func() bool { return clock.fireNext() })
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

	for beat := 1; beat <= 3; beat++ {
		waitFor(t, func() bool { return clock.fireNext() })
		want := beat
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

	// Keep firing pending timers (a stale heartbeat timer plus the backoff
	// timer) until the second connection re-registers from scratch
	// (CONTROL_PLANE.md Section 4.4).
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

func TestUnhandledCommandStillUnsupportedWithHandler(t *testing.T) {
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	handler := newFakeHandler(CommandResult{Success: true})
	r := NewRunner(dialer, testCaps(), clock, discardLogger(), WithCommandHandler(handler))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()

	// ReadFile is not handled yet (epic #9); it must stay unsupported and never
	// reach the handler, even with one wired.
	transport.commands <- Command{CommandID: "cmd-2", ServerID: "srv-1", Kind: "ReadFile"}

	waitFor(t, func() bool { return len(transport.resultsCopy()) == 1 })
	got := transport.resultsCopy()[0]
	if got.Success {
		t.Fatal("ReadFile should remain unsupported even with a handler")
	}
	if len(handler.handledCopy()) != 0 {
		t.Fatal("ReadFile should not reach the handler")
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

func (h *blockingHandler) Events() <-chan StatusEvent { return h.events }

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
