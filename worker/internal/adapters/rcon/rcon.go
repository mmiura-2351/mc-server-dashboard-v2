// Package rcon implements the execution.ServerControl Port over the Source RCON
// protocol: forwarding console commands (FR-SRV-5) and issuing save-all / stop
// on the graceful-stop path (ARCHITECTURE.md Section 5.2).
//
// The protocol is hand-rolled rather than pulled in as a dependency: it is a
// trivial length-prefixed little-endian packet format (a few dozen lines), so a
// third-party module would add a supply-chain surface and a 7-day-cooldown gate
// for no real saving (docs/dev/DEPENDENCIES.md). The wire format is the
// documented Source RCON protocol.
package rcon

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"strings"
	"time"
)

// Packet types from the Source RCON protocol.
const (
	typeResponseValue int32 = 0
	typeExecCommand   int32 = 2
	typeAuthResponse  int32 = 2
	typeAuth          int32 = 3
)

// maxBodyLen bounds a packet so a misbehaving or hostile peer cannot exhaust
// memory. The Source RCON spec caps a packet payload at 4096 bytes; the headroom
// covers the id/type/terminator overhead.
const maxBodyLen = 4096 + 16

// ErrAuthFailed is returned when the RCON password is rejected (auth response
// id = -1).
var ErrAuthFailed = errors.New("rcon: authentication failed")

// defaultExecuteTimeout bounds a single Execute round trip when the caller's
// ctx carries no deadline. In practice RCON replies are sub-second; 30s is a
// generous ceiling that still guarantees a hung server (one that accepts the
// TCP connect but never replies) cannot wedge the call — and thus the lane or a
// global concurrency slot — forever. A var (not a const) so tests can shrink it.
//
// The same ceiling bounds the dial+authenticate handshake (Dial): a peer that
// TCP-accepts but never sends an AUTH_RESPONSE would otherwise block the read
// forever on a deadline-less lane ctx — the same wedge shape one step earlier.
var defaultExecuteTimeout = 30 * time.Second

// ErrConnBroken is returned by Execute when a prior round trip failed mid-stream
// (timeout or cancel), which can leave the connection mis-framed. The connection
// is poisoned on such a failure so reuse fails fast and forces a redial rather
// than reading a stale response off the broken stream.
var ErrConnBroken = errors.New("rcon: connection poisoned by a prior I/O error")

// Client is a single authenticated RCON connection. It is not safe for
// concurrent use; callers serialize commands per server.
type Client struct {
	conn   net.Conn
	nextID int32
	broken bool
}

// Dial opens an RCON connection to addr and authenticates with password. It
// returns ErrAuthFailed on a rejected password. Both the TCP connect and the
// handshake honour ctx's deadline and fall back to defaultExecuteTimeout when
// ctx carries none: a peer that never completes the SYN handshake (firewalled or
// gone) cannot ride the OS's ~2-minute SYN timeout, and one that accepts the
// connect but never sends an AUTH_RESPONSE cannot wedge the read forever. It
// returns ctx.Err() when ctx cancellation caused the failure.
func Dial(ctx context.Context, addr, password string) (*Client, error) {
	// DialContext honours ctx's deadline; set Timeout as the fallback bound for a
	// deadline-less ctx so the connect cannot hang on the OS SYN timeout.
	d := net.Dialer{Timeout: defaultExecuteTimeout}
	conn, err := d.DialContext(ctx, "tcp", addr)
	if err != nil {
		return nil, fmt.Errorf("rcon: dial %s: %w", addr, err)
	}

	c := &Client{conn: conn, nextID: 1}
	if err := c.withDeadline(ctx, func() error { return c.authenticate(password) }); err != nil {
		_ = conn.Close()
		return nil, err
	}
	return c, nil
}

// Execute sends one command line and returns the server's reply body. Vanilla
// Minecraft's RCON server fragments replies longer than 4096 bytes into multiple
// RESPONSE_VALUE packets with the same request id, with no end marker. Execute
// sends a second marker command (empty body) with its own id after the real
// command: the arrival of the marker's reply deterministically signals that all
// fragments for the real command have been received.
//
// It honours ctx's deadline for the round trip, and falls back to
// defaultExecuteTimeout when ctx carries none, so a server that accepts the
// connection but never replies cannot block the call forever. It returns
// ctx.Err() when ctx cancellation caused the failure, and ErrConnBroken when
// the connection was poisoned by a prior failed round trip.
func (c *Client) Execute(ctx context.Context, line string) (string, error) {
	if c.broken {
		return "", ErrConnBroken
	}
	var body string
	err := c.withDeadline(ctx, func() error {
		cmdID := c.id()
		markerID := c.id()
		if err := c.write(cmdID, typeExecCommand, line); err != nil {
			return err
		}
		if err := c.write(markerID, typeExecCommand, ""); err != nil {
			return err
		}
		var buf strings.Builder
		for {
			respID, _, b, err := c.read()
			if err != nil {
				return err
			}
			if respID == markerID {
				break
			}
			if respID != cmdID {
				return fmt.Errorf("rcon: response id %d did not match request id %d or marker id %d", respID, cmdID, markerID)
			}
			buf.WriteString(b)
		}
		body = buf.String()
		return nil
	})
	if err != nil {
		// A timeout or cancel can leave the stream mid-frame; poison the
		// connection so the next reuse fails fast and redials rather than reading
		// a stale response off a mis-framed stream.
		c.broken = true
		_ = c.conn.Close()
		return "", err
	}
	return body, nil
}

// withDeadline runs fn while a per-call deadline (ctx's, or defaultExecuteTimeout
// when ctx carries none) is set on the connection, so a hung read cannot block
// forever. A watcher goroutine honours ctx cancellation: on ctx.Done() it sets
// an immediate connection deadline, the standard net.Conn idiom for unblocking
// an in-flight read from another goroutine. The watcher is closed and waited for
// before returning so it never touches the connection after fn returns. When ctx
// cancellation caused fn to fail, it returns ctx.Err() in place of the opaque
// i/o timeout.
func (c *Client) withDeadline(ctx context.Context, fn func() error) error {
	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = time.Now().Add(defaultExecuteTimeout)
	}
	_ = c.conn.SetDeadline(deadline)
	defer func() { _ = c.conn.SetDeadline(time.Time{}) }()

	watcherDone := make(chan struct{})
	stop := make(chan struct{})
	go func() {
		defer close(watcherDone)
		select {
		case <-ctx.Done():
			_ = c.conn.SetDeadline(time.Now())
		case <-stop:
		}
	}()
	defer func() {
		close(stop)
		<-watcherDone
	}()

	if err := fn(); err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return err
	}
	return nil
}

// Close releases the connection.
func (c *Client) Close() error {
	return c.conn.Close()
}

// authenticate runs the AUTH handshake. The server replies with an
// AUTH_RESPONSE whose id is -1 on failure, echoing the request id on success.
func (c *Client) authenticate(password string) error {
	id := c.id()
	if err := c.write(id, typeAuth, password); err != nil {
		return err
	}
	for {
		respID, typ, _, err := c.read()
		if err != nil {
			return err
		}
		// The server may send a RESPONSE_VALUE before the AUTH_RESPONSE; the
		// AUTH_RESPONSE is the packet that carries the verdict.
		if typ != typeAuthResponse {
			continue
		}
		if respID == -1 {
			return ErrAuthFailed
		}
		if respID != id {
			return fmt.Errorf("rcon: auth response id %d did not match request id %d", respID, id)
		}
		return nil
	}
}

// id allocates the next request id.
func (c *Client) id() int32 {
	id := c.nextID
	c.nextID++
	return id
}

// write encodes and sends one packet.
func (c *Client) write(id, typ int32, body string) error {
	payload := make([]byte, 8+len(body)+2)
	binary.LittleEndian.PutUint32(payload[0:4], uint32(id))
	binary.LittleEndian.PutUint32(payload[4:8], uint32(typ))
	copy(payload[8:], body)
	// Two trailing NUL bytes terminate the body and the packet.

	frame := make([]byte, 4+len(payload))
	binary.LittleEndian.PutUint32(frame[0:4], uint32(len(payload)))
	copy(frame[4:], payload)

	if _, err := c.conn.Write(frame); err != nil {
		return fmt.Errorf("rcon: write: %w", err)
	}
	return nil
}

// read decodes one packet.
func (c *Client) read() (id, typ int32, body string, err error) {
	var length int32
	if err = binary.Read(c.conn, binary.LittleEndian, &length); err != nil {
		return 0, 0, "", fmt.Errorf("rcon: read length: %w", err)
	}
	if length < 10 || length > maxBodyLen {
		return 0, 0, "", fmt.Errorf("rcon: invalid packet length %d", length)
	}
	buf := make([]byte, length)
	if _, err = io.ReadFull(c.conn, buf); err != nil {
		return 0, 0, "", fmt.Errorf("rcon: read body: %w", err)
	}
	id = int32(binary.LittleEndian.Uint32(buf[0:4]))
	typ = int32(binary.LittleEndian.Uint32(buf[4:8]))
	body = string(buf[8 : len(buf)-2]) // strip the two trailing NULs
	return id, typ, body, nil
}
