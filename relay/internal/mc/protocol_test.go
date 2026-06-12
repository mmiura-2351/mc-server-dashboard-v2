package mc

import (
	"bufio"
	"bytes"
	"testing"
)

// buildPacket frames id+body as a length-prefixed packet, for test fixtures.
func buildPacket(id int32, body []byte) []byte {
	inner := appendVarInt(nil, id)
	inner = append(inner, body...)
	out := appendVarInt(nil, int32(len(inner)))
	return append(out, inner...)
}

func handshakeBytes(protocol int32, addr string, port uint16, next int32) []byte {
	var body []byte
	body = appendVarInt(body, protocol)
	body = appendString(body, addr)
	body = append(body, byte(port>>8), byte(port))
	body = appendVarInt(body, next)
	return buildPacket(0x00, body)
}

func TestReadHandshake(t *testing.T) {
	raw := handshakeBytes(765, "amber-falcon-42.mc.example.com", 25565, 2)
	r := bufio.NewReader(bytes.NewReader(raw))

	hs, err := ReadHandshake(r)
	if err != nil {
		t.Fatalf("ReadHandshake: %v", err)
	}
	if hs.ProtocolVersion != 765 {
		t.Errorf("protocol = %d, want 765", hs.ProtocolVersion)
	}
	if hs.ServerAddress != "amber-falcon-42.mc.example.com" {
		t.Errorf("address = %q", hs.ServerAddress)
	}
	if hs.Port != 25565 {
		t.Errorf("port = %d, want 25565", hs.Port)
	}
	if hs.NextState != NextStateLogin {
		t.Errorf("next_state = %d, want login", hs.NextState)
	}
	if !hs.IsLogin() {
		t.Error("IsLogin() = false, want true")
	}
	if !bytes.Equal(hs.Raw, raw) {
		t.Errorf("Raw mismatch:\n got %x\nwant %x", hs.Raw, raw)
	}
}

func TestReadHandshakeStatusAndTransfer(t *testing.T) {
	status := handshakeBytes(765, "x.mc.example.com", 25565, 1)
	hs, err := ReadHandshake(bufio.NewReader(bytes.NewReader(status)))
	if err != nil {
		t.Fatal(err)
	}
	if hs.NextState != NextStateStatus || hs.IsLogin() {
		t.Errorf("status handshake misrouted: next=%d isLogin=%v", hs.NextState, hs.IsLogin())
	}

	transfer := handshakeBytes(766, "x.mc.example.com", 25565, 3)
	hs, err = ReadHandshake(bufio.NewReader(bytes.NewReader(transfer)))
	if err != nil {
		t.Fatal(err)
	}
	if !hs.IsLogin() {
		t.Error("transfer (next_state=3) should route like login")
	}
}

func TestReadHandshakeMalformed(t *testing.T) {
	cases := map[string][]byte{
		"empty":                {},
		"length only":          {0x05},
		"oversize length":      append([]byte{0xff, 0xff, 0xff, 0xff, 0x7f}, 0x00),
		"wrong packet id":      buildPacket(0x01, []byte{0x00}),
		"truncated body":       {0x10, 0x00, 0x01},
		"varint too long":      {0x05, 0xff, 0xff, 0xff, 0xff, 0xff},
		"length exceeds 1 KiB": append(appendVarInt(nil, 2000), make([]byte, 3)...),
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			r := bufio.NewReader(bytes.NewReader(raw))
			if _, err := ReadHandshake(r); err == nil {
				t.Errorf("expected error for malformed %q", name)
			}
		})
	}
}

func TestReadLoginStartModern(t *testing.T) {
	// Protocol 765 (1.20.4): name + 16-byte UUID, always present.
	uuid := []byte{0x12, 0x34, 0x56, 0x78, 0x9a, 0xbc, 0xde, 0xf0, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88}
	var body []byte
	body = appendString(body, "Steve")
	body = append(body, uuid...)
	raw := buildPacket(0x00, body)

	ls, err := ReadLoginStart(bufio.NewReader(bytes.NewReader(raw)), 765)
	if err != nil {
		t.Fatal(err)
	}
	if ls.Name != "Steve" {
		t.Errorf("name = %q, want Steve", ls.Name)
	}
	if ls.UUID != "12345678-9abc-def0-1122-334455667788" {
		t.Errorf("uuid = %q", ls.UUID)
	}
	if !bytes.Equal(ls.Raw, raw) {
		t.Error("Raw mismatch")
	}
}

func TestReadLoginStartNameOnlyLegacy(t *testing.T) {
	// Protocol 578 (1.15.2): name only, no UUID field.
	raw := buildPacket(0x00, appendString(nil, "Alex"))
	ls, err := ReadLoginStart(bufio.NewReader(bytes.NewReader(raw)), 578)
	if err != nil {
		t.Fatal(err)
	}
	if ls.Name != "Alex" {
		t.Errorf("name = %q, want Alex", ls.Name)
	}
	if ls.UUID != "" {
		t.Errorf("uuid = %q, want empty for legacy protocol", ls.UUID)
	}
}

func TestReadLoginStartOptionalUUID(t *testing.T) {
	// Protocol 760 (1.19.1) "has UUID" = false: name, then a 0 byte.
	raw := buildPacket(0x00, append(appendString(nil, "Bob"), 0x00))
	ls, err := ReadLoginStart(bufio.NewReader(bytes.NewReader(raw)), 760)
	if err != nil {
		t.Fatal(err)
	}
	if ls.Name != "Bob" || ls.UUID != "" {
		t.Errorf("name=%q uuid=%q, want Bob / empty", ls.Name, ls.UUID)
	}
}

func TestReadLoginStartUnparseableStillSplices(t *testing.T) {
	// A non-LoginStart packet id: framing is valid so Raw is returned, but
	// Name/UUID are empty and err is nil (the relay splices anyway).
	raw := buildPacket(0x07, []byte{0xde, 0xad})
	ls, err := ReadLoginStart(bufio.NewReader(bytes.NewReader(raw)), 765)
	if err != nil {
		t.Fatalf("unparseable login should not error: %v", err)
	}
	if ls.Name != "" || ls.UUID != "" {
		t.Errorf("expected null identity, got name=%q uuid=%q", ls.Name, ls.UUID)
	}
	if !bytes.Equal(ls.Raw, raw) {
		t.Error("Raw must be preserved for splice replay")
	}
}

func TestReadLoginStartOverlongName(t *testing.T) {
	// A name longer than 16 chars is rejected to a null identity (still spliceable).
	raw := buildPacket(0x00, appendString(nil, "ThisNameIsWayTooLong"))
	ls, err := ReadLoginStart(bufio.NewReader(bytes.NewReader(raw)), 765)
	if err != nil {
		t.Fatal(err)
	}
	if ls.Name != "" {
		t.Errorf("overlong name should be rejected, got %q", ls.Name)
	}
}

func TestVarIntRoundTrip(t *testing.T) {
	for _, v := range []int32{0, 1, 127, 128, 255, 25565, 2147483647, -1} {
		enc := appendVarInt(nil, v)
		got, n, err := readVarInt(bytes.NewReader(enc))
		if err != nil {
			t.Fatalf("readVarInt(%d): %v", v, err)
		}
		if got != v {
			t.Errorf("round trip %d -> %d", v, got)
		}
		if n != len(enc) {
			t.Errorf("consumed %d bytes, encoded %d", n, len(enc))
		}
	}
}

func TestVarIntTooLong(t *testing.T) {
	if _, _, err := readVarInt(bytes.NewReader([]byte{0xff, 0xff, 0xff, 0xff, 0xff})); err != ErrVarIntTooLong {
		t.Errorf("expected ErrVarIntTooLong, got %v", err)
	}
}
