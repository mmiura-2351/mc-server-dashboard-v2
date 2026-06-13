//go:build e2e

// Package e2e drives a minimal protocol-level Java-edition Minecraft client
// against the REAL relay, running in the compose stack with the `relay` profile,
// end to end through the real API's RelayService and a real Postgres (epic #659,
// issue #962). It is the protocol-level acceptance suite from docs/app/RELAY.md
// Sections 4-7.
//
// Orchestration is owned by scripts/run_relay_e2e.sh (one source of truth, shared
// with CI): it brings the stack up, generates the tunnel TLS material, seeds an
// admin + a STOPPED server, and runs this suite pointed at the relay's published
// game port. The suite needs that live stack, so it is gated three ways and never
// runs in the ordinary `go test ./...` pass:
//   - the `e2e` build tag (this file compiles only under `-tags e2e`),
//   - MCD_RELAY_E2E_GAME_ADDR must name the relay's host:port (the orchestrator
//     sets it; absent, every test skips), and
//   - MCD_RELAY_E2E_STOPPED_SLUG carries the seeded stopped server's slug.
//
// The client speaks only the always-plaintext, always-uncompressed packets the
// relay handles (RELAY.md Section 7): the handshake (0x00) carrying the virtual
// hostname, the Status Request/Response + Ping/Pong, and Login Start / Login
// Disconnect. Hostname routing needs no DNS: the client connects to the relay's
// address and puts the virtual hostname in the handshake — all the protocol needs.
//
// The status-running, status-cache, and login game_session paths need a server
// the worker has actually BOOTED (a real `java -jar` launch behind the tunnel —
// the API start path has no stub-JAR seam), which is too heavy for the E2E
// budget; this suite does not exercise them. The relay's running-server protocol
// logic (status cache, login splice, session recording) is already covered
// in-process against the real relay components by relay/test/integration_test.go.
package e2e

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"strings"
	"testing"
	"time"
)

// gameAddr returns the relay's published game listener host:port, or "" when the
// live stack is not provisioned (so the suite skips rather than failing).
func gameAddr() string { return os.Getenv("MCD_RELAY_E2E_GAME_ADDR") }

func baseDomain() string {
	if d := os.Getenv("MCD_RELAY_E2E_BASE_DOMAIN"); d != "" {
		return d
	}
	return "mc.test"
}

func requireStack(t *testing.T) {
	t.Helper()
	if gameAddr() == "" {
		t.Skip("MCD_RELAY_E2E_GAME_ADDR not set; run via scripts/run_relay_e2e.sh")
	}
}

// dialRelay opens a TCP connection to the relay's game listener with a bounded
// read deadline (the relay's own pre-route budget is 5 s; we allow a little more
// for the API round trip).
func dialRelay(t *testing.T) net.Conn {
	t.Helper()
	conn, err := net.DialTimeout("tcp", gameAddr(), 5*time.Second)
	if err != nil {
		t.Fatalf("dial relay %s: %v", gameAddr(), err)
	}
	_ = conn.SetDeadline(time.Now().Add(8 * time.Second))
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

// TestStoppedServerStatus pings a stopped server's hostname and asserts the relay
// answers in-protocol with the synthesized stopped response: protocol -1 and the
// "stopped. Start it from the dashboard." MOTD (RELAY.md Section 7).
func TestStoppedServerStatus(t *testing.T) {
	requireStack(t)
	slug := os.Getenv("MCD_RELAY_E2E_STOPPED_SLUG")
	if slug == "" {
		t.Skip("MCD_RELAY_E2E_STOPPED_SLUG not set")
	}

	conn := dialRelay(t)
	host := slug + "." + baseDomain()
	if _, err := conn.Write(handshakePacket(765, host, 25565, 1)); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Write([]byte{0x01, 0x00}); err != nil { // empty Status Request
		t.Fatal(err)
	}

	br := bufio.NewReader(conn)
	statusJSON, err := readStatusResponse(br)
	if err != nil {
		t.Fatalf("read status response: %v", err)
	}
	var parsed struct {
		Version struct {
			Protocol int `json:"protocol"`
		} `json:"version"`
		Description struct {
			Text string `json:"text"`
		} `json:"description"`
	}
	if err := json.Unmarshal([]byte(statusJSON), &parsed); err != nil {
		t.Fatalf("status JSON %q: %v", statusJSON, err)
	}
	if parsed.Version.Protocol != -1 {
		t.Errorf("stopped status protocol = %d, want -1", parsed.Version.Protocol)
	}
	if !strings.Contains(parsed.Description.Text, "stopped. Start it from the dashboard.") {
		t.Errorf("stopped MOTD missing from %q", parsed.Description.Text)
	}
}

// TestStoppedServerLogin attempts a login against a stopped server's hostname and
// asserts the relay returns a Login Disconnect carrying the stopped reason
// (RELAY.md Section 7).
func TestStoppedServerLogin(t *testing.T) {
	requireStack(t)
	slug := os.Getenv("MCD_RELAY_E2E_STOPPED_SLUG")
	if slug == "" {
		t.Skip("MCD_RELAY_E2E_STOPPED_SLUG not set")
	}

	conn := dialRelay(t)
	host := slug + "." + baseDomain()
	payload := append(handshakePacket(765, host, 25565, 2), loginStartPacket("Steve")...)
	if _, err := conn.Write(payload); err != nil {
		t.Fatal(err)
	}

	br := bufio.NewReader(conn)
	reason, err := readLoginDisconnect(br)
	if err != nil {
		t.Fatalf("read login disconnect: %v", err)
	}
	if !strings.Contains(reason, "stopped. Start it from the dashboard.") {
		t.Errorf("login disconnect reason = %q, want the stopped reason", reason)
	}
}

// TestUnknownSlugDropped pings an unknown hostname and asserts the relay drops the
// connection with NO protocol response — no information leaks to a scanner
// (RELAY.md Section 3 / Section 7).
func TestUnknownSlugDropped(t *testing.T) {
	requireStack(t)

	conn := dialRelay(t)
	// A slug that cannot exist under the base domain (no such server was seeded).
	host := "no-such-server-zzz." + baseDomain()
	if _, err := conn.Write(handshakePacket(765, host, 25565, 1)); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Write([]byte{0x01, 0x00}); err != nil {
		t.Fatal(err)
	}

	// The relay closes silently: the next read should hit EOF, not data.
	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	n, err := conn.Read(make([]byte, 1))
	if err == nil && n > 0 {
		t.Errorf("unknown slug should be dropped with no response, got %d bytes", n)
	}
}

// --- minimal Minecraft protocol client (RELAY.md Section 7) ---

// handshakePacket builds a handshake (0x00): protocol version, server address
// (the virtual hostname), port, and next state (1 = status, 2 = login).
func handshakePacket(protocol int32, addr string, port uint16, next int32) []byte {
	var body []byte
	body = appendVarInt(body, protocol)
	body = appendString(body, addr)
	body = append(body, byte(port>>8), byte(port))
	body = appendVarInt(body, next)
	return framePacket(0x00, body)
}

// loginStartPacket builds a Login Start (0x00, login state): the username plus a
// 16-byte UUID (the protocol-765 / 1.20.2+ form).
func loginStartPacket(name string) []byte {
	body := appendString(nil, name)
	body = append(body, make([]byte, 16)...)
	return framePacket(0x00, body)
}

func framePacket(id int32, body []byte) []byte {
	inner := appendVarInt(nil, id)
	inner = append(inner, body...)
	out := appendVarInt(nil, int32(len(inner)))
	return append(out, inner...)
}

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

func appendString(dst []byte, s string) []byte {
	dst = appendVarInt(dst, int32(len(s)))
	return append(dst, s...)
}

func readVarInt(r *bufio.Reader) (int32, error) {
	var value uint32
	for n := 0; n < 5; n++ {
		b, err := r.ReadByte()
		if err != nil {
			return 0, err
		}
		value |= uint32(b&0x7F) << (7 * n)
		if b&0x80 == 0 {
			return int32(value), nil
		}
	}
	return 0, io.ErrUnexpectedEOF
}

// readPacketBody reads one length-prefixed packet and returns its body (no length
// prefix).
func readPacketBody(r *bufio.Reader) ([]byte, error) {
	length, err := readVarInt(r)
	if err != nil {
		return nil, err
	}
	body := make([]byte, length)
	if _, err := io.ReadFull(r, body); err != nil {
		return nil, err
	}
	return body, nil
}

// readVarIntString reads a VarInt-length-prefixed string from a byte slice.
func readVarIntString(b []byte) (string, error) {
	br := bufio.NewReader(bytes.NewReader(b))
	length, err := readVarInt(br)
	if err != nil {
		return "", err
	}
	s := make([]byte, length)
	if _, err := io.ReadFull(br, s); err != nil {
		return "", err
	}
	return string(s), nil
}

// readStatusResponse reads a Status Response (id 0x00) and returns its JSON
// payload.
func readStatusResponse(r *bufio.Reader) (string, error) {
	body, err := readPacketBody(r)
	if err != nil {
		return "", err
	}
	// body = id (0x00) + VarInt-string JSON.
	if len(body) < 1 {
		return "", fmt.Errorf("status response body is empty")
	}
	return readVarIntString(body[1:])
}

// readLoginDisconnect reads a Login Disconnect (id 0x00, login state) and returns
// the reason text extracted from its JSON chat component.
func readLoginDisconnect(r *bufio.Reader) (string, error) {
	body, err := readPacketBody(r)
	if err != nil {
		return "", err
	}
	if len(body) < 1 {
		return "", fmt.Errorf("login disconnect body is empty")
	}
	jsonStr, err := readVarIntString(body[1:])
	if err != nil {
		return "", err
	}
	var component struct {
		Text string `json:"text"`
	}
	if err := json.Unmarshal([]byte(jsonStr), &component); err != nil {
		return "", err
	}
	return component.Text, nil
}
