package bedrock

import (
	"bytes"
	"context"
	"errors"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/quic-go/quic-go"
	"google.golang.org/protobuf/proto"

	bedrocktunnelv1 "github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/genproto/mcsd/bedrocktunnel/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
)

// fakeValidator is a Validator test double that records every call and
// returns a canned (valid, err) pair. mu also guards valid/err (not just
// calls) so a test can flip the outcome mid-run via setValid while the
// listener's accept loop is concurrently calling ValidateBedrockTunnel in the
// background (#1565: a redial's hello must be rejected once the credential
// is no longer valid).
type fakeValidator struct {
	mu    sync.Mutex
	valid bool
	err   error
	calls []validateCall
}

type validateCall struct {
	serverID    string
	bedrockPort uint32
	token       string
}

func (f *fakeValidator) ValidateBedrockTunnel(_ context.Context, serverID string, bedrockPort uint32, token string) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, validateCall{serverID, bedrockPort, token})
	return f.valid, f.err
}

func (f *fakeValidator) lastCall() validateCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls[len(f.calls)-1]
}

// setValid updates valid under the same lock ValidateBedrockTunnel reads it
// with.
func (f *fakeValidator) setValid(valid bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.valid = valid
}

// newTestListener runs a Listener with unlimited pre-auth handshake caps; use
// newTestListenerWithCaps to exercise the cap itself. An optional deadline
// overrides the production handshake deadline; it is applied before Serve
// starts so the accept loop never races the write.
func newTestListener(t *testing.T, validator Validator, deadline ...time.Duration) (*Listener, func()) {
	t.Helper()
	return newTestListenerWithCaps(t, validator, ipcaps.NewIPCaps(0, 0, 0, nil, nil), deadline...)
}

func newTestListenerWithCaps(t *testing.T, validator Validator, preAuthCaps *ipcaps.IPCaps, deadline ...time.Duration) (*Listener, func()) {
	t.Helper()
	newCaps := func() *ipcaps.IPCaps { return ipcaps.NewIPCaps(0, 0, 0, nil, nil) }
	ln, err := NewListener("127.0.0.1:0", selfSignedTLS(t), validator, preAuthCaps, newCaps, testLogger())
	if err != nil {
		t.Fatalf("NewListener: %v", err)
	}
	if len(deadline) > 0 {
		ln.handshakeDeadline = deadline[0]
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		_ = ln.Serve(ctx)
		close(done)
	}()

	stop := func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Fatal("Serve did not return after ctx cancel")
		}
	}
	return ln, stop
}

// doHandshake dials ln, opens the first bidirectional stream, sends hello, and
// returns the connection plus the decoded ack.
func doHandshake(t *testing.T, ln *Listener, hello *bedrocktunnelv1.TunnelHello) (conn *quic.Conn, ack *bedrocktunnelv1.TunnelHelloAck) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	c := dialQUIC(ctx, t, ln.Addr().String())
	stream, err := c.OpenStreamSync(ctx)
	if err != nil {
		t.Fatalf("OpenStreamSync: %v", err)
	}

	data, err := proto.Marshal(hello)
	if err != nil {
		t.Fatalf("marshal TunnelHello: %v", err)
	}
	if err := writeFramed(stream, data); err != nil {
		t.Fatalf("writeFramed: %v", err)
	}

	ackData, err := readFramed(stream)
	if err != nil {
		t.Fatalf("readFramed ack: %v", err)
	}
	// A well-behaved Worker closes its side of the stream immediately after
	// reading the ack (proto/mcsd/bedrocktunnel/v1/bedrock_tunnel.proto:
	// "the stream is closed immediately after (both sides)").
	_ = stream.Close()

	var got bedrocktunnelv1.TunnelHelloAck
	if err := proto.Unmarshal(ackData, &got); err != nil {
		t.Fatalf("unmarshal ack: %v", err)
	}
	return c, &got
}

func TestListenerHandshakeAccept(t *testing.T) {
	validator := &fakeValidator{valid: true}
	ln, stop := newTestListener(t, validator)
	defer stop()

	// BedrockPort 0 makes the accepted handshake's bind OS-assigned, so the
	// test cannot collide with a busy port on a shared CI host.
	hello := &bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: 0, Token: "tok"}
	_, ack := doHandshake(t, ln, hello)

	if !ack.GetAccepted() {
		t.Fatalf("accepted = false, reject_reason = %q, want accepted", ack.GetRejectReason())
	}

	call := validator.lastCall()
	if call.serverID != "srv-1" || call.bedrockPort != 0 || call.token != "tok" {
		t.Errorf("ValidateBedrockTunnel called with %+v, want {srv-1 0 tok}", call)
	}
}

func TestListenerHandshakeRejectInvalidToken(t *testing.T) {
	validator := &fakeValidator{valid: false}
	ln, stop := newTestListener(t, validator)
	defer stop()

	hello := &bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: 25702, Token: "wrong"}
	conn, ack := doHandshake(t, ln, hello)

	if ack.GetAccepted() {
		t.Fatal("accepted = true, want false for an invalid token")
	}
	if ack.GetRejectReason() == "" {
		t.Error("expected a non-empty reject_reason")
	}

	select {
	case <-conn.Context().Done():
	case <-time.After(5 * time.Second):
		t.Fatal("expected the QUIC connection to be closed after rejection")
	}
}

func TestListenerHandshakeRejectValidatorError(t *testing.T) {
	validator := &fakeValidator{err: errors.New("api unavailable")}
	ln, stop := newTestListener(t, validator)
	defer stop()

	hello := &bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: 25703, Token: "tok"}
	conn, ack := doHandshake(t, ln, hello)

	if ack.GetAccepted() {
		t.Fatal("accepted = true, want false when validation RPC fails")
	}

	select {
	case <-conn.Context().Done():
	case <-time.After(5 * time.Second):
		t.Fatal("expected the QUIC connection to be closed after a validation error")
	}
}

func TestListenerAcceptStreamTimeout(t *testing.T) {
	validator := &fakeValidator{valid: true}
	// A short, injected accept deadline keeps this test off the production 5 s
	// const while still exercising the same AcceptStream-timeout path.
	deadline := 200 * time.Millisecond
	ln, stop := newTestListener(t, validator, deadline)
	defer stop()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn := dialQUIC(ctx, t, ln.Addr().String())

	// Never open a stream: the connection must still be closed by the relay's
	// AcceptStream deadline (the injected handshake deadline), not left to
	// quic-go's much longer idle timeout.
	select {
	case <-conn.Context().Done():
	case <-time.After(deadline + 3*time.Second):
		t.Fatal("expected the relay to close a connection that never opens a handshake stream")
	}
}

func TestListenerHandshakeRejectOnBindConflict(t *testing.T) {
	// Occupy a real UDP port so the Listener's bind attempt for the same port
	// fails.
	occupied, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = occupied.Close() }()
	port := uint32(occupied.LocalAddr().(*net.UDPAddr).Port)

	validator := &fakeValidator{valid: true}
	ln, stop := newTestListener(t, validator)
	defer stop()

	hello := &bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: port, Token: "tok"}
	_, ack := doHandshake(t, ln, hello)

	if ack.GetAccepted() {
		t.Fatal("accepted = true, want false when the declared port is already bound")
	}
}

// freeUDPPort grabs an OS-assigned loopback UDP port and releases it
// immediately so a test can declare a concrete bedrock_port for two
// successive hellos (needed for takeover: BedrockPort 0 would give each
// hello its own OS-assigned port, never colliding). Carries the same small
// TOCTOU risk as any "bind to :0, close, reuse" idiom.
func freeUDPPort(t *testing.T) uint32 {
	t.Helper()
	probe, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	port := uint32(probe.LocalAddr().(*net.UDPAddr).Port)
	if err := probe.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	return port
}

// sendAndExpectDatagram writes payload from a fresh UDP socket to udpAddr and
// asserts it is forwarded, unmodified, as a QUIC DATAGRAM on conn within
// timeout.
func sendAndExpectDatagram(t *testing.T, udpAddr *net.UDPAddr, conn *quic.Conn, payload []byte, timeout time.Duration) {
	t.Helper()
	client, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = client.Close() }()
	if _, err := client.WriteTo(payload, udpAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	frame, err := conn.ReceiveDatagram(ctx)
	if err != nil {
		t.Fatalf("ReceiveDatagram: %v", err)
	}
	if got := frame[FlowIDSize:]; !bytes.Equal(got, payload) {
		t.Errorf("forwarded payload = %q, want %q", got, payload)
	}
}

// TestListenerTakeoverDisplacesStaleConnection covers #1565's core
// acceptance criteria: a validated hello for a port already bound to
// another (here, still fully live -- the worse case, since an undetected-dead
// connection is strictly easier to displace) connection displaces it rather
// than being rejected, the displaced connection's QUIC connection is closed
// (no leaked goroutine, per TestTunnelCloseUnblocksRunAndFreesPort's proof of
// the underlying mechanism), and the port ends up serving traffic through the
// new connection.
func TestListenerTakeoverDisplacesStaleConnection(t *testing.T) {
	validator := &fakeValidator{valid: true}
	ln, stop := newTestListener(t, validator)
	defer stop()

	port := freeUDPPort(t)
	hello := &bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: port, Token: "tok"}

	oldConn, oldAck := doHandshake(t, ln, hello)
	if !oldAck.GetAccepted() {
		t.Fatalf("first handshake rejected: %q", oldAck.GetRejectReason())
	}
	udpAddr := &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: int(port)}

	// Confirm the old connection is actually serving traffic before it is
	// displaced.
	sendAndExpectDatagram(t, udpAddr, oldConn, []byte("pre-takeover"), 5*time.Second)

	// A second validated hello for the same port -- the redial -- must be
	// accepted (takeover), not rejected as a bind conflict.
	newConn, newAck := doHandshake(t, ln, hello)
	if !newAck.GetAccepted() {
		t.Fatalf("redial rejected: %q, want takeover to accept it", newAck.GetRejectReason())
	}

	// The old connection must be closed -- displaced, not left running
	// alongside the new one.
	select {
	case <-oldConn.Context().Done():
	case <-time.After(5 * time.Second):
		t.Fatal("expected the displaced connection to be closed")
	}

	// No datagram reaches the displaced connection post-takeover.
	staleCtx, staleCancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer staleCancel()
	if _, err := oldConn.ReceiveDatagram(staleCtx); err == nil {
		t.Error("expected the displaced connection to receive nothing after takeover")
	}

	// The port now serves traffic through the new connection.
	sendAndExpectDatagram(t, udpAddr, newConn, []byte("post-takeover"), 5*time.Second)
}

// TestListenerInvalidHelloDoesNotDisplace covers #1565's non-hijack
// requirement: an invalid hello for a bound port must be rejected exactly as
// today, without touching the existing tunnel -- takeover is gated by the
// same ValidateBedrockTunnel call as any other bind, not a new auth surface.
func TestListenerInvalidHelloDoesNotDisplace(t *testing.T) {
	validator := &fakeValidator{valid: true}
	ln, stop := newTestListener(t, validator)
	defer stop()

	port := freeUDPPort(t)
	goodHello := &bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: port, Token: "tok"}
	oldConn, oldAck := doHandshake(t, ln, goodHello)
	if !oldAck.GetAccepted() {
		t.Fatalf("first handshake rejected: %q", oldAck.GetRejectReason())
	}
	udpAddr := &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: int(port)}

	// Now the validator rejects: an attacker (or a misconfigured Worker)
	// without a valid credential for this port must not be able to displace
	// the live tunnel.
	validator.setValid(false)
	badHello := &bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: port, Token: "wrong"}
	_, badAck := doHandshake(t, ln, badHello)
	if badAck.GetAccepted() {
		t.Fatal("accepted = true, want false for an invalid token on a bound port")
	}

	// The original connection must still be alive and still serving traffic.
	select {
	case <-oldConn.Context().Done():
		t.Fatal("the original connection was closed by a rejected hello")
	default:
	}
	sendAndExpectDatagram(t, udpAddr, oldConn, []byte("still-alive"), 5*time.Second)
}

// gatedValidator blocks each ValidateBedrockTunnel call until gate is closed,
// signalling entry on started, so a test can hold a pre-auth handshake window
// open deliberately.
type gatedValidator struct {
	started chan struct{} // buffered; receives one signal per call entry
	gate    chan struct{} // close to release all blocked (and future) calls
}

func (g *gatedValidator) ValidateBedrockTunnel(ctx context.Context, _ string, _ uint32, _ string) (bool, error) {
	select {
	case g.started <- struct{}{}:
	default:
	}
	select {
	case <-g.gate:
		return true, nil
	case <-ctx.Done():
		return false, ctx.Err()
	}
}

func TestListenerPreAuthCapEnforcedAndReleased(t *testing.T) {
	validator := &gatedValidator{started: make(chan struct{}, 1), gate: make(chan struct{})}
	// One concurrent pre-auth handshake window per source IP.
	preAuthCaps := ipcaps.NewIPCaps(1, 0, 0, nil, nil)
	ln, stop := newTestListenerWithCaps(t, validator, preAuthCaps)
	defer stop()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	// First dial-out: occupies the single slot, held open by the blocked
	// validator.
	conn1 := dialQUIC(ctx, t, ln.Addr().String())
	stream1, err := conn1.OpenStreamSync(ctx)
	if err != nil {
		t.Fatalf("OpenStreamSync: %v", err)
	}
	data, err := proto.Marshal(&bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: 0, Token: "tok"})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if err := writeFramed(stream1, data); err != nil {
		t.Fatalf("writeFramed: %v", err)
	}
	// Wait until the relay is inside validation, so the slot is definitely
	// held before the second dial.
	select {
	case <-validator.started:
	case <-time.After(5 * time.Second):
		t.Fatal("validator was never entered")
	}

	// Second dial-out from the same IP: over the cap, closed silently before
	// any handshake processing.
	conn2 := dialQUIC(ctx, t, ln.Addr().String())
	select {
	case <-conn2.Context().Done():
	case <-time.After(4 * time.Second):
		t.Fatal("expected the over-cap connection to be closed")
	}

	// Release the validator; the first handshake resolves (accepted).
	close(validator.gate)
	ackData, err := readFramed(stream1)
	if err != nil {
		t.Fatalf("readFramed ack: %v", err)
	}
	_ = stream1.Close()
	var ack1 bedrocktunnelv1.TunnelHelloAck
	if err := proto.Unmarshal(ackData, &ack1); err != nil {
		t.Fatalf("unmarshal ack: %v", err)
	}
	if !ack1.GetAccepted() {
		t.Fatalf("first handshake rejected: %q", ack1.GetRejectReason())
	}

	// The slot must be released once the handshake resolved -- NOT held for
	// the (still running) first tunnel's lifetime: a third dial-out from the
	// same IP succeeds while conn1's tunnel is live.
	_, ack3 := doHandshake(t, ln, &bedrocktunnelv1.TunnelHello{ServerId: "srv-2", BedrockPort: 0, Token: "tok"})
	if !ack3.GetAccepted() {
		t.Fatalf("third handshake rejected (%q); pre-auth slot not released after resolution", ack3.GetRejectReason())
	}
}
