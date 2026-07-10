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
	"sync"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/mc"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/netutil"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/splice"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/tunnel"
)

// preRouteDeadline bounds reading the handshake (and Login Start) before a
// routing decision (RELAY.md Section 7).
const preRouteDeadline = 5 * time.Second

// dialBackTimeout bounds how long the relay waits for the Worker's tunnel
// dial-back after a TUNNEL decision (RELAY.md Section 4).
const dialBackTimeout = 10 * time.Second

// resolveJoinTimeout bounds each ResolveJoin RPC so a black-holed API
// connection cannot wedge the player goroutine indefinitely (the conn read
// deadline is already cleared on the login path before this call).
const resolveJoinTimeout = 5 * time.Second

// disconnectWriteTimeout bounds the Login Disconnect write so a stalled client
// cannot pin the goroutine (issue #971).
const disconnectWriteTimeout = 10 * time.Second

// statusFlightWaitTimeout bounds how long a coalesced status waiter blocks on
// another connection's in-flight exchange (issue #1720). It is the leader's
// bounded worst case — ResolveJoin + dial-back wait + status read — so a
// waiter never gives up on a leader that can still succeed, yet escapes one
// wedged past its own deadlines.
const statusFlightWaitTimeout = resolveJoinTimeout + dialBackTimeout + preRouteDeadline

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
	caps     *ipcaps.IPCaps
	sessions SessionRecorder
	logger   *slog.Logger

	// flights coalesces concurrent status-cache misses per slug (issue #1720).
	flights statusFlights

	// inflight tracks handle goroutines so Drain can wait for them on shutdown.
	inflight sync.WaitGroup
}

// NewListener binds the game listener on addr.
func NewListener(addr string, resolver Resolver, tokens *tunnel.TokenTable, cache *StatusCache, caps *ipcaps.IPCaps, sessions SessionRecorder, logger *slog.Logger) (*Listener, error) {
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

// Drain blocks until all in-flight handle goroutines finish or the timeout
// elapses. Call after Serve returns to let active splices complete before
// shutting down downstream services (e.g. the session reporter). It returns
// true if all goroutines drained within the deadline, false on timeout.
func (l *Listener) Drain(timeout time.Duration) bool {
	done := make(chan struct{})
	go func() {
		l.inflight.Wait()
		close(done)
	}()
	select {
	case <-done:
		return true
	case <-time.After(timeout):
		return false
	}
}

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
		l.inflight.Add(1)
		go func() {
			defer l.inflight.Done()
			l.handle(ctx, conn)
		}()
	}
}

// handle drives one player connection from handshake to splice or drop.
func (l *Listener) handle(ctx context.Context, conn net.Conn) {
	ip := netutil.HostOf(conn.RemoteAddr())

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

	// The client sends an (empty) Status Request (id 0x00) next; read and discard
	// it. Reject anything that is not a Status Request rather than answering a
	// stray packet (RELAY.md Section 7).
	if id, _, err := readStatusRequest(r); err != nil || id != 0x00 {
		return
	}

	statusJSON, ok := l.cache.Get(slug)
	if !ok {
		// Per-IP join-rate cap: cache misses trigger a ResolveJoin RPC, so
		// they share the login path's rate budget (RELAY.md Section 11).
		if !l.caps.AllowJoin(ip) {
			return
		}
		statusJSON = l.coalesceStatus(ctx, hs, slug, ip)
		if statusJSON == "" {
			return
		}
	}

	// Refresh the deadline: a cache miss can spend most of the accept deadline on
	// the resolve + dial-back status exchange, so the status response and the
	// Ping read must not inherit a nearly-expired deadline (RELAY.md Section 7).
	_ = conn.SetDeadline(time.Now().Add(preRouteDeadline))
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

// coalesceStatus runs resolveStatus behind a per-slug flight (issue #1720):
// concurrent misses for the same slug share one live exchange instead of each
// spawning a ResolveJoin + Worker dial-back. The leader's result — including
// its failure fallback or NOT_FOUND drop — is handed to every waiter; nothing
// beyond resolveStatus's own success-path Put reaches the cache.
func (l *Listener) coalesceStatus(ctx context.Context, hs mc.Handshake, slug, ip string) string {
	f, leader := l.flights.join(slug)
	if !leader {
		return l.waitStatusFlight(ctx, f, statusFlightWaitTimeout)
	}
	// Another flight may have completed and cached between this connection's
	// cache miss and its join; re-check before paying for a fresh exchange.
	statusJSON, ok := l.cache.Get(slug)
	if !ok {
		statusJSON = l.resolveStatus(ctx, hs, slug, ip)
	}
	l.flights.finish(slug, f, statusJSON)
	return statusJSON
}

// waitStatusFlight blocks until the flight's leader publishes its result, the
// timeout elapses, or ctx is cancelled. A timed-out waiter answers the
// "unavailable" fallback for its own client only — the leader keeps fetching
// and its result stays intact for the remaining waiters. Shutdown drops.
func (l *Listener) waitStatusFlight(ctx context.Context, f *statusFlight, timeout time.Duration) string {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-f.done:
		return f.json
	case <-timer.C:
		return mc.SynthesizedStatus(mc.UnavailableMOTD)
	case <-ctx.Done():
		return ""
	}
}

// resolveStatus handles a status-cache miss: resolve the slug and either fetch
// the real status through a tunnel (running), synthesize a stopped response, or
// answer from the unavailable fallback. It returns the status JSON to send, or
// "" if the connection should be dropped (NOT_FOUND). Callers must gate on
// AllowJoin before calling (the resolve issues a ResolveJoin RPC).
func (l *Listener) resolveStatus(ctx context.Context, hs mc.Handshake, slug, ip string) string {
	rctx, cancel := context.WithTimeout(ctx, resolveJoinTimeout)
	defer cancel()
	res, err := l.resolver.ResolveJoin(rctx, slug, ip, apiclient.IntentStatus)
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
	// Per-IP join-rate cap (RELAY.md Section 11).
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

	rctx, cancel := context.WithTimeout(ctx, resolveJoinTimeout)
	res, err := l.resolver.ResolveJoin(rctx, slug, ip, apiclient.IntentLogin)
	cancel()
	if err != nil {
		l.disconnect(conn, "Dashboard unavailable — please try again shortly.")
		return
	}

	switch res.Decision {
	case apiclient.DecisionTunnel:
		l.spliceLogin(ctx, conn, r, hs, login, res.ServerID, slug, ip, res.Token)
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
func (l *Listener) spliceLogin(ctx context.Context, conn net.Conn, r *bufio.Reader, hs mc.Handshake, login mc.LoginStart, serverID, slug, ip, token string) {
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
	// Forward any client bytes already pulled into the bufio buffer past the Login
	// Start (a client that pipelined more before the splice). Reading from conn
	// directly in the splice would strand them. Vanilla clients wait for the
	// server's Encryption Request so this is usually empty, but a correct relay
	// must not drop pipelined bytes.
	if n := r.Buffered(); n > 0 {
		buffered, _ := r.Peek(n)
		if _, err := tconn.Write(buffered); err != nil {
			_ = tconn.Close()
			_ = conn.Close()
			return
		}
	}

	// serverID is the API's server identifier from the ResolveJoin decision; the
	// slug is recorded separately as the historical hostname label (RELAY.md
	// Section 8).
	sessionID := l.sessions.Start(serverID, slug, ip, login.Name, login.UUID)
	defer l.sessions.End(sessionID)

	splice.Splice(conn, tconn)
}

// awaitTunnel registers a waiter for token and blocks until the Worker dials
// back or the timeout elapses. On timeout it cancels the waiter and returns
// ok=false.
func (l *Listener) awaitTunnel(ctx context.Context, token string) (net.Conn, bool) {
	ch := l.tokens.Register(token)

	timer := time.NewTimer(dialBackTimeout)
	defer timer.Stop()
	select {
	case tconn := <-ch:
		l.tokens.Cancel(token)
		if tconn == nil {
			return nil, false
		}
		return tconn, true
	case <-timer.C:
	case <-ctx.Done():
	}

	// Timed out (or shutting down). If Cancel finds the waiter gone, a concurrent
	// Deliver already won the race and a connection is en route on ch; drain and
	// close it so the Worker's dial-back is not leaked.
	if !l.tokens.Cancel(token) {
		if tconn := <-ch; tconn != nil {
			_ = tconn.Close()
		}
	}
	return nil, false
}

// disconnect sends a Login Disconnect with reason and closes the connection
// (RELAY.md Section 7). A short write deadline bounds the write so a stalled
// client cannot pin the goroutine (issue #971).
func (l *Listener) disconnect(conn net.Conn, reason string) {
	_ = conn.SetWriteDeadline(time.Now().Add(disconnectWriteTimeout))
	_ = writePacket(conn, mc.LoginDisconnectPacket(reason))
	_ = conn.Close()
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
