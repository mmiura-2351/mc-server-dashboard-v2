package tunnel

import (
	"net"
	"strings"
	"testing"
	"time"
)

// readHandshakeResult drives readHandshake over an in-memory pipe: the test
// writes raw bytes into one end while readHandshake reads the other.
func readHandshakeResult(t *testing.T, write func(net.Conn)) (string, bool) {
	t.Helper()
	client, server := net.Pipe()
	defer func() { _ = server.Close() }()

	go func() {
		defer func() { _ = client.Close() }()
		// Bound the writer: readHandshake stops reading at the cap, so an over-long
		// write would otherwise block this goroutine on the pipe forever.
		_ = client.SetWriteDeadline(time.Now().Add(time.Second))
		write(client)
	}()

	type result struct {
		token string
		ok    bool
	}
	done := make(chan result, 1)
	go func() {
		token, ok := readHandshake(server)
		done <- result{token, ok}
	}()

	select {
	case r := <-done:
		return r.token, r.ok
	case <-time.After(2 * time.Second):
		t.Fatal("readHandshake did not return")
		return "", false
	}
}

func TestReadHandshakeValid(t *testing.T) {
	token, ok := readHandshakeResult(t, func(c net.Conn) {
		_, _ = c.Write([]byte(handshakePrefix + "\n" + "abc123" + "\n"))
	})
	if !ok || token != "abc123" {
		t.Fatalf("valid handshake: token=%q ok=%v", token, ok)
	}
}

// TestReadHandshakeOverLongLine asserts the pre-auth read is hard-capped: a
// first line longer than maxHandshakeBytes with no newline is rejected silently
// rather than buffered unbounded.
func TestReadHandshakeOverLongLine(t *testing.T) {
	_, ok := readHandshakeResult(t, func(c net.Conn) {
		// Far more than maxHandshakeBytes, no newline in sight.
		_, _ = c.Write([]byte(strings.Repeat("A", maxHandshakeBytes*4)))
	})
	if ok {
		t.Error("an over-long newline-free handshake must be rejected")
	}
}

// TestReadHandshakeDoubleNewlinePrefix rejects a prefix line with trailing
// extra newlines. "MCSD-TUNNEL/1\n\n" (two newlines) must not pass: only a
// single trailing newline is valid per the protocol.
func TestReadHandshakeDoubleNewlinePrefix(t *testing.T) {
	_, ok := readHandshakeResult(t, func(c net.Conn) {
		_, _ = c.Write([]byte(handshakePrefix + "\n\n" + "abc123" + "\n"))
	})
	if ok {
		t.Error("prefix with double newline must be rejected")
	}
}

// TestReadHandshakeDoubleNewlineToken rejects a token line with an extra
// trailing newline. The token "abc123\n\n" has extra data after the handshake,
// which the Buffered() > 0 guard catches, but with TrimSuffix the token itself
// is correctly preserved.
func TestReadHandshakeDoubleNewlineToken(t *testing.T) {
	_, ok := readHandshakeResult(t, func(c net.Conn) {
		_, _ = c.Write([]byte(handshakePrefix + "\n" + "abc123" + "\n\n"))
	})
	if ok {
		t.Error("token with trailing extra newline must be rejected")
	}
}

// TestReadHandshakeOverLongToken asserts the cap also bounds the token line: a
// valid prefix followed by a token longer than the remaining cap (no newline)
// is rejected.
func TestReadHandshakeOverLongToken(t *testing.T) {
	_, ok := readHandshakeResult(t, func(c net.Conn) {
		_, _ = c.Write([]byte(handshakePrefix + "\n"))
		_, _ = c.Write([]byte(strings.Repeat("B", maxHandshakeBytes*4)))
	})
	if ok {
		t.Error("an over-long token line must be rejected")
	}
}
