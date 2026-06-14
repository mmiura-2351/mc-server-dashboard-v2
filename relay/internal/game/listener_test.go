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

// TestAwaitTunnelNilConnFromSweptChannel is the regression for issue #1045:
// when the token sweep (sweepExpired) closes the waiter channel before the
// dial-back timer fires, <-ch yields nil. awaitTunnel must detect this and
// return (nil, false) instead of (nil, true) — the latter causes a nil
// dereference panic in both callers.
func TestAwaitTunnelNilConnFromSweptChannel(t *testing.T) {
	// A short TTL so the sweep finds the entry expired immediately.
	var now atomic.Int64
	now.Store(time.Unix(0, 0).UnixNano())
	tokens := tunnel.NewTokenTable(1*time.Millisecond, func() time.Time { return time.Unix(0, now.Load()) })
	l := &Listener{tokens: tokens, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}

	ctx := context.Background()
	done := make(chan struct{})
	var gotConn net.Conn
	var gotOK bool

	go func() {
		defer close(done)
		gotConn, gotOK = l.awaitTunnel(ctx, "tok-sweep")
	}()

	// Let the goroutine register the waiter.
	time.Sleep(50 * time.Millisecond)

	// Advance the clock past TTL and trigger the sweep — this closes the channel.
	now.Add(int64(10 * time.Second))
	tokens.SweepExpiredForTest()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("awaitTunnel hung after sweep closed the waiter channel")
	}

	if gotOK {
		t.Error("awaitTunnel must return ok=false when the channel yields nil")
	}
	if gotConn != nil {
		t.Error("awaitTunnel must return nil conn when the channel is closed")
	}
}

// deadlineConn records the write deadline set on it and discards writes, so the
// disconnect-path deadline (issue #971) is observable.
type deadlineConn struct {
	net.Conn
	writeDeadline atomic.Pointer[time.Time]
	closed        atomic.Bool
}

func (c *deadlineConn) Write(b []byte) (int, error) { return len(b), nil }
func (c *deadlineConn) Close() error                { c.closed.Store(true); return nil }
func (c *deadlineConn) SetWriteDeadline(t time.Time) error {
	c.writeDeadline.Store(&t)
	return nil
}

// TestDisconnectSetsWriteDeadline proves disconnect bounds the Login Disconnect
// write so a stalled client cannot pin the goroutine (issue #971).
func TestDisconnectSetsWriteDeadline(t *testing.T) {
	l := &Listener{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	conn := &deadlineConn{}

	before := time.Now()
	l.disconnect(conn, "go away")
	after := time.Now()

	d := conn.writeDeadline.Load()
	if d == nil {
		t.Fatal("disconnect did not set a write deadline")
	}
	if d.Before(before.Add(disconnectWriteTimeout)) || d.After(after.Add(disconnectWriteTimeout)) {
		t.Errorf("write deadline %v not within [now+%v]", *d, disconnectWriteTimeout)
	}
	if !conn.closed.Load() {
		t.Error("disconnect must close the connection")
	}
}
