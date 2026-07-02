package bedrock

import (
	"encoding/binary"
	"net"
	"testing"

	"google.golang.org/protobuf/proto"

	bedrocktunnelv1 "github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/genproto/mcsd/bedrocktunnel/v1"
)

// pipeStreams returns a connected pair of handshakeStreams backed by
// net.Pipe, which already satisfies io.Reader, io.Writer, and SetDeadline.
func pipeStreams() (net.Conn, net.Conn) {
	return net.Pipe()
}

func TestReadHelloRoundTrip(t *testing.T) {
	a, b := pipeStreams()
	defer func() { _ = a.Close() }()
	defer func() { _ = b.Close() }()

	want := &bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: 25701, Token: "tok"}
	data, err := proto.Marshal(want)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	go func() {
		_ = writeFramed(a, data)
	}()

	got, err := readHello(b)
	if err != nil {
		t.Fatalf("readHello: %v", err)
	}
	if got.GetServerId() != want.GetServerId() || got.GetBedrockPort() != want.GetBedrockPort() || got.GetToken() != want.GetToken() {
		t.Errorf("readHello = %+v, want %+v", got, want)
	}
}

func TestReadHelloOversizedFrame(t *testing.T) {
	a, b := pipeStreams()
	defer func() { _ = a.Close() }()
	defer func() { _ = b.Close() }()

	var lenBuf [4]byte
	binary.BigEndian.PutUint32(lenBuf[:], maxHandshakeMessageBytes+1)
	go func() {
		_, _ = a.Write(lenBuf[:])
	}()

	if _, err := readHello(b); err == nil {
		t.Error("expected an error for an oversized frame")
	}
}

func TestReadHelloTruncatedConnection(t *testing.T) {
	a, b := pipeStreams()
	defer func() { _ = b.Close() }()

	go func() {
		var lenBuf [4]byte
		binary.BigEndian.PutUint32(lenBuf[:], 10)
		_, _ = a.Write(lenBuf[:])
		_ = a.Close() // close before sending the promised 10 bytes
	}()

	if _, err := readHello(b); err == nil {
		t.Error("expected an error when the connection closes mid-frame")
	}
}

func TestReadHelloMalformedProtobuf(t *testing.T) {
	a, b := pipeStreams()
	defer func() { _ = a.Close() }()
	defer func() { _ = b.Close() }()

	// A single byte with the varint continuation bit set and nothing following
	// is an invalid (truncated) varint -- proto.Unmarshal must reject it.
	garbage := []byte{0xFF}
	go func() {
		_ = writeFramed(a, garbage)
	}()

	if _, err := readHello(b); err == nil {
		t.Error("expected an error for malformed protobuf bytes")
	}
}

func TestWriteAckRoundTrip(t *testing.T) {
	a, b := pipeStreams()
	defer func() { _ = a.Close() }()
	defer func() { _ = b.Close() }()

	go func() {
		_ = writeAck(a, true, "")
	}()

	data, err := readFramed(b)
	if err != nil {
		t.Fatalf("readFramed: %v", err)
	}
	var ack bedrocktunnelv1.TunnelHelloAck
	if err := proto.Unmarshal(data, &ack); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if !ack.GetAccepted() {
		t.Error("accepted = false, want true")
	}
}

func TestWriteAckRejection(t *testing.T) {
	a, b := pipeStreams()
	defer func() { _ = a.Close() }()
	defer func() { _ = b.Close() }()

	go func() {
		_ = writeAck(a, false, "invalid token")
	}()

	data, err := readFramed(b)
	if err != nil {
		t.Fatalf("readFramed: %v", err)
	}
	var ack bedrocktunnelv1.TunnelHelloAck
	if err := proto.Unmarshal(data, &ack); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if ack.GetAccepted() {
		t.Error("accepted = true, want false")
	}
	if ack.GetRejectReason() != "invalid token" {
		t.Errorf("reject_reason = %q, want %q", ack.GetRejectReason(), "invalid token")
	}
}
