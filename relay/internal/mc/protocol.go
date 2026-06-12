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
	// maxVarIntBytes is the byte ceiling for a 32-bit VarInt.
	maxVarIntBytes = 5
	// maxStringLen bounds a decoded string field. server_address is ≤255 and the
	// login name is ≤16; this single ceiling rejects an absurd length prefix
	// before allocating.
	maxStringLen = 32767
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

// readString reads a VarInt-length-prefixed UTF-8 string from r.
func readString(r io.Reader, br io.ByteReader) (string, error) {
	length, _, err := readVarInt(br)
	if err != nil {
		return "", err
	}
	if length < 0 || int(length) > maxStringLen {
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
	addr, err := readString(br, br)
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
// Section 7). 764 is 1.20.2 (UUID required); 759 is 1.19 (the optional/varying
// range begins). Below 759 the packet is name-only.
const (
	protoUUIDRequired = 764
	protoUUIDOptional = 759
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
	name, err := readString(br, br)
	if err != nil || len(name) > 16 {
		return ls, nil
	}
	ls.Name = name

	if protocolVersion >= protoUUIDOptional {
		// 1.20.2+ always sends the UUID; the 1.19.x range sends it optionally
		// (preceded by a bool on some builds). Best-effort: read 16 bytes if they
		// are there. A short read just leaves UUID empty.
		if protocolVersion < protoUUIDRequired {
			// In the optional range some clients prefix a boolean "has UUID". Peek it
			// without committing: if a single byte remains and it is 0, there is no
			// UUID; if 1, a UUID follows.
			has, err := br.ReadByte()
			if err != nil {
				return ls, nil
			}
			if has == 0 {
				return ls, nil
			}
			// has == 1 (UUID present) or any other build shape: fall through and try
			// to read 16 bytes.
		}
		uuid, err := readUUID(br)
		if err == nil {
			ls.UUID = uuid
		}
	}
	return ls, nil
}

// readPacket reads one length-prefixed packet from r, returning the verbatim
// bytes (length prefix + body) and a slice of just the body. It enforces the
// 1 KiB pre-route cap.
func readPacket(r *bufio.Reader) (raw, body []byte, err error) {
	length, lenBytes, err := readVarInt(r)
	if err != nil {
		return nil, nil, err
	}
	if length <= 0 || int(length) > MaxPreRouteBytes {
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
