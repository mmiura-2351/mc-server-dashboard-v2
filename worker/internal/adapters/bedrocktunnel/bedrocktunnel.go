// Package bedrocktunnel forwards RakNet datagrams between the relay and Geyser over QUIC.
// Each server has one tunnel that retries dropped connections until closed or shutdown.
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

// ALPN must match relay/internal/bedrock.ALPN.
const ALPN = "mcsd-bedrock/1"

// geyserPort is the in-container port, independent of the public Bedrock port.
const geyserPort = "19132"

// handshakeTimeout covers the QUIC dial, TunnelHello send, and TunnelHelloAck read.
const handshakeTimeout = 5 * time.Second

// keepAlivePeriod stays below the relay's idle timeout and keeps the NAT mapping open.
const keepAlivePeriod = 5 * time.Second

// flowIDSize is the size, in bytes, of the big-endian flow id prefix on every
// QUIC DATAGRAM (docs/app/BEDROCK_TUNNEL.md Section 5).
const flowIDSize = 4

// Reject oversized handshake frames before allocating their payload.
const maxHandshakeMessageBytes = 256

// Reset backoff only after a stable connection so competing holders of one token cannot redial in a tight loop.
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
	// Token is reused on redial because it is valid for the tunnel's lifetime.
	Token string
	// CAPEM is the optional PEM CA bundle to verify the relay's QUIC
	// certificate; empty means system roots.
	CAPEM string
}

func (s Spec) equal(o Spec) bool {
	return s.ServerID == o.ServerID && s.RelayEndpoint == o.RelayEndpoint &&
		s.BedrockPort == o.BedrockPort && s.Token == o.Token && s.CAPEM == o.CAPEM
}

// Manager owns one tunnel per server; network I/O runs asynchronously on baseCtx.
type Manager struct {
	// Cancelling baseCtx closes every tunnel on Worker shutdown.
	baseCtx    context.Context
	gameBindIP string
	gameHost   func(serverID string) string
	logger     *slog.Logger

	backoff   session.Backoff
	randFloat func() float64

	// dialQUIC opens the client-side QUIC connection; injectable for tests.
	dialQUIC func(ctx context.Context, addr string, tlsConf *tls.Config, quicConf *quic.Config) (*quic.Conn, error)
	// dialUDP is injectable so tests can use a local listener.
	dialUDP func(ctx context.Context, addr string) (net.Conn, error)
	// afterFunc is injectable so backoff tests need no wall-clock waits.
	afterFunc func(time.Duration) <-chan time.Time
	// minStableDuration is the minimum pump lifetime before a connection is considered stable enough to reset the
	// backoff counter.
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

// New resolves Geyser through container DNS or gameBindIP; nil gameHost uses the latter.
// Cancelling baseCtx ends all tunnels.
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

// Open is idempotent for an unchanged spec and replaces a tunnel when its spec changes.
// Network setup runs asynchronously; malformed CAPEM is the only synchronous failure.
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

// Close cancels and forgets the tunnel; its run loop closes the connection and flow sockets.
func (m *Manager) Close(serverID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if t, ok := m.tunnels[serverID]; ok {
		t.cancel()
		delete(m.tunnels, serverID)
	}
}

// forget removes only this handle so an old tunnel cannot delete its replacement.
func (m *Manager) forget(serverID string, handle *tunnelHandle) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.tunnels[serverID] == handle {
		delete(m.tunnels, serverID)
	}
}

// run retries drops and handshake rejections, including stale relay bindings.
// Backoff also applies after short-lived successful handshakes to prevent token holders from dueling.
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
			// Short-lived connections keep escalating backoff; only a stable one resets it.
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

// dialAndHandshake bounds setup by handshakeTimeout and closes the connection on failure.
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

// pump discards its flow registry on disconnect because flow IDs are scoped to one connection.
func (m *Manager) pump(ctx context.Context, conn *quic.Conn, spec Spec) {
	target := net.JoinHostPort(m.dialHost(spec.ServerID), geyserPort)
	flows := newFlowRegistry(m.dialUDP, target, conn, m.logger, spec.ServerID)
	defer flows.closeAll()

	// Cancel the receive loop on either shutdown or connection loss; close the connection below on every exit.
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

	// Send CONNECTION_CLOSE on shutdown so the relay need not wait for its idle timeout.
	_ = conn.CloseWithError(0, "worker closing")
}

// dialHost must resolve the same container host as the TCP tunnel.
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

// tlsConfig uses custom roots when CAPEM is set, otherwise system roots.
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

// handshake uses the first bidirectional QUIC stream; a rejected ack fails setup.
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
