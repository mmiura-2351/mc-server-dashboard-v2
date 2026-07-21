package bedrocktunnel

import (
	"context"
	"crypto/tls"
	"encoding/binary"
	"errors"
	"net"
	"sync/atomic"
	"testing"
	"time"

	"github.com/quic-go/quic-go"

	bedrocktunnelv1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/bedrocktunnel/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// fastBackoff shrinks the reconnect backoff so retry/redial tests run in
// milliseconds instead of the production 1s-30s range.
func fastBackoff() session.Backoff {
	return session.Backoff{Initial: time.Millisecond, Max: 5 * time.Millisecond, Multiplier: 2}
}

// newTestManager builds a Manager with a shrunk backoff; gameHost/gameBindIP
// default to the no-network loopback case, overridable per test.
func newTestManager(ctx context.Context, t *testing.T) *Manager {
	t.Helper()
	m := New(ctx, "0.0.0.0", nil, discardLogger())
	m.backoff = fastBackoff()
	return m
}

// waitFor polls cond until it is true or 5s elapse, failing the test on
// timeout.
func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	if !cond() {
		t.Fatal("condition not met within 5s")
	}
}

// fakeGeyser is a loopback UDP listener standing in for the container's
// Geyser port: it echoes every received datagram back to its source address,
// prefixed with "echo:" so a reply is distinguishable from a fresh probe.
type fakeGeyser struct {
	conn *net.UDPConn
}

func newFakeGeyser(t *testing.T) *fakeGeyser {
	t.Helper()
	conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: 0})
	if err != nil {
		t.Fatalf("net.ListenUDP: %v", err)
	}
	g := &fakeGeyser{conn: conn}
	go g.serve()
	t.Cleanup(func() { _ = conn.Close() })
	return g
}

func (g *fakeGeyser) serve() {
	buf := make([]byte, 2048)
	for {
		n, addr, err := g.conn.ReadFromUDP(buf)
		if err != nil {
			return
		}
		reply := append([]byte("echo:"), buf[:n]...)
		_, _ = g.conn.WriteToUDP(reply, addr)
	}
}

func (g *fakeGeyser) addr() string { return g.conn.LocalAddr().String() }

// dialToGeyser builds a dialUDP func that ignores the requested address and
// always dials geyser -- the tests exercising the datagram pump target a fake
// local listener instead of a real container's :19132.
func dialToGeyser(geyser *fakeGeyser) func(context.Context, string) (net.Conn, error) {
	return func(_ context.Context, _ string) (net.Conn, error) {
		return net.Dial("udp", geyser.addr())
	}
}

func specFor(relay *fakeRelay, serverID, token string) Spec {
	return Spec{ServerID: serverID, RelayEndpoint: relay.addr(), BedrockPort: 19132, Token: token, CAPEM: relay.caPEM}
}

func sendFlowDatagram(t *testing.T, conn *quic.Conn, id uint32, payload string) {
	t.Helper()
	frame := make([]byte, flowIDSize+len(payload))
	binary.BigEndian.PutUint32(frame[:flowIDSize], id)
	copy(frame[flowIDSize:], payload)
	if err := conn.SendDatagram(frame); err != nil {
		t.Fatalf("SendDatagram(flow %d): %v", id, err)
	}
}

// Open dials the relay, sends TunnelHello with the spec's fields, and — on
// acceptance — the tunnel is established (docs/app/BEDROCK_TUNNEL.md
// Section 4).
func TestOpenEstablishesTunnelAndSendsCorrectHello(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, nil) // accept everything
	m := newTestManager(ctx, t)

	if err := m.Open(specFor(relay, "s1", "tok-abc")); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}

	got := relay.waitAccepted(t)
	if got.hello.GetServerId() != "s1" || got.hello.GetBedrockPort() != 19132 || got.hello.GetToken() != "tok-abc" {
		t.Fatalf("TunnelHello = %+v, want {s1 19132 tok-abc}", got.hello)
	}
}

// The QUIC config the Worker dials with pins EnableDatagrams and the
// keepalive period the relay's 15s idle timeout requires
// (docs/app/BEDROCK_TUNNEL.md Section 3: a binding obligation).
func TestDialUsesPinnedKeepAliveAndDatagrams(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	m := newTestManager(ctx, t)
	captured := make(chan *quic.Config, 1)
	m.dialQUIC = func(_ context.Context, _ string, _ *tls.Config, quicCfg *quic.Config) (*quic.Conn, error) {
		select {
		case captured <- quicCfg:
		default:
		}
		return nil, errors.New("test: dial refused")
	}

	if err := m.Open(Spec{ServerID: "s1", RelayEndpoint: "127.0.0.1:1", BedrockPort: 1, Token: "t"}); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}

	select {
	case cfg := <-captured:
		if cfg.KeepAlivePeriod != keepAlivePeriod {
			t.Fatalf("KeepAlivePeriod = %v, want %v", cfg.KeepAlivePeriod, keepAlivePeriod)
		}
		if !cfg.EnableDatagrams {
			t.Fatal("EnableDatagrams = false, want true")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("dialQUIC was not called within 2s")
	}
}

// Datagrams for multiple flows round-trip independently: each flow id gets
// its own local UDP socket to the container's Geyser port, and a reply is
// tagged with the same flow id it arrived on (docs/app/BEDROCK_TUNNEL.md
// Section 5).
func TestDatagramRoundTripMultipleFlows(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, nil)
	geyser := newFakeGeyser(t)
	m := newTestManager(ctx, t)
	m.dialUDP = dialToGeyser(geyser)

	if err := m.Open(specFor(relay, "s1", "tok")); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}
	got := relay.waitAccepted(t)

	sendFlowDatagram(t, got.conn, 1, "hello-from-1")
	sendFlowDatagram(t, got.conn, 2, "hello-from-2")

	recvCtx, recvCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer recvCancel()
	replies := map[uint32]string{}
	for range 2 {
		data, err := got.conn.ReceiveDatagram(recvCtx)
		if err != nil {
			t.Fatalf("ReceiveDatagram: %v", err)
		}
		if len(data) < flowIDSize {
			t.Fatalf("reply frame too short: %d bytes", len(data))
		}
		id := binary.BigEndian.Uint32(data[:flowIDSize])
		replies[id] = string(data[flowIDSize:])
	}

	if replies[1] != "echo:hello-from-1" {
		t.Fatalf("flow 1 reply = %q, want echo:hello-from-1", replies[1])
	}
	if replies[2] != "echo:hello-from-2" {
		t.Fatalf("flow 2 reply = %q, want echo:hello-from-2", replies[2])
	}
}

// pump resolves the Geyser dial target the same way the TCP tunnel resolves
// the game server: the container name when gameHost returns one.
func TestPumpDialsContainerHostOverNetwork(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, nil)
	m := New(ctx, "0.0.0.0", func(serverID string) string { return "mcsd-" + serverID }, discardLogger())
	m.backoff = fastBackoff()

	addrCh := make(chan string, 1)
	m.dialUDP = func(_ context.Context, addr string) (net.Conn, error) {
		select {
		case addrCh <- addr:
		default:
		}
		return nil, errors.New("test: no real dial")
	}

	if err := m.Open(specFor(relay, "s1", "tok")); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}
	got := relay.waitAccepted(t)
	sendFlowDatagram(t, got.conn, 1, "x")

	select {
	case gotAddr := <-addrCh:
		if want := "mcsd-s1:19132"; gotAddr != want {
			t.Fatalf("dialUDP addr = %q, want %q", gotAddr, want)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("dialUDP was not called within 5s")
	}
}

// Without a configured network (gameHost empty), pump falls back to the
// gameBindIP-derived loopback, mirroring
// worker/internal/adapters/tunnel.Dialer.dialHost.
func TestPumpDialsLoopbackWithoutNetwork(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, nil)
	m := newTestManager(ctx, t) // gameBindIP "0.0.0.0", gameHost nil

	addrCh := make(chan string, 1)
	m.dialUDP = func(_ context.Context, addr string) (net.Conn, error) {
		select {
		case addrCh <- addr:
		default:
		}
		return nil, errors.New("test: no real dial")
	}

	if err := m.Open(specFor(relay, "s1", "tok")); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}
	got := relay.waitAccepted(t)
	sendFlowDatagram(t, got.conn, 1, "x")

	select {
	case gotAddr := <-addrCh:
		if want := "127.0.0.1:19132"; gotAddr != want {
			t.Fatalf("dialUDP addr = %q, want %q", gotAddr, want)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("dialUDP was not called within 5s")
	}
}

// A redial after a connection drop must discard all flow state: the same
// numeric flow id on the new connection dials a fresh local socket rather
// than reusing anything from the old connection (docs/app/BEDROCK_TUNNEL.md
// Section 5: flow ids are connection-scoped).
func TestFlowStateDiscardedOnRedial(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, nil)
	geyser := newFakeGeyser(t)
	m := newTestManager(ctx, t)

	var dialCount int32
	m.dialUDP = func(ctx context.Context, _ string) (net.Conn, error) {
		atomic.AddInt32(&dialCount, 1)
		return dialToGeyser(geyser)(ctx, "")
	}

	if err := m.Open(specFor(relay, "s1", "tok")); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}
	first := relay.waitAccepted(t)

	sendFlowDatagram(t, first.conn, 7, "first-connection")
	waitFor(t, func() bool { return atomic.LoadInt32(&dialCount) == 1 })

	// Simulate an ungraceful drop: the relay closes the connection out from
	// under the Worker.
	_ = first.conn.CloseWithError(0, "simulated drop")

	second := relay.waitAccepted(t)
	if second.conn == first.conn {
		t.Fatal("expected a new connection on redial")
	}

	// The same numeric flow id on the new connection must trigger a fresh dial,
	// not reuse the first connection's (already-torn-down) flow socket.
	sendFlowDatagram(t, second.conn, 7, "second-connection")
	waitFor(t, func() bool { return atomic.LoadInt32(&dialCount) == 2 })
}

// countingCloseConn wraps a net.Conn to record every Close call, so a test can
// assert a per-flow socket was actually torn down.
type countingCloseConn struct {
	net.Conn
	closes *int32
}

func (c *countingCloseConn) Close() error {
	atomic.AddInt32(c.closes, 1)
	return c.Conn.Close()
}

// Close gracefully closes the QUIC connection (observed by the relay as
// CONNECTION_CLOSE) and every per-flow local UDP socket
// (docs/app/BEDROCK_TUNNEL.md Section 3).
func TestCloseGracefullyClosesConnectionAndFlowSockets(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, nil)
	geyser := newFakeGeyser(t)
	m := newTestManager(ctx, t)

	var opened, closed int32
	m.dialUDP = func(ctx context.Context, _ string) (net.Conn, error) {
		atomic.AddInt32(&opened, 1)
		conn, err := dialToGeyser(geyser)(ctx, "")
		if err != nil {
			return nil, err
		}
		return &countingCloseConn{Conn: conn, closes: &closed}, nil
	}

	if err := m.Open(specFor(relay, "s1", "tok")); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}
	got := relay.waitAccepted(t)

	sendFlowDatagram(t, got.conn, 1, "hi")
	waitFor(t, func() bool { return atomic.LoadInt32(&opened) == 1 })

	m.Close("s1")

	waitConnDone(t, got.conn)
	waitFor(t, func() bool { return atomic.LoadInt32(&closed) == 1 })
}

// Worker shutdown (the Manager's baseCtx ending) gracefully closes every open
// tunnel, the same as an explicit Close (docs/app/BEDROCK_TUNNEL.md Section 3).
func TestBaseCtxCancelGracefullyClosesTunnel(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())

	relay := newFakeRelay(t, nil)
	m := newTestManager(ctx, t)

	if err := m.Open(specFor(relay, "s1", "tok")); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}
	got := relay.waitAccepted(t)

	cancel() // simulate Worker shutdown (main.go's sigCtx cancellation).

	waitConnDone(t, got.conn)
}

// A repeated Open with an unchanged spec is idempotent: it does not tear down
// or redial a healthy tunnel (docs/app/BEDROCK_TUNNEL.md Section 3).
func TestOpenIdempotentSameSpec(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, nil)
	m := newTestManager(ctx, t)

	spec := specFor(relay, "s1", "tok")
	if err := m.Open(spec); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}
	relay.waitAccepted(t)

	if err := m.Open(spec); err != nil {
		t.Fatalf("second Open = %v, want nil", err)
	}

	select {
	case <-relay.accepted:
		t.Fatal("idempotent Open triggered a second dial")
	case <-time.After(200 * time.Millisecond):
	}
	if got := relay.helloCount(); got != 1 {
		t.Fatalf("relay saw %d TunnelHello, want 1 (idempotent Open must not redial)", got)
	}
}

// An Open with a spec that differs from an already-open tunnel for the same
// server (e.g. a fresh token after a restart) supersedes it: the old
// connection is closed and a new one dialed with the new credential.
func TestOpenReplacesOnSpecChange(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, nil)
	m := newTestManager(ctx, t)

	spec1 := specFor(relay, "s1", "tok-1")
	if err := m.Open(spec1); err != nil {
		t.Fatalf("Open(spec1) = %v, want nil", err)
	}
	first := relay.waitAccepted(t)

	spec2 := spec1
	spec2.Token = "tok-2"
	if err := m.Open(spec2); err != nil {
		t.Fatalf("Open(spec2) = %v, want nil", err)
	}

	waitConnDone(t, first.conn)
	second := relay.waitAccepted(t)
	if second.hello.GetToken() != "tok-2" {
		t.Fatalf("new tunnel token = %q, want tok-2", second.hello.GetToken())
	}
}

// A rejected handshake (the relay's stale-bind window, up to ~15s,
// docs/app/BEDROCK_TUNNEL.md Section 3.1) is retried with backoff using the
// SAME token, and a later acceptance succeeds.
func TestRetryBackoffOnRejectedRedial(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var attempts int32
	relay := newFakeRelay(t, func(*bedrocktunnelv1.TunnelHello) (bool, string) {
		if atomic.AddInt32(&attempts, 1) < 3 {
			return false, "stale bind"
		}
		return true, ""
	})
	m := newTestManager(ctx, t)

	if err := m.Open(specFor(relay, "s1", "tok")); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}

	got := relay.waitAccepted(t)
	if got.hello.GetToken() != "tok" {
		t.Fatalf("accepted hello token = %q, want tok (same credential every redial)", got.hello.GetToken())
	}
	if n := atomic.LoadInt32(&attempts); n < 3 {
		t.Fatalf("relay saw %d attempts, want at least 3 (2 rejections then an accept)", n)
	}
}

// After a successful handshake whose connection drops immediately (pump
// returns), the run loop must apply a backoff delay before redialing
// rather than looping with zero delay — the fix for the duel hot-loop
// described in issue #1988. Mirrors session.Runner.Run, which delays
// before every redial regardless of whether the previous attempt succeeded.
func TestRedialAfterPumpDropAppliesBackoff(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, nil)
	m := newTestManager(ctx, t)
	m.randFloat = func() float64 { return 0.5 }

	// afterFunc is called by the run loop's unified backoff path. Capture
	// every delay it receives after the connection drops so we can verify
	// the delay is applied, with the correct value, on the pump-return
	// path — not just the handshake-error path.
	var dropped atomic.Bool
	afterCalled := make(chan time.Duration, 8)
	m.afterFunc = func(d time.Duration) <-chan time.Time {
		if dropped.Load() {
			afterCalled <- d
		}
		ch := make(chan time.Time, 1)
		ch <- time.Now() // unblock immediately
		return ch
	}

	if err := m.Open(specFor(relay, "s1", "tok")); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}

	// First connection: accept, then drop from the relay side so pump
	// returns and the run loop must redial.
	first := relay.waitAccepted(t)
	dropped.Store(true)
	_ = first.conn.CloseWithError(0, "simulated drop")

	// The run loop must call afterFunc with the correct backoff delay
	// after the pump returns. With fastBackoff (Initial=1ms) and
	// randFloat=0.5, Delay(0, 0.5) = 500ns (attempt starts at 0; an instant
	// pump drop does not reset it, but it was already 0).
	want := m.backoff.Delay(0, 0.5)
	select {
	case got := <-afterCalled:
		if got != want {
			t.Fatalf("backoff delay after pump drop = %v, want %v", got, want)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("afterFunc was not called after pump drop (issue #1988)")
	}

	// The redial must succeed after the delay.
	second := relay.waitAccepted(t)
	if second.hello.GetToken() != first.hello.GetToken() {
		t.Fatalf("redial token = %q, want %q", second.hello.GetToken(), first.hello.GetToken())
	}
}

// Repeated instant displacements (pump returns well under minStableDuration)
// must escalate the backoff delay rather than staying at Delay(0) forever
// (issue #2153: two workers with the same token displacing each other).
func TestInstantDisplacementEscalatesBackoff(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, nil)
	geyser := newFakeGeyser(t)
	m := newTestManager(ctx, t)
	m.dialUDP = dialToGeyser(geyser)
	m.randFloat = func() float64 { return 0.5 }

	afterCalled := make(chan time.Duration, 16)
	m.afterFunc = func(d time.Duration) <-chan time.Time {
		afterCalled <- d
		ch := make(chan time.Time, 1)
		ch <- time.Now()
		return ch
	}

	if err := m.Open(specFor(relay, "s1", "tok")); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}

	// Drop three successive connections; the pump runs for less than
	// minStableDuration (fastBackoff's default) each time, so the backoff
	// must escalate. Send a datagram before each drop to prove the pump is
	// running (the Worker finished handshake and entered pump).
	for i := range 3 {
		conn := relay.waitAccepted(t)
		sendFlowDatagram(t, conn.conn, 1, "ping")
		_ = conn.conn.CloseWithError(0, "simulated displacement")

		want := m.backoff.Delay(i, 0.5)
		select {
		case got := <-afterCalled:
			if got != want {
				t.Fatalf("drop %d: backoff delay = %v, want %v (attempt %d)", i, got, want, i)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("drop %d: afterFunc not called within 5s", i)
		}
	}
}

// A connection that survives longer than minStableDuration resets the backoff
// counter, so the next drop starts the sequence over (issue #2153).
func TestStableConnectionResetsBackoff(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, nil)
	geyser := newFakeGeyser(t)
	m := newTestManager(ctx, t)
	m.dialUDP = dialToGeyser(geyser)
	m.randFloat = func() float64 { return 0.5 }
	// Set a short stable threshold so the test can sleep past it.
	m.minStableDuration = 50 * time.Millisecond

	afterCalled := make(chan time.Duration, 16)
	m.afterFunc = func(d time.Duration) <-chan time.Time {
		afterCalled <- d
		ch := make(chan time.Time, 1)
		ch <- time.Now()
		return ch
	}

	if err := m.Open(specFor(relay, "s1", "tok")); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}

	// First connection: instant drop → attempt escalates (delay at attempt 0).
	first := relay.waitAccepted(t)
	sendFlowDatagram(t, first.conn, 1, "ping")
	_ = first.conn.CloseWithError(0, "instant drop")

	select {
	case got := <-afterCalled:
		if want := m.backoff.Delay(0, 0.5); got != want {
			t.Fatalf("first drop: backoff = %v, want %v", got, want)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("first drop: afterFunc not called")
	}

	// Second connection: let the pump run past minStableDuration so the
	// backoff resets on drop.
	second := relay.waitAccepted(t)
	sendFlowDatagram(t, second.conn, 1, "ping")
	time.Sleep(100 * time.Millisecond) // > 50ms threshold
	_ = second.conn.CloseWithError(0, "drop after stable")

	select {
	case got := <-afterCalled:
		// After a stable connection, attempt resets to 0.
		if want := m.backoff.Delay(0, 0.5); got != want {
			t.Fatalf("stable drop: backoff = %v, want %v (should have reset)", got, want)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("stable drop: afterFunc not called")
	}
}

// An always-rejecting relay does not make the Worker give up: it keeps
// retrying with backoff indefinitely while the tunnel is still open
// (docs/app/BEDROCK_TUNNEL.md Section 3.1).
func TestRejectionRetriesIndefinitely(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	relay := newFakeRelay(t, func(*bedrocktunnelv1.TunnelHello) (bool, string) {
		return false, "always rejected"
	})
	m := newTestManager(ctx, t)

	if err := m.Open(specFor(relay, "s1", "tok")); err != nil {
		t.Fatalf("Open = %v, want nil", err)
	}

	waitFor(t, func() bool { return relay.helloCount() >= 3 })
}
