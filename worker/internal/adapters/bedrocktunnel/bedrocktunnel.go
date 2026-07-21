// Package bedrocktunnel implements the Worker side of the Bedrock relay QUIC
// tunnel (docs/app/BEDROCK_TUNNEL.md, epic #1540, issue #1546): for one
// Bedrock-enabled server it dials the relay's Bedrock tunnel QUIC listener
// outbound, authenticates with the OpenBedrockTunnel command's token, and
// pumps RakNet datagrams both directions between the relay and the
// container's Geyser port (docker network, :19132/udp — the same target the
// TCP tunnel resolves via dialHost, worker/internal/adapters/tunnel/tunnel.go).
//
// One Manager serves the whole Worker; it owns at most one live-or-reconnecting
// tunnel per server. Open is idempotent for a repeated command carrying the
// same credential (docs/app/BEDROCK_TUNNEL.md Section 3: the token is valid for
// the tunnel's whole lifetime, so the Worker may see it again on a resync). A
// connection that drops while the tunnel is still open is redialed with
// backoff — including a handshake rejected during the relay's stale-bind
// window after an ungraceful prior disconnect (Section 3.1), which is
// retryable, not terminal. Close (or the Manager's base context ending, i.e.
// Worker shutdown) gracefully closes the QUIC connection and every per-flow
// local UDP socket.
package bedrocktunnel

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/binary"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"net"
	"sync"
	"time"

	"github.com/quic-go/quic-go"
	"google.golang.org/protobuf/proto"

	bedrocktunnelv1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/bedrocktunnel/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// ALPN is the QUIC application-layer protocol negotiated with the relay's
// Bedrock tunnel listener (docs/app/BEDROCK_TUNNEL.md Section 4); it must
// match relay/internal/bedrock.ALPN exactly.
const ALPN = "mcsd-bedrock/1"

// geyserPort is Geyser's constant in-container RakNet port
// (docs/app/BEDROCK_TUNNEL.md Section 2): every server's container listens
// here regardless of its public bedrock_port, since each server has its own
// container.
const geyserPort = "19132"

// handshakeTimeout bounds one dial-and-handshake attempt: the QUIC dial, the
// TunnelHello send, and the TunnelHelloAck read must complete within it
// (mirrors relay/internal/bedrock.handshakeDeadline and
// worker/internal/adapters/tunnel.handshakeTimeout).
const handshakeTimeout = 5 * time.Second

// keepAlivePeriod is the QUIC keepalive period the Worker enables on every
// tunnel connection — a binding obligation (docs/app/BEDROCK_TUNNEL.md
// Section 3): well under the relay's 15s idle timeout so a tunnel with no
// connected Bedrock players does not collapse, and it is what holds the
// Worker's NAT mapping open for the tunnel's lifetime (epic #1540).
const keepAlivePeriod = 5 * time.Second

// flowIDSize is the size, in bytes, of the big-endian flow id prefix on every
// QUIC DATAGRAM (docs/app/BEDROCK_TUNNEL.md Section 5).
const flowIDSize = 4

// maxHandshakeMessageBytes caps the single framed TunnelHelloAck the Worker
// reads; a relay that declares a larger frame is a protocol violation, not a
// message to buffer (mirrors relay/internal/bedrock.maxHandshakeMessageBytes).
const maxHandshakeMessageBytes = 256

// defaultMinStableDuration is the minimum time a connection must survive
// pumping before it is considered stable enough to reset the backoff counter.
// Without this, two Workers with the same token displace each other on every
// handshake, each connection succeeds briefly then drops, and the backoff
// stays at Delay(0) forever (issue #2153).
const defaultMinStableDuration = 10 * time.Second

// Spec is everything one OpenBedrockTunnel command needs to open (or redial) a
// tunnel (docs/app/BEDROCK_TUNNEL.md Section 3).
type Spec struct {
	// ServerID identifies the local server whose Geyser port this tunnel
	// forwards.
	ServerID string
	// RelayEndpoint is the relay's Bedrock tunnel QUIC listener, host:port.
	RelayEndpoint string
	// BedrockPort is the public UDP port the relay binds for this server.
	BedrockPort uint32
	// Token is the credential presented in TunnelHello; valid for the tunnel's
	// whole lifetime, so the same value is presented again on every redial
	// (docs/app/BEDROCK_TUNNEL.md Section 3).
	Token string
	// CAPEM is the optional PEM CA bundle to verify the relay's QUIC
	// certificate; empty means system roots.
	CAPEM string
}

// equal reports whether two specs describe the same tunnel, so a repeated Open
// with an unchanged spec can be treated as idempotent
// (docs/app/BEDROCK_TUNNEL.md Section 3).
func (s Spec) equal(o Spec) bool {
	return s.ServerID == o.ServerID && s.RelayEndpoint == o.RelayEndpoint &&
		s.BedrockPort == o.BedrockPort && s.Token == o.Token && s.CAPEM == o.CAPEM
}

// Manager owns at most one QUIC tunnel per server. Open starts (or, for an
// unchanged spec, idempotently confirms) it; Close tears it down. Every
// tunnel's dial, handshake, datagram pump, and reconnect-with-backoff run on
// their own goroutine off the Manager's baseCtx, so Open and Close never block
// on network I/O.
type Manager struct {
	// baseCtx bounds every tunnel's lifetime: cancelling it (Worker shutdown)
	// gracefully closes every open connection, mirroring
	// worker/internal/adapters/tunnel.Dialer's baseCtx.
	baseCtx    context.Context
	gameBindIP string
	gameHost   func(serverID string) string
	logger     *slog.Logger

	backoff   session.Backoff
	randFloat func() float64

	// dialQUIC opens the client-side QUIC connection; injectable for tests.
	dialQUIC func(ctx context.Context, addr string, tlsConf *tls.Config, quicConf *quic.Config) (*quic.Conn, error)
	// dialUDP opens the local UDP socket to the target container's Geyser
	// port for one flow; injectable so tests point flows at a fake local
	// listener instead of a real container.
	dialUDP func(ctx context.Context, addr string) (net.Conn, error)
	// afterFunc creates a timer channel for backoff delays; injectable so
	// tests verify delay behavior without wall-clock waits. Defaults to
	// time.After.
	afterFunc func(time.Duration) <-chan time.Time
	// minStableDuration is the minimum pump lifetime before a connection is
	// considered stable enough to reset the backoff counter (issue #2153).
	minStableDuration time.Duration

	mu      sync.Mutex
	tunnels map[string]*tunnelHandle
}

// tunnelHandle is one server's live-or-reconnecting tunnel entry in the
// Manager's registry.
type tunnelHandle struct {
	spec   Spec
	cancel context.CancelFunc
}

// New builds a Manager. baseCtx bounds every tunnel's lifetime. gameBindIP and
// gameHost resolve the per-server Geyser dial target exactly as the TCP
// tunnel's dialHost does (worker/internal/adapters/tunnel/tunnel.go): the
// container name over a user-defined network (gameHost), else the
// gameBindIP-derived loopback. A nil gameHost is treated as the no-network
// case (always loopback), mirroring tunnel.New.
func New(baseCtx context.Context, gameBindIP string, gameHost func(serverID string) string, logger *slog.Logger) *Manager {
	if logger == nil {
		logger = slog.Default()
	}
	if gameHost == nil {
		gameHost = func(string) string { return "" }
	}
	m := &Manager{
		baseCtx:    baseCtx,
		gameBindIP: gameBindIP,
		gameHost:   gameHost,
		logger:     logger,
		backoff:    session.DefaultBackoff,
		randFloat:  rand.Float64,
		tunnels:    map[string]*tunnelHandle{},
	}
	m.dialQUIC = func(ctx context.Context, addr string, tlsConf *tls.Config, quicConf *quic.Config) (*quic.Conn, error) {
		return quic.DialAddr(ctx, addr, tlsConf, quicConf)
	}
	m.dialUDP = func(ctx context.Context, addr string) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, "udp", addr)
	}
	m.afterFunc = time.After
	m.minStableDuration = defaultMinStableDuration
	return m
}

// Open starts spec's tunnel, or confirms an already-open one is current
// (idempotent for a repeated Open with the same spec — docs/app/BEDROCK_TUNNEL.md
// Section 3: neither tears down nor redials a healthy tunnel). A spec that
// differs from an already-open tunnel for the same server (e.g. a fresh token
// after a restart) supersedes it: the old connection is closed and a new one
// dialed. Open returns once the tunnel is registered — the dial, handshake,
// datagram pump, and any reconnect-with-backoff all run off the Manager's
// baseCtx, not this call. The only synchronous failure is a malformed
// spec.CAPEM.
func (m *Manager) Open(spec Spec) error {
	tlsCfg, err := tlsConfig(spec.CAPEM)
	if err != nil {
		return err
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	if existing, ok := m.tunnels[spec.ServerID]; ok {
		if existing.spec.equal(spec) {
			return nil
		}
		existing.cancel()
	}
	ctx, cancel := context.WithCancel(m.baseCtx)
	handle := &tunnelHandle{spec: spec, cancel: cancel}
	m.tunnels[spec.ServerID] = handle
	go m.run(ctx, handle, spec, tlsCfg)
	return nil
}

// Close tears down serverID's tunnel, if any (idempotent: a no-op when none is
// open). It only cancels the tunnel's context and forgets it; the graceful
// CONNECTION_CLOSE and per-flow socket teardown happen in the run loop's own
// cleanup, off the caller.
func (m *Manager) Close(serverID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if t, ok := m.tunnels[serverID]; ok {
		t.cancel()
		delete(m.tunnels, serverID)
	}
}

// forget removes handle from the registry, but only if it is still the
// current entry for its server id — an Open that supersedes handle already
// installed a new one under the same key, and that replacement must not be
// deleted out from under it by the superseded goroutine's own cleanup.
func (m *Manager) forget(serverID string, handle *tunnelHandle) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.tunnels[serverID] == handle {
		delete(m.tunnels, serverID)
	}
}

// run dials, handshakes, and pumps spec's tunnel until ctx is cancelled
// (Close, a superseding Open, or Worker shutdown), redialing with backoff
// whenever a connection drops or a handshake is rejected
// (docs/app/BEDROCK_TUNNEL.md Section 3.1: a redial can be rejected for up to
// ~15s while the relay's stale prior connection times out — backoff treats
// that as retryable, never terminal).
//
// Backoff is applied before every redial, including after a successful
// handshake whose connection then drops (mirroring session.Runner.Run) —
// without this, two Workers holding the same token duel at network speed,
// each displacing the other's connection on every handshake (issue #1988).
func (m *Manager) run(ctx context.Context, handle *tunnelHandle, spec Spec, tlsCfg *tls.Config) {
	defer m.forget(spec.ServerID, handle)

	attempt := 0
	for {
		if ctx.Err() != nil {
			return
		}
		conn, err := m.dialAndHandshake(ctx, spec, tlsCfg)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			m.logger.Warn("bedrock tunnel dial/handshake failed; retrying",
				"server_id", spec.ServerID, "error", err)
		} else {
			m.logger.Info("bedrock tunnel established", "server_id", spec.ServerID, "bedrock_port", spec.BedrockPort)
			connStart := time.Now()
			m.pump(ctx, conn, spec)
			// pump returned: either ctx was cancelled (checked below) or
			// the connection dropped — both fall through to the backoff.
			// Only reset the backoff when the connection survived long
			// enough to be considered stable; an instant displacement
			// (the duel scenario, issue #2153) keeps escalating.
			if time.Since(connStart) > m.minStableDuration {
				attempt = 0
			}
		}

		if ctx.Err() != nil {
			return
		}

		delay := m.backoff.Delay(attempt, m.randFloat())
		attempt++
		select {
		case <-ctx.Done():
			return
		case <-m.afterFunc(delay):
		}
	}
}

// dialAndHandshake opens one QUIC connection to spec.RelayEndpoint and runs
// the TunnelHello/TunnelHelloAck handshake, bounded by handshakeTimeout. On
// any failure (dial, handshake I/O, or an explicit rejection) it closes the
// connection (if one was established) and returns the error for the caller to
// treat as retryable.
func (m *Manager) dialAndHandshake(ctx context.Context, spec Spec, tlsCfg *tls.Config) (*quic.Conn, error) {
	dialCtx, cancel := context.WithTimeout(ctx, handshakeTimeout)
	defer cancel()

	quicCfg := &quic.Config{EnableDatagrams: true, KeepAlivePeriod: keepAlivePeriod}
	conn, err := m.dialQUIC(dialCtx, spec.RelayEndpoint, tlsCfg, quicCfg)
	if err != nil {
		return nil, fmt.Errorf("bedrocktunnel: dial relay %q: %w", spec.RelayEndpoint, err)
	}
	if err := handshake(dialCtx, conn, spec); err != nil {
		_ = conn.CloseWithError(0, "handshake failed")
		return nil, err
	}
	return conn, nil
}

// pump exchanges RakNet datagrams over conn until ctx is cancelled or the
// connection drops. Flow state (the per-flow local UDP sockets) is entirely
// local to this call and is discarded when it returns: a redial starts a
// fresh flow registry, since flow ids are connection-scoped
// (docs/app/BEDROCK_TUNNEL.md Section 5).
func (m *Manager) pump(ctx context.Context, conn *quic.Conn, spec Spec) {
	target := net.JoinHostPort(m.dialHost(spec.ServerID), geyserPort)
	flows := newFlowRegistry(m.dialUDP, target, conn, m.logger, spec.ServerID)
	defer flows.closeAll()

	// pumpCtx bounds the receive loop below: whichever ends first, the outer
	// ctx (Close, a superseding Open, or Worker shutdown) or the connection's
	// own Context() (the connection dropping on its own), cancels it. Both
	// derive from ctx, so a cancel here does not race a bare "case <-ctx.Done()"
	// against "case <-pumpCtx.Done()" the way a second, independent context
	// would — the watcher goroutine's only job is the cancel() call itself; the
	// unconditional CloseWithError below (not inside the goroutine) is what
	// makes graceful close deterministic, mirroring
	// relay/internal/bedrock/tunnel.go's run().
	pumpCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		select {
		case <-conn.Context().Done():
		case <-pumpCtx.Done():
		}
		cancel()
	}()

	for {
		data, err := conn.ReceiveDatagram(pumpCtx)
		if err != nil {
			break
		}
		if len(data) < flowIDSize {
			continue // malformed frame: drop.
		}
		id := binary.BigEndian.Uint32(data[:flowIDSize])
		payload := data[flowIDSize:]
		if err := flows.forward(pumpCtx, id, payload); err != nil {
			m.logger.Debug("bedrock tunnel: forward to container failed",
				"server_id", spec.ServerID, "flow_id", id, "error", err)
		}
	}

	// Close the connection unconditionally once the loop ends: harmless if the
	// peer already closed it (a natural drop), and what makes a Close- or
	// shutdown-triggered end visible to the relay immediately as
	// CONNECTION_CLOSE rather than waiting out its idle timeout
	// (docs/app/BEDROCK_TUNNEL.md Section 3: graceful close is binding).
	_ = conn.CloseWithError(0, "worker closing")
}

// dialHost picks the host to reach serverID's Geyser port at, mirroring
// worker/internal/adapters/tunnel.Dialer.dialHost exactly so the TCP tunnel
// and the Bedrock tunnel can never target different hosts for the same
// server's container.
func (m *Manager) dialHost(serverID string) string {
	if host := m.gameHost(serverID); host != "" {
		return host
	}
	switch m.gameBindIP {
	case "", "0.0.0.0", "::", "[::]":
		return "127.0.0.1"
	}
	return m.gameBindIP
}

// tlsConfig builds the relay-dial TLS config: ALPN mcsd-bedrock/1, and a
// custom root pool when caPEM is non-empty (otherwise system roots — a public
// CA), mirroring worker/internal/adapters/tunnel.Dialer.tlsConfig.
func tlsConfig(caPEM string) (*tls.Config, error) {
	cfg := &tls.Config{MinVersion: tls.VersionTLS13, NextProtos: []string{ALPN}}
	if caPEM == "" {
		return cfg, nil
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM([]byte(caPEM)) {
		return nil, fmt.Errorf("bedrocktunnel: tls_ca_pem contained no usable certificate")
	}
	cfg.RootCAs = pool
	return cfg, nil
}

// handshake runs the TunnelHello/TunnelHelloAck exchange on the first
// bidirectional QUIC stream (docs/app/BEDROCK_TUNNEL.md Section 4): open the
// stream, send TunnelHello, read TunnelHelloAck, then close the Worker's side
// of the stream. A rejecting ack (Accepted false) is reported as an error; the
// caller closes the connection.
func handshake(ctx context.Context, conn *quic.Conn, spec Spec) error {
	stream, err := conn.OpenStreamSync(ctx)
	if err != nil {
		return fmt.Errorf("bedrocktunnel: open handshake stream: %w", err)
	}
	defer func() { _ = stream.Close() }()
	if deadline, ok := ctx.Deadline(); ok {
		_ = stream.SetDeadline(deadline)
	}

	hello, err := proto.Marshal(&bedrocktunnelv1.TunnelHello{
		ServerId:    spec.ServerID,
		BedrockPort: spec.BedrockPort,
		Token:       spec.Token,
	})
	if err != nil {
		return fmt.Errorf("bedrocktunnel: marshal TunnelHello: %w", err)
	}
	if err := writeFramed(stream, hello); err != nil {
		return fmt.Errorf("bedrocktunnel: send TunnelHello: %w", err)
	}

	ackData, err := readFramed(stream, maxHandshakeMessageBytes)
	if err != nil {
		return fmt.Errorf("bedrocktunnel: read TunnelHelloAck: %w", err)
	}
	var ack bedrocktunnelv1.TunnelHelloAck
	if err := proto.Unmarshal(ackData, &ack); err != nil {
		return fmt.Errorf("bedrocktunnel: unmarshal TunnelHelloAck: %w", err)
	}
	if !ack.GetAccepted() {
		return fmt.Errorf("bedrocktunnel: relay rejected tunnel: %s", ack.GetRejectReason())
	}
	return nil
}
