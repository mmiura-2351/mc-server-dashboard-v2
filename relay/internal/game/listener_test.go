package game

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/domain/ingress"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/mc"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/metrics"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/tunnel"
)

// TestAwaitTunnelExpiredDialBackDoesNotHang is the regression for the
// expired-token leak: the token's TTL elapses while the waiter is still inside
// its dial-back window, then a dial-back arrives. Deliver must reject it
// (expired) without consuming the waiter entry, so awaitTunnel reclaims the
// entry via Cancel and returns ok=false instead of blocking on its channel
// forever. We assert awaitTunnel does not hang (which would leak the goroutine,
// the player conn, and the IP-cap slot).
func TestAwaitTunnelExpiredDialBackDoesNotHang(t *testing.T) {
	// now is read by the table's clock from the awaitTunnel goroutine while the
	// test advances it; guard it with an atomic to stay race-free.
	var now atomic.Int64
	now.Store(time.Unix(0, 0).UnixNano())
	tokens := tunnel.NewTokenTable(10*time.Second, func() time.Time { return time.Unix(0, now.Load()) })
	l := &Listener{tokens: tokens, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}

	// Cancellable context drives awaitTunnel's timeout path quickly without
	// waiting out the 10 s dial-back timer.
	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan struct{})
	go func() {
		defer close(done)
		if _, ok := l.awaitTunnel(ctx, "tok"); ok {
			t.Error("awaitTunnel should not report success for an expired/cancelled wait")
		}
	}()

	// Give the goroutine time to register the waiter (a synchronous map insert at
	// awaitTunnel entry), then expire the token and present a late dial-back.
	time.Sleep(50 * time.Millisecond)
	now.Add(int64(11 * time.Second))
	dialBack, _ := net.Pipe()
	defer func() { _ = dialBack.Close() }()
	if tokens.Deliver("tok", dialBack) {
		t.Fatal("expired Deliver should not match the waiter")
	}

	// Trigger awaitTunnel's timeout path; with the fix the waiter entry survived
	// the expired Deliver, so Cancel reclaims it and the goroutine exits.
	cancel()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("awaitTunnel hung on an expired dial-back (goroutine/conn leak)")
	}
}

// TestAwaitTunnelNilConnFromSweptChannel is the regression for issue #1045:
// when the token sweep (sweepExpired) closes the waiter channel before the
// dial-back timer fires, <-ch yields nil. awaitTunnel must detect this and
// return (nil, false) instead of (nil, true) — the latter causes a nil
// dereference panic in both callers.
func TestAwaitTunnelNilConnFromSweptChannel(t *testing.T) {
	// A short TTL so the sweep finds the entry expired immediately.
	var now atomic.Int64
	now.Store(time.Unix(0, 0).UnixNano())
	tokens := tunnel.NewTokenTable(1*time.Millisecond, func() time.Time { return time.Unix(0, now.Load()) })
	l := &Listener{tokens: tokens, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}

	ctx := context.Background()
	done := make(chan struct{})
	var gotConn net.Conn
	var gotOK bool

	go func() {
		defer close(done)
		gotConn, gotOK = l.awaitTunnel(ctx, "tok-sweep")
	}()

	// Let the goroutine register the waiter.
	time.Sleep(50 * time.Millisecond)

	// Advance the clock past TTL and trigger the sweep — this closes the channel.
	now.Add(int64(10 * time.Second))
	tokens.SweepExpiredForTest()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("awaitTunnel hung after sweep closed the waiter channel")
	}

	if gotOK {
		t.Error("awaitTunnel must return ok=false when the channel yields nil")
	}
	if gotConn != nil {
		t.Error("awaitTunnel must return nil conn when the channel is closed")
	}
}

// TestDrainReturnsImmediatelyWhenIdle proves Drain returns true immediately
// when there are no in-flight handle goroutines.
func TestDrainReturnsImmediatelyWhenIdle(t *testing.T) {
	l := &Listener{}
	if !l.Drain(time.Second) {
		t.Error("Drain should return true when no goroutines are in flight")
	}
}

// TestDrainWaitsForInflight proves Drain blocks until all in-flight handle
// goroutines finish and returns true.
func TestDrainWaitsForInflight(t *testing.T) {
	l := &Listener{}
	l.inflight.Add(1)

	done := make(chan bool, 1)
	go func() { done <- l.Drain(2 * time.Second) }()

	// Drain should be blocked because the WaitGroup counter is 1.
	select {
	case <-done:
		t.Fatal("Drain returned before the in-flight goroutine finished")
	case <-time.After(50 * time.Millisecond):
	}

	// Simulate the goroutine finishing.
	l.inflight.Done()

	select {
	case ok := <-done:
		if !ok {
			t.Error("Drain should return true when goroutines finish within the deadline")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Drain did not return after the in-flight goroutine finished")
	}
}

// TestDrainTimesOut proves Drain returns false when in-flight goroutines do
// not finish within the deadline.
func TestDrainTimesOut(t *testing.T) {
	l := &Listener{}
	l.inflight.Add(1)
	defer l.inflight.Done() // clean up so the test does not leak

	if l.Drain(50 * time.Millisecond) {
		t.Error("Drain should return false when the deadline elapses with goroutines still in flight")
	}
}

// deadlineConn records the write deadline set on it and discards writes, so the
// disconnect-path deadline (issue #971) is observable.
type deadlineConn struct {
	net.Conn
	writeDeadline atomic.Pointer[time.Time]
	closed        atomic.Bool
}

func (c *deadlineConn) Write(b []byte) (int, error) { return len(b), nil }
func (c *deadlineConn) Close() error                { c.closed.Store(true); return nil }
func (c *deadlineConn) SetWriteDeadline(t time.Time) error {
	c.writeDeadline.Store(&t)
	return nil
}

// TestDisconnectSetsWriteDeadline proves disconnect bounds the Login Disconnect
// write so a stalled client cannot pin the goroutine (issue #971).
func TestDisconnectSetsWriteDeadline(t *testing.T) {
	l := &Listener{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	conn := &deadlineConn{}

	before := time.Now()
	l.disconnect(conn, "go away")
	after := time.Now()

	d := conn.writeDeadline.Load()
	if d == nil {
		t.Fatal("disconnect did not set a write deadline")
	}
	if d.Before(before.Add(disconnectWriteTimeout)) || d.After(after.Add(disconnectWriteTimeout)) {
		t.Errorf("write deadline %v not within [now+%v]", *d, disconnectWriteTimeout)
	}
	if !conn.closed.Load() {
		t.Error("disconnect must close the connection")
	}
}

// --- fakeResolver ---

type fakeResolver struct {
	result ingress.ResolveResult
	err    error
	domain string
	calls  atomic.Int32
}

func (f *fakeResolver) ResolveJoin(_ context.Context, _, _ string, _ ingress.Intent) (ingress.ResolveResult, error) {
	f.calls.Add(1)
	return f.result, f.err
}

func (f *fakeResolver) BaseDomain() string { return f.domain }

// --- fakeSessionRecorder ---

type fakeSessionRecorder struct {
	started int
	ended   int
}

func (f *fakeSessionRecorder) Start(_, _, _, _, _ string, _ ingress.Source) string {
	f.started++
	return "sess-1"
}
func (f *fakeSessionRecorder) End(_ string) { f.ended++ }

// --- test helpers ---

// handshakePacket builds a valid Minecraft handshake packet.
func handshakePacket(protocol int32, addr string, port uint16, next int32) []byte {
	var body []byte
	body = appendTestVarInt(body, protocol)
	body = appendTestString(body, addr)
	body = append(body, byte(port>>8), byte(port))
	body = appendTestVarInt(body, next)
	return frameTestPacket(0x00, body)
}

// loginStartPacket builds a Login Start packet (protocol 765 form: name + 16-byte UUID).
func loginStartPacket(name string) []byte {
	body := appendTestString(nil, name)
	body = append(body, make([]byte, 16)...) // 16-byte UUID
	return frameTestPacket(0x00, body)
}

func frameTestPacket(id int32, body []byte) []byte {
	inner := appendTestVarInt(nil, id)
	inner = append(inner, body...)
	out := appendTestVarInt(nil, int32(len(inner)))
	return append(out, inner...)
}

func appendTestVarInt(dst []byte, v int32) []byte {
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

func appendTestString(dst []byte, s string) []byte {
	dst = appendTestVarInt(dst, int32(len(s)))
	return append(dst, s...)
}

// --- fetchStatusThroughTunnel tests ---

// TestFetchStatusThroughTunnelHappyPath delivers a tunnel connection that speaks
// the status exchange protocol and verifies the JSON is returned.
func TestFetchStatusThroughTunnelHappyPath(t *testing.T) {
	tokens := tunnel.NewTokenTable(10*time.Second, time.Now)
	l := &Listener{tokens: tokens, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}

	const wantJSON = `{"description":{"text":"hello"}}`
	hs := mc.Handshake{
		ProtocolVersion: 765,
		ServerAddress:   "amber.mc.example.com",
		Port:            25565,
		NextState:       mc.NextStateStatus,
		Raw:             handshakePacket(765, "amber.mc.example.com", 25565, 1),
	}

	workerSide, relaySide := net.Pipe()

	// Simulate the worker (tunnel) side in a goroutine: read "OK\n", read the
	// replayed handshake + status request, write a Status Response.
	go func() {
		defer func() { _ = workerSide.Close() }()
		br := bufio.NewReader(workerSide)

		// Read the "OK\n" ack from ConfirmAndAttach.
		ack := make([]byte, 3)
		if _, err := io.ReadFull(br, ack); err != nil || string(ack) != "OK\n" {
			t.Errorf("expected OK ack, got %q (err %v)", ack, err)
			return
		}

		// Read and discard the replayed handshake packet.
		readTestPacket(t, br)
		// Read and discard the status request packet.
		readTestPacket(t, br)

		// Write a Status Response back.
		if _, err := workerSide.Write(mc.StatusResponsePacket(wantJSON)); err != nil {
			t.Errorf("write status response: %v", err)
		}
	}()

	// Deliver the relay side concurrently: fetchStatusThroughTunnel calls
	// awaitTunnel which calls Register then blocks; the Deliver must happen after
	// the Register. A short sleep lets the goroutine reach the blocking select.
	go func() {
		time.Sleep(50 * time.Millisecond)
		tokens.Deliver("tok-status", relaySide)
	}()

	gotJSON, ok := l.fetchStatusThroughTunnel(context.Background(), hs, "tok-status")
	if !ok {
		t.Fatal("fetchStatusThroughTunnel returned ok=false")
	}
	if gotJSON != wantJSON {
		t.Errorf("got %q, want %q", gotJSON, wantJSON)
	}
}

// readTestPacket reads one length-prefixed Minecraft packet from r and returns
// its body (excluding the length prefix).
func readTestPacket(t *testing.T, r *bufio.Reader) []byte {
	t.Helper()
	length, err := readTestVarInt(r)
	if err != nil {
		t.Fatalf("readTestPacket: read length: %v", err)
	}
	body := make([]byte, length)
	if _, err := io.ReadFull(r, body); err != nil {
		t.Fatalf("readTestPacket: read body: %v", err)
	}
	return body
}

func readTestVarInt(r *bufio.Reader) (int32, error) {
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

// --- resolveStatus tests ---

// TestResolveStatusAPIError verifies that when the API returns an error (and no
// cache), resolveStatus returns the "unavailable" synthesized response.
func TestResolveStatusAPIError(t *testing.T) {
	resolver := &fakeResolver{err: errors.New("connection refused")}
	cache := NewStatusCache(5*time.Second, 1024, time.Now)
	l := &Listener{
		resolver: resolver,
		cache:    cache,
		logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	hs := mc.Handshake{
		ProtocolVersion: 765,
		ServerAddress:   "amber.mc.example.com",
		NextState:       mc.NextStateStatus,
	}

	got := l.resolveStatus(context.Background(), hs, "amber", "10.0.0.1")
	want := mc.SynthesizedStatus(mc.UnavailableMOTD)
	if got != want {
		t.Errorf("resolveStatus on API error:\n  got  %q\n  want %q", got, want)
	}
}

// --- handleLogin tests ---

// TestHandleLoginStoppedDecision verifies that a login to a stopped server sends
// a Login Disconnect with the stopped MOTD and closes the connection.
func TestHandleLoginStoppedDecision(t *testing.T) {
	const displayName = "Test Server"
	resolver := &fakeResolver{
		result: ingress.ResolveResult{
			Decision:    ingress.DecisionStopped,
			DisplayName: displayName,
		},
		domain: "mc.example.com",
	}
	tokens := tunnel.NewTokenTable(10*time.Second, time.Now)
	cache := NewStatusCache(5*time.Second, 1024, time.Now)
	caps := ipcaps.NewIPCaps(32, 10, 0, time.Now, nil)
	sessions := &fakeSessionRecorder{}
	l := &Listener{
		resolver: resolver,
		tokens:   tokens,
		cache:    cache,
		caps:     caps,
		sessions: sessions,
		logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	playerSide, relaySide := net.Pipe()
	defer func() { _ = playerSide.Close() }()

	// Build a valid handshake (login, next_state=2) and login start.
	hsBytes := handshakePacket(765, "stopped.mc.example.com", 25565, 2)
	lsBytes := loginStartPacket("Steve")
	hs, err := mc.ReadHandshake(bufio.NewReaderSize(
		strings.NewReader(string(hsBytes)), mc.MaxPreRouteBytes))
	if err != nil {
		t.Fatalf("parse handshake: %v", err)
	}

	// Simulate writing the login start into a bufio.Reader that handleLogin reads.
	r := bufio.NewReaderSize(strings.NewReader(string(lsBytes)), mc.MaxPreRouteBytes)

	go l.handleLogin(context.Background(), relaySide, r, hs, "stopped", "10.0.0.1")

	// The player should receive a Login Disconnect packet with the stopped MOTD.
	_ = playerSide.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf := make([]byte, 1024)
	n, err := playerSide.Read(buf)
	if err != nil && err != io.EOF {
		t.Fatalf("player read: %v", err)
	}
	got := string(buf[:n])
	wantMOTD := mc.StoppedMOTD(displayName)
	if !strings.Contains(got, wantMOTD) {
		t.Errorf("Login Disconnect should contain %q, got %q", wantMOTD, got)
	}
	if sessions.started != 0 {
		t.Errorf("no session should be started for a stopped server, got %d", sessions.started)
	}
}

// --- handleStatus rate-cap tests ---

// statusHandshake returns a valid status handshake (next_state=1) and its
// parsed form for the given slug under baseDomain.
func statusHandshake(t *testing.T, slug, baseDomain string) ([]byte, mc.Handshake) {
	t.Helper()
	addr := slug + "." + baseDomain
	raw := handshakePacket(765, addr, 25565, 1)
	hs, err := mc.ReadHandshake(bufio.NewReaderSize(
		strings.NewReader(string(raw)), mc.MaxPreRouteBytes))
	if err != nil {
		t.Fatalf("parse handshake: %v", err)
	}
	return raw, hs
}

// statusRequestPacket returns a framed Status Request (id 0x00, empty body).
func statusRequestPacket() []byte { return frameTestPacket(0x00, nil) }

// pingPacket builds a Ping packet (id 0x01) with an int64 payload.
func pingPacket(payload int64) []byte {
	body := make([]byte, 8)
	for i := 0; i < 8; i++ {
		body[7-i] = byte(payload >> (8 * i))
	}
	return frameTestPacket(0x01, body)
}

// TestHandleStatusCacheMissRateCapped verifies that a status-cache miss from
// an IP that has exhausted its join-rate budget is silently dropped without
// calling ResolveJoin.
func TestHandleStatusCacheMissRateCapped(t *testing.T) {
	resolver := &fakeResolver{
		result: ingress.ResolveResult{Decision: ingress.DecisionStopped, DisplayName: "X"},
		domain: "mc.example.com",
	}
	cache := NewStatusCache(5*time.Second, 1024, time.Now)
	caps := ipcaps.NewIPCaps(32, 1, 0, time.Now, nil) // 1 join/s

	l := &Listener{
		resolver: resolver,
		cache:    cache,
		caps:     caps,
		logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	// Burn the single allowed join for this IP.
	if !caps.AllowJoin("10.0.0.1") {
		t.Fatal("first AllowJoin should succeed")
	}

	_, hs := statusHandshake(t, "amber", "mc.example.com")

	playerSide, relaySide := net.Pipe()
	defer func() { _ = playerSide.Close() }()

	// Write a valid Status Request so handleStatus gets past the read.
	go func() {
		_, _ = playerSide.Write(statusRequestPacket())
	}()

	r := bufio.NewReaderSize(relaySide, mc.MaxPreRouteBytes)
	l.handleStatus(context.Background(), relaySide, r, hs, "amber", "10.0.0.1")

	// The connection should be closed silently (EOF on player side).
	_ = playerSide.SetReadDeadline(time.Now().Add(time.Second))
	buf := make([]byte, 1)
	_, err := playerSide.Read(buf)
	if !errors.Is(err, io.EOF) && !errors.Is(err, io.ErrClosedPipe) {
		t.Errorf("expected EOF/closed pipe after rate-cap drop, got %v", err)
	}

	if c := resolver.calls.Load(); c != 0 {
		t.Errorf("resolver should not have been called, got %d calls", c)
	}
}

// TestHandleStatusCacheHitNotRateCapped verifies that cached status responses
// are served even when the IP has exhausted its join-rate budget (cache hits
// are not gated).
func TestHandleStatusCacheHitNotRateCapped(t *testing.T) {
	const cachedJSON = `{"description":{"text":"cached"}}`

	resolver := &fakeResolver{domain: "mc.example.com"}
	cache := NewStatusCache(5*time.Second, 1024, time.Now)
	cache.Put("amber", cachedJSON)
	caps := ipcaps.NewIPCaps(32, 1, 0, time.Now, nil) // 1 join/s

	l := &Listener{
		resolver: resolver,
		cache:    cache,
		caps:     caps,
		logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	// Exhaust the join budget.
	caps.AllowJoin("10.0.0.1")

	_, hs := statusHandshake(t, "amber", "mc.example.com")

	playerSide, relaySide := net.Pipe()
	defer func() { _ = playerSide.Close() }()

	// Write Status Request, then a Ping so handleStatus can complete the full
	// status exchange.
	go func() {
		_, _ = playerSide.Write(statusRequestPacket())
		// Read the Status Response, then send a Ping, then read the Pong so
		// the write does not block until the deadline expires.
		br := bufio.NewReader(playerSide)
		readTestPacket(t, br)
		_, _ = playerSide.Write(pingPacket(42))
		readTestPacket(t, br)
	}()

	r := bufio.NewReaderSize(relaySide, mc.MaxPreRouteBytes)
	l.handleStatus(context.Background(), relaySide, r, hs, "amber", "10.0.0.1")

	if c := resolver.calls.Load(); c != 0 {
		t.Errorf("resolver should not have been called for a cache hit, got %d calls", c)
	}
}

// TestHandleStatusCacheMissAllowedResolves verifies that a status-cache miss
// with sufficient join-rate budget proceeds to resolve.
func TestHandleStatusCacheMissAllowedResolves(t *testing.T) {
	resolver := &fakeResolver{
		result: ingress.ResolveResult{Decision: ingress.DecisionStopped, DisplayName: "X"},
		domain: "mc.example.com",
	}
	cache := NewStatusCache(5*time.Second, 1024, time.Now)
	caps := ipcaps.NewIPCaps(32, 100, 0, time.Now, nil) // generous budget

	l := &Listener{
		resolver: resolver,
		cache:    cache,
		caps:     caps,
		logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	_, hs := statusHandshake(t, "amber", "mc.example.com")

	playerSide, relaySide := net.Pipe()
	defer func() { _ = playerSide.Close() }()

	go func() {
		_, _ = playerSide.Write(statusRequestPacket())
		// Read the Status Response (synthesized stopped), then send a Ping,
		// then read the Pong so the write does not block until the deadline
		// expires.
		br := bufio.NewReader(playerSide)
		readTestPacket(t, br)
		_, _ = playerSide.Write(pingPacket(42))
		readTestPacket(t, br)
	}()

	r := bufio.NewReaderSize(relaySide, mc.MaxPreRouteBytes)
	l.handleStatus(context.Background(), relaySide, r, hs, "amber", "10.0.0.1")

	if c := resolver.calls.Load(); c != 1 {
		t.Errorf("resolver should have been called once, got %d calls", c)
	}
}

// --- Serve transient-accept-error retry tests ---

// scriptedListener is a fake net.Listener that returns a scripted sequence of
// (conn, error) results from Accept. Once the sequence is exhausted, Accept
// blocks until the test closes the done channel.
type scriptedListener struct {
	results []acceptResult
	idx     int
	done    chan struct{}
}

type acceptResult struct {
	conn net.Conn
	err  error
}

func (s *scriptedListener) Accept() (net.Conn, error) {
	if s.idx < len(s.results) {
		r := s.results[s.idx]
		s.idx++
		return r.conn, r.err
	}
	<-s.done
	return nil, net.ErrClosed
}

func (s *scriptedListener) Close() error {
	select {
	case <-s.done:
	default:
		close(s.done)
	}
	return nil
}

func (s *scriptedListener) Addr() net.Addr { return addrStr("127.0.0.1:0") }

// addrStr implements net.Addr for tests.
type addrStr string

func (a addrStr) Network() string { return "tcp" }
func (a addrStr) String() string  { return string(a) }

// TestServeRetriesTransientAcceptError verifies that a transient EMFILE error
// does not cause Serve to return; Serve must retry and only return on a
// permanent (non-transient) error.
func TestServeRetriesTransientAcceptError(t *testing.T) {
	permanent := errors.New("permanent listener failure")
	sl := &scriptedListener{
		results: []acceptResult{
			{nil, syscall.EMFILE}, // transient: should be retried
			{nil, syscall.ENFILE}, // transient: should be retried
			{nil, permanent},      // permanent: Serve must return this
		},
		done: make(chan struct{}),
	}

	l := &Listener{
		ln:     sl,
		logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	err := l.Serve(context.Background())
	if !errors.Is(err, permanent) {
		t.Errorf("Serve returned %v, want %v", err, permanent)
	}
	// Serve must have consumed all 3 results (retried the transient ones).
	if sl.idx != 3 {
		t.Errorf("Accept called %d times, want 3", sl.idx)
	}
}

// TestServeTransientRetryStopsOnCancel verifies that when Accept returns
// an endless stream of transient errors, cancelling the context causes Serve
// to return nil (clean shutdown).
func TestServeTransientRetryStopsOnCancel(t *testing.T) {
	// An "infinite" stream of EMFILE errors (100 is more than enough to
	// outlast the test timeout if the cancel did not work).
	results := make([]acceptResult, 100)
	for i := range results {
		results[i] = acceptResult{nil, syscall.EMFILE}
	}
	sl := &scriptedListener{
		results: results,
		done:    make(chan struct{}),
	}

	l := &Listener{
		ln:     sl,
		logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	ctx, cancel := context.WithCancel(context.Background())
	// Cancel after a short delay so the backoff sleep is interrupted.
	go func() {
		time.Sleep(20 * time.Millisecond)
		cancel()
	}()

	err := l.Serve(ctx)
	if err != nil {
		t.Errorf("Serve returned %v on ctx cancel, want nil", err)
	}
}

// TestServeReturnEndsEstablishedSession pins the shutdown contract Drain relies
// on (issue #3169): once Serve has returned — here on a fatal accept error, with
// the caller's ctx never cancelled — an established spliced session whose peers
// are both still connected is ended, so Drain completes promptly and the
// session's End is recorded through the handler's normal return path.
func TestServeReturnEndsEstablishedSession(t *testing.T) {
	reg := prometheus.NewRegistry()
	tokens := tunnel.NewTokenTable(10*time.Second, time.Now)
	sessions := &fakeSessionRecorder{}

	playerSide, relaySide := net.Pipe()  // conn (player)
	workerSide, tunnelSide := net.Pipe() // tconn (worker dial-back)
	defer func() { _ = playerSide.Close() }()
	defer func() { _ = workerSide.Close() }()

	sl := &scriptedListener{
		results: []acceptResult{{relaySide, nil}},
		done:    make(chan struct{}),
	}
	l := &Listener{
		ln: sl,
		resolver: &fakeResolver{
			result: ingress.ResolveResult{Decision: ingress.DecisionTunnel, Token: "tok", ServerID: "srv"},
			domain: "mc.example.com",
		},
		tokens:   tokens,
		caps:     ipcaps.NewIPCaps(32, 10, 0, time.Now, nil),
		sessions: sessions,
		metrics:  metrics.New(reg, "test"),
		logger:   nopLogger(),
	}

	serveErr := make(chan error, 1)
	go func() { serveErr <- l.Serve(context.Background()) }()

	// Player logs in; the worker dials back and then holds its connection open,
	// as a live Minecraft session does.
	hs := handshakePacket(765, "amber.mc.example.com", 25565, 2)
	login := loginStartPacket("Steve")
	if _, err := playerSide.Write(append(hs, login...)); err != nil {
		t.Fatalf("player write: %v", err)
	}
	go func() {
		for !tokens.Deliver("tok", tunnelSide) {
			time.Sleep(5 * time.Millisecond)
		}
	}()
	replayed := make([]byte, 3+len(hs)+len(login))
	if _, err := io.ReadFull(workerSide, replayed); err != nil {
		t.Fatalf("worker read replay: %v", err)
	}
	waitSeries(t, reg, "relay_game_active_sessions", nil, 1, 2*time.Second)

	// The listener fails: Serve returns without the caller cancelling anything.
	_ = sl.Close()
	select {
	case err := <-serveErr:
		if err == nil {
			t.Fatal("Serve should return the accept error")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Serve did not return after the listener closed")
	}

	start := time.Now()
	if !l.Drain(5 * time.Second) {
		t.Fatal("Drain timed out: the established session was not ended when Serve returned")
	}
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Errorf("Drain took %v; ending an established session must not wait on its peers", elapsed)
	}
	if sessions.started != 1 || sessions.ended != 1 {
		t.Errorf("sessions started=%d ended=%d, want 1 and 1", sessions.started, sessions.ended)
	}
	waitSeries(t, reg, "relay_game_active_sessions", nil, 0, 2*time.Second)

	// Both peers see their connection closed by the relay.
	for name, peer := range map[string]net.Conn{"player": playerSide, "worker": workerSide} {
		_ = peer.SetReadDeadline(time.Now().Add(2 * time.Second))
		if _, err := peer.Read(make([]byte, 1)); !errors.Is(err, io.EOF) {
			t.Errorf("%s peer read = %v, want EOF", name, err)
		}
	}
}

// --- pre-splice tunnel writes (issue #3287) ---

// stalledTunnel delivers one side of a net.Pipe as the dial-back for token once a
// waiter registers, and has the Worker side read exactly n bytes and then stop
// reading without closing: a Worker whose receive buffer is full. net.Pipe is
// unbuffered, so every relay write past those n bytes blocks. delivered is
// closed once the waiter holds the connection.
func stalledTunnel(t *testing.T, tokens *tunnel.TokenTable, token string, n int) (worker net.Conn, delivered <-chan struct{}) {
	t.Helper()
	workerSide, tunnelSide := net.Pipe()
	t.Cleanup(func() { _ = workerSide.Close() })
	ch := make(chan struct{})
	go func() {
		for !tokens.Deliver(token, tunnelSide) {
			time.Sleep(5 * time.Millisecond)
		}
		close(ch)
	}()
	go func() { _, _ = io.ReadFull(workerSide, make([]byte, n)) }()
	return workerSide, ch
}

// returnsWithin runs f and fails the test unless it returns within d. On a miss
// it closes unblock so the stuck f can exit before the test ends.
func returnsWithin(t *testing.T, d time.Duration, unblock net.Conn, f func()) {
	t.Helper()
	done := make(chan struct{})
	go func() {
		defer close(done)
		f()
	}()
	select {
	case <-done:
	case <-time.After(d):
		t.Errorf("did not return within %v", d)
		_ = unblock.Close()
		<-done
	}
}

// preSpliceLogin is a login whose pre-splice tunnel writes are, in order, the
// "OK\n" ack, the handshake, the Login Start, and the pipelined client bytes.
type preSpliceLogin struct {
	hs        mc.Handshake
	login     mc.LoginStart
	pipelined []byte
}

func newPreSpliceLogin() preSpliceLogin {
	return preSpliceLogin{
		hs:        mc.Handshake{ProtocolVersion: 765, Raw: handshakePacket(765, "amber.mc.example.com", 25565, 2)},
		login:     mc.LoginStart{Name: "Steve", Raw: loginStartPacket("Steve")},
		pipelined: []byte("pipelined"),
	}
}

// run calls spliceLogin with the player's pipelined bytes already buffered, as
// the pre-route read leaves them.
func (p preSpliceLogin) run(ctx context.Context, l *Listener, conn net.Conn) {
	r := bufio.NewReader(bytes.NewReader(p.pipelined))
	_, _ = r.Peek(len(p.pipelined))
	l.spliceLogin(ctx, conn, r, p.hs, p.login, "srv", "amber", "203.0.113.1", "tok")
}

// stallPoints is how many bytes a Worker reads before it stops, for a stall
// at each pre-splice write of the login.
func (p preSpliceLogin) stallPoints() map[string]int {
	return map[string]int{
		"ack":        0,
		"handshake":  3,
		"loginStart": 3 + len(p.hs.Raw),
		"pipelined":  3 + len(p.hs.Raw) + len(p.login.Raw),
	}
}

// TestSpliceLoginNonReadingTunnelTimesOut proves each pre-splice write of the
// login path is bounded by a write deadline: a Worker that stops reading
// cannot pin the handler (issue #3287). The ack's own deadline is
// tunnel.ConfirmAndAttach's and is pinned in that package.
func TestSpliceLoginNonReadingTunnelTimesOut(t *testing.T) {
	t.Parallel()
	p := newPreSpliceLogin()
	for name, n := range p.stallPoints() {
		if name == "ack" {
			continue
		}
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			tokens := tunnel.NewTokenTable(10*time.Second, time.Now)
			sessions := &fakeSessionRecorder{}
			l := &Listener{tokens: tokens, sessions: sessions, logger: nopLogger()}
			worker, _ := stalledTunnel(t, tokens, "tok", n)
			playerSide, relaySide := net.Pipe()
			defer func() { _ = playerSide.Close() }()

			returnsWithin(t, preRouteDeadline+2*time.Second, worker, func() {
				p.run(context.Background(), l, relaySide)
			})
			if sessions.started != 0 {
				t.Errorf("sessions started=%d, want 0: a stalled tunnel must not splice", sessions.started)
			}
		})
	}
}

// TestSpliceLoginNonReadingTunnelReturnsOnCancel proves a shutdown does not
// wait out the pre-splice write deadlines: cancelling the handler's context
// unblocks a write to a Worker that stopped reading (issue #3287).
func TestSpliceLoginNonReadingTunnelReturnsOnCancel(t *testing.T) {
	t.Parallel()
	p := newPreSpliceLogin()
	for name, n := range p.stallPoints() {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			tokens := tunnel.NewTokenTable(10*time.Second, time.Now)
			sessions := &fakeSessionRecorder{}
			l := &Listener{tokens: tokens, sessions: sessions, logger: nopLogger()}
			worker, delivered := stalledTunnel(t, tokens, "tok", n)
			playerSide, relaySide := net.Pipe()
			defer func() { _ = playerSide.Close() }()

			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			go func() {
				<-delivered
				time.Sleep(50 * time.Millisecond) // let the write block
				cancel()
			}()
			returnsWithin(t, time.Second, worker, func() { p.run(ctx, l, relaySide) })
			if sessions.started != 0 {
				t.Errorf("sessions started=%d, want 0: a cancelled pre-splice must not splice", sessions.started)
			}
		})
	}
}

// recordingConn records the last write deadline set on the wrapped connection.
type recordingConn struct {
	net.Conn
	lastWriteDeadline atomic.Pointer[time.Time]
}

func (c *recordingConn) SetWriteDeadline(t time.Time) error {
	c.lastWriteDeadline.Store(&t)
	return c.Conn.SetWriteDeadline(t)
}

// TestSpliceLoginClearsTunnelWriteDeadline proves the pre-splice write deadline
// is cleared before the splice, which installs its own (issue #3287).
func TestSpliceLoginClearsTunnelWriteDeadline(t *testing.T) {
	p := newPreSpliceLogin()
	tokens := tunnel.NewTokenTable(10*time.Second, time.Now)
	sessions := &fakeSessionRecorder{}
	l := &Listener{tokens: tokens, sessions: sessions, logger: nopLogger()}

	workerSide, tunnelSide := net.Pipe()
	tconn := &recordingConn{Conn: tunnelSide}
	go func() {
		for !tokens.Deliver("tok", tconn) {
			time.Sleep(5 * time.Millisecond)
		}
	}()
	playerSide, relaySide := net.Pipe()
	// The Worker takes the whole pre-splice stream, then both peers hang up, so
	// the splice ends without moving a byte (and without touching deadlines).
	go func() {
		_, _ = io.ReadFull(workerSide, make([]byte, p.stallPoints()["pipelined"]+len(p.pipelined)))
		_ = workerSide.Close()
		_ = playerSide.Close()
	}()

	returnsWithin(t, 2*time.Second, workerSide, func() { p.run(context.Background(), l, relaySide) })
	if sessions.started != 1 {
		t.Fatalf("sessions started=%d, want 1", sessions.started)
	}
	d := tconn.lastWriteDeadline.Load()
	if d == nil {
		t.Fatal("no write deadline was set on the tunnel connection")
	}
	if !d.IsZero() {
		t.Errorf("write deadline %v left set at the splice", *d)
	}
}

// statusStallPoints is how many bytes a Worker reads before it stops, for a
// stall at each pre-read write of the status exchange: the "OK\n" ack, the
// handshake, then the Status Request.
func statusStallPoints(hs mc.Handshake) map[string]int {
	return map[string]int{
		"ack":           0,
		"handshake":     3,
		"statusRequest": 3 + len(hs.Raw),
	}
}

func statusFetchHandshake() mc.Handshake {
	return mc.Handshake{ProtocolVersion: 765, Raw: handshakePacket(765, "amber.mc.example.com", 25565, 1)}
}

// TestFetchStatusNonReadingTunnelTimesOut is the status path's counterpart of
// TestSpliceLoginNonReadingTunnelTimesOut (issue #3287).
func TestFetchStatusNonReadingTunnelTimesOut(t *testing.T) {
	t.Parallel()
	hs := statusFetchHandshake()
	for name, n := range statusStallPoints(hs) {
		if name == "ack" {
			continue
		}
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			tokens := tunnel.NewTokenTable(10*time.Second, time.Now)
			l := &Listener{tokens: tokens, logger: nopLogger()}
			worker, _ := stalledTunnel(t, tokens, "tok", n)

			returnsWithin(t, preRouteDeadline+2*time.Second, worker, func() {
				if _, ok := l.fetchStatusThroughTunnel(context.Background(), hs, "tok"); ok {
					t.Error("fetchStatusThroughTunnel succeeded against a stalled tunnel")
				}
			})
		})
	}
}

// TestFetchStatusNonReadingTunnelReturnsOnCancel is the status path's
// counterpart of TestSpliceLoginNonReadingTunnelReturnsOnCancel (issue #3287).
func TestFetchStatusNonReadingTunnelReturnsOnCancel(t *testing.T) {
	t.Parallel()
	hs := statusFetchHandshake()
	for name, n := range statusStallPoints(hs) {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			tokens := tunnel.NewTokenTable(10*time.Second, time.Now)
			l := &Listener{tokens: tokens, logger: nopLogger()}
			worker, delivered := stalledTunnel(t, tokens, "tok", n)

			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			go func() {
				<-delivered
				time.Sleep(50 * time.Millisecond) // let the write block
				cancel()
			}()
			returnsWithin(t, time.Second, worker, func() {
				if _, ok := l.fetchStatusThroughTunnel(ctx, hs, "tok"); ok {
					t.Error("fetchStatusThroughTunnel succeeded after cancellation")
				}
			})
		})
	}
}
