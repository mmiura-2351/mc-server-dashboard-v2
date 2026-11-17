package session

import (
	"context"
	"testing"
)

// The tests below pin that a dropped session's lanes run nothing more (issue
// #3658): a command still queued when its stream drops is discarded unexecuted,
// a quick one as much as a capped one, so it can never act after the reconnect
// against an intent the new session has moved past.

// droppedLane runs a Runner over two fake streams. On the first it parks a
// ctx-detached HydrateTrigger on s1 and queues stale behind it in s1's lane,
// then drops that stream and waits for the Runner to register on the second.
// It returns the second transport, the handler, the dropped stream's
// dispatcher and a stop that cancels the run and waits for it to exit.
func droppedLane(t *testing.T, stale Command) (*fakeTransport, *gateHandler, *dispatcher, func()) {
	t.Helper()
	t1 := newFakeTransport(acceptedAck())
	t2 := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{t1, t2}}
	clock := newFakeClock()
	handler := newGateHandler("HydrateTrigger")
	handler.detached = true
	r := NewRunner(dialer, testCaps(), clock, discardLogger(),
		WithCommandHandler(handler), WithRandFloat(func() float64 { return 0 }))

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()
	stop := func() { cancel(); <-done }

	var old *dispatcher
	waitFor(t, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		old = r.dispatcher
		return old != nil
	})

	t1.commands <- Command{CommandID: "hydrate", ServerID: "s1", Kind: "HydrateTrigger"}
	t1.commands <- stale
	waitFor(t, func() bool { return handler.inflightCount() == 1 })
	waitFor(t, func() bool {
		old.mu.Lock()
		defer old.mu.Unlock()
		l, ok := old.lanes["s1"]
		return ok && len(l.queue) == 1
	})

	close(t1.commands)
	waitFor(t, func() bool {
		clock.fireNext()
		return t2.registerCount() == 1
	})
	return t2, handler, old, stop
}

// releaseAndDrain lets the parked hydrate finish and waits until the dropped
// stream's lane has exited, so every command it was going to run has run.
func releaseAndDrain(t *testing.T, handler *gateHandler, old *dispatcher) {
	t.Helper()
	close(handler.release)
	waitFor(t, func() bool {
		old.mu.Lock()
		defer old.mu.Unlock()
		return len(old.lanes) == 0
	})
}

// A quick command queued in a lane when its stream drops is not executed after
// the drop, for every quick kind: the dropped session's lane discards it as it
// already discards a capped one.
func TestQuickCommandQueuedBeforeDropDoesNotRunAfterIt(t *testing.T) {
	for _, kind := range []string{"ServerCommand", "TunnelDial", "OpenBedrockTunnel", "CloseBedrockTunnel", "ReclaimDeletedServer"} {
		t.Run(kind, func(t *testing.T) {
			if !isQuickCommand(kind) {
				t.Fatalf("%s is not a quick command; the table is stale", kind)
			}
			_, handler, old, stop := droppedLane(t, Command{CommandID: "stale", ServerID: "s1", Kind: kind})
			defer stop()

			releaseAndDrain(t, handler, old)
			for _, id := range handler.handledIDs() {
				if id == "stale" {
					t.Fatalf("handled %v: a %s queued before the drop ran after it", handler.handledIDs(), kind)
				}
			}
		})
	}
}

// A stale OpenBedrockTunnel from a dropped session does not reopen the tunnel
// the new session has since closed: the new session's Close is the last word.
func TestStaleBedrockOpenDoesNotReopenTunnelClosedByNewSession(t *testing.T) {
	t2, handler, old, stop := droppedLane(t, Command{CommandID: "stale-open", ServerID: "s1", Kind: "OpenBedrockTunnel"})
	defer stop()

	t2.commands <- Command{CommandID: "close", ServerID: "s1", Kind: "CloseBedrockTunnel"}
	waitForResult(t, t2, "close")

	releaseAndDrain(t, handler, old)
	closed := false
	for _, id := range handler.handledIDs() {
		closed = closed || id == "close"
		if closed && id == "stale-open" {
			t.Fatalf("handled %v: a stale Open ran after the new session's Close and reopened the tunnel", handler.handledIDs())
		}
	}
}
