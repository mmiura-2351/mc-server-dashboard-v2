package rcon

import (
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"testing"
	"time"
)

// fakeServer is an in-process Source RCON server for tests. It authenticates a
// single password and echoes a canned reply per command, recording what it saw.
type fakeServer struct {
	ln       net.Listener
	password string
	// reply maps a received command body to the reply body it returns.
	reply map[string]string
	// authFails forces the auth handshake to reject (id=-1).
	authFails bool

	got chan string
}

func newFakeServer(t *testing.T, password string) *fakeServer {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	fs := &fakeServer{
		ln:       ln,
		password: password,
		reply:    map[string]string{},
		got:      make(chan string, 8),
	}
	go fs.serve()
	t.Cleanup(func() { _ = ln.Close() })
	return fs
}

func (fs *fakeServer) addr() string { return fs.ln.Addr().String() }

func (fs *fakeServer) serve() {
	conn, err := fs.ln.Accept()
	if err != nil {
		return
	}
	defer func() { _ = conn.Close() }()

	for {
		id, typ, body, err := readPacket(conn)
		if err != nil {
			return
		}
		switch typ {
		case typeAuth:
			respID := id
			if fs.authFails || body != fs.password {
				respID = -1
			}
			// Server sends an (empty) RESPONSE_VALUE then the AUTH_RESPONSE.
			_ = writePacket(conn, id, typeResponseValue, "")
			_ = writePacket(conn, respID, typeAuthResponse, "")
		case typeExecCommand:
			fs.got <- body
			_ = writePacket(conn, id, typeResponseValue, fs.reply[body])
		}
	}
}

func TestDialExecute(t *testing.T) {
	fs := newFakeServer(t, "secret")
	fs.reply["list"] = "There are 2 players online"

	ctx := context.Background()
	c, err := Dial(ctx, fs.addr(), "secret")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer func() { _ = c.Close() }()

	out, err := c.Execute(ctx, "list")
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if out != "There are 2 players online" {
		t.Fatalf("Execute reply = %q", out)
	}
	select {
	case got := <-fs.got:
		if got != "list" {
			t.Fatalf("server received %q, want list", got)
		}
	case <-time.After(time.Second):
		t.Fatal("server never received command")
	}
}

// silentFakeServer accepts a connection and authenticates, but never replies to
// an EXEC_COMMAND — modelling an MC server hung mid-tick that accepts the TCP
// connect but goes silent. It blocks the read until the test ends.
func newSilentFakeServer(t *testing.T, password string) *fakeServer {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	fs := &fakeServer{
		ln:       ln,
		password: password,
		reply:    map[string]string{},
		got:      make(chan string, 8),
	}
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		for {
			id, typ, body, err := readPacket(conn)
			if err != nil {
				return
			}
			switch typ {
			case typeAuth:
				respID := id
				if body != fs.password {
					respID = -1
				}
				_ = writePacket(conn, id, typeResponseValue, "")
				_ = writePacket(conn, respID, typeAuthResponse, "")
			case typeExecCommand:
				fs.got <- body
				// Deliberately never reply.
			}
		}
	}()
	t.Cleanup(func() { _ = ln.Close() })
	return fs
}

// With no deadline on ctx, Execute against a silent server must not block
// forever: it falls back to the default per-call deadline and returns an error.
func TestExecuteDefaultDeadlineUnblocksSilentServer(t *testing.T) {
	prev := defaultExecuteTimeout
	defaultExecuteTimeout = 100 * time.Millisecond
	t.Cleanup(func() { defaultExecuteTimeout = prev })

	fs := newSilentFakeServer(t, "secret")
	c, err := Dial(context.Background(), fs.addr(), "secret")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer func() { _ = c.Close() }()

	done := make(chan error, 1)
	go func() { _, err := c.Execute(context.Background(), "list"); done <- err }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("Execute returned nil against a silent server, want timeout error")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Execute hung past the default deadline against a silent server")
	}
}

// Cancelling ctx must unblock a read that is waiting on a silent server promptly,
// well before the default deadline.
func TestExecuteCtxCancellationUnblocksSilentServer(t *testing.T) {
	prev := defaultExecuteTimeout
	defaultExecuteTimeout = time.Hour // ensure it is cancellation, not the deadline, that unblocks
	t.Cleanup(func() { defaultExecuteTimeout = prev })

	fs := newSilentFakeServer(t, "secret")
	c, err := Dial(context.Background(), fs.addr(), "secret")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer func() { _ = c.Close() }()

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { _, err := c.Execute(ctx, "list"); done <- err }()

	// Wait until the server has received the command, so the client is blocked
	// in read, then cancel.
	select {
	case <-fs.got:
	case <-time.After(time.Second):
		t.Fatal("server never received command")
	}
	cancel()

	select {
	case err := <-done:
		if err == nil {
			t.Fatal("Execute returned nil after ctx cancellation, want error")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Execute did not unblock promptly after ctx cancellation")
	}
}

func TestDialAuthFailure(t *testing.T) {
	fs := newFakeServer(t, "secret")
	_, err := Dial(context.Background(), fs.addr(), "wrong")
	if !errors.Is(err, ErrAuthFailed) {
		t.Fatalf("Dial error = %v, want ErrAuthFailed", err)
	}
}

// An oversized frame (length prefix beyond maxBodyLen) is rejected before any
// body allocation, with a clean error and no panic.
func TestReadRejectsOversizedFrame(t *testing.T) {
	srv, cli := net.Pipe()
	c := &Client{conn: cli, nextID: 1}
	t.Cleanup(func() { _ = cli.Close() })

	go func() {
		defer func() { _ = srv.Close() }()
		var length int32 = maxBodyLen + 1
		_ = binary.Write(srv, binary.LittleEndian, length)
	}()

	done := make(chan error, 1)
	go func() { _, _, _, err := c.read(); done <- err }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("read accepted an oversized frame, want error")
		}
	case <-time.After(time.Second):
		t.Fatal("read hung on an oversized frame")
	}
}

// A truncated frame (length prefix promises more body than is delivered before
// the peer closes) yields a clean read error and no hang.
func TestReadRejectsTruncatedFrame(t *testing.T) {
	srv, cli := net.Pipe()
	c := &Client{conn: cli, nextID: 1}
	t.Cleanup(func() { _ = cli.Close() })

	go func() {
		// Promise a 100-byte body but send only a few bytes, then close.
		_ = binary.Write(srv, binary.LittleEndian, int32(100))
		_, _ = srv.Write([]byte{1, 2, 3})
		_ = srv.Close()
	}()

	done := make(chan error, 1)
	go func() { _, _, _, err := c.read(); done <- err }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("read accepted a truncated frame, want error")
		}
	case <-time.After(time.Second):
		t.Fatal("read hung on a truncated frame")
	}
}

// readPacket reads one RCON packet from the test server side.
func readPacket(r io.Reader) (id, typ int32, body string, err error) {
	var length int32
	if err = binary.Read(r, binary.LittleEndian, &length); err != nil {
		return 0, 0, "", err
	}
	buf := make([]byte, length)
	if _, err = io.ReadFull(r, buf); err != nil {
		return 0, 0, "", err
	}
	id = int32(binary.LittleEndian.Uint32(buf[0:4]))
	typ = int32(binary.LittleEndian.Uint32(buf[4:8]))
	// body is buf[8:] minus the two trailing NUL bytes.
	body = string(buf[8 : len(buf)-2])
	return id, typ, body, nil
}

// writePacket writes one RCON packet from the test server side.
func writePacket(w io.Writer, id, typ int32, body string) error {
	payload := make([]byte, 8+len(body)+2)
	binary.LittleEndian.PutUint32(payload[0:4], uint32(id))
	binary.LittleEndian.PutUint32(payload[4:8], uint32(typ))
	copy(payload[8:], body)
	out := make([]byte, 4+len(payload))
	binary.LittleEndian.PutUint32(out[0:4], uint32(len(payload)))
	copy(out[4:], payload)
	_, err := w.Write(out)
	return err
}
