package session

import (
	"context"
	"fmt"
	"log/slog"
	"testing"
	"time"
)

// The tests below pin the command expiry contract (issue #3305, CONTROL_PLANE.md
// Section 5): a command still waiting when its expires_after budget runs out is
// answered EXPIRED and never reaches the handler; one that starts within its
// budget, or carries none, runs as before.

// startExpiryRunner runs a Runner over one fake stream with handler wired, and
// returns its transport and fake clock. The returned stop cancels the run and
// waits for it to exit.
func startExpiryRunner(t *testing.T, handler CommandHandler, logger *slog.Logger) (*fakeTransport, *fakeClock, func()) {
	t.Helper()
	transport := newFakeTransport(acceptedAck())
	dialer := &fakeDialer{transports: []*fakeTransport{transport}}
	clock := newFakeClock()
	r := NewRunner(dialer, testCaps(), clock, logger, WithCommandHandler(handler))
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = r.Run(ctx); close(done) }()
	return transport, clock, func() { cancel(); <-done }
}

// awaitReceived sends a command with no server id and waits for its inline
// answer. The receive loop handles commands one at a time, so once that answer
// is out every command sent before it has been received and stamped with its
// receipt time: moving the fake clock after this is moving it after the receipt.
func awaitReceived(t *testing.T, transport *fakeTransport) {
	t.Helper()
	transport.commands <- Command{CommandID: "received-marker", Kind: "Unknown"}
	waitFor(t, func() bool { _, ok := resultFor(transport, "received-marker"); return ok })
}

func resultFor(transport *fakeTransport, commandID string) (CommandResult, bool) {
	for _, res := range transport.resultsCopy() {
		if res.CommandID == commandID {
			return res, true
		}
	}
	return CommandResult{}, false
}

func waitForResult(t *testing.T, transport *fakeTransport, commandID string) CommandResult {
	t.Helper()
	waitFor(t, func() bool { _, ok := resultFor(transport, commandID); return ok })
	res, _ := resultFor(transport, commandID)
	return res
}

func (h *gateHandler) handledIDs() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	ids := make([]string, 0, len(h.handled))
	for _, cmd := range h.handled {
		ids = append(ids, cmd.CommandID)
	}
	return ids
}

func advance(clock *fakeClock, d time.Duration) {
	clock.mu.Lock()
	clock.now = clock.now.Add(d)
	clock.mu.Unlock()
}

func assertExpired(t *testing.T, res CommandResult) {
	t.Helper()
	if res.Success || res.ErrorCode != CommandErrorExpired {
		t.Fatalf("result = %+v, want a failure with CommandErrorExpired", res)
	}
}

func assertNotHandled(t *testing.T, handler *gateHandler, commandID string) {
	t.Helper()
	for _, id := range handler.handledIDs() {
		if id == commandID {
			t.Fatalf("handler ran %q (handled %v); an expired command must never reach it", commandID, handler.handledIDs())
		}
	}
}

// A Start queued behind a long same-server Restart outwaits its budget in the
// lane: it is answered EXPIRED when it reaches the head, and the handler never
// sees it. This is the observed #3305 case: a reconciler start the API had
// already given up on ran once the restart finished.
func TestCommandExpiredInTheLaneIsNotExecuted(t *testing.T) {
	logger, logs := captureLogger()
	handler := newGateHandler("RestartServer")
	transport, clock, stop := startExpiryRunner(t, handler, logger)
	defer stop()

	transport.commands <- Command{CommandID: "restart", ServerID: "s1", Kind: "RestartServer"}
	transport.commands <- Command{CommandID: "start", ServerID: "s1", Kind: "StartServer", ExpiresAfter: 20 * time.Second}
	awaitReceived(t, transport)
	waitFor(t, func() bool { return handler.inflightCount() == 1 })

	advance(clock, 30*time.Second)
	close(handler.release)

	assertExpired(t, waitForResult(t, transport, "start"))
	assertNotHandled(t, handler, "start")
	if res, _ := resultFor(transport, "restart"); !res.Success {
		t.Fatalf("restart result = %+v, want the gated restart to succeed", res)
	}
	var expiredLogs int
	for _, rec := range logs.recordsAtLevel(slog.LevelInfo) {
		if rec.Message == "command expired before it started; not executing it" {
			expiredLogs++
		}
	}
	if expiredLogs != 1 {
		t.Fatalf("expired INFO logs = %d, want 1", expiredLogs)
	}
	for _, rec := range logs.recordsAtLevel(slog.LevelWarn) {
		rec.Attrs(func(a slog.Attr) bool {
			if a.Key == "command_id" && a.Value.String() == "start" {
				t.Errorf("WARN %q for the expired start: it did not fail, it was never run", rec.Message)
			}
			return true
		})
	}
}

// A quick command (one that bypasses the concurrency cap) expires in the lane the
// same way: the check runs on the quick path too.
func TestQuickCommandExpiredInTheLaneIsNotExecuted(t *testing.T) {
	handler := newGateHandler("RestartServer")
	transport, clock, stop := startExpiryRunner(t, handler, discardLogger())
	defer stop()

	transport.commands <- Command{CommandID: "restart", ServerID: "s1", Kind: "RestartServer"}
	transport.commands <- Command{CommandID: "console", ServerID: "s1", Kind: "ServerCommand", Line: "say hi", ExpiresAfter: 20 * time.Second}
	awaitReceived(t, transport)
	waitFor(t, func() bool { return handler.inflightCount() == 1 })

	advance(clock, 30*time.Second)
	close(handler.release)

	assertExpired(t, waitForResult(t, transport, "console"))
	assertNotHandled(t, handler, "console")
}

// Commands parked on a saturated Worker-wide cap, behind other servers' long
// work, expire the same way. The check runs after the slot is acquired: before it
// the commands had not waited long enough, so a check placed only there would let
// them run. As many commands expire as the cap has slots, so a slot an expired
// command kept would leave none for the command after them.
func TestCommandExpiredOnTheSaturatedCapIsNotExecuted(t *testing.T) {
	handler := newGateHandler("HydrateTrigger")
	transport, clock, stop := startExpiryRunner(t, handler, discardLogger())
	defer stop()

	for i := 0; i < maxConcurrentLanes; i++ {
		id := fmt.Sprintf("slow-%d", i)
		transport.commands <- Command{CommandID: id, ServerID: id, Kind: "HydrateTrigger"}
	}
	waitFor(t, func() bool { return handler.inflightCount() == maxConcurrentLanes })
	for i := 0; i < maxConcurrentLanes; i++ {
		id := fmt.Sprintf("start-%d", i)
		transport.commands <- Command{CommandID: id, ServerID: id, Kind: "StartServer", ExpiresAfter: 20 * time.Second}
	}
	awaitReceived(t, transport)

	advance(clock, 30*time.Second)
	close(handler.release)

	for i := 0; i < maxConcurrentLanes; i++ {
		id := fmt.Sprintf("start-%d", i)
		assertExpired(t, waitForResult(t, transport, id))
		assertNotHandled(t, handler, id)
	}
	// Every expired command gave its slot back: a later command still gets one.
	transport.commands <- Command{CommandID: "after", ServerID: "s2", Kind: "StartServer"}
	if res := waitForResult(t, transport, "after"); !res.Success {
		t.Fatalf("result = %+v, want the later command to run", res)
	}
}

// A command that reaches the head within its budget runs as before.
func TestCommandDequeuedWithinItsBudgetRuns(t *testing.T) {
	handler := newGateHandler("RestartServer")
	transport, clock, stop := startExpiryRunner(t, handler, discardLogger())
	defer stop()

	transport.commands <- Command{CommandID: "restart", ServerID: "s1", Kind: "RestartServer"}
	transport.commands <- Command{CommandID: "start", ServerID: "s1", Kind: "StartServer", ExpiresAfter: 20 * time.Second}
	awaitReceived(t, transport)
	waitFor(t, func() bool { return handler.inflightCount() == 1 })

	advance(clock, 10*time.Second)
	close(handler.release)

	if res := waitForResult(t, transport, "start"); !res.Success {
		t.Fatalf("result = %+v, want the start to run", res)
	}
}

// A command without an expiry never expires, however long it waits.
func TestCommandWithoutExpiryNeverExpires(t *testing.T) {
	handler := newGateHandler("RestartServer")
	transport, clock, stop := startExpiryRunner(t, handler, discardLogger())
	defer stop()

	transport.commands <- Command{CommandID: "restart", ServerID: "s1", Kind: "RestartServer"}
	transport.commands <- Command{CommandID: "start", ServerID: "s1", Kind: "StartServer"}
	awaitReceived(t, transport)
	waitFor(t, func() bool { return handler.inflightCount() == 1 })

	advance(clock, 10*time.Minute)
	close(handler.release)

	if res := waitForResult(t, transport, "start"); !res.Success {
		t.Fatalf("result = %+v, want the start to run", res)
	}
}

// A command without an expiry parked on a saturated cap runs once a slot frees,
// however long it waited. This is a stop's final snapshot (issue #3305): the API
// sends it unstamped because nothing retries it once it settles.
func TestCommandWithoutExpiryRunsAfterALongCapWait(t *testing.T) {
	handler := newGateHandler("HydrateTrigger")
	transport, clock, stop := startExpiryRunner(t, handler, discardLogger())
	defer stop()

	for i := 0; i < maxConcurrentLanes; i++ {
		id := fmt.Sprintf("slow-%d", i)
		transport.commands <- Command{CommandID: id, ServerID: id, Kind: "HydrateTrigger"}
	}
	waitFor(t, func() bool { return handler.inflightCount() == maxConcurrentLanes })
	transport.commands <- Command{CommandID: "final", ServerID: "s1", Kind: "SnapshotTrigger"}
	awaitReceived(t, transport)
	if res, ok := resultFor(transport, "final"); ok {
		t.Fatalf("result = %+v before a slot freed, want the snapshot parked on the cap", res)
	}

	advance(clock, 10*time.Minute)
	close(handler.release)

	if res := waitForResult(t, transport, "final"); !res.Success {
		t.Fatalf("result = %+v, want the final snapshot to run", res)
	}
}
