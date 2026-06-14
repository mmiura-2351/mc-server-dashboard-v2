// Package mc implements the deliberately tiny slice of the Minecraft Java
// protocol the relay needs (docs/app/RELAY.md Section 7): only packets that are
// always plaintext and uncompressed — handshake, Login Start, status
// request/response, ping/pong, and the login disconnect. Everything after the
// login state's encryption negotiation is opaque to the relay and spliced
// byte-for-byte.
//
// Wire format (all pre-encryption/pre-compression): a packet is a VarInt length
// prefix, then VarInt packet id, then the packet body. Strings are a VarInt
// length followed by that many UTF-8 bytes.
package mc

import (
	"bufio"
	"errors"
	"fmt"
	"io"
)

// Parse caps (RELAY.md Section 7): at most 1 KiB is read before a routing
// decision, and every VarInt is bounded to 5 bytes (the maximum for a 32-bit
// value). These guard the unauthenticated game listener against a malicious or
// malformed peer.
const (
	// MaxPreRouteBytes bounds the bytes buffered before the relay decides how to
	// route a connection (RELAY.md Section 7).
	MaxPreRouteBytes = 1024
	// MaxStatusResponseBytes bounds a status response read from the trusted
	// tunnel side. Status responses with a base64 server icon routinely exceed
	// 1 KiB (~12 KiB for a 64x64 PNG), so the pre-route cap is too tight.
	MaxStatusResponseBytes = 64 * 1024
	// maxVarIntBytes is the byte ceiling for a 32-bit VarInt.
	maxVarIntBytes = 5
	// maxStringLen is the protocol's absolute string ceiling (32767). It bounds
	// fields with no tighter contextual limit (e.g. the server's Status Response
	// JSON, itself capped by MaxStatusResponseBytes at the packet level).
	maxStringLen = 32767
	// maxServerAddressLen bounds the handshake server_address field (the spec
	// caps it at 255), rejecting a hostile length prefix before allocating.
	maxServerAddressLen = 255
	// maxPlayerNameLen bounds the Login Start player name (the spec caps it at
	// 16).
	maxPlayerNameLen = 16
)

// ErrVarIntTooLong is returned when a VarInt exceeds five bytes (it would not
// fit a 32-bit value) — a malformed or hostile length prefix.
var ErrVarIntTooLong = errors.New("mc: VarInt is too long")

// readVarInt reads a Minecraft VarInt from r and returns its value and the
// number of bytes consumed. It rejects encodings longer than five bytes.
func readVarInt(r io.ByteReader) (int32, int, error) {
	var value uint32
	var n int
	for {
		b, err := r.ReadByte()
		if err != nil {
			return 0, n, err
		}
		n++
		value |= uint32(b&0x7F) << (7 * (n - 1))
		if b&0x80 == 0 {
			return int32(value), n, nil
		}
		if n >= maxVarIntBytes {
			return 0, n, ErrVarIntTooLong
		}
	}
}

// appendVarInt appends the VarInt encoding of v to dst.
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

// readString reads a VarInt-length-prefixed UTF-8 string from r, rejecting a
// length prefix above maxLen (the caller's contextual ceiling) before
// allocating.
func readString(r io.Reader, br io.ByteReader, maxLen int) (string, error) {
	length, _, err := readVarInt(br)
	if err != nil {
		return "", err
	}
	if length < 0 || int(length) > maxLen {
		return "", fmt.Errorf("mc: string length %d out of range", length)
	}
	buf := make([]byte, length)
	if _, err := io.ReadFull(r, buf); err != nil {
		return "", err
	}
	return string(buf), nil
}

// appendString appends the VarInt-length-prefixed UTF-8 encoding of s to dst.
func appendString(dst []byte, s string) []byte {
	dst = appendVarInt(dst, int32(len(s)))
	return append(dst, s...)
}

// NextState is the handshake's next-state field. The transfer state (3, added
// in 1.20.5) is treated like login for routing (RELAY.md Section 7).
type NextState int32

const (
	// NextStateStatus is a server-list status ping (next_state = 1).
	NextStateStatus NextState = 1
	// NextStateLogin is a full login attempt (next_state = 2).
	NextStateLogin NextState = 2
	// NextStateTransfer is a transfer (next_state = 3, 1.20.5+); routed like login.
	NextStateTransfer NextState = 3
)

// Handshake is the parsed serverbound handshake packet (id 0x00).
type Handshake struct {
	// ProtocolVersion is the client's protocol version. It drives the
	// version-tolerant Login Start parse (RELAY.md Section 7).
	ProtocolVersion int32
	// ServerAddress is the hostname the player typed, used for routing.
	ServerAddress string
	// Port is the port field (unused for routing; the relay owns the listener).
	Port uint16
	// NextState selects status or login.
	NextState NextState
	// Raw is the verbatim bytes of the whole handshake packet (length prefix
	// included), so they can be replayed into the tunnel untouched.
	Raw []byte
}

// IsLogin reports whether the handshake routes to the login path (login or
// transfer; transfer is treated like login for routing — RELAY.md Section 7).
func (h Handshake) IsLogin() bool {
	return h.NextState == NextStateLogin || h.NextState == NextStateTransfer
}

// ReadHandshake reads and parses one handshake packet from r, returning the
// parsed fields and the verbatim packet bytes. It enforces the 1 KiB pre-route
// cap and VarInt bounds (RELAY.md Section 7). r must be a *bufio.Reader so the
// caller and this function share one buffered stream.
func ReadHandshake(r *bufio.Reader) (Handshake, error) {
	raw, body, err := readPacket(r)
	if err != nil {
		return Handshake{}, err
	}
	br := newByteSliceReader(body)

	id, _, err := readVarInt(br)
	if err != nil {
		return Handshake{}, err
	}
	if id != 0x00 {
		return Handshake{}, fmt.Errorf("mc: handshake: unexpected packet id 0x%02x", id)
	}

	protocol, _, err := readVarInt(br)
	if err != nil {
		return Handshake{}, err
	}
	addr, err := readString(br, br, maxServerAddressLen)
	if err != nil {
		return Handshake{}, err
	}
	port, err := readUint16(br)
	if err != nil {
		return Handshake{}, err
	}
	next, _, err := readVarInt(br)
	if err != nil {
		return Handshake{}, err
	}

	return Handshake{
		ProtocolVersion: protocol,
		ServerAddress:   addr,
		Port:            port,
		NextState:       NextState(next),
		Raw:             raw,
	}, nil
}

// LoginStart is the parsed Login Start packet (id 0x00, login state). Parsing is
// protocol-version-tolerant: the name is always first; the UUID is present only
// on newer protocols (RELAY.md Section 7).
type LoginStart struct {
	// Name is the claimed username (≤16). Empty when unparseable.
	Name string
	// UUID is the claimed player UUID in canonical 8-4-4-4-12 form, or empty when
	// the protocol version does not carry it / it was unparseable.
	UUID string
	// Raw is the verbatim Login Start packet bytes (length prefix included), for
	// replay into the tunnel.
	Raw []byte
}

// Protocol version boundaries for the Login Start UUID field (RELAY.md
// Section 7). The Login Start body shape varies by version:
//
//   - 764+ (1.20.2+):  name + UUID (always present, no prefix bool).
//   - 761–763 (1.19.3–1.20.1): name + bool "has UUID" + optional UUID.
//   - 759–760 (1.19–1.19.2): name + bool "has signature data" + optional sig
//     data + bool "has UUID" + optional UUID. The FIRST bool is signature
//     data, not the UUID flag, so the bool+UUID shortcut must NOT be applied
//     here — misreading the signature bytes yields a garbage claimed UUID. We
//     record name only.
//   - <759: name only.
const (
	protoUUIDRequired = 764
	protoUUIDBoolMin  = 761
)

// ReadLoginStart reads one Login Start packet from r and parses it
// best-effort by protocol version. The verbatim bytes are always returned (Raw)
// so routing can splice even an unparseable packet; on a parse miss Name/UUID
// are left empty and err is nil (RELAY.md Section 7). A non-nil err means the
// packet framing itself was unreadable.
func ReadLoginStart(r *bufio.Reader, protocolVersion int32) (LoginStart, error) {
	raw, body, err := readPacket(r)
	if err != nil {
		return LoginStart{}, err
	}
	ls := LoginStart{Raw: raw}

	br := newByteSliceReader(body)
	id, _, err := readVarInt(br)
	if err != nil || id != 0x00 {
		// Framing was valid but this is not a Login Start we understand; splice
		// anyway with a null identity.
		return ls, nil
	}
	name, err := readString(br, br, maxPlayerNameLen)
	if err != nil {
		return ls, nil
	}
	ls.Name = name

	switch {
	case protocolVersion >= protoUUIDRequired:
		// 1.20.2+ always sends the UUID directly after the name.
		if uuid, err := readUUID(br); err == nil {
			ls.UUID = uuid
		}
	case protocolVersion >= protoUUIDBoolMin:
		// 1.19.3–1.20.1: a boolean "has UUID" precedes the optional UUID.
		has, err := br.ReadByte()
		if err != nil || has == 0 {
			return ls, nil
		}
		if uuid, err := readUUID(br); err == nil {
			ls.UUID = uuid
		}
	default:
		// 759–760 (1.19–1.19.2) the byte after the name is "has signature data",
		// not "has UUID"; parsing a UUID here would record signature bytes as a
		// garbage identity, so record name only. <759 is name-only too.
	}
	return ls, nil
}

// readPacket reads one length-prefixed packet from r, returning the verbatim
// bytes (length prefix + body) and a slice of just the body. It enforces the
// 1 KiB pre-route cap.
func readPacket(r *bufio.Reader) (raw, body []byte, err error) {
	return readPacketWithLimit(r, MaxPreRouteBytes)
}

// readPacketWithLimit is like readPacket but accepts a caller-supplied byte
// ceiling instead of the default MaxPreRouteBytes. Use the larger
// MaxStatusResponseBytes for trusted tunnel-side reads that may carry a
// server icon.
func readPacketWithLimit(r *bufio.Reader, maxLen int) (raw, body []byte, err error) {
	length, lenBytes, err := readVarInt(r)
	if err != nil {
		return nil, nil, err
	}
	if length <= 0 || int(length) > maxLen {
		return nil, nil, fmt.Errorf("mc: packet length %d out of range", length)
	}
	body = make([]byte, length)
	if _, err := io.ReadFull(r, body); err != nil {
		return nil, nil, err
	}
	raw = make([]byte, 0, lenBytes+int(length))
	raw = appendVarInt(raw, length)
	raw = append(raw, body...)
	return raw, body, nil
}

func readUint16(r io.ByteReader) (uint16, error) {
	hi, err := r.ReadByte()
	if err != nil {
		return 0, err
	}
	lo, err := r.ReadByte()
	if err != nil {
		return 0, err
	}
	return uint16(hi)<<8 | uint16(lo), nil
}

func readUUID(r *byteSliceReader) (string, error) {
	var b [16]byte
	for i := range b {
		v, err := r.ReadByte()
		if err != nil {
			return "", err
		}
		b[i] = v
	}
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}
