package rcon

import (
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"sync"
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
	// silent makes the server authenticate but never reply to an EXEC_COMMAND,
	// modelling an MC server hung mid-tick that accepts the TCP connect but goes
	// silent. The client's read then blocks until the test ends.
	silent bool
	// silentAuth makes the server accept the TCP connect but never send an
	// AUTH_RESPONSE, modelling a peer that wedges the dial+authenticate handshake.
	silentAuth bool

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
			if fs.silentAuth {
				continue // Deliberately never send an AUTH_RESPONSE.
			}
			respID := id
			if fs.authFails || body != fs.password {
				respID = -1
			}
			// Server sends an (empty) RESPONSE_VALUE then the AUTH_RESPONSE.
			_ = writePacket(conn, id, typeResponseValue, "")
			_ = writePacket(conn, respID, typeAuthResponse, "")
		case typeExecCommand:
			fs.got <- body
			if fs.silent {
				continue // Deliberately never reply.
			}
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

// newSilentFakeServer accepts and authenticates but never replies to an
// EXEC_COMMAND. See fakeServer.silent.
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
		silent:   true,
		got:      make(chan string, 8),
	}
	go fs.serve()
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

	var wg sync.WaitGroup
	done := make(chan error, 1)
	wg.Add(1)
	go func() {
		defer wg.Done()
		_, err := c.Execute(context.Background(), "list")
		done <- err
	}()
	// Close the connection on cleanup so a hung Execute unblocks, then wait for
	// the goroutine to finish: it can never be left running past the test (racing
	// the t.Cleanup that restores defaultExecuteTimeout) even on the failure path.
	t.Cleanup(func() {
		_ = c.Close()
		wg.Wait()
	})
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
		// The failure was caused by ctx cancellation, so Execute must surface
		// ctx.Err() rather than the opaque underlying i/o timeout.
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("Execute error = %v, want context.Canceled", err)
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

// With no deadline on ctx, Dial against an address that never completes the SYN
// handshake (firewalled / blackholed) must bound the TCP connect at the default
// fallback rather than ride the OS's ~2-minute SYN timeout (issue #832). It uses
// a TEST-NET-3 address (RFC 5737, guaranteed non-routable) so the SYN is dropped
// with no RST, exercising the connect bound rather than a fast refusal.
func TestDialDefaultDeadlineBoundsConnect(t *testing.T) {
	prev := defaultExecuteTimeout
	defaultExecuteTimeout = 100 * time.Millisecond
	t.Cleanup(func() { defaultExecuteTimeout = prev })

	done := make(chan error, 1)
	go func() {
		_, err := Dial(context.Background(), "203.0.113.1:25575", "secret")
		done <- err
	}()

	select {
	case err := <-done:
		if err == nil {
			t.Fatal("Dial returned nil against a non-accepting address, want timeout error")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Dial hung past the default deadline against a non-accepting address")
	}
}

// With no deadline on ctx, Dial against a peer that TCP-accepts but never sends
// an AUTH_RESPONSE must not block forever: the handshake falls back to the
// default per-call deadline and returns an error.
func TestDialDefaultDeadlineUnblocksSilentAuthServer(t *testing.T) {
	prev := defaultExecuteTimeout
	defaultExecuteTimeout = 100 * time.Millisecond
	t.Cleanup(func() { defaultExecuteTimeout = prev })

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	fs := &fakeServer{
		ln:         ln,
		password:   "secret",
		reply:      map[string]string{},
		silentAuth: true,
		got:        make(chan string, 8),
	}
	go fs.serve()

	// Cleanups run LIFO: register wg.Wait first so ln.Close (registered after)
	// runs before it. A hung Dial would otherwise block wg.Wait forever with no
	// unblock; closing the listener first lets a regression fail cleanly.
	var wg sync.WaitGroup
	t.Cleanup(func() { wg.Wait() })
	t.Cleanup(func() { _ = ln.Close() })

	done := make(chan error, 1)
	wg.Add(1)
	go func() {
		defer wg.Done()
		_, err := Dial(context.Background(), fs.addr(), "secret")
		done <- err
	}()

	select {
	case err := <-done:
		if err == nil {
			t.Fatal("Dial returned nil against a silent-auth server, want timeout error")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Dial hung past the default deadline against a silent-auth server")
	}
}

// After a round trip fails mid-stream, the connection is poisoned: a subsequent
// Execute on the same Client fails fast with ErrConnBroken instead of reading a
// stale, mis-framed response. This forces the only reuser (instancemanager's
// quiesce save-on, on the next snapshot) to redial rather than act on garbage.
func TestExecutePoisonsConnAfterIOError(t *testing.T) {
	prev := defaultExecuteTimeout
	defaultExecuteTimeout = 100 * time.Millisecond
	t.Cleanup(func() { defaultExecuteTimeout = prev })

	fs := newSilentFakeServer(t, "secret")
	c, err := Dial(context.Background(), fs.addr(), "secret")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	t.Cleanup(func() { _ = c.Close() })

	if _, err := c.Execute(context.Background(), "save-off"); err == nil {
		t.Fatal("first Execute returned nil against a silent server, want timeout error")
	}

	// The connection is now poisoned; the next Execute must fail fast without
	// touching the wire.
	_, err = c.Execute(context.Background(), "save-on")
	if !errors.Is(err, ErrConnBroken) {
		t.Fatalf("second Execute error = %v, want ErrConnBroken", err)
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
