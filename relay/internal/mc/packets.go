package mc

import (
	"bufio"
	"encoding/json"
	"fmt"
)

// statusRequestPacket is the fixed client Status Request: length 1, id 0x00,
// empty body (RELAY.md Section 7). The relay sends it when performing the
// status exchange on a player's behalf.
var statusRequestPacket = []byte{0x01, 0x00}

// StatusRequestPacket returns the bytes of an empty Status Request packet.
func StatusRequestPacket() []byte {
	out := make([]byte, len(statusRequestPacket))
	copy(out, statusRequestPacket)
	return out
}

// encodePacket frames a packet id + body as a length-prefixed packet.
func encodePacket(id int32, body []byte) []byte {
	inner := appendVarInt(nil, id)
	inner = append(inner, body...)
	out := appendVarInt(nil, int32(len(inner)))
	return append(out, inner...)
}

// StatusResponsePacket frames a Status Response (id 0x00) carrying the JSON
// string payload (RELAY.md Section 7).
func StatusResponsePacket(statusJSON string) []byte {
	body := appendString(nil, statusJSON)
	return encodePacket(0x00, body)
}

// PongPacket frames a Pong (id 0x01) echoing the client's ping payload
// (RELAY.md Section 7).
func PongPacket(payload int64) []byte {
	body := make([]byte, 8)
	for i := 0; i < 8; i++ {
		body[7-i] = byte(payload >> (8 * i))
	}
	return encodePacket(0x01, body)
}

// LoginDisconnectPacket frames a Login Disconnect (clientbound 0x00 in the login
// state) carrying a JSON text component with the given reason (RELAY.md
// Section 7).
func LoginDisconnectPacket(reason string) []byte {
	component, _ := json.Marshal(struct {
		Text string `json:"text"`
	}{Text: reason})
	body := appendString(nil, string(component))
	return encodePacket(0x00, body)
}

// ReadStatusResponse reads a server's Status Response packet (id 0x00) from r
// and returns its JSON string payload (RELAY.md Section 7). Used when the relay
// performs the status exchange on the player's behalf.
func ReadStatusResponse(r *bufio.Reader) (string, error) {
	_, body, err := readPacket(r)
	if err != nil {
		return "", err
	}
	br := newByteSliceReader(body)
	id, _, err := readVarInt(br)
	if err != nil {
		return "", err
	}
	if id != 0x00 {
		return "", fmt.Errorf("mc: status response: unexpected packet id 0x%02x", id)
	}
	return readString(br, br)
}

// ReadStatusRequest reads the client's Status Request packet (id 0x00, empty
// body) from r, returning its id and verbatim bytes. The relay reads and
// discards it before answering from cache or a synthesized response.
func ReadStatusRequest(r *bufio.Reader) (int32, []byte, error) {
	raw, body, err := readPacket(r)
	if err != nil {
		return 0, nil, err
	}
	br := newByteSliceReader(body)
	id, _, err := readVarInt(br)
	if err != nil {
		return 0, nil, err
	}
	return id, raw, nil
}

// ReadPing reads a client Ping packet (id 0x01) from r and returns its i64
// payload, to be echoed in a Pong (RELAY.md Section 7).
func ReadPing(r *bufio.Reader) (int64, error) {
	_, body, err := readPacket(r)
	if err != nil {
		return 0, err
	}
	br := newByteSliceReader(body)
	id, _, err := readVarInt(br)
	if err != nil {
		return 0, err
	}
	if id != 0x01 {
		return 0, fmt.Errorf("mc: ping: unexpected packet id 0x%02x", id)
	}
	var v int64
	for i := 0; i < 8; i++ {
		b, err := br.ReadByte()
		if err != nil {
			return 0, err
		}
		v = v<<8 | int64(b)
	}
	return v, nil
}

// SynthesizedStatus builds the JSON status string for a stopped or unreachable
// server: protocol -1 so the client renders it as incompatible with the MOTD
// visible (the standard offline-placeholder trick), zero players, and the given
// MOTD (RELAY.md Section 7).
func SynthesizedStatus(motd string) string {
	payload := struct {
		Version struct {
			Name     string `json:"name"`
			Protocol int    `json:"protocol"`
		} `json:"version"`
		Players struct {
			Max    int `json:"max"`
			Online int `json:"online"`
		} `json:"players"`
		Description struct {
			Text string `json:"text"`
		} `json:"description"`
	}{}
	payload.Version.Name = "offline"
	payload.Version.Protocol = -1
	payload.Description.Text = motd
	b, _ := json.Marshal(payload)
	return string(b)
}

// StoppedMOTD is the MOTD shown for a stopped server (RELAY.md Section 7).
func StoppedMOTD(displayName string) string {
	return fmt.Sprintf("%s — stopped. Start it from the dashboard.", displayName)
}

// UnavailableMOTD is the MOTD shown when the API is unreachable and no cached
// status exists (RELAY.md Section 10).
const UnavailableMOTD = "Dashboard unavailable — try again shortly."
