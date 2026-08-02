package rcon

import (
	"context"
	"encoding/binary"
	"errors"
	"io"
	"math"
	"net"
	"strings"
	"sync"
	"testing"
	"time"
)

// mcReadSettle is how long the fake server waits for the wire to go quiet before
// each command read, so "what one read() returns" is what the client has actually
// put on the wire rather than a race. Two packets written back-to-back land within
// microseconds on loopback, so this window makes a client that keeps two request
// packets in flight fail deterministically; a client that keeps only one in
// flight is unaffected however long the window is.
//
// The settle covers only the post-auth (command) read, not the auth read: the
// sequencing it catches is a property of the EXEC_COMMAND path (a command written
// back-to-back with its end-of-response marker, issue #2618), and the auth
// handshake only ever puts a single packet on the wire. Keeping the auth read off
// the settle also keeps this constant decoupled from Dial's handshake deadline —
// raising it no longer eats into that budget (issue #2624). See serve().
const mcReadSettle = 25 * time.Millisecond

// errMCFraming is what the fake reports when a read carried something other than
// exactly one packet. See readPacketMC.
var errMCFraming = errors.New("fake mc server: read did not carry exactly one packet")

// fakeServer is an in-process RCON server for tests. It authenticates a single
// password and echoes a canned reply per command, recording what it saw.
//
// It frames reads the way Minecraft's own RCON server does, not the way a
// stream-oriented client would — see readPacketMC. That distinction is the whole
// point of the fake: a stub that reassembles the request stream accepts wire
// traffic vanilla Minecraft rejects, which is exactly how issue #2618 survived
// a full test suite.
type fakeServer struct {
	ln       net.Listener
	password string
	// reply maps a received command body to the reply body it returns.
	reply map[string]string
	// fragmentReply maps a received command body to multiple reply fragments
	// sent as separate RESPONSE_VALUE packets with the same id.
	fragmentReply map[string][]string
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
	// framingViolation carries the byte count of a read that did not carry
	// exactly one packet, so a test can report the cause instead of the bare EOF
	// the client sees once the server drops the connection.
	framingViolation chan int
}

func newFakeServer(t *testing.T, password string) *fakeServer {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	fs := &fakeServer{
		ln:               ln,
		password:         password,
		reply:            map[string]string{},
		fragmentReply:    map[string][]string{},
		got:              make(chan string, 8),
		framingViolation: make(chan int, 1),
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

	// settle turns on once the handshake is done, so the settle window covers the
	// command reads (where a client can keep two request packets in flight) but
	// not the single-packet auth read. See mcReadSettle.
	settle := false
	for {
		id, typ, body, n, err := readPacketMC(conn, settle)
		if errors.Is(err, errMCFraming) {
			// Vanilla drops the connection on a malformed read without executing
			// anything. Record the byte count first so a test can name the cause.
			select {
			case fs.framingViolation <- n:
			default:
			}
			return
		}
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
			settle = true // command reads settle; the auth read did not.
		case typeExecCommand:
			fs.got <- body
			if fs.silent {
				continue // Deliberately never reply.
			}
			if fragments, ok := fs.fragmentReply[body]; ok {
				for _, frag := range fragments {
					_ = writePacket(conn, id, typeResponseValue, frag)
				}
			} else {
				_ = writePacket(conn, id, typeResponseValue, fs.reply[body])
			}
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
		ln:               ln,
		password:         password,
		reply:            map[string]string{},
		fragmentReply:    map[string][]string{},
		silent:           true,
		got:              make(chan string, 8),
		framingViolation: make(chan int, 1),
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

// Execute must never keep two request packets in flight (issue #2618): vanilla
// Minecraft reads one packet per read() and drops the connection when the length
// prefix does not match the bytes that read returned, so a command written
// back-to-back with its end-of-response marker lands in one read and kills the
// connection before either command runs. Against a real 1.21.1 server that made
// every save-off / save-all fail with "rcon: read length: EOF", so no running
// server could be quiesced.
func TestExecuteKeepsOneRequestPacketInFlight(t *testing.T) {
	fs := newFakeServer(t, "secret")
	fs.reply["save-off"] = "Automatic saving is now disabled"

	ctx := context.Background()
	c, err := Dial(ctx, fs.addr(), "secret")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer func() { _ = c.Close() }()

	out, execErr := c.Execute(ctx, "save-off")
	select {
	case n := <-fs.framingViolation:
		t.Fatalf("server read %d bytes in one read: Execute put two request packets on the wire at once, "+
			"which vanilla Minecraft rejects as one malformed packet (Execute error = %v)", n, execErr)
	default:
	}
	if execErr != nil {
		t.Fatalf("Execute: %v", execErr)
	}
	if want := "Automatic saving is now disabled"; out != want {
		t.Fatalf("Execute reply = %q, want %q", out, want)
	}
}

// A command whose reply body is empty must still complete. Vanilla always sends
// at least one RESPONSE_VALUE per command — measured on 1.21.1, where `say` and
// `me` reply with a zero-byte body rather than nothing at all — which is what
// lets Execute wait for the first reply before writing its marker. A server that
// answered some command with no packet would strand that wait, so pin the
// boundary.
func TestExecuteEmptyReplyCompletes(t *testing.T) {
	fs := newFakeServer(t, "secret")
	fs.reply["say hello"] = ""

	ctx := context.Background()
	c, err := Dial(ctx, fs.addr(), "secret")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer func() { _ = c.Close() }()

	out, err := c.Execute(ctx, "say hello")
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if out != "" {
		t.Fatalf("Execute reply = %q, want empty", out)
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
		ln:               ln,
		password:         "secret",
		reply:            map[string]string{},
		silentAuth:       true,
		got:              make(chan string, 8),
		framingViolation: make(chan int, 1),
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

// A fragmented reply (multiple RESPONSE_VALUE packets with the same id) is
// reassembled into a single string by Execute.
func TestExecuteFragmentedReply(t *testing.T) {
	fs := newFakeServer(t, "secret")
	fs.fragmentReply["help"] = []string{"fragment-1-", "fragment-2-", "fragment-3"}

	ctx := context.Background()
	c, err := Dial(ctx, fs.addr(), "secret")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer func() { _ = c.Close() }()

	out, err := c.Execute(ctx, "help")
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	want := "fragment-1-fragment-2-fragment-3"
	if out != want {
		t.Fatalf("Execute reply = %q, want %q", out, want)
	}
}

// After a fragmented reply, the next Execute on the same connection must return
// the correct reply with no desync from leftover fragments.
func TestExecuteFragmentedThenNormal(t *testing.T) {
	fs := newFakeServer(t, "secret")
	fs.fragmentReply["help"] = []string{"frag-a-", "frag-b"}
	fs.reply["list"] = "There are 0 players online"

	ctx := context.Background()
	c, err := Dial(ctx, fs.addr(), "secret")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer func() { _ = c.Close() }()

	out1, err := c.Execute(ctx, "help")
	if err != nil {
		t.Fatalf("Execute(help): %v", err)
	}
	if want := "frag-a-frag-b"; out1 != want {
		t.Fatalf("Execute(help) = %q, want %q", out1, want)
	}

	out2, err := c.Execute(ctx, "list")
	if err != nil {
		t.Fatalf("Execute(list): %v", err)
	}
	if want := "There are 0 players online"; out2 != want {
		t.Fatalf("Execute(list) = %q, want %q", out2, want)
	}
}

// Execute must reject a reassembled response whose total size exceeds
// maxResponseSize, returning ErrResponseTooLarge.
func TestExecuteRejectsOversizedResponse(t *testing.T) {
	srv, cli := net.Pipe()
	t.Cleanup(func() { _ = cli.Close() })

	go func() {
		defer func() { _ = srv.Close() }()
		cmdID, _, _, err := readPacket(srv)
		if err != nil {
			return
		}
		// Execute writes the marker only after the command's first reply packet
		// arrives (#2618), so send one fragment, drain the marker, then send the
		// rest — fragments totalling > maxResponseSize.
		frag := strings.Repeat("X", 4096)
		if err := writePacket(srv, cmdID, typeResponseValue, frag); err != nil {
			return
		}
		if _, _, _, err := readPacket(srv); err != nil { // marker
			return
		}
		for i := 0; i < 299; i++ {
			if err := writePacket(srv, cmdID, typeResponseValue, frag); err != nil {
				return
			}
		}
	}()

	c := &Client{conn: cli, nextID: 1}
	_, err := c.Execute(context.Background(), "list")
	if !errors.Is(err, ErrResponseTooLarge) {
		t.Fatalf("Execute error = %v, want ErrResponseTooLarge", err)
	}
}

// id() must stay positive and never return -1 (the auth-failure sentinel),
// even after int32 overflow.
func TestIDWrapsPositiveAndSkipsNegativeOne(t *testing.T) {
	c := &Client{nextID: math.MaxInt32 - 1}

	ids := make([]int32, 5)
	for i := range ids {
		ids[i] = c.id()
	}
	for i, id := range ids {
		if id < 0 {
			t.Fatalf("id() call %d returned negative id %d", i, id)
		}
		if id == -1 {
			t.Fatalf("id() call %d returned -1 (auth-failure sentinel)", i)
		}
	}
}

// The reassembly loop must reject a fragment whose packet type is not
// typeResponseValue, surfacing a protocol desync error.
func TestExecuteRejectsWrongPacketType(t *testing.T) {
	srv, cli := net.Pipe()
	t.Cleanup(func() { _ = cli.Close() })

	go func() {
		defer func() { _ = srv.Close() }()
		cmdID, _, _, err := readPacket(srv)
		if err != nil {
			return
		}
		// Execute writes the marker only after the command's first reply packet
		// arrives (#2618), so send one valid fragment, drain the marker, then send
		// a fragment with the wrong packet type.
		_ = writePacket(srv, cmdID, typeResponseValue, "good")
		if _, _, _, err := readPacket(srv); err != nil { // marker
			return
		}
		_ = writePacket(srv, cmdID, typeAuth, "bad-type")
	}()

	c := &Client{conn: cli, nextID: 1}
	_, err := c.Execute(context.Background(), "list")
	if err == nil {
		t.Fatal("Execute accepted a fragment with wrong packet type, want error")
	}
	if !strings.Contains(err.Error(), "unexpected packet type") {
		t.Fatalf("Execute error = %v, want error mentioning unexpected packet type", err)
	}
}

// readPacketMC reads one request packet the way vanilla Minecraft's RCON server
// does. Measured against a real 1.21.1 server for issue #2618: it performs ONE
// read() per loop iteration into a fixed 1460-byte buffer and requires the
// length prefix to equal the bytes that read returned, minus the 4 prefix bytes.
// Anything else — including two well-formed packets that happened to arrive in
// the same read — is a malformed packet, and the server closes the connection
// without executing either.
//
// So a client must never keep two request packets in flight: writing a command
// and its end-of-response marker back-to-back puts both in one read and kills
// the connection. A stream-oriented reader (readPacket below) reassembles that
// traffic happily and hides the defect, which is why the fake models the real
// framing instead.
//
// n is the byte count the read returned, reported so a framing violation can be
// described rather than surfacing as a bare EOF on the client.
//
// settle gates the pre-read settle window (mcReadSettle): the caller passes true
// for command reads, where a client can keep two request packets in flight, and
// false for the single-packet auth read, which has nothing to settle for.
func readPacketMC(conn net.Conn, settle bool) (id, typ int32, body string, n int, err error) {
	// Let the wire settle first, so a client that put two packets on it is seen
	// to have done so rather than racing the read.
	if settle {
		time.Sleep(mcReadSettle)
	}

	buf := make([]byte, 1460) // vanilla's RconClient buffer size
	n, err = conn.Read(buf)
	if err != nil {
		return 0, 0, "", n, err
	}
	if n < 10 {
		return 0, 0, "", n, errMCFraming
	}
	if length := int32(binary.LittleEndian.Uint32(buf[0:4])); length != int32(n-4) {
		return 0, 0, "", n, errMCFraming
	}
	id = int32(binary.LittleEndian.Uint32(buf[4:8]))
	typ = int32(binary.LittleEndian.Uint32(buf[8:12]))
	body = string(buf[12 : n-2]) // strip the two trailing NULs
	return id, typ, body, n, nil
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
