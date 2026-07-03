package bedrock

import (
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
// returns a canned (valid, err) pair.
type fakeValidator struct {
	valid bool
	err   error

	mu    sync.Mutex
	calls []validateCall
}

type validateCall struct {
	serverID    string
	bedrockPort uint32
	token       string
}

func (f *fakeValidator) ValidateBedrockTunnel(_ context.Context, serverID string, bedrockPort uint32, token string) (bool, error) {
	f.mu.Lock()
	f.calls = append(f.calls, validateCall{serverID, bedrockPort, token})
	f.mu.Unlock()
	return f.valid, f.err
}

func (f *fakeValidator) lastCall() validateCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls[len(f.calls)-1]
}

// newTestListener runs a Listener with unlimited pre-auth handshake caps; use
// newTestListenerWithCaps to exercise the cap itself.
func newTestListener(t *testing.T, validator Validator) (*Listener, func()) {
	t.Helper()
	return newTestListenerWithCaps(t, validator, ipcaps.NewIPCaps(0, 0, 0, nil, nil))
}

func newTestListenerWithCaps(t *testing.T, validator Validator, preAuthCaps *ipcaps.IPCaps) (*Listener, func()) {
	t.Helper()
	newCaps := func() *ipcaps.IPCaps { return ipcaps.NewIPCaps(0, 0, 0, nil, nil) }
	ln, err := NewListener("127.0.0.1:0", selfSignedTLS(t), validator, preAuthCaps, newCaps, testLogger())
	if err != nil {
		t.Fatalf("NewListener: %v", err)
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
	ln, stop := newTestListener(t, validator)
	defer stop()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn := dialQUIC(ctx, t, ln.Addr().String())

	// Never open a stream: the connection must still be closed by the relay's
	// AcceptStream deadline (handshakeDeadline), not left to quic-go's much
	// longer idle timeout.
	select {
	case <-conn.Context().Done():
	case <-time.After(handshakeDeadline + 3*time.Second):
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
