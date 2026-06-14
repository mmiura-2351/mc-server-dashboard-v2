// Package integration exercises the relay end to end: a real game listener and
// a real TLS tunnel listener, a fake API serving RelayService in-process, and a
// fake Worker dialing the tunnel — driving a real TCP login through both
// listeners and a status ping through the cache path. It is the integration-
// style acceptance test from docs/app/RELAY.md / issue #959.
package integration

import (
	"bufio"
	"bytes"
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
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/game"
	relayv1 "github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/genproto/mcsd/relay/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/relaysvc"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/session"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/tunnel"
)

const baseDomain = "mc.example.com"

// testServerID is the server_id the fake API returns on a TUNNEL decision; the
// relay must carry it into SessionStart.server_id (distinct from the slug).
const testServerID = "srv-uuid-1234"

// fakeAPI is an in-process RelayService. It returns a fixed token for TUNNEL
// resolves and captures reported sessions.
type fakeAPI struct {
	relayv1.UnimplementedRelayServiceServer
	token string

	// dialTunnel fires the token a TUNNEL resolve produced, mimicking the API's
	// real side effect of dispatching a TunnelDial command to the Worker. The
	// fake Worker waits on it so its dial-back never races the relay's token
	// registration (the registration happens inside ResolveJoin's caller, after
	// this RPC returns).
	dialTunnel chan string

	mu     sync.Mutex
	starts []*relayv1.SessionStart
	ends   []*relayv1.SessionEnd
}

func (a *fakeAPI) Register(_ context.Context, _ *relayv1.RegisterRequest) (*relayv1.RegisterResponse, error) {
	return &relayv1.RegisterResponse{BaseDomain: baseDomain}, nil
}

func (a *fakeAPI) ResolveJoin(_ context.Context, req *relayv1.ResolveJoinRequest) (*relayv1.ResolveJoinResponse, error) {
	if req.GetSlug() == "stopped" {
		return &relayv1.ResolveJoinResponse{Decision: relayv1.JoinDecision_JOIN_DECISION_STOPPED, DisplayName: "Stopped Server"}, nil
	}
	// Mimic the real TunnelDial side effect: tell the fake Worker to dial back.
	// Non-blocking so a status resolve (which the test does not back with a
	// Worker) does not wedge the RPC.
	if a.dialTunnel != nil {
		select {
		case a.dialTunnel <- a.token:
		default:
		}
	}
	return &relayv1.ResolveJoinResponse{Decision: relayv1.JoinDecision_JOIN_DECISION_TUNNEL, Token: a.token, ServerId: testServerID}, nil
}

func (a *fakeAPI) ReportSessions(_ context.Context, req *relayv1.ReportSessionsRequest) (*relayv1.ReportSessionsResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, ev := range req.GetEvents() {
		if s := ev.GetStart(); s != nil {
			a.starts = append(a.starts, s)
		}
		if e := ev.GetEnd(); e != nil {
			a.ends = append(a.ends, e)
		}
	}
	return &relayv1.ReportSessionsResponse{}, nil
}

func (a *fakeAPI) sessionCounts() (int, int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.starts), len(a.ends)
}

// harness wires the relay around a fake API over a real loopback gRPC server.
type harness struct {
	api        *fakeAPI
	gameAddr   string
	tunnelAddr string
	wg         sync.WaitGroup
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))

	// In-process gRPC API.
	api := &fakeAPI{token: "test-token", dialTunnel: make(chan string, 1)}
	grpcLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	grpcSrv := grpc.NewServer()
	relayv1.RegisterRelayServiceServer(grpcSrv, api)
	go func() { _ = grpcSrv.Serve(grpcLn) }()

	conn, err := grpc.NewClient(grpcLn.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}

	apiClient := apiclient.New(conn, "relay-cred")
	reporter := session.NewReporter(apiClient, logger, time.Now).WithFlushInterval(50 * time.Millisecond)
	svc := relaysvc.New(apiClient, conn, reporter, "relay:25665", "", logger)

	tokens := tunnel.NewTokenTable(10*time.Second, time.Now)
	cache := game.NewStatusCache(5*time.Second, 1024, time.Now)
	caps := ipcaps.NewIPCaps(32, 10, time.Now)
	tunnelCaps := ipcaps.NewIPCaps(64, 0, time.Now)

	tunnelLn, err := tunnel.NewListener("127.0.0.1:0", selfSignedTLS(t), tokens, tunnelCaps, logger)
	if err != nil {
		t.Fatal(err)
	}
	gameLn, err := game.NewListener("127.0.0.1:0", svc, tokens, cache, caps, reporter, logger)
	if err != nil {
		t.Fatal(err)
	}

	h := &harness{
		api:        api,
		gameAddr:   gameLn.Addr().String(),
		tunnelAddr: tunnelLn.Addr().String(),
	}

	// Register so the relay learns base_domain.
	if err := svc.RegisterOnce(ctx); err != nil {
		t.Fatalf("register: %v", err)
	}

	h.wg.Add(3)
	go func() { defer h.wg.Done(); _ = tunnelLn.Serve(ctx) }()
	go func() { defer h.wg.Done(); _ = gameLn.Serve(ctx) }()
	go func() { defer h.wg.Done(); reporter.Run(ctx) }()

	t.Cleanup(func() {
		cancel()
		grpcSrv.Stop()
		_ = grpcLn.Close()
		h.wg.Wait()
	})
	return h
}

// TestLoginSplice drives a real login through the game listener, a fake Worker
// dial-back through the tunnel listener, and verifies the byte splice carries
// data both ways and the session is reported.
func TestLoginSplice(t *testing.T) {
	h := newHarness(t)

	// Fake Worker: when the relay dispatches a tunnel dial-back, the relay only
	// has the token in its table; the Worker is the dialing party. We launch the
	// Worker as soon as the player connects (the API would normally trigger it via
	// TunnelDial). Since the API is faked, drive the Worker dial directly with the
	// known token after the player starts.
	serverGot := make(chan []byte, 1)
	workerReplay := make(chan net.Conn, 1)
	go func() {
		// Wait for the API's TunnelDial side effect (mirrors the real Worker, which
		// dials only after the API dispatches the command).
		token := <-h.api.dialTunnel

		// Dial the tunnel listener, present the handshake + token, expect "OK\n".
		// Retry briefly: the relay registers the token waiter just after its
		// ResolveJoin RPC returns, so a dial that lands microseconds early finds no
		// waiter and is dropped — a real Worker's control-round-trip makes this race
		// vanishingly unlikely, but the in-process test must tolerate it.
		tlsConn := dialTunnelWithRetry(t, h.tunnelAddr, token)
		if tlsConn == nil {
			return
		}
		workerReplay <- tlsConn

		// Read the replayed handshake + login start, capture the login start body.
		br := bufio.NewReader(tlsConn)
		// First the handshake packet, then the login start packet.
		if _, err := readOnePacket(br); err != nil {
			t.Errorf("read replayed handshake: %v", err)
			return
		}
		loginStart, err := readOnePacket(br)
		if err != nil {
			t.Errorf("read replayed login start: %v", err)
			return
		}
		serverGot <- loginStart

		// Echo a byte back to the player to prove the reverse splice direction.
		if _, err := tlsConn.Write([]byte{0xAB}); err != nil {
			t.Errorf("worker write back: %v", err)
		}

		// Drain until the relay half-closes (the player closed and the relay
		// propagated the close), then close our side so the splice completes and
		// the relay reports SessionEnd. This is what a real MC server does when the
		// client disconnects.
		_, _ = io.Copy(io.Discard, br)
		_ = tlsConn.Close()
	}()

	// Player connects and sends handshake (login) + login start.
	player, err := net.Dial("tcp", h.gameAddr)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = player.Close() }()

	hs := handshakePacket(765, "amber.mc.example.com", 25565, 2)
	ls := loginStartPacket("Steve")
	if _, err := player.Write(append(hs, ls...)); err != nil {
		t.Fatal(err)
	}

	// The worker replays and echoes; the player should receive 0xAB.
	<-workerReplay
	got := <-serverGot
	if !bytes.Equal(got, ls) {
		t.Errorf("worker received login start %x, want %x", got, ls)
	}

	_ = player.SetReadDeadline(time.Now().Add(2 * time.Second))
	echo := make([]byte, 1)
	if _, err := io.ReadFull(player, echo); err != nil {
		t.Fatalf("player read echo: %v", err)
	}
	if echo[0] != 0xAB {
		t.Errorf("player echo = %#x, want 0xAB", echo[0])
	}

	// Close the player; the session end should be reported.
	_ = player.Close()

	waitFor(t, 2*time.Second, func() bool {
		s, e := h.api.sessionCounts()
		return s >= 1 && e >= 1
	})
	h.api.mu.Lock()
	if len(h.api.starts) > 0 {
		start := h.api.starts[0]
		if start.GetUsername() != "Steve" {
			t.Errorf("reported username = %q, want Steve", start.GetUsername())
		}
		// server_id is the API's identifier from ResolveJoin, distinct from the
		// slug (the historical hostname label).
		if start.GetServerId() != testServerID {
			t.Errorf("reported server_id = %q, want %q", start.GetServerId(), testServerID)
		}
		if start.GetServerId() == start.GetSlug() {
			t.Errorf("server_id must not duplicate the slug (%q)", start.GetSlug())
		}
	}
	h.api.mu.Unlock()
}

// TestLoginSplicePipelinedBytes verifies that bytes a client pipelines right
// after Login Start (pulled into the relay's read buffer before the splice) are
// forwarded to the Worker, not stranded.
func TestLoginSplicePipelinedBytes(t *testing.T) {
	h := newHarness(t)

	const sentinel = byte(0xCC)
	gotSentinel := make(chan byte, 1)
	go func() {
		token := <-h.api.dialTunnel
		tlsConn := dialTunnelWithRetry(t, h.tunnelAddr, token)
		if tlsConn == nil {
			return
		}
		br := bufio.NewReader(tlsConn)
		// handshake, login start, then the pipelined sentinel byte.
		if _, err := readOnePacket(br); err != nil {
			t.Errorf("read handshake: %v", err)
			return
		}
		if _, err := readOnePacket(br); err != nil {
			t.Errorf("read login start: %v", err)
			return
		}
		b, err := br.ReadByte()
		if err != nil {
			t.Errorf("read pipelined byte: %v", err)
			return
		}
		gotSentinel <- b
		_, _ = io.Copy(io.Discard, br)
		_ = tlsConn.Close()
	}()

	player, err := net.Dial("tcp", h.gameAddr)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = player.Close() }()

	// Send handshake + login start + a sentinel byte in ONE write, so the
	// sentinel lands in the relay's read buffer behind the parsed packets.
	payload := append(handshakePacket(765, "amber.mc.example.com", 25565, 2), loginStartPacket("Steve")...)
	payload = append(payload, sentinel)
	if _, err := player.Write(payload); err != nil {
		t.Fatal(err)
	}

	select {
	case b := <-gotSentinel:
		if b != sentinel {
			t.Errorf("worker got pipelined byte %#x, want %#x", b, sentinel)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("worker never received the pipelined byte")
	}
}

// TestStatusStoppedSynthesized verifies a status ping to a stopped server gets
// the synthesized in-protocol response (no tunnel involved).
func TestStatusStoppedSynthesized(t *testing.T) {
	h := newHarness(t)

	player, err := net.Dial("tcp", h.gameAddr)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = player.Close() }()

	hs := handshakePacket(765, "stopped.mc.example.com", 25565, 1)
	statusReq := []byte{0x01, 0x00} // empty Status Request
	if _, err := player.Write(append(hs, statusReq...)); err != nil {
		t.Fatal(err)
	}

	_ = player.SetReadDeadline(time.Now().Add(2 * time.Second))
	br := bufio.NewReader(player)
	body, err := readOnePacketBody(br)
	if err != nil {
		t.Fatalf("read status response: %v", err)
	}
	// body = id(0x00) + VarInt-string JSON.
	if body[0] != 0x00 {
		t.Fatalf("status response id = 0x%02x", body[0])
	}
	if !strings.Contains(string(body), "stopped. Start it from the dashboard.") {
		t.Errorf("stopped MOTD missing from %q", string(body))
	}
}

// TestUnknownHostDropped verifies an unknown hostname gets no protocol response.
func TestUnknownHostDropped(t *testing.T) {
	h := newHarness(t)

	player, err := net.Dial("tcp", h.gameAddr)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = player.Close() }()

	hs := handshakePacket(765, "evil.attacker.com", 25565, 1)
	if _, err := player.Write(hs); err != nil {
		t.Fatal(err)
	}

	// The relay drops silently: the player's next read should hit EOF, not data.
	_ = player.SetReadDeadline(time.Now().Add(2 * time.Second))
	if n, err := player.Read(make([]byte, 1)); err == nil && n > 0 {
		t.Errorf("unknown host should be dropped without a response, got %d bytes", n)
	}
}

// dialTunnelWithRetry dials the tunnel listener and presents the token,
// retrying until the relay acks "OK\n" (the token waiter is registered) or the
// attempts run out. Returns the live connection, or nil after reporting a
// failure.
func dialTunnelWithRetry(t *testing.T, addr, token string) net.Conn {
	t.Helper()
	for attempt := 0; attempt < 50; attempt++ {
		tlsConn, err := tls.Dial("tcp", addr, &tls.Config{InsecureSkipVerify: true}) //nolint:gosec // test self-signed cert
		if err != nil {
			t.Errorf("worker dial: %v", err)
			return nil
		}
		if _, err := tlsConn.Write([]byte("MCSD-TUNNEL/1\n" + token + "\n")); err != nil {
			_ = tlsConn.Close()
			time.Sleep(5 * time.Millisecond)
			continue
		}
		ack := make([]byte, 3)
		_ = tlsConn.SetReadDeadline(time.Now().Add(time.Second))
		if _, err := io.ReadFull(tlsConn, ack); err != nil || string(ack) != "OK\n" {
			// No waiter yet (relay closed without an ack): retry.
			_ = tlsConn.Close()
			time.Sleep(5 * time.Millisecond)
			continue
		}
		_ = tlsConn.SetReadDeadline(time.Time{})
		return tlsConn
	}
	t.Error("worker could not establish a tunnel within the retry budget")
	return nil
}

// --- protocol fixture helpers ---

func handshakePacket(protocol int32, addr string, port uint16, next int32) []byte {
	var body []byte
	body = appendVarInt(body, protocol)
	body = appendString(body, addr)
	body = append(body, byte(port>>8), byte(port))
	body = appendVarInt(body, next)
	return framePacket(0x00, body)
}

func loginStartPacket(name string) []byte {
	body := appendString(nil, name)
	body = append(body, make([]byte, 16)...) // 16-byte UUID (protocol 765 form)
	return framePacket(0x00, body)
}

func framePacket(id int32, body []byte) []byte {
	inner := appendVarInt(nil, id)
	inner = append(inner, body...)
	out := appendVarInt(nil, int32(len(inner)))
	return append(out, inner...)
}

func appendVarInt(dst []byte, v int32) []byte {
	u := uint32(v)
	for {
		b := byte(u & 0x7F)
		u >>= 7
		if u != 0 {
			b |= 0x80
		}
		dst = append(dst, b)
		if u == 0 {
			return dst
		}
	}
}

func appendString(dst []byte, s string) []byte {
	dst = appendVarInt(dst, int32(len(s)))
	return append(dst, s...)
}

func readVarInt(r *bufio.Reader) (int32, error) {
	var value uint32
	for n := 0; n < 5; n++ {
		b, err := r.ReadByte()
		if err != nil {
			return 0, err
		}
		value |= uint32(b&0x7F) << (7 * n)
		if b&0x80 == 0 {
			return int32(value), nil
		}
	}
	return 0, io.ErrUnexpectedEOF
}

// readOnePacket reads one length-prefixed packet and returns the bytes including
// the length prefix.
func readOnePacket(r *bufio.Reader) ([]byte, error) {
	length, err := readVarInt(r)
	if err != nil {
		return nil, err
	}
	body := make([]byte, length)
	if _, err := io.ReadFull(r, body); err != nil {
		return nil, err
	}
	raw := appendVarInt(nil, length)
	return append(raw, body...), nil
}

// readOnePacketBody reads one packet and returns just the body (no length prefix).
func readOnePacketBody(r *bufio.Reader) ([]byte, error) {
	length, err := readVarInt(r)
	if err != nil {
		return nil, err
	}
	body := make([]byte, length)
	if _, err := io.ReadFull(r, body); err != nil {
		return nil, err
	}
	return body, nil
}

func waitFor(t *testing.T, d time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("condition not met within deadline")
}

// selfSignedTLS builds a one-off self-signed server TLS config for the tunnel
// listener.
func selfSignedTLS(t *testing.T) *tls.Config {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "relay-test"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyDER, _ := x509.MarshalPKCS8PrivateKey(key)
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})

	dir := t.TempDir()
	certFile := filepath.Join(dir, "cert.pem")
	keyFile := filepath.Join(dir, "key.pem")
	_ = os.WriteFile(certFile, certPEM, 0o600)
	_ = os.WriteFile(keyFile, keyPEM, 0o600)

	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		t.Fatal(err)
	}
	return &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12}
}
