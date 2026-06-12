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

func TestReadLoginStartSigDataVersions(t *testing.T) {
	// Protocols 759/760 (1.19–1.19.2): the byte after the name is "has signature
	// data", NOT "has UUID". The relay must record name only and never read the
	// trailing bytes as a UUID, even when has_sig_data=1 is followed by
	// signature material that looks like a 16-byte UUID.
	uuidLike := []byte{0xde, 0xad, 0xbe, 0xef, 0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb}
	for _, proto := range []int32{759, 760} {
		for _, hasSig := range []byte{0x00, 0x01} {
			body := appendString(nil, "Bob")
			body = append(body, hasSig)
			if hasSig == 0x01 {
				body = append(body, uuidLike...) // signature bytes — must not be read as a UUID
			}
			raw := buildPacket(0x00, body)
			ls, err := ReadLoginStart(bufio.NewReader(bytes.NewReader(raw)), proto)
			if err != nil {
				t.Fatalf("proto %d has_sig=%d: %v", proto, hasSig, err)
			}
			if ls.Name != "Bob" {
				t.Errorf("proto %d has_sig=%d: name=%q, want Bob", proto, hasSig, ls.Name)
			}
			if ls.UUID != "" {
				t.Errorf("proto %d has_sig=%d: uuid=%q, want empty (signature bytes must not be parsed as UUID)", proto, hasSig, ls.UUID)
			}
		}
	}
}

func TestReadLoginStartBoolUUIDVersions(t *testing.T) {
	// Protocols 761–763 (1.19.3–1.20.1): name + bool "has UUID" + optional UUID.
	uuid := []byte{0x12, 0x34, 0x56, 0x78, 0x9a, 0xbc, 0xde, 0xf0, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88}
	for _, proto := range []int32{761, 762, 763} {
		// has UUID = true.
		body := appendString(nil, "Bob")
		body = append(body, 0x01)
		body = append(body, uuid...)
		ls, err := ReadLoginStart(bufio.NewReader(bytes.NewReader(buildPacket(0x00, body))), proto)
		if err != nil {
			t.Fatalf("proto %d has_uuid=1: %v", proto, err)
		}
		if ls.Name != "Bob" || ls.UUID != "12345678-9abc-def0-1122-334455667788" {
			t.Errorf("proto %d has_uuid=1: name=%q uuid=%q", proto, ls.Name, ls.UUID)
		}

		// has UUID = false.
		raw := buildPacket(0x00, append(appendString(nil, "Bob"), 0x00))
		ls, err = ReadLoginStart(bufio.NewReader(bytes.NewReader(raw)), proto)
		if err != nil {
			t.Fatalf("proto %d has_uuid=0: %v", proto, err)
		}
		if ls.Name != "Bob" || ls.UUID != "" {
			t.Errorf("proto %d has_uuid=0: name=%q uuid=%q, want Bob / empty", proto, ls.Name, ls.UUID)
		}
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
