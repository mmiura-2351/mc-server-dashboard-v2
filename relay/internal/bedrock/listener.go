// Package bedrock implements the relay's Bedrock (RakNet/UDP) tunnel: a QUIC
// listener that authenticates a Worker's outbound dial-out (the Bedrock
// analogue of the Java tunnel listener in relay/internal/tunnel), binds a
// per-server public UDP port on acceptance, and pumps RakNet datagrams both
// directions over QUIC DATAGRAM frames (RFC 9221). The relay stays a thin
// forwarder -- it never parses RakNet -- and holds no state beyond a live
// tunnel's flow table; the authenticated dial-out IS the registration, so
// there is no separate server table. See docs/app/BEDROCK_TUNNEL.md (epic
// #1540, issue #1545).
package bedrock

import (
	"context"
	"crypto/tls"
	"log/slog"
	"net"
	"sync"
	"time"

	"github.com/quic-go/quic-go"

	bedrocktunnelv1 "github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/genproto/mcsd/bedrocktunnel/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/netutil"
)

// ALPN is the QUIC application-layer protocol the Bedrock tunnel listener
// negotiates, distinguishing it on the wire from any other QUIC service that
// might share the relay's tunnel certificate (docs/app/BEDROCK_TUNNEL.md).
const ALPN = "mcsd-bedrock/1"

// maxDatagramPayload is the RakNet-payload budget the relay is willing to
// forward in one QUIC DATAGRAM. It is deliberately conservative -- well under
// quic-go's initial per-datagram estimate and under RakNet's own commonly used
// "safe" MTU tier -- so RakNet's own MTU discovery (the client<->relay leg,
// which the relay does not participate in) converges on a size the
// relay<->Worker QUIC leg can always carry. See docs/app/BEDROCK_TUNNEL.md
// "Datagram MTU" for the full rationale; do not change without updating that
// doc.
const maxDatagramPayload = 1200

// maxIdleTimeout is the explicit QUIC idle timeout the listener applies to
// every tunnel connection (quic.Config.MaxIdleTimeout; the effective timeout
// is the minimum of both peers' values). Pinning it -- rather than inheriting
// quic-go's default, which can drift across upgrades -- bounds the
// bind-conflict window after an ungraceful Worker disconnect
// (docs/app/BEDROCK_TUNNEL.md Section 3.1) and is the value the Worker's
// mandated keepalive period must stay well under (Section 3).
const maxIdleTimeout = 15 * time.Second

// Validator confirms a Worker's declared Bedrock tunnel credential against the
// API (mcsd.relay.v1.RelayService.ValidateBedrockTunnel) -- the relay has no
// local waiter to match against for this API-initiated tunnel, unlike the
// per-player Java tunnel token (relay/internal/tunnel.TokenTable).
type Validator interface {
	ValidateBedrockTunnel(ctx context.Context, serverID string, bedrockPort uint32, token string) (bool, error)
}

// Listener accepts Worker QUIC dial-outs, authenticates each one via the
// TunnelHello/TunnelHelloAck handshake, and on acceptance binds a per-server
// Tunnel (docs/app/BEDROCK_TUNNEL.md).
type Listener struct {
	ln        *quic.Listener
	validator Validator
	caps      *ipcaps.IPCaps
	newIPCaps func() *ipcaps.IPCaps
	logger    *slog.Logger

	// mu guards tunnels, the live port->Tunnel index used for takeover
	// (#1565). This is NOT a server table: it holds only ports with a
	// currently bound Tunnel -- populated by bindOrTakeover on a successful
	// bind and cleared by unregister once that Tunnel's run() returns -- so a
	// Worker that never dials leaves no trace, matching the invariant that
	// the authenticated dial-out IS the registration.
	mu      sync.Mutex
	tunnels map[uint32]*Tunnel
}

// NewListener binds the Bedrock tunnel QUIC listener on addr. tlsConf should
// reuse the relay's existing tunnel certificate with ALPN overridden to
// bedrock.ALPN (docs/app/BEDROCK_TUNNEL.md); RFC 9221 datagram support is
// enabled unconditionally here via quic.Config (a TLS-level setting would not
// apply). caps bounds concurrent unauthenticated handshake windows per source
// IP (the #968 posture the TCP tunnel listener already has; only its
// connection cap is used). newIPCaps builds a fresh per-server IPCaps for each
// accepted Tunnel (the public UDP ingress hygiene caps); its lifetime matches
// the Tunnel's.
func NewListener(addr string, tlsConf *tls.Config, validator Validator, caps *ipcaps.IPCaps, newIPCaps func() *ipcaps.IPCaps, logger *slog.Logger) (*Listener, error) {
	quicConf := &quic.Config{EnableDatagrams: true, MaxIdleTimeout: maxIdleTimeout}
	ln, err := quic.ListenAddr(addr, tlsConf, quicConf)
	if err != nil {
		return nil, err
	}
	return &Listener{ln: ln, validator: validator, caps: caps, newIPCaps: newIPCaps, logger: logger, tunnels: make(map[uint32]*Tunnel)}, nil
}

// Addr returns the listener's bound address.
func (l *Listener) Addr() net.Addr { return l.ln.Addr() }

// Serve accepts Worker QUIC connections until ctx is cancelled or the
// listener closes.
func (l *Listener) Serve(ctx context.Context) error {
	go func() {
		<-ctx.Done()
		_ = l.ln.Close()
	}()
	for {
		conn, err := l.ln.Accept(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		go l.handle(ctx, conn)
	}
}

// handle authenticates one Worker dial-out on the first bidirectional stream
// and, on acceptance, runs its Tunnel until the QUIC connection closes
// (docs/app/BEDROCK_TUNNEL.md). Any rejection closes the QUIC connection.
func (l *Listener) handle(ctx context.Context, conn *quic.Conn) {
	ip := netutil.HostOf(conn.RemoteAddr())

	// Per-IP concurrent cap on unauthenticated handshake windows (the #968
	// posture shared with the TCP tunnel listener): each pre-auth connection
	// holds relay resources for up to ~15 s (AcceptStream + readHello +
	// reject's bounded wait) and a parseable TunnelHello drives a
	// ValidateBedrockTunnel RPC to the API, so bound how many windows one
	// source IP can hold. Over the cap is a silent close. The slot covers
	// only the pre-auth window -- released once the handshake resolves either
	// way; an accepted tunnel's lifetime is governed by its authenticated
	// QUIC connection, not this cap.
	if !l.caps.Acquire(ip) {
		_ = conn.CloseWithError(0, "")
		return
	}
	tun, hello := l.handshake(ctx, conn)
	l.caps.Release(ip)
	if tun == nil {
		return
	}

	l.logger.Info("bedrock tunnel bound", "server_id", hello.GetServerId(), "bedrock_port", hello.GetBedrockPort())
	tun.run(ctx) // blocks until the connection closes or ctx is cancelled; unbinds on return
	l.unregister(hello.GetBedrockPort(), tun)
}

// handshake runs the pre-auth phase of one dial-out: accept the first
// bidirectional stream, read the TunnelHello, validate it against the API,
// bind the declared UDP port, and ack. On any failure it closes the QUIC
// connection and returns a nil Tunnel; on success the tunnel is bound, the
// accepting ack is sent, and the handshake stream is closed.
func (l *Listener) handshake(ctx context.Context, conn *quic.Conn) (*Tunnel, *bedrocktunnelv1.TunnelHello) {
	// Bound how long a connection can hold resources before opening the
	// handshake stream -- otherwise a peer that completes the QUIC/TLS
	// handshake and then never opens a stream is reclaimed only by the (much
	// longer) idle timeout.
	acceptCtx, acceptCancel := context.WithTimeout(ctx, handshakeDeadline)
	stream, err := conn.AcceptStream(acceptCtx)
	acceptCancel()
	if err != nil {
		l.logger.Debug("bedrock: no handshake stream", "remote", conn.RemoteAddr(), "error", err)
		_ = conn.CloseWithError(0, "handshake failed")
		return nil, nil
	}

	hello, err := readHello(stream)
	if err != nil {
		l.logger.Debug("bedrock: handshake read failed", "remote", conn.RemoteAddr(), "error", err)
		_ = conn.CloseWithError(0, "handshake failed")
		return nil, nil
	}

	validateCtx, validateCancel := context.WithTimeout(ctx, handshakeDeadline)
	valid, err := l.validator.ValidateBedrockTunnel(validateCtx, hello.GetServerId(), hello.GetBedrockPort(), hello.GetToken())
	validateCancel()
	if err != nil {
		l.logger.Warn("bedrock: ValidateBedrockTunnel RPC failed", "server_id", hello.GetServerId(), "error", err)
		l.reject(conn, stream, "validation unavailable")
		return nil, nil
	}
	if !valid {
		l.logger.Debug("bedrock: rejected", "server_id", hello.GetServerId(), "bedrock_port", hello.GetBedrockPort())
		l.reject(conn, stream, "invalid credential")
		return nil, nil
	}

	tun, err := l.bindOrTakeover(hello.GetBedrockPort(), conn, l.newIPCaps())
	if err != nil {
		l.logger.Warn("bedrock: bind failed", "bedrock_port", hello.GetBedrockPort(), "error", err)
		l.reject(conn, stream, "bind failed")
		return nil, nil
	}

	if err := writeAck(stream, true, ""); err != nil {
		l.logger.Debug("bedrock: ack write failed", "error", err)
		_ = stream.Close()
		tun.close("ack failed")
		l.unregister(hello.GetBedrockPort(), tun)
		return nil, nil
	}
	_ = stream.Close()
	return tun, hello
}

// bindOrTakeover binds bedrockPort to conn, displacing any tunnel already
// bound to that port instead of rejecting the dial (takeover semantics,
// issue #1565): a hello reaches here only after passing
// ValidateBedrockTunnel, so displacing a stale connection is not a new auth
// surface, and only the live tunnels index is consulted -- no server table.
// The whole displace-then-bind-then-register sequence (including bind's
// OS-level ListenPacket) runs under l.mu, so l.mu serializes any two
// concurrent hellos for the same port: they never reach the OS-level bind at
// once. The second blocks on l.mu, then when it proceeds it sees the first's
// tunnel as the current occupant and displaces it in turn -- one takeover
// after another, never both binding the port simultaneously.
func (l *Listener) bindOrTakeover(bedrockPort uint32, conn *quic.Conn, caps *ipcaps.IPCaps) (*Tunnel, error) {
	l.mu.Lock()
	defer l.mu.Unlock()

	displaced := false
	if old, ok := l.tunnels[bedrockPort]; ok {
		// close is idempotent (sync.Once), so this races harmlessly with
		// old's own natural teardown (idle timeout, graceful Worker close)
		// if that happens to fire concurrently -- whichever runs first does
		// the actual work. It also unblocks old's pumps (closed UDP socket,
		// closed QUIC connection), so no datagram is delivered to old after
		// this point, and its run() goroutine is guaranteed to return
		// (no leak) once its caller observes that. It must run before bind
		// below so the UDP port is free to rebind.
		old.close("displaced by new connection")
		displaced = true
	}

	tun, err := bind(bedrockPort, conn, caps, l.logger)
	if err != nil {
		delete(l.tunnels, bedrockPort)
		return nil, err
	}
	if displaced {
		// Log only after bind succeeds: on the error path above the caller
		// logs "bind failed", so claiming a successful takeover here would be
		// misleading (and doubly logged).
		l.logger.Info("bedrock: tunnel displaced by redial", "bedrock_port", bedrockPort)
	}
	l.tunnels[bedrockPort] = tun
	return tun, nil
}

// unregister removes tun from the port index if it is still the current
// occupant of bedrockPort -- a no-op if a later takeover has already
// replaced it. This compare-and-delete, combined with bindOrTakeover holding
// l.mu across its whole displace-then-bind-then-register sequence, guards
// the race between a tunnel's own natural teardown (calling this after
// run() returns) and a concurrent takeover of the same port: whichever of
// the two reaches l.mu second either finds its own entry already displaced
// (no-op here) or displaces the other first (old.close() above).
func (l *Listener) unregister(bedrockPort uint32, tun *Tunnel) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.tunnels[bedrockPort] == tun {
		delete(l.tunnels, bedrockPort)
	}
}

// reject answers the handshake stream with a rejecting ack (best-effort),
// waits for the Worker to finish reading it, and then closes the QUIC
// connection, per docs/app/BEDROCK_TUNNEL.md: "connection closed on
// rejection". The wait matters: closing the connection immediately after
// writing races the ack's delivery (a connection close can outrun buffered
// stream data that has not been acknowledged yet), so this blocks until the
// peer closes its side of the stream (the wire contract has the Worker close
// immediately after reading, proto/mcsd/bedrocktunnel/v1) or
// handshakeDeadline elapses, whichever comes first -- bounding how long a
// misbehaving or absent peer can delay the close.
func (l *Listener) reject(conn *quic.Conn, stream *quic.Stream, reason string) {
	_ = writeAck(stream, false, reason)
	_ = stream.Close()
	awaitStreamPeerClose(stream)
	_ = conn.CloseWithError(0, reason)
}

// awaitStreamPeerClose blocks until the peer closes its side of stream or
// handshakeDeadline elapses.
func awaitStreamPeerClose(stream *quic.Stream) {
	_ = stream.SetReadDeadline(time.Now().Add(handshakeDeadline))
	buf := make([]byte, 1)
	for {
		if _, err := stream.Read(buf); err != nil {
			return
		}
	}
}
