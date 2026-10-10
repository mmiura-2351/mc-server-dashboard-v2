// Package rcon implements execution.ServerControl over the Source RCON protocol.
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

// Bound packet allocation; headroom covers the ID, type, and terminators beyond a 4096-byte payload.
const maxBodyLen = 4096 + 16

// ErrAuthFailed is returned when the RCON password is rejected (auth response
// id = -1).
var ErrAuthFailed = errors.New("rcon: authentication failed")

// defaultExecuteTimeout bounds dial, authentication, and execution without a caller deadline.
// Tests can lower it to exercise unresponsive peers.
var defaultExecuteTimeout = 30 * time.Second

// maxResponseSize bounds the total reassembled response body. A misbehaving
// server streaming fragments for the full deadline cannot grow memory past this.
const maxResponseSize = 1 << 20 // 1 MiB

// ErrResponseTooLarge is returned when the reassembled response exceeds
// maxResponseSize.
var ErrResponseTooLarge = errors.New("rcon: response too large")

// ErrConnBroken rejects reuse after an I/O failure that may have left the stream mid-frame.
var ErrConnBroken = errors.New("rcon: connection poisoned by a prior I/O error")

// Client is a single authenticated RCON connection. It is not safe for
// concurrent use; callers serialize commands per server.
type Client struct {
	conn   net.Conn
	nextID int32
	broken bool
}

// Dial authenticates within the caller deadline or defaultExecuteTimeout.
// Rejected credentials return ErrAuthFailed; cancellation returns ctx.Err().
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

// Execute collects fragmented replies until an empty marker command's reply arrives.
// Send the marker only after the first reply: Vanilla can reject coalesced request packets.
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
		var buf strings.Builder
		markerSent := false
		for {
			respID, typ, b, err := c.read()
			if err != nil {
				return err
			}
			if !markerSent {
				// The first reply byte proves the server has consumed the command
				// packet, so the marker cannot share a read with it.
				if err := c.write(markerID, typeExecCommand, ""); err != nil {
					return err
				}
				markerSent = true
			}
			if respID == markerID {
				break
			}
			if respID != cmdID {
				return fmt.Errorf("rcon: response id %d did not match request id %d or marker id %d", respID, cmdID, markerID)
			}
			if typ != typeResponseValue {
				return fmt.Errorf("rcon: unexpected packet type %d in response stream", typ)
			}
			buf.WriteString(b)
			if buf.Len() > maxResponseSize {
				return ErrResponseTooLarge
			}
		}
		body = buf.String()
		return nil
	})
	if err != nil {
		// Poison a failed round trip: a partial frame makes the connection unsafe to reuse.
		c.broken = true
		_ = c.conn.Close()
		return "", err
	}
	return body, nil
}

// withDeadline bounds I/O and joins the cancellation watcher before returning.
// Cancellation errors are returned as ctx.Err().
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

// id allocates the next request id, keeping it positive and avoiding -1 (the
// auth-failure sentinel) across int32 wraparound.
func (c *Client) id() int32 {
	id := c.nextID
	c.nextID = (c.nextID + 1) & 0x7FFFFFFF
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
