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

func newTestListener(t *testing.T, validator Validator) (*Listener, func()) {
	t.Helper()
	newCaps := func() *ipcaps.IPCaps { return ipcaps.NewIPCaps(0, 0, 0, nil) }
	ln, err := NewListener("127.0.0.1:0", selfSignedTLS(t), validator, newCaps, testLogger())
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

	hello := &bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: 25701, Token: "tok"}
	_, ack := doHandshake(t, ln, hello)

	if !ack.GetAccepted() {
		t.Fatalf("accepted = false, reject_reason = %q, want accepted", ack.GetRejectReason())
	}

	call := validator.lastCall()
	if call.serverID != "srv-1" || call.bedrockPort != 25701 || call.token != "tok" {
		t.Errorf("ValidateBedrockTunnel called with %+v, want {srv-1 25701 tok}", call)
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
