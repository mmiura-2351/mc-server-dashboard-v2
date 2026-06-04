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

// Client is a single authenticated RCON connection. It is not safe for
// concurrent use; callers serialize commands per server.
type Client struct {
	conn   net.Conn
	nextID int32
}

// Dial opens an RCON connection to addr and authenticates with password. It
// returns ErrAuthFailed on a rejected password. The dial honours ctx's deadline.
func Dial(ctx context.Context, addr, password string) (*Client, error) {
	var d net.Dialer
	conn, err := d.DialContext(ctx, "tcp", addr)
	if err != nil {
		return nil, fmt.Errorf("rcon: dial %s: %w", addr, err)
	}
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}

	c := &Client{conn: conn, nextID: 1}
	if err := c.authenticate(password); err != nil {
		_ = conn.Close()
		return nil, err
	}
	_ = conn.SetDeadline(time.Time{})
	return c, nil
}

// Execute sends one command line and returns the server's reply body. It honours
// ctx's deadline for the round trip.
func (c *Client) Execute(ctx context.Context, line string) (string, error) {
	if deadline, ok := ctx.Deadline(); ok {
		_ = c.conn.SetDeadline(deadline)
		defer func() { _ = c.conn.SetDeadline(time.Time{}) }()
	}
	id := c.id()
	if err := c.write(id, typeExecCommand, line); err != nil {
		return "", err
	}
	respID, _, body, err := c.read()
	if err != nil {
		return "", err
	}
	if respID != id {
		return "", fmt.Errorf("rcon: response id %d did not match request id %d", respID, id)
	}
	return body, nil
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
