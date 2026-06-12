// Package tunnel implements the Worker side of the relay dial-back tunnel
// (RELAY.md Section 5): for one player session it dials the relay's tunnel
// listener over TLS, presents a single-use token, dials the local server's
// loopback game port, and splices the two connections verbatim so the player's
// byte stream reaches the Minecraft server end to end.
//
// There is no persistent Worker↔relay connection: one dial-back per join, owned
// by the Worker process. The splice goroutines run on the Dialer's base context
// and are torn down when the Worker shuts down (close all tunnel conns); the
// relay/client recovers by rejoining (no reconnect here).
package tunnel

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// handshakePreamble is the line the Worker sends before the token, identifying
// the tunnel protocol version (RELAY.md Section 5).
const handshakePreamble = "MCSD-TUNNEL/1\n"

// handshakeOK is the reply the relay sends once it has matched the token to a
// waiting player connection (RELAY.md Section 5).
const handshakeOK = "OK\n"

// handshakeTimeout bounds the whole dial-back handshake: TLS dial, token send,
// and the "OK\n" read must complete within it or the dial fails (RELAY.md
// Section 5 — the relay drops a tunnel that sends nothing within 5 s; the Worker
// applies the symmetric bound to its own attempt).
const handshakeTimeout = 5 * time.Second

// defaultGamePort is the Minecraft default game port, used when the server's
// server.properties does not set server-port. It mirrors the container driver's
// default (the published loopback port the relay path dials).
const defaultGamePort = "25565"

// Dialer dials the relay tunnel and splices it to a local server's game port.
// One Dialer serves the whole Worker; each Dial handles one player session. The
// splice goroutines live on baseCtx so they outlive the per-command result and
// are torn down on Worker shutdown (RELAY.md Section 5).
type Dialer struct {
	// baseCtx bounds every live splice: when it is cancelled (Worker shutdown)
	// the registry below closes all tunnel conns so no splice goroutine leaks.
	baseCtx context.Context
	// gameBindIP is driver.container.game_bind_ip: the host interface the game
	// port is published on. The Worker dials the loopback (127.0.0.1) when it is
	// 0.0.0.0 (loopback still reaches an all-interfaces bind) and the configured
	// IP otherwise (RELAY.md Section 5).
	gameBindIP string
	logger     *slog.Logger
	// tlsDial opens a TLS connection to addr verifying against cfg; injectable so
	// tests drive a fake relay listener without real certificate plumbing.
	tlsDial func(ctx context.Context, addr string, cfg *tls.Config) (net.Conn, error)
	// gameDial opens a plain TCP connection to addr; injectable for tests.
	gameDial func(ctx context.Context, addr string) (net.Conn, error)

	mu    sync.Mutex
	conns map[net.Conn]struct{}
}

// Spec is everything one TunnelDial needs (RELAY.md Section 5): the local
// server's working dir (for its game port), and the relay endpoint, token, and
// optional CA the Worker dials back to.
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

// New builds a Dialer. baseCtx bounds every splice (cancel it to tear down all
// live tunnels on Worker shutdown); gameBindIP is driver.container.game_bind_ip.
func New(baseCtx context.Context, gameBindIP string, logger *slog.Logger) *Dialer {
	if logger == nil {
		logger = slog.Default()
	}
	d := &Dialer{
		baseCtx:    baseCtx,
		gameBindIP: gameBindIP,
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

// Dial dials the relay, completes the token handshake, dials the local game
// port, and starts splicing. It returns once the splice is established (or with
// an error on any dial/handshake failure); the splice itself runs on the
// Dialer's base context, off the caller's command context, so it outlives the
// CommandResult (RELAY.md Section 5). ctx bounds only the synchronous setup.
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

	gameAddr := net.JoinHostPort(d.dialHost(), gamePort(spec.WorkingDir))
	gameConn, err := d.gameDial(ctx, gameAddr)
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
		return &tls.Config{MinVersion: tls.VersionTLS12}, nil
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM([]byte(caPEM)) {
		return nil, fmt.Errorf("tunnel: tls_ca_pem contained no usable certificate")
	}
	return &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: pool}, nil
}

// dialHost picks the host to dial the game port at: loopback when the game bind
// IP is unset or 0.0.0.0 (loopback reaches an all-interfaces publish), the
// configured IP otherwise (RELAY.md Section 5).
func (d *Dialer) dialHost() string {
	if d.gameBindIP == "" || d.gameBindIP == "0.0.0.0" {
		return "127.0.0.1"
	}
	return d.gameBindIP
}

// handshake sends the preamble + token and requires "OK\n" before the context
// deadline (RELAY.md Section 5). Any other reply, EOF, or timeout is an error.
// It reads the reply one byte at a time and stops at the newline, so it never
// consumes the player bytes the relay starts splicing right after "OK\n" — a
// buffered reader would swallow them and corrupt the stream.
func handshake(ctx context.Context, conn net.Conn, token string) error {
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
	// Clear the handshake deadline: the splice has no idle timeout of its own
	// (RELAY.md Section 5 — the relay propagates close, the MC protocol keep-alives
	// do the rest).
	_ = conn.SetDeadline(time.Time{})
	return nil
}

// readLine reads up to limit bytes one at a time, stopping after the first
// newline (inclusive). Reading single bytes keeps the read from over-consuming
// into the spliced byte stream that follows "OK\n"; limit bounds a relay that
// never sends a newline.
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

// splice copies bytes both ways between the relay and game connections with
// half-close propagation: when one direction reaches EOF the peer's write half is
// closed (CloseWrite) so the other side sees a clean end of stream, and once both
// directions finish both conns are fully closed and deregistered. RELAY.md
// Section 5.
func (d *Dialer) splice(relayConn, gameConn net.Conn, serverID string) {
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); copyHalf(gameConn, relayConn) }()
	go func() { defer wg.Done(); copyHalf(relayConn, gameConn) }()
	wg.Wait()

	d.deregister(relayConn, gameConn)
	d.logger.Debug("tunnel closed", "server_id", serverID)
}

// copyHalf copies src into dst until EOF or error, then half-closes dst's write
// side so the peer sees end-of-stream while its own reverse copy still drains. On
// any error it fully closes both ends to unblock the reverse copy (RELAY.md
// Section 5 — close both on either side's error/EOF).
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

// register / deregister track live conns so closeAll can tear them down on
// Worker shutdown. A conn registered after baseCtx is already cancelled is closed
// immediately (the shutdown sweep already ran).
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

// gamePort reads server-port from the server's working-dir server.properties,
// falling back to the Minecraft default when the file is absent or the key is
// unset — mirroring the container driver's published-port resolution.
func gamePort(workingDir string) string {
	props := readProperties(filepath.Join(workingDir, "server.properties"))
	if port := props["server-port"]; port != "" {
		return port
	}
	return defaultGamePort
}

// readProperties parses a Java .properties file into a map, skipping blanks and
// comments. A missing/unreadable file yields an empty map (the caller falls back
// to defaults).
func readProperties(path string) map[string]string {
	out := map[string]string{}
	f, err := os.Open(path) //nolint:gosec // path is the server's own working dir, not user-controlled.
	if err != nil {
		return out
	}
	defer func() { _ = f.Close() }()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "!") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		out[strings.TrimSpace(key)] = strings.TrimSpace(value)
	}
	return out
}
