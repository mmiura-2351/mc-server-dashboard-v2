package integration

import (
	"bufio"
	"context"
	"io"
	"log/slog"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/game"
	relayv1 "github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/genproto/mcsd/relay/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/metrics"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/relaysvc"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/session"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/tunnel"
)

// shutdownHarness wires the relay like newHarness, but with the two contexts
// cmd/relay/main.go uses, so a test can walk the real shutdown sequence: stop
// the listeners, Drain the game listener, and only then stop the reporter. The
// reporter's periodic flush is effectively disabled, so anything the fake API
// receives arrived through the reporter's shutdown flush.
type shutdownHarness struct {
	api        *fakeAPI
	gameLn     *game.Listener
	gameAddr   string
	tunnelAddr string

	stopListeners context.CancelFunc
	gameServed    chan struct{}
	stopReporter  context.CancelFunc
	reporterDone  chan struct{}
}

func newShutdownHarness(t *testing.T) *shutdownHarness {
	t.Helper()
	sigCtx, stopListeners := context.WithCancel(context.Background())
	svcCtx, stopReporter := context.WithCancel(context.Background())
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))

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
	m := metrics.New(prometheus.NewRegistry(), "test")
	reporter := session.NewReporter(apiClient, logger, time.Now, m).WithFlushInterval(time.Hour)
	svc := relaysvc.New(apiClient, conn, reporter, "relay:25665", "", logger)

	tokens := tunnel.NewTokenTable(10*time.Second, time.Now)
	cache := game.NewStatusCache(5*time.Second, 1024, time.Now)
	caps := ipcaps.NewIPCaps(32, 10, 0, time.Now, logger)
	tunnelCaps := ipcaps.NewIPCaps(64, 0, 0, time.Now, logger)

	tunnelLn, err := tunnel.NewListener("127.0.0.1:0", selfSignedTLS(t), tokens, tunnelCaps, m, logger)
	if err != nil {
		t.Fatal(err)
	}
	gameLn, err := game.NewListener("127.0.0.1:0", svc, tokens, cache, caps, reporter, m, logger)
	if err != nil {
		t.Fatal(err)
	}
	if err := svc.RegisterOnce(sigCtx); err != nil {
		t.Fatalf("register: %v", err)
	}

	h := &shutdownHarness{
		api:           api,
		gameLn:        gameLn,
		gameAddr:      gameLn.Addr().String(),
		tunnelAddr:    tunnelLn.Addr().String(),
		stopListeners: stopListeners,
		gameServed:    make(chan struct{}),
		stopReporter:  stopReporter,
		reporterDone:  make(chan struct{}),
	}
	tunnelServed := make(chan struct{})
	go func() { defer close(tunnelServed); _ = tunnelLn.Serve(sigCtx) }()
	go func() { defer close(h.gameServed); _ = gameLn.Serve(sigCtx) }()
	go func() { defer close(h.reporterDone); reporter.Run(svcCtx) }()

	t.Cleanup(func() {
		stopListeners()
		stopReporter()
		<-tunnelServed
		<-h.gameServed
		<-h.reporterDone
		grpcSrv.Stop()
		_ = grpcLn.Close()
		_ = conn.Close()
	})
	return h
}

// TestShutdownEndsEstablishedSessionAndReportsItsEnd walks main.go's shutdown
// sequence over a live session whose player and server both stay connected —
// the session a drain could never end while the splice took no context (issue
// #3169). The relay must close both ends, Drain must return well inside its
// bound, and the session's real End must reach the API through the reporter's
// shutdown flush.
func TestShutdownEndsEstablishedSessionAndReportsItsEnd(t *testing.T) {
	h := newShutdownHarness(t)

	// Fake Worker: dial back, consume the replay, then hold the tunnel open and
	// report when the relay closes it.
	workerUp := make(chan struct{})
	workerClosed := make(chan struct{})
	go func() {
		defer close(workerClosed)
		token := <-h.api.dialTunnel
		tlsConn := dialTunnelWithRetry(t, h.tunnelAddr, token)
		if tlsConn == nil {
			return
		}
		defer func() { _ = tlsConn.Close() }()
		br := bufio.NewReader(tlsConn)
		for i := 0; i < 2; i++ { // handshake, login start
			if _, err := readOnePacket(br); err != nil {
				t.Errorf("read replayed packet: %v", err)
				return
			}
		}
		if _, err := tlsConn.Write([]byte{0xAB}); err != nil {
			t.Errorf("worker write back: %v", err)
			return
		}
		close(workerUp)
		_, _ = io.Copy(io.Discard, br)
	}()

	player, err := net.Dial("tcp", h.gameAddr)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = player.Close() }()
	if _, err := player.Write(append(handshakePacket(765, "amber.mc.example.com", 25565, 2), loginStartPacket("Steve")...)); err != nil {
		t.Fatal(err)
	}

	// The session is established end to end: the server's byte reaches the player.
	select {
	case <-workerUp:
	case <-time.After(5 * time.Second):
		t.Fatal("worker never established the tunnel")
	}
	_ = player.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, err := io.ReadFull(player, make([]byte, 1)); err != nil {
		t.Fatalf("player read echo: %v", err)
	}

	// main.go's order: stop accepting, let Serve return, then Drain.
	h.stopListeners()
	<-h.gameServed
	start := time.Now()
	if !h.gameLn.Drain(10 * time.Second) {
		t.Fatal("Drain timed out with an established session still spliced")
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("Drain took %v; it must not wait on the session's peers", elapsed)
	}

	// The relay closed both ends of the session.
	_ = player.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, err := player.Read(make([]byte, 1)); err == nil || isTimeout(err) {
		t.Errorf("player read = %v, want a closed connection", err)
	}
	select {
	case <-workerClosed:
	case <-time.After(2 * time.Second):
		t.Error("worker tunnel was not closed by the shutdown")
	}

	// Nothing was reported before the reporter stops (its tick is disabled), so
	// what arrives now is exactly what the shutdown flush carries.
	if s, e := h.api.sessionCounts(); s != 0 || e != 0 {
		t.Fatalf("reported before the reporter stopped: starts=%d ends=%d, want 0 and 0", s, e)
	}
	h.stopReporter()
	select {
	case <-h.reporterDone:
	case <-time.After(5 * time.Second):
		t.Fatal("reporter did not finish its shutdown flush")
	}

	h.api.mu.Lock()
	defer h.api.mu.Unlock()
	if len(h.api.starts) != 1 || len(h.api.ends) != 1 {
		t.Fatalf("reported starts=%d ends=%d, want 1 and 1", len(h.api.starts), len(h.api.ends))
	}
	if got, want := h.api.ends[0].GetSessionId(), h.api.starts[0].GetSessionId(); got != want {
		t.Errorf("End reported for session %q, want the started session %q", got, want)
	}
}

// TestShutdownLetsUnsplicedHandlerFinish verifies that a shutdown does not cut
// off a handler that has not spliced: Drain keeps waiting for it (and its
// timeout still bounds a handler that does not finish), and once the player's
// login arrives the handler answers it through its normal path — a Login
// Disconnect, since no new join is resolved during shutdown.
func TestShutdownLetsUnsplicedHandlerFinish(t *testing.T) {
	h := newShutdownHarness(t)

	// A player connects and stalls before its handshake; the handler is parked
	// in its pre-route read.
	player, err := net.Dial("tcp", h.gameAddr)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = player.Close() }()
	// The listener closes on shutdown, so make sure the connection was accepted
	// (handed to a handler) first: a status ping on a second connection is
	// answered only by a later Accept iteration.
	pingStopped(t, h.gameAddr)

	h.stopListeners()
	<-h.gameServed

	if h.gameLn.Drain(200 * time.Millisecond) {
		t.Fatal("Drain returned while a handler was still mid-handshake")
	}

	// The handler was not cut off: it still reads the login and answers it.
	if _, err := player.Write(append(handshakePacket(765, "amber.mc.example.com", 25565, 2), loginStartPacket("Steve")...)); err != nil {
		t.Fatalf("player write after shutdown began: %v", err)
	}
	_ = player.SetReadDeadline(time.Now().Add(2 * time.Second))
	disconnect, err := readOnePacket(bufio.NewReader(player))
	if err != nil {
		t.Fatalf("player read Login Disconnect: %v", err)
	}
	if !strings.Contains(string(disconnect), "Dashboard unavailable") {
		t.Errorf("Login Disconnect = %q, want the dashboard-unavailable reason", disconnect)
	}

	if !h.gameLn.Drain(2 * time.Second) {
		t.Fatal("Drain timed out after the unspliced handler finished")
	}
	if s, e := h.api.sessionCounts(); s != 0 || e != 0 {
		t.Errorf("reported starts=%d ends=%d for a login that never spliced, want 0 and 0", s, e)
	}
}

// pingStopped completes a status ping against the synthesized stopped-server
// response, proving the game listener has accepted every earlier connection.
func pingStopped(t *testing.T, gameAddr string) {
	t.Helper()
	c, err := net.Dial("tcp", gameAddr)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close() }()
	if _, err := c.Write(append(handshakePacket(765, "stopped.mc.example.com", 25565, 1), framePacket(0x00, nil)...)); err != nil {
		t.Fatal(err)
	}
	_ = c.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, err := readOnePacket(bufio.NewReader(c)); err != nil {
		t.Fatalf("status response: %v", err)
	}
}

func isTimeout(err error) bool {
	ne, ok := err.(net.Error)
	return ok && ne.Timeout()
}
