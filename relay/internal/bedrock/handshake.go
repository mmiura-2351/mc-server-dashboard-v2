package bedrock

import (
	"encoding/binary"
	"fmt"
	"io"
	"time"

	"google.golang.org/protobuf/proto"

	bedrocktunnelv1 "github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/genproto/mcsd/bedrocktunnel/v1"
)

// handshakeDeadline bounds how long a Worker's QUIC connection has to send its
// TunnelHello before the relay drops it (mirrors tunnel.dialHandshakeDeadline
// for the Java tunnel listener).
const handshakeDeadline = 5 * time.Second

// maxHandshakeMessageBytes hard-caps a single framed handshake message. A
// TunnelHello (a UUID server_id, a uint32 port, a 32-hex-char token) and a
// TunnelHelloAck are both well under this; a peer that declares a larger frame
// is dropped without reading further, so a hostile dial-out cannot make the
// relay buffer unbounded data pre-authentication.
const maxHandshakeMessageBytes = 256

// lengthPrefixSize is the size, in bytes, of the big-endian length prefix on
// each framed handshake message on the QUIC stream (proto/mcsd/bedrocktunnel/v1
// package doc: "Stream wire format").
const lengthPrefixSize = 4

// handshakeStream is the subset of *quic.Stream the handshake needs. Narrowed
// to an interface so tests can use an in-memory fake instead of a real QUIC
// stream.
type handshakeStream interface {
	io.Reader
	io.Writer
	SetDeadline(t time.Time) error
}

// readHello reads and decodes one framed TunnelHello from s, within
// handshakeDeadline. A read/decode failure (including an oversized frame) is
// reported as an error; the caller closes the connection without a response,
// matching the Java tunnel listener's posture for a malformed handshake.
func readHello(s handshakeStream) (*bedrocktunnelv1.TunnelHello, error) {
	_ = s.SetDeadline(time.Now().Add(handshakeDeadline))
	data, err := readFramed(s)
	if err != nil {
		return nil, fmt.Errorf("bedrock: read TunnelHello: %w", err)
	}
	var hello bedrocktunnelv1.TunnelHello
	if err := proto.Unmarshal(data, &hello); err != nil {
		return nil, fmt.Errorf("bedrock: unmarshal TunnelHello: %w", err)
	}
	return &hello, nil
}

// writeAck encodes and writes a framed TunnelHelloAck to s.
func writeAck(s handshakeStream, accepted bool, reason string) error {
	data, err := proto.Marshal(&bedrocktunnelv1.TunnelHelloAck{Accepted: accepted, RejectReason: reason})
	if err != nil {
		return fmt.Errorf("bedrock: marshal TunnelHelloAck: %w", err)
	}
	_ = s.SetDeadline(time.Now().Add(handshakeDeadline))
	if err := writeFramed(s, data); err != nil {
		return fmt.Errorf("bedrock: write TunnelHelloAck: %w", err)
	}
	return nil
}

// readFramed reads one length-prefixed message: a 4-byte big-endian length
// followed by that many bytes.
func readFramed(r io.Reader) ([]byte, error) {
	var lenBuf [lengthPrefixSize]byte
	if _, err := io.ReadFull(r, lenBuf[:]); err != nil {
		return nil, err
	}
	n := binary.BigEndian.Uint32(lenBuf[:])
	if n > maxHandshakeMessageBytes {
		return nil, fmt.Errorf("frame too large (%d bytes, max %d)", n, maxHandshakeMessageBytes)
	}
	buf := make([]byte, n)
	if _, err := io.ReadFull(r, buf); err != nil {
		return nil, err
	}
	return buf, nil
}

// writeFramed writes data as one length-prefixed message.
func writeFramed(w io.Writer, data []byte) error {
	var lenBuf [lengthPrefixSize]byte
	binary.BigEndian.PutUint32(lenBuf[:], uint32(len(data)))
	if _, err := w.Write(lenBuf[:]); err != nil {
		return err
	}
	_, err := w.Write(data)
	return err
}
