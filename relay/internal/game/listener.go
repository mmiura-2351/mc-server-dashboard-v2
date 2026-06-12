// Package game implements the relay's public player listener: the hardened
// Minecraft handshake parse, hostname → slug routing, the status-ping path
// (cache + synthesized responses) and the login path (resolve, wait for the
// Worker dial-back, replay, splice). See docs/app/RELAY.md Sections 3, 4, 7,
// and 11.
package game

import (
	"bufio"
	"context"
	"log/slog"
	"net"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/mc"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/splice"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/tunnel"
)

// preRouteDeadline bounds reading the handshake (and Login Start) before a
// routing decision (RELAY.md Section 7).
const preRouteDeadline = 5 * time.Second

// dialBackTimeout bounds how long the relay waits for the Worker's tunnel
// dial-back after a TUNNEL decision (RELAY.md Section 4).
const dialBackTimeout = 10 * time.Second

// Resolver is the API surface the listener needs. Narrowed to an interface so
// tests inject a fake.
type Resolver interface {
	ResolveJoin(ctx context.Context, slug, playerIP string, intent apiclient.Intent) (apiclient.ResolveResult, error)
	BaseDomain() string
}

// SessionRecorder records accepted login sessions (RELAY.md Section 8).
type SessionRecorder interface {
	Start(serverID, slug, playerIP, username, playerUUID string) string
	End(id string)
}

// Listener is the public game listener.
type Listener struct {
	ln       net.Listener
	resolver Resolver
	tokens   *tunnel.TokenTable
	cache    *StatusCache
	caps     *IPCaps
	sessions SessionRecorder
	logger   *slog.Logger
}

// NewListener binds the game listener on addr.
func NewListener(addr string, resolver Resolver, tokens *tunnel.TokenTable, cache *StatusCache, caps *IPCaps, sessions SessionRecorder, logger *slog.Logger) (*Listener, error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, err
	}
	return &Listener{
		ln:       ln,
		resolver: resolver,
		tokens:   tokens,
		cache:    cache,
		caps:     caps,
		sessions: sessions,
		logger:   logger,
	}, nil
}

// Addr returns the listener's bound address.
func (l *Listener) Addr() net.Addr { return l.ln.Addr() }

// Serve accepts player connections until ctx is cancelled or the listener
// closes.
func (l *Listener) Serve(ctx context.Context) error {
	go func() {
		<-ctx.Done()
		_ = l.ln.Close()
	}()
	for {
		conn, err := l.ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		go l.handle(ctx, conn)
	}
}

// handle drives one player connection from handshake to splice or drop.
func (l *Listener) handle(ctx context.Context, conn net.Conn) {
	ip := hostOf(conn.RemoteAddr())

	if !l.caps.Acquire(ip) {
		_ = conn.Close()
		return
	}
	defer l.caps.Release(ip)

	// Parse the handshake under the pre-route caps. Anything malformed is dropped
	// silently (RELAY.md Section 7).
	_ = conn.SetReadDeadline(time.Now().Add(preRouteDeadline))
	r := bufio.NewReaderSize(conn, mc.MaxPreRouteBytes)
	hs, err := mc.ReadHandshake(r)
	if err != nil {
		l.logger.Debug("handshake parse failed; dropping", "ip", ip, "error", err)
		_ = conn.Close()
		return
	}

	slug, ok := MatchSlug(hs.ServerAddress, l.resolver.BaseDomain())
	if !ok {
		// Unknown hostname: silent drop, no protocol response (RELAY.md Section 3).
		_ = conn.Close()
		return
	}

	if hs.IsLogin() {
		l.handleLogin(ctx, conn, r, hs, slug, ip)
		return
	}
	l.handleStatus(ctx, conn, r, hs, slug, ip)
}

// handleStatus serves the status-ping path (RELAY.md Section 7).
func (l *Listener) handleStatus(ctx context.Context, conn net.Conn, r *bufio.Reader, hs mc.Handshake, slug, ip string) {
	defer func() { _ = conn.Close() }()

	// The client sends an (empty) Status Request next; read and discard it. We
	// answer from the cache or a synthesized/forwarded response regardless.
	if _, _, err := readStatusRequest(r); err != nil {
		return
	}

	statusJSON, ok := l.cache.Get(slug)
	if !ok {
		statusJSON = l.resolveStatus(ctx, hs, slug, ip)
		if statusJSON == "" {
			return
		}
	}

	if err := writePacket(conn, mc.StatusResponsePacket(statusJSON)); err != nil {
		return
	}
	// Ping → Pong: echo the client's payload (RELAY.md Section 7).
	payload, err := mc.ReadPing(r)
	if err != nil {
		return
	}
	_ = writePacket(conn, mc.PongPacket(payload))
}

// resolveStatus handles a status-cache miss: resolve the slug and either fetch
// the real status through a tunnel (running), synthesize a stopped response, or
// answer from the unavailable fallback. It returns the status JSON to send, or
// "" if the connection should be dropped (NOT_FOUND).
func (l *Listener) resolveStatus(ctx context.Context, hs mc.Handshake, slug, ip string) string {
	res, err := l.resolver.ResolveJoin(ctx, slug, ip, apiclient.IntentStatus)
	if err != nil {
		// API unreachable: no cache entry (we are here on a miss), so synthesize an
		// "unavailable" response (RELAY.md Section 10).
		l.logger.Debug("status resolve failed; answering unavailable", "slug", slug, "error", err)
		return mc.SynthesizedStatus(mc.UnavailableMOTD)
	}

	switch res.Decision {
	case apiclient.DecisionTunnel:
		statusJSON, ok := l.fetchStatusThroughTunnel(ctx, hs, res.Token)
		if !ok {
			// Could not complete the live status exchange; fall back to unavailable
			// rather than dropping, so the player still sees the slug exists.
			return mc.SynthesizedStatus(mc.UnavailableMOTD)
		}
		l.cache.Put(slug, statusJSON)
		return statusJSON
	case apiclient.DecisionStopped:
		return mc.SynthesizedStatus(mc.StoppedMOTD(res.DisplayName))
	default:
		// NOT_FOUND or unknown: drop silently.
		return ""
	}
}

// fetchStatusThroughTunnel waits for the Worker's dial-back, replays the
// buffered handshake + Status Request, reads the server's Status Response, and
// returns its JSON. The tunnel connection is closed afterwards (status is a
// one-shot exchange — RELAY.md Section 7).
func (l *Listener) fetchStatusThroughTunnel(ctx context.Context, hs mc.Handshake, token string) (string, bool) {
	tconn, ok := l.awaitTunnel(ctx, token)
	if !ok {
		return "", false
	}
	defer func() { _ = tconn.Close() }()

	if err := tunnel.ConfirmAndAttach(tconn); err != nil {
		return "", false
	}
	// Replay the buffered handshake, then send a fresh Status Request, then read
	// the Status Response.
	if _, err := tconn.Write(hs.Raw); err != nil {
		return "", false
	}
	if _, err := tconn.Write(mc.StatusRequestPacket()); err != nil {
		return "", false
	}
	_ = tconn.SetReadDeadline(time.Now().Add(preRouteDeadline))
	tr := bufio.NewReaderSize(tconn, mc.MaxPreRouteBytes)
	statusJSON, err := mc.ReadStatusResponse(tr)
	if err != nil {
		return "", false
	}
	return statusJSON, true
}

// handleLogin serves the login path (RELAY.md Section 4).
func (l *Listener) handleLogin(ctx context.Context, conn net.Conn, r *bufio.Reader, hs mc.Handshake, slug, ip string) {
	// Per-IP join-rate cap applies to login attempts only (RELAY.md Section 11).
	if !l.caps.AllowJoin(ip) {
		_ = conn.Close()
		return
	}

	// Peek the Login Start best-effort; an unparseable packet still splices with a
	// null identity (RELAY.md Section 7).
	login, err := mc.ReadLoginStart(r, hs.ProtocolVersion)
	if err != nil {
		_ = conn.Close()
		return
	}
	// Clear the pre-route deadline before the splice (RELAY.md Section 5: no idle
	// timeout on spliced sessions).
	_ = conn.SetReadDeadline(time.Time{})

	res, err := l.resolver.ResolveJoin(ctx, slug, ip, apiclient.IntentLogin)
	if err != nil {
		l.disconnect(conn, "Dashboard unavailable — please try again shortly.")
		return
	}

	switch res.Decision {
	case apiclient.DecisionTunnel:
		l.spliceLogin(ctx, conn, hs, login, slug, ip, res.Token)
	case apiclient.DecisionStopped:
		l.disconnect(conn, mc.StoppedMOTD(res.DisplayName))
	default:
		// NOT_FOUND or unknown: drop silently.
		_ = conn.Close()
	}
}

// spliceLogin completes a TUNNEL login: wait for the dial-back, replay the
// buffered bytes, record the session, and splice. A worker that never dials
// back yields a Login Disconnect (RELAY.md Section 4).
func (l *Listener) spliceLogin(ctx context.Context, conn net.Conn, hs mc.Handshake, login mc.LoginStart, slug, ip, token string) {
	tconn, ok := l.awaitTunnel(ctx, token)
	if !ok {
		l.disconnect(conn, "Could not reach the server — please try again shortly.")
		return
	}
	if err := tunnel.ConfirmAndAttach(tconn); err != nil {
		_ = conn.Close()
		return
	}
	// Replay the buffered handshake + Login Start so the server sees a pristine
	// client byte stream (RELAY.md Section 4).
	if _, err := tconn.Write(hs.Raw); err != nil {
		_ = tconn.Close()
		_ = conn.Close()
		return
	}
	if _, err := tconn.Write(login.Raw); err != nil {
		_ = tconn.Close()
		_ = conn.Close()
		return
	}

	// serverID is not yet known to the relay (the API holds the slug→server
	// mapping). The session is keyed by the relay-minted id; the API resolves the
	// server from the slug on its side. Pass the slug as the server reference.
	sessionID := l.sessions.Start(slug, slug, ip, login.Name, login.UUID)
	defer l.sessions.End(sessionID)

	splice.Splice(conn, tconn)
}

// awaitTunnel registers a waiter for token and blocks until the Worker dials
// back or the timeout elapses. On timeout it cancels the waiter and returns
// ok=false.
func (l *Listener) awaitTunnel(ctx context.Context, token string) (net.Conn, bool) {
	ch := l.tokens.Register(token)
	defer l.tokens.Cancel(token)

	timer := time.NewTimer(dialBackTimeout)
	defer timer.Stop()
	select {
	case tconn := <-ch:
		return tconn, true
	case <-timer.C:
		return nil, false
	case <-ctx.Done():
		return nil, false
	}
}

// disconnect sends a Login Disconnect with reason and closes the connection
// (RELAY.md Section 7).
func (l *Listener) disconnect(conn net.Conn, reason string) {
	_ = writePacket(conn, mc.LoginDisconnectPacket(reason))
	_ = conn.Close()
}

// hostOf extracts the IP (without port) from a remote address.
func hostOf(addr net.Addr) string {
	host, _, err := net.SplitHostPort(addr.String())
	if err != nil {
		return addr.String()
	}
	return host
}

// writePacket writes a fully-framed Minecraft packet to conn.
func writePacket(conn net.Conn, pkt []byte) error {
	_, err := conn.Write(pkt)
	return err
}

// readStatusRequest reads (and discards) the client's empty Status Request
// packet, returning its raw bytes for completeness.
func readStatusRequest(r *bufio.Reader) (id int32, raw []byte, err error) {
	return mc.ReadStatusRequest(r)
}
