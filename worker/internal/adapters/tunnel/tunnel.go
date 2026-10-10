// Package tunnel splices one TLS relay dial-back to a Minecraft game connection per player join.
// Splices live until connection close or Worker shutdown; reconnect requires a new join.
package tunnel

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/javaproperties"
)

// handshakePreamble is the line the Worker sends before the token, identifying
// the tunnel protocol version (RELAY.md Section 5).
const handshakePreamble = "MCSD-TUNNEL/1\n"

// handshakeOK is the reply the relay sends once it has matched the token to a
// waiting player connection (RELAY.md Section 5).
const handshakeOK = "OK\n"

// Bound TLS dial, token send, and OK reply together so an unresponsive peer cannot hold the command lane.
const handshakeTimeout = 5 * time.Second

// defaultGamePort must match the container driver's fallback port.
const defaultGamePort = "25565"

// Dialer owns player splices on baseCtx so they outlive command completion and close on shutdown.
type Dialer struct {
	// baseCtx bounds every live splice: when it is cancelled (Worker shutdown)
	// the registry below closes all tunnel conns so no splice goroutine leaks.
	baseCtx context.Context
	// Dial loopback for an all-interfaces publication; otherwise use gameBindIP.
	gameBindIP string
	// Use container DNS on a configured network; the Worker's loopback cannot reach host publications.
	gameHost func(serverID string) string
	logger   *slog.Logger
	// tlsDial opens a TLS connection to addr verifying against cfg; injectable so
	// tests drive a fake relay listener without real certificate plumbing.
	tlsDial func(ctx context.Context, addr string, cfg *tls.Config) (net.Conn, error)
	// gameDial opens a plain TCP connection to addr; injectable for tests.
	gameDial func(ctx context.Context, addr string) (net.Conn, error)

	mu    sync.Mutex
	conns map[net.Conn]struct{}
}

// Spec holds relay credentials and working-set paths for one player dial-back.
type Spec struct {
	// ServerID is the target server, used only for logging.
	ServerID string
	// WorkingDir is the server's working-set root; the game port is read from its
	// server.properties (server-port).
	WorkingDir string
	// Endpoint is the relay tunnel endpoint to dial, host:port.
	Endpoint string
	// Token is the single-use session token presented after the TLS handshake.
	Token string
	// CAPEM is the optional PEM CA bundle to verify the relay's tunnel
	// certificate against; empty means system roots.
	CAPEM string
}

// New resolves game connections through container DNS or gameBindIP; nil gameHost uses the latter.
// Cancelling baseCtx ends all splices.
func New(baseCtx context.Context, gameBindIP string, gameHost func(serverID string) string, logger *slog.Logger) *Dialer {
	if logger == nil {
		logger = slog.Default()
	}
	if gameHost == nil {
		gameHost = func(string) string { return "" }
	}
	d := &Dialer{
		baseCtx:    baseCtx,
		gameBindIP: gameBindIP,
		gameHost:   gameHost,
		logger:     logger,
		conns:      map[net.Conn]struct{}{},
	}
	d.tlsDial = func(ctx context.Context, addr string, cfg *tls.Config) (net.Conn, error) {
		dialer := &tls.Dialer{Config: cfg}
		return dialer.DialContext(ctx, "tcp", addr)
	}
	d.gameDial = func(ctx context.Context, addr string) (net.Conn, error) {
		var dialer net.Dialer
		return dialer.DialContext(ctx, "tcp", addr)
	}
	// Closing all tunnel conns on Worker shutdown unblocks both splice copies so
	// their goroutines exit; the per-conn cleanup then deregisters them.
	go func() {
		<-baseCtx.Done()
		d.closeAll()
	}()
	return d
}

// Dial returns after setup; ctx bounds setup while baseCtx owns the established splice.
func (d *Dialer) Dial(ctx context.Context, spec Spec) error {
	tlsCfg, err := d.tlsConfig(spec.CAPEM)
	if err != nil {
		return err
	}

	// Bound the whole handshake (TLS dial + token + OK) by handshakeTimeout, or
	// the caller's deadline if it is sooner.
	setupCtx, cancel := context.WithTimeout(ctx, handshakeTimeout)
	defer cancel()

	relayConn, err := d.tlsDial(setupCtx, spec.Endpoint, tlsCfg)
	if err != nil {
		return fmt.Errorf("tunnel: dial relay %q: %w", spec.Endpoint, err)
	}
	if err := handshake(setupCtx, relayConn, spec.Token); err != nil {
		_ = relayConn.Close()
		return err
	}

	port, err := gamePort(spec.WorkingDir)
	if err != nil {
		_ = relayConn.Close()
		return err
	}
	gameAddr := net.JoinHostPort(d.dialHost(spec.ServerID), port)
	// Keep the game dial inside the setup deadline so a blackholed address cannot stall the lane.
	gameConn, err := d.gameDial(setupCtx, gameAddr)
	if err != nil {
		_ = relayConn.Close()
		return fmt.Errorf("tunnel: dial game port %q: %w", gameAddr, err)
	}

	d.register(relayConn, gameConn)
	d.logger.Info("tunnel established", "server_id", spec.ServerID, "endpoint", spec.Endpoint)
	go d.splice(relayConn, gameConn, spec.ServerID)
	return nil
}

// tlsConfig builds the relay-dial TLS config: a custom root pool when caPEM is
// non-empty, otherwise system roots (a public CA). RELAY.md Section 5.
func (d *Dialer) tlsConfig(caPEM string) (*tls.Config, error) {
	if caPEM == "" {
		return &tls.Config{MinVersion: tls.VersionTLS13}, nil
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM([]byte(caPEM)) {
		return nil, fmt.Errorf("tunnel: tls_ca_pem contained no usable certificate")
	}
	return &tls.Config{MinVersion: tls.VersionTLS13, RootCAs: pool}, nil
}

// Use container DNS on a configured network, otherwise the published host IP.
// Unset and all-interfaces addresses resolve to loopback.
func (d *Dialer) dialHost(serverID string) string {
	if host := d.gameHost(serverID); host != "" {
		return host
	}
	switch d.gameBindIP {
	case "", "0.0.0.0", "::", "[::]":
		return "127.0.0.1"
	}
	return d.gameBindIP
}

// Read the OK reply without buffering ahead: consuming player bytes here would corrupt the splice.
func handshake(ctx context.Context, conn net.Conn, token string) error {
	if token == "" || strings.ContainsAny(token, "\n\r") {
		return fmt.Errorf("tunnel: invalid tunnel token")
	}
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}
	if _, err := io.WriteString(conn, handshakePreamble+token+"\n"); err != nil {
		return fmt.Errorf("tunnel: send handshake: %w", err)
	}
	reply, err := readLine(conn, len(handshakeOK))
	if err != nil {
		return fmt.Errorf("tunnel: read handshake reply: %w", err)
	}
	if reply != handshakeOK {
		return fmt.Errorf("tunnel: relay refused handshake (reply %q)", reply)
	}
	// Clear the setup deadline; established splices rely on protocol keepalives and peer close.
	_ = conn.SetDeadline(time.Time{})
	return nil
}

// readLine stops exactly at newline so player bytes following OK remain unread.
// limit bounds a peer that never sends a newline.
func readLine(conn net.Conn, limit int) (string, error) {
	buf := make([]byte, 0, limit)
	one := make([]byte, 1)
	for len(buf) < limit {
		if _, err := io.ReadFull(conn, one); err != nil {
			return "", err
		}
		buf = append(buf, one[0])
		if one[0] == '\n' {
			break
		}
	}
	return string(buf), nil
}

// splice propagates half-closes so the reverse direction can drain before both connections close.
func (d *Dialer) splice(relayConn, gameConn net.Conn, serverID string) {
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); copyHalf(gameConn, relayConn) }()
	go func() { defer wg.Done(); copyHalf(relayConn, gameConn) }()
	wg.Wait()

	// Half-close leaves read descriptors open; release both connections after the copies finish.
	_ = relayConn.Close()
	_ = gameConn.Close()
	d.deregister(relayConn, gameConn)
	d.logger.Debug("tunnel closed", "server_id", serverID)
}

// copyHalf preserves reverse draining on EOF and closes both ends on error to unblock the peer copy.
func copyHalf(dst, src net.Conn) {
	_, err := io.Copy(dst, src)
	if err != nil {
		// A read/write failure tears the whole session down: close both ends so the
		// reverse copy unblocks too.
		_ = src.Close()
		_ = dst.Close()
		return
	}
	// Clean EOF: half-close dst's write side so the peer sees the stream end but
	// the reverse direction can still finish.
	if cw, ok := dst.(interface{ CloseWrite() error }); ok {
		_ = cw.CloseWrite()
		return
	}
	_ = dst.Close()
}

// Track live connections for shutdown; close late registrations immediately after baseCtx cancellation.
func (d *Dialer) register(conns ...net.Conn) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.baseCtx.Err() != nil {
		for _, c := range conns {
			_ = c.Close()
		}
		return
	}
	for _, c := range conns {
		d.conns[c] = struct{}{}
	}
}

func (d *Dialer) deregister(conns ...net.Conn) {
	d.mu.Lock()
	defer d.mu.Unlock()
	for _, c := range conns {
		delete(d.conns, c)
	}
}

// closeAll closes every live tunnel conn so the splice goroutines unblock and
// exit; it runs once on baseCtx cancellation (Worker shutdown).
func (d *Dialer) closeAll() {
	d.mu.Lock()
	conns := make([]net.Conn, 0, len(d.conns))
	for c := range d.conns {
		conns = append(conns, c)
	}
	d.conns = map[net.Conn]struct{}{}
	d.mu.Unlock()
	for _, c := range conns {
		_ = c.Close()
	}
}

// gamePort must match the container driver's port resolution.
// Only an absent file or key uses the default; unreadable files fail to avoid dialing the relay itself.
func gamePort(workingDir string) (string, error) {
	props, err := readProperties(filepath.Join(workingDir, "server.properties"))
	if err != nil {
		return "", err
	}
	if port := props["server-port"]; port != "" {
		return port, nil
	}
	return defaultGamePort, nil
}

// readProperties uses Java properties grammar; an absent file yields defaults, other read errors fail.
func readProperties(path string) (map[string]string, error) {
	data, err := os.ReadFile(path) //nolint:gosec // path is the server's own working dir, not user-controlled.
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return map[string]string{}, nil
		}
		return nil, fmt.Errorf("tunnel: read %s: %w", path, err)
	}
	return javaproperties.Parse(data), nil
}
