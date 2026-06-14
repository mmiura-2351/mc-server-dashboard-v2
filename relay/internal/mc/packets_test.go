package mc

import (
	"bufio"
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestStatusResponsePacketRoundTrip(t *testing.T) {
	const payload = `{"version":{"name":"1.20.4","protocol":765}}`
	pkt := StatusResponsePacket(payload)

	got, err := ReadStatusResponse(bufio.NewReader(bytes.NewReader(pkt)))
	if err != nil {
		t.Fatalf("ReadStatusResponse: %v", err)
	}
	if got != payload {
		t.Errorf("status JSON = %q, want %q", got, payload)
	}
}

func TestPongEchoesPing(t *testing.T) {
	const payload int64 = 0x0102030405060708
	pingPkt := encodePacket(0x01, bigEndian64(payload))

	got, err := ReadPing(bufio.NewReader(bytes.NewReader(pingPkt)))
	if err != nil {
		t.Fatalf("ReadPing: %v", err)
	}
	if got != payload {
		t.Errorf("ping payload = %#x, want %#x", got, payload)
	}

	pong := PongPacket(payload)
	// Pong is id 0x01 with the same 8-byte big-endian payload as the ping body.
	if !bytes.Equal(pong, pingPkt) {
		t.Errorf("pong %x != ping %x", pong, pingPkt)
	}
}

func bigEndian64(v int64) []byte {
	b := make([]byte, 8)
	for i := 0; i < 8; i++ {
		b[7-i] = byte(v >> (8 * i))
	}
	return b
}

func TestLoginDisconnectPacket(t *testing.T) {
	pkt := LoginDisconnectPacket("could not reach the server")
	// id 0x00, then a JSON-string text component.
	_, body, err := readPacket(bufio.NewReader(bytes.NewReader(pkt)))
	if err != nil {
		t.Fatal(err)
	}
	br := newByteSliceReader(body)
	id, _, _ := readVarInt(br)
	if id != 0x00 {
		t.Fatalf("disconnect id = 0x%02x, want 0x00", id)
	}
	js, err := readString(br, br, maxStringLen)
	if err != nil {
		t.Fatal(err)
	}
	var comp struct {
		Text string `json:"text"`
	}
	if err := json.Unmarshal([]byte(js), &comp); err != nil {
		t.Fatalf("disconnect reason is not a JSON text component: %v", err)
	}
	if comp.Text != "could not reach the server" {
		t.Errorf("reason = %q", comp.Text)
	}
}

func TestSynthesizedStatus(t *testing.T) {
	js := SynthesizedStatus(StoppedMOTD("My Server"))
	var payload struct {
		Version struct {
			Protocol int `json:"protocol"`
		} `json:"version"`
		Players struct {
			Max    int `json:"max"`
			Online int `json:"online"`
		} `json:"players"`
		Description struct {
			Text string `json:"text"`
		} `json:"description"`
	}
	if err := json.Unmarshal([]byte(js), &payload); err != nil {
		t.Fatalf("synthesized status is not valid JSON: %v", err)
	}
	if payload.Version.Protocol != -1 {
		t.Errorf("protocol = %d, want -1", payload.Version.Protocol)
	}
	if payload.Players.Online != 0 || payload.Players.Max != 0 {
		t.Errorf("players = %d/%d, want 0/0", payload.Players.Online, payload.Players.Max)
	}
	if !strings.Contains(payload.Description.Text, "stopped. Start it from the dashboard.") {
		t.Errorf("MOTD = %q", payload.Description.Text)
	}
	if !strings.HasPrefix(payload.Description.Text, "My Server") {
		t.Errorf("MOTD should embed the display name, got %q", payload.Description.Text)
	}
}

func TestReadStatusResponseLargePayload(t *testing.T) {
	// A status response with a base64 server icon routinely exceeds 1 KiB.
	// ReadStatusResponse must accept it (up to MaxStatusResponseBytes).
	icon := strings.Repeat("A", 16000) // simulates ~12 KiB base64 icon
	payload := `{"version":{"name":"1.20.4","protocol":765},"favicon":"data:image/png;base64,` + icon + `"}`
	pkt := StatusResponsePacket(payload)

	got, err := ReadStatusResponse(bufio.NewReader(bytes.NewReader(pkt)))
	if err != nil {
		t.Fatalf("ReadStatusResponse with large icon: %v", err)
	}
	if got != payload {
		t.Errorf("payload mismatch: got %d bytes, want %d bytes", len(got), len(payload))
	}
}

func TestReadStatusResponseRejectsOversize(t *testing.T) {
	// A status response exceeding MaxStatusResponseBytes must be rejected.
	huge := strings.Repeat("X", MaxStatusResponseBytes+1)
	pkt := StatusResponsePacket(huge)

	if _, err := ReadStatusResponse(bufio.NewReader(bytes.NewReader(pkt))); err == nil {
		t.Error("expected error for oversize status response")
	}
}

func TestStatusRequestPacketIsEmpty(t *testing.T) {
	id, _, err := ReadStatusRequest(bufio.NewReader(bytes.NewReader(StatusRequestPacket())))
	if err != nil {
		t.Fatal(err)
	}
	if id != 0x00 {
		t.Errorf("status request id = 0x%02x, want 0x00", id)
	}
}
