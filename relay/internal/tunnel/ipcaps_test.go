package tunnel

import (
	"io"
	"log/slog"
	"net"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
)

// fakeAddr is a net.Addr with a fixed host:port, so tests can drive handle with
// a chosen source IP over an in-memory pipe.
type fakeAddr struct{ s string }

func (a fakeAddr) Network() string { return "tcp" }
func (a fakeAddr) String() string  { return a.s }

// capConn wraps a pipe end so its RemoteAddr reports a chosen source IP and so
// the test can observe when handle closes it.
type capConn struct {
	net.Conn
	remote net.Addr
	closed chan struct{}
}

func (c *capConn) RemoteAddr() net.Addr { return c.remote }

func (c *capConn) Close() error {
	select {
	case <-c.closed:
	default:
		close(c.closed)
	}
	return c.Conn.Close()
}

// newCapConn returns a server-side conn whose RemoteAddr reports ip, plus the
// client end the test drives. The caller closes clientEnd.
func newCapConn(ip string) (server *capConn, clientEnd net.Conn) {
	c, s := net.Pipe()
	return &capConn{Conn: s, remote: fakeAddr{ip + ":40000"}, closed: make(chan struct{})}, c
}

// newCapListener builds a Listener with only the cap and token table wired; ln
// is nil because these tests call handle directly rather than Serve.
func newCapListener(maxConns uint32) *Listener {
	return &Listener{
		tokens: NewTokenTable(10*time.Second, time.Now),
		caps:   ipcaps.NewIPCaps(maxConns, 0, time.Now),
		logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

// writeValidHandshake sends a well-formed handshake whose token has no waiter,
// so Deliver returns false and handle closes the conn after releasing its slot.
func writeValidHandshake(t *testing.T, c net.Conn, token string) {
	t.Helper()
	_ = c.SetWriteDeadline(time.Now().Add(time.Second))
	if _, err := c.Write([]byte(handshakePrefix + "\n" + token + "\n")); err != nil {
		t.Fatalf("write handshake: %v", err)
	}
}

// closedWithin reports whether c was closed within d.
func closedWithin(c *capConn, d time.Duration) bool {
	select {
	case <-c.closed:
		return true
	case <-time.After(d):
		return false
	}
}

// TestTunnelCapRejectsOverCapFromSameIP asserts that once an IP holds maxConns
// concurrent connections, the next connection from that IP is closed without
// being read, while a different IP is unaffected.
func TestTunnelCapRejectsOverCapFromSameIP(t *testing.T) {
	const maxConns = 2
	l := newCapListener(maxConns)

	// Pre-saturate the same IP's slots directly on the shared cap, so the test
	// does not depend on goroutine scheduling to reach the cap before probing.
	for i := 0; i < maxConns; i++ {
		if !l.caps.Acquire("1.1.1.1") {
			t.Fatalf("pre-saturating acquire %d should succeed", i)
		}
	}

	// The (maxConns+1)th from the same IP must be closed by handle without
	// reading its handshake (nothing is written, yet it closes promptly).
	over, overCli := newCapConn("1.1.1.1")
	defer func() { _ = overCli.Close() }()
	go l.handle(over)
	if !closedWithin(over, 2*time.Second) {
		t.Fatal("over-cap connection from the same IP was not closed")
	}

	// A different IP is independent: it is accepted into the handshake and
	// completes (no waiter ⇒ silent close by handle itself).
	other, otherCli := newCapConn("2.2.2.2")
	defer func() { _ = otherCli.Close() }()
	otherDone := make(chan struct{})
	go func() { l.handle(other); close(otherDone) }()
	writeValidHandshake(t, otherCli, "no-waiter-token")
	select {
	case <-otherDone:
	case <-time.After(2 * time.Second):
		t.Fatal("connection from a different IP should have been handled")
	}

	l.caps.Release("1.1.1.1")
	l.caps.Release("1.1.1.1")
}

// TestTunnelCapReleasedOnClose asserts the per-IP slot is released when a
// handled connection finishes, so repeated sequential connections from one IP
// over a cap of 1 all succeed rather than the second being rejected.
func TestTunnelCapReleasedOnClose(t *testing.T) {
	l := newCapListener(1)
	for i := 0; i < 5; i++ {
		srv, cli := newCapConn("3.3.3.3")
		done := make(chan struct{})
		go func() { l.handle(srv); close(done) }()
		writeValidHandshake(t, cli, "no-waiter-token")
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Fatalf("handle %d did not return (slot not released ⇒ blocked)", i)
		}
		// handle closed srv after the no-waiter Deliver; if the slot had not been
		// released, a cap of 1 would reject the next iteration and that connection
		// would be closed before its handshake is read, never reaching done above.
		if !closedWithin(srv, time.Second) {
			t.Fatalf("handle %d did not close its connection", i)
		}
		_ = cli.Close()
	}
}
