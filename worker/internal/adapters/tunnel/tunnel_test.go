package tunnel

import (
	"bufio"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"log/slog"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// discardLogger writes nowhere, keeping test output clean.
func discardLogger(_ *testing.T) *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// fakeRelay is an in-process TLS tunnel listener standing in for the relay. It
// performs the dial-back handshake (RELAY.md Section 5): read the preamble +
// token line, accept or reject the token, and on accept reply "OK\n" and hand the
// accepted conn to the test for splicing.
type fakeRelay struct {
	ln       net.Listener
	caPEM    string
	wantTok  string
	accepted chan net.Conn // accepted tunnel conns (post-OK)
	// noOK, when set, makes the relay read the handshake but never reply "OK\n",
	// exercising the Worker's handshake timeout.
	noOK bool
}

// newFakeRelay starts a TLS listener with a fresh self-signed cert. wantTok is
// the token it accepts; any other token is rejected with a silent close.
func newFakeRelay(t *testing.T, wantTok string) *fakeRelay {
	t.Helper()
	cert, caPEM := selfSignedCert(t)
	ln, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{
		Certificates: []tls.Certificate{cert},
		MinVersion:   tls.VersionTLS12,
	})
	if err != nil {
		t.Fatalf("tls.Listen: %v", err)
	}
	r := &fakeRelay{ln: ln, caPEM: caPEM, wantTok: wantTok, accepted: make(chan net.Conn, 1)}
	go r.serve()
	t.Cleanup(func() { _ = ln.Close() })
	return r
}

func (r *fakeRelay) addr() string { return r.ln.Addr().String() }

func (r *fakeRelay) serve() {
	for {
		conn, err := r.ln.Accept()
		if err != nil {
			return
		}
		go r.handle(conn)
	}
}

func (r *fakeRelay) handle(conn net.Conn) {
	br := bufio.NewReader(conn)
	preamble, err := br.ReadString('\n')
	if err != nil || preamble != handshakePreamble {
		_ = conn.Close()
		return
	}
	token, err := br.ReadString('\n')
	if err != nil || token != r.wantTok+"\n" {
		_ = conn.Close() // unknown/expired token: close without a response.
		return
	}
	if r.noOK {
		return // hold the conn open, never reply: drive the Worker's timeout.
	}
	if _, err := io.WriteString(conn, handshakeOK); err != nil {
		_ = conn.Close()
		return
	}
	// Wrap the buffered reader so a test reading from the accepted conn does not
	// lose bytes already buffered after "OK\n".
	r.accepted <- &bufConn{Conn: conn, r: br}
}

// bufConn is a net.Conn whose reads come from a bufio.Reader (preserving any
// bytes buffered during the handshake) while writes/close go to the raw conn.
type bufConn struct {
	net.Conn
	r *bufio.Reader
}

func (c *bufConn) Read(p []byte) (int, error) { return c.r.Read(p) }

// CloseWrite forwards to the underlying conn's half-close so the test's relay
// side can exercise half-close propagation.
func (c *bufConn) CloseWrite() error {
	if cw, ok := c.Conn.(interface{ CloseWrite() error }); ok {
		return cw.CloseWrite()
	}
	return c.Close()
}

// selfSignedCert returns a TLS cert for 127.0.0.1 and its CA PEM (the leaf is
// its own CA here — fine for a test root).
func selfSignedCert(t *testing.T) (tls.Certificate, string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "test-relay"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("CreateCertificate: %v", err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("MarshalECPrivateKey: %v", err)
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
	cert, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		t.Fatalf("X509KeyPair: %v", err)
	}
	return cert, string(certPEM)
}

// fakeGame is an in-process stand-in for the local Minecraft server's game port.
type fakeGame struct {
	ln       net.Listener
	accepted chan net.Conn
}

func newFakeGame(t *testing.T) *fakeGame {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("net.Listen: %v", err)
	}
	g := &fakeGame{ln: ln, accepted: make(chan net.Conn, 1)}
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			g.accepted <- conn
		}
	}()
	t.Cleanup(func() { _ = ln.Close() })
	return g
}

func (g *fakeGame) port() string {
	_, port, _ := net.SplitHostPort(g.ln.Addr().String())
	return port
}

// newDialerForGame builds a Dialer whose gameDial targets the fake game listener
// regardless of the computed loopback address, and writes a server.properties so
// the (unused-for-routing here) port read still resolves.
func newDialerForTest(ctx context.Context, t *testing.T, game *fakeGame) (*Dialer, string) {
	t.Helper()
	workingDir := t.TempDir()
	writeServerPort(t, workingDir, game.port())
	d := New(ctx, "0.0.0.0", discardLogger(t))
	return d, workingDir
}

func writeServerPort(t *testing.T, workingDir, port string) {
	t.Helper()
	body := "server-port=" + port + "\n"
	if err := os.WriteFile(filepath.Join(workingDir, "server.properties"), []byte(body), 0o600); err != nil {
		t.Fatalf("write server.properties: %v", err)
	}
}

// TestDialSplicesBothDirections is the happy path: a good token is accepted, the
// game port is dialed, and bytes flow both ways through the splice.
func TestDialSplicesBothDirections(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, "tok-123")
	game := newFakeGame(t)
	d, workingDir := newDialerForTest(ctx, t, game)

	if err := d.Dial(ctx, Spec{
		ServerID: "s1", WorkingDir: workingDir,
		Endpoint: relay.addr(), Token: "tok-123", CAPEM: relay.caPEM,
	}); err != nil {
		t.Fatalf("Dial = %v, want nil", err)
	}

	relayConn := <-relay.accepted
	gameConn := <-game.accepted

	// player -> server: bytes the relay writes reach the game port.
	if _, err := io.WriteString(relayConn, "hello-server"); err != nil {
		t.Fatalf("write to relay: %v", err)
	}
	if got := readN(t, gameConn, len("hello-server")); got != "hello-server" {
		t.Fatalf("game received %q, want %q", got, "hello-server")
	}

	// server -> player: bytes the game port writes reach the relay.
	if _, err := io.WriteString(gameConn, "hi-player"); err != nil {
		t.Fatalf("write to game: %v", err)
	}
	if got := readN(t, relayConn, len("hi-player")); got != "hi-player" {
		t.Fatalf("relay received %q, want %q", got, "hi-player")
	}
}

// TestHalfClosePropagates: closing one direction's write half delivers EOF to the
// peer while the reverse direction keeps flowing (RELAY.md Section 5).
func TestHalfClosePropagates(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, "tok")
	game := newFakeGame(t)
	d, workingDir := newDialerForTest(ctx, t, game)

	if err := d.Dial(ctx, Spec{ServerID: "s1", WorkingDir: workingDir, Endpoint: relay.addr(), Token: "tok", CAPEM: relay.caPEM}); err != nil {
		t.Fatalf("Dial = %v", err)
	}
	relayConn := <-relay.accepted
	gameConn := <-game.accepted

	// The relay (player side) half-closes its write side; the game side must see EOF.
	if cw, ok := relayConn.(interface{ CloseWrite() error }); ok {
		if err := cw.CloseWrite(); err != nil {
			t.Fatalf("relay CloseWrite: %v", err)
		}
	} else {
		t.Fatal("relay conn does not support CloseWrite")
	}
	_ = gameConn.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, err := io.ReadAll(gameConn); err != nil {
		t.Fatalf("game read after half-close = %v, want EOF (nil)", err)
	}

	// The reverse direction still works: game -> player.
	if _, err := io.WriteString(gameConn, "still-here"); err != nil {
		t.Fatalf("write to game after half-close: %v", err)
	}
	if got := readN(t, relayConn, len("still-here")); got != "still-here" {
		t.Fatalf("relay received %q, want %q", got, "still-here")
	}
}

// TestSpliceClosesBothConnsOnCleanEOF: after a clean EOF in both directions the
// splice must fully close both conns (not just CloseWrite), so their fds are
// released deterministically rather than waiting on the netFD finalizer.
func TestSpliceClosesBothConnsOnCleanEOF(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, "tok")
	game := newFakeGame(t)
	d, workingDir := newDialerForTest(ctx, t, game)

	if err := d.Dial(ctx, Spec{ServerID: "s1", WorkingDir: workingDir, Endpoint: relay.addr(), Token: "tok", CAPEM: relay.caPEM}); err != nil {
		t.Fatalf("Dial = %v", err)
	}
	relayConn := <-relay.accepted
	gameConn := <-game.accepted

	// Both peers half-close their write side: each copy direction sees a clean EOF,
	// so splice runs the clean-EOF path and must then fully close both conns.
	closeWrite(t, relayConn)
	closeWrite(t, gameConn)

	// Both peers see their reads end (the Dialer half-closed and then fully closed).
	assertReadClosed(t, relayConn)
	assertReadClosed(t, gameConn)

	// splice runs in a goroutine: poll until it has finished the full Close +
	// deregister. An empty registry proves splice completed past wg.Wait() and ran
	// the explicit Close on both conns (the only path that reaches deregister).
	deadline := time.Now().Add(2 * time.Second)
	for {
		d.mu.Lock()
		remaining := len(d.conns)
		d.mu.Unlock()
		if remaining == 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("Dialer still tracks %d conns after clean EOF, want 0", remaining)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// closeWrite half-closes conn's write side, failing the test if it cannot.
func closeWrite(t *testing.T, conn net.Conn) {
	t.Helper()
	cw, ok := conn.(interface{ CloseWrite() error })
	if !ok {
		t.Fatalf("conn %T does not support CloseWrite", conn)
	}
	if err := cw.CloseWrite(); err != nil {
		t.Fatalf("CloseWrite: %v", err)
	}
}

// assertReadClosed reads from conn under a short deadline and requires the read
// to return without delivering data (EOF or a closed-conn error).
func assertReadClosed(t *testing.T, conn net.Conn) {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf := make([]byte, 1)
	if n, err := conn.Read(buf); err == nil {
		t.Fatalf("read returned %d bytes with nil error, want EOF/closed", n)
	}
}

// TestDialRejectsBadToken: the relay closes a tunnel presenting an unknown token
// without replying, so the Worker's handshake read fails.
func TestDialRejectsBadToken(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, "good-tok")
	game := newFakeGame(t)
	d, workingDir := newDialerForTest(ctx, t, game)

	err := d.Dial(ctx, Spec{ServerID: "s1", WorkingDir: workingDir, Endpoint: relay.addr(), Token: "bad-tok", CAPEM: relay.caPEM})
	if err == nil {
		t.Fatal("Dial with bad token = nil, want handshake error")
	}
	select {
	case <-game.accepted:
		t.Fatal("game port was dialed despite a rejected handshake")
	default:
	}
}

// TestDialHandshakeTimeout: a relay that never sends "OK\n" must make Dial fail
// (the Worker bounds the handshake; RELAY.md Section 5).
func TestDialHandshakeTimeout(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, "tok")
	relay.noOK = true
	game := newFakeGame(t)
	d, workingDir := newDialerForTest(ctx, t, game)

	// A short caller deadline drives the timeout fast (the 5 s internal bound is the
	// ceiling; the caller's sooner deadline wins).
	dialCtx, dialCancel := context.WithTimeout(ctx, 300*time.Millisecond)
	defer dialCancel()
	err := d.Dial(dialCtx, Spec{ServerID: "s1", WorkingDir: workingDir, Endpoint: relay.addr(), Token: "tok", CAPEM: relay.caPEM})
	if err == nil {
		t.Fatal("Dial against a silent relay = nil, want timeout error")
	}
}

// TestDialRejectsInvalidCAPEM: a garbage tls_ca_pem yields no usable certificate,
// so Dial fails before dialing anything (the error becomes a CommandResult error).
func TestDialRejectsInvalidCAPEM(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	game := newFakeGame(t)
	d, workingDir := newDialerForTest(ctx, t, game)

	err := d.Dial(ctx, Spec{
		ServerID: "s1", WorkingDir: workingDir,
		Endpoint: "127.0.0.1:1", Token: "tok", CAPEM: "-----BEGIN CERTIFICATE-----\nnot-a-cert\n-----END CERTIFICATE-----\n",
	})
	if err == nil {
		t.Fatal("Dial with garbage tls_ca_pem = nil, want a no-usable-certificate error")
	}
	if !strings.Contains(err.Error(), "no usable certificate") {
		t.Fatalf("Dial error = %v, want a no-usable-certificate error", err)
	}
	select {
	case <-game.accepted:
		t.Fatal("game port was dialed despite an invalid CA bundle")
	default:
	}
}

// TestShutdownClosesTunnels: cancelling the Dialer's base context tears down a
// live splice (close all tunnel conns), so the relay side sees its conn close.
func TestShutdownClosesTunnels(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())

	relay := newFakeRelay(t, "tok")
	game := newFakeGame(t)
	d, workingDir := newDialerForTest(ctx, t, game)

	if err := d.Dial(ctx, Spec{ServerID: "s1", WorkingDir: workingDir, Endpoint: relay.addr(), Token: "tok", CAPEM: relay.caPEM}); err != nil {
		t.Fatalf("Dial = %v", err)
	}
	relayConn := <-relay.accepted
	<-game.accepted

	cancel() // Worker shutdown.

	// The relay's conn must reach EOF/closed once the Dialer closes it.
	_ = relayConn.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf := make([]byte, 1)
	if _, err := relayConn.Read(buf); err == nil {
		t.Fatal("relay conn still readable after shutdown; tunnel was not closed")
	}
}

// readN reads exactly n bytes from conn under a short deadline.
func readN(t *testing.T, conn net.Conn, n int) string {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf := make([]byte, n)
	if _, err := io.ReadFull(conn, buf); err != nil {
		t.Fatalf("read %d bytes: %v", n, err)
	}
	return string(buf)
}
