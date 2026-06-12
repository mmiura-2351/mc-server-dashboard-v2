package game

import (
	"context"
	"io"
	"log/slog"
	"net"
	"sync/atomic"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/tunnel"
)

// TestAwaitTunnelExpiredDialBackDoesNotHang is the regression for the
// expired-token leak: the token's TTL elapses while the waiter is still inside
// its dial-back window, then a dial-back arrives. Deliver must reject it
// (expired) without consuming the waiter entry, so awaitTunnel reclaims the
// entry via Cancel and returns ok=false instead of blocking on its channel
// forever. We assert awaitTunnel does not hang (which would leak the goroutine,
// the player conn, and the IP-cap slot).
func TestAwaitTunnelExpiredDialBackDoesNotHang(t *testing.T) {
	// now is read by the table's clock from the awaitTunnel goroutine while the
	// test advances it; guard it with an atomic to stay race-free.
	var now atomic.Int64
	now.Store(time.Unix(0, 0).UnixNano())
	tokens := tunnel.NewTokenTable(10*time.Second, func() time.Time { return time.Unix(0, now.Load()) })
	l := &Listener{tokens: tokens, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}

	// Cancellable context drives awaitTunnel's timeout path quickly without
	// waiting out the 10 s dial-back timer.
	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan struct{})
	go func() {
		defer close(done)
		if _, ok := l.awaitTunnel(ctx, "tok"); ok {
			t.Error("awaitTunnel should not report success for an expired/cancelled wait")
		}
	}()

	// Give the goroutine time to register the waiter (a synchronous map insert at
	// awaitTunnel entry), then expire the token and present a late dial-back.
	time.Sleep(50 * time.Millisecond)
	now.Add(int64(11 * time.Second))
	dialBack, _ := net.Pipe()
	defer func() { _ = dialBack.Close() }()
	if tokens.Deliver("tok", dialBack) {
		t.Fatal("expired Deliver should not match the waiter")
	}

	// Trigger awaitTunnel's timeout path; with the fix the waiter entry survived
	// the expired Deliver, so Cancel reclaims it and the goroutine exits.
	cancel()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("awaitTunnel hung on an expired dial-back (goroutine/conn leak)")
	}
}
