package session

import (
	"context"
	"slices"
	"sync"
	"testing"
)

// The tests below pin what a running server's snapshot does to the rest of its
// lane (issue #3659): a ServerCommand or TunnelDial runs beside it instead of
// waiting for the backup to finish, while every ordering against a lifecycle
// command for the same server stays as it was.

// orderHandler holds each command whose id it was built with until that gate is
// opened, and records when each command starts and ends.
type orderHandler struct {
	gates map[string]chan struct{}

	mu     sync.Mutex
	events []string
}

func newOrderHandler(gated ...string) *orderHandler {
	h := &orderHandler{gates: make(map[string]chan struct{}, len(gated))}
	for _, id := range gated {
		h.gates[id] = make(chan struct{})
	}
	return h
}

func (h *orderHandler) Handle(ctx context.Context, cmd Command) CommandResult {
	h.record("start:" + cmd.CommandID)
	if gate, ok := h.gates[cmd.CommandID]; ok {
		select {
		case <-gate:
		case <-ctx.Done():
		}
	}
	h.record("end:" + cmd.CommandID)
	return CommandResult{CommandID: cmd.CommandID, Success: true}
}

func (h *orderHandler) Events() <-chan StatusEvent   { return nil }
func (h *orderHandler) Logs() <-chan LogEvent        { return nil }
func (h *orderHandler) Metrics() <-chan MetricsEvent { return nil }

func (h *orderHandler) record(event string) {
	h.mu.Lock()
	h.events = append(h.events, event)
	h.mu.Unlock()
}

func (h *orderHandler) open(id string) { close(h.gates[id]) }

func (h *orderHandler) started(id string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return slices.Contains(h.events, "start:"+id)
}

// assertBefore fails unless both events happened and first came before second.
func (h *orderHandler) assertBefore(t *testing.T, first, second string) {
	t.Helper()
	h.mu.Lock()
	defer h.mu.Unlock()
	i, j := slices.Index(h.events, first), slices.Index(h.events, second)
	if i < 0 || j < 0 || i > j {
		t.Fatalf("events = %v, want %q before %q", h.events, first, second)
	}
}

var lifecycleKinds = []string{"StartServer", "StopServer", "RestartServer"}

var passingKinds = []string{"ServerCommand", "TunnelDial"}

// While a server's snapshot runs, a player join and a console line for that
// server are answered without waiting for it, in the order they arrived.
func TestQuickCommandsDoNotWaitForASameServerSnapshot(t *testing.T) {
	handler := newOrderHandler("snap")
	transport, _, stop := startExpiryRunner(t, handler, discardLogger())
	defer stop()

	transport.commands <- Command{CommandID: "snap", ServerID: "s1", Kind: "SnapshotTrigger"}
	waitFor(t, func() bool { return handler.started("snap") })
	transport.commands <- Command{CommandID: "join", ServerID: "s1", Kind: "TunnelDial"}
	transport.commands <- Command{CommandID: "console", ServerID: "s1", Kind: "ServerCommand", Line: "say hi"}

	waitForResult(t, transport, "join")
	waitForResult(t, transport, "console")
	if res, ok := resultFor(transport, "snap"); ok {
		t.Fatalf("snapshot answered %+v before its gate opened; it did not hold the lane as set up", res)
	}
	handler.assertBefore(t, "end:join", "start:console")

	handler.open("snap")
	waitForResult(t, transport, "snap")
}

// A lifecycle command that arrives after a quick command has passed the
// snapshot, and the lane's queue has emptied, still waits for the snapshot: a
// running snapshot takes no reservation, so the lane is all that keeps a stop
// from ending the server under it.
func TestLifecycleCommandWaitsForTheSnapshotAQuickCommandPassed(t *testing.T) {
	for _, kind := range lifecycleKinds {
		t.Run(kind, func(t *testing.T) {
			handler := newOrderHandler("snap")
			transport, _, stop := startExpiryRunner(t, handler, discardLogger())
			defer stop()

			transport.commands <- Command{CommandID: "snap", ServerID: "s1", Kind: "SnapshotTrigger"}
			waitFor(t, func() bool { return handler.started("snap") })
			transport.commands <- Command{CommandID: "console", ServerID: "s1", Kind: "ServerCommand"}
			waitForResult(t, transport, "console")
			transport.commands <- Command{CommandID: "lifecycle", ServerID: "s1", Kind: kind}
			awaitReceived(t, transport)

			handler.open("snap")
			waitForResult(t, transport, "lifecycle")
			handler.assertBefore(t, "end:snap", "start:lifecycle")
		})
	}
}

// A quick command queued behind a lifecycle command waits for it, even while a
// snapshot runs ahead of both: it passes only the snapshot, never a command that
// changes whether the server runs.
func TestQuickCommandDoesNotPassAQueuedLifecycleCommand(t *testing.T) {
	for _, kind := range lifecycleKinds {
		for _, quick := range passingKinds {
			t.Run(kind+"/"+quick, func(t *testing.T) {
				handler := newOrderHandler("snap")
				transport, _, stop := startExpiryRunner(t, handler, discardLogger())
				defer stop()

				transport.commands <- Command{CommandID: "snap", ServerID: "s1", Kind: "SnapshotTrigger"}
				waitFor(t, func() bool { return handler.started("snap") })
				transport.commands <- Command{CommandID: "lifecycle", ServerID: "s1", Kind: kind}
				transport.commands <- Command{CommandID: "quick", ServerID: "s1", Kind: quick}
				awaitReceived(t, transport)

				handler.open("snap")
				waitForResult(t, transport, "quick")
				handler.assertBefore(t, "end:lifecycle", "start:quick")
			})
		}
	}
}

// A lifecycle command waits for a quick command sent before it, even when that
// quick command passed a snapshot and the snapshot finishes first.
func TestLifecycleCommandWaitsForAQuickCommandBeforeIt(t *testing.T) {
	for _, kind := range lifecycleKinds {
		for _, quick := range passingKinds {
			t.Run(kind+"/"+quick, func(t *testing.T) {
				handler := newOrderHandler("snap", "quick")
				transport, _, stop := startExpiryRunner(t, handler, discardLogger())
				defer stop()

				transport.commands <- Command{CommandID: "snap", ServerID: "s1", Kind: "SnapshotTrigger"}
				waitFor(t, func() bool { return handler.started("snap") })
				transport.commands <- Command{CommandID: "quick", ServerID: "s1", Kind: quick}
				waitFor(t, func() bool { return handler.started("quick") })
				transport.commands <- Command{CommandID: "lifecycle", ServerID: "s1", Kind: kind}
				awaitReceived(t, transport)

				handler.open("snap")
				waitForResult(t, transport, "snap")
				handler.open("quick")
				waitForResult(t, transport, "lifecycle")
				handler.assertBefore(t, "end:quick", "start:lifecycle")
			})
		}
	}
}
