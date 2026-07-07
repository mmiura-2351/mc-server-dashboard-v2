package bedrocktunnel

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"log/slog"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeSender records every datagram frame it is asked to send, standing in
// for a *quic.Conn on the reply path.
type fakeSender struct {
	mu   sync.Mutex
	sent [][]byte
}

func (f *fakeSender) SendDatagram(p []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	cp := make([]byte, len(p))
	copy(cp, p)
	f.sent = append(f.sent, cp)
	return nil
}

func (f *fakeSender) waitSent(t *testing.T, n int) [][]byte {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		f.mu.Lock()
		if len(f.sent) >= n {
			out := make([][]byte, len(f.sent))
			copy(out, f.sent)
			f.mu.Unlock()
			return out
		}
		f.mu.Unlock()
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("sender got fewer than %d datagrams", n)
	return nil
}

// forward dials a fresh local socket the first time a flow id is seen, and
// reuses it for a repeat of the same id.
func TestFlowRegistryForwardCreatesOneSocketPerFlow(t *testing.T) {
	geyser := newFakeGeyser(t)
	sender := &fakeSender{}
	var dials int
	var mu sync.Mutex
	dialUDP := func(_ context.Context, _ string) (net.Conn, error) {
		mu.Lock()
		dials++
		mu.Unlock()
		return net.Dial("udp", geyser.addr())
	}
	r := newFlowRegistry(dialUDP, geyser.addr(), sender, discardLogger(), "s1")
	defer r.closeAll()

	if err := r.forward(context.Background(), 1, []byte("a")); err != nil {
		t.Fatalf("forward(1): %v", err)
	}
	if err := r.forward(context.Background(), 2, []byte("b")); err != nil {
		t.Fatalf("forward(2): %v", err)
	}
	if err := r.forward(context.Background(), 1, []byte("a2")); err != nil {
		t.Fatalf("forward(1) again: %v", err)
	}

	mu.Lock()
	got := dials
	mu.Unlock()
	if got != 2 {
		t.Fatalf("dialUDP called %d times, want 2 (one per distinct flow id)", got)
	}

	frames := sender.waitSent(t, 3)
	for _, f := range frames {
		id := binary.BigEndian.Uint32(f[:flowIDSize])
		payload := string(f[flowIDSize:])
		if id != 1 && id != 2 {
			t.Fatalf("unexpected flow id %d in reply", id)
		}
		if len(payload) < 5 || payload[:5] != "echo:" {
			t.Fatalf("reply payload = %q, want an echo: prefix", payload)
		}
	}
}

// forward surfaces a dial failure to the caller (pump logs and drops it) and
// leaves no flow registered.
func TestFlowRegistryForwardDialFailure(t *testing.T) {
	sender := &fakeSender{}
	wantErr := errors.New("dial refused")
	dialUDP := func(context.Context, string) (net.Conn, error) { return nil, wantErr }
	r := newFlowRegistry(dialUDP, "127.0.0.1:1", sender, discardLogger(), "s1")
	defer r.closeAll()

	if err := r.forward(context.Background(), 1, []byte("x")); !errors.Is(err, wantErr) {
		t.Fatalf("forward error = %v, want %v", err, wantErr)
	}
}

// evictIdle closes and forgets a flow idle for at least flowIdleTimeout,
// unblocking its readPump (a further write to the socket then fails).
func TestFlowRegistryEvictIdleClosesSocket(t *testing.T) {
	geyser := newFakeGeyser(t)
	sender := &fakeSender{}
	dialUDP := func(_ context.Context, _ string) (net.Conn, error) { return net.Dial("udp", geyser.addr()) }
	r := newFlowRegistry(dialUDP, geyser.addr(), sender, discardLogger(), "s1")
	defer r.closeAll()

	if err := r.forward(context.Background(), 5, []byte("x")); err != nil {
		t.Fatalf("forward: %v", err)
	}

	r.mu.Lock()
	fs := r.byID[5]
	fs.lastSeen = time.Now().Add(-flowIdleTimeout - time.Second)
	r.mu.Unlock()

	r.evictIdle()

	r.mu.Lock()
	_, stillThere := r.byID[5]
	r.mu.Unlock()
	if stillThere {
		t.Fatal("flow 5 still present after evictIdle, want evicted")
	}
	if _, err := fs.conn.Write([]byte("after-close")); err == nil {
		t.Fatal("write to evicted flow's socket succeeded, want it closed")
	}
}

// An active flow (lastSeen recent) survives a sweep.
func TestFlowRegistryEvictIdleKeepsActiveFlow(t *testing.T) {
	geyser := newFakeGeyser(t)
	sender := &fakeSender{}
	dialUDP := func(_ context.Context, _ string) (net.Conn, error) { return net.Dial("udp", geyser.addr()) }
	r := newFlowRegistry(dialUDP, geyser.addr(), sender, discardLogger(), "s1")
	defer r.closeAll()

	if err := r.forward(context.Background(), 9, []byte("x")); err != nil {
		t.Fatalf("forward: %v", err)
	}

	r.evictIdle()

	r.mu.Lock()
	_, stillThere := r.byID[9]
	r.mu.Unlock()
	if !stillThere {
		t.Fatal("active flow 9 evicted, want kept")
	}
}

// syncBuffer is a concurrency-safe bytes.Buffer for capturing slog output,
// mirroring worker/internal/adapters/containerdriver/containerdriver_test.go's
// helper of the same name.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// seedFlows directly inserts n placeholder flows into r.byID, bypassing
// dialUDP/forward, so ceiling tests can cheaply fill the registry up to (or
// past) maxFlowsPerTunnel without dialing thousands of real sockets. Each
// placeholder is backed by an in-memory net.Pipe end (no OS socket), which
// still satisfies net.Conn for evictIdle/closeAll's Close calls. Seeded ids
// start at 1_000_000, clear of any id a test forwards explicitly.
func seedFlows(r *flowRegistry, n int, lastSeen time.Time) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for i := 0; i < n; i++ {
		c, _ := net.Pipe()
		r.byID[uint32(1_000_000+i)] = &flowSocket{conn: c, lastSeen: lastSeen}
	}
}

// A new flow id arriving once the registry already holds maxFlowsPerTunnel
// flows is dropped: no socket is dialed and no error is returned (the caller,
// pump, would otherwise log it per datagram).
func TestFlowRegistryForwardDropsNewFlowPastCeiling(t *testing.T) {
	sender := &fakeSender{}
	// dialUDP must not be called for a flow past the ceiling: erroring here
	// makes any such call surface as a non-nil forward() error below, rather
	// than silently opening a socket the test wouldn't otherwise notice.
	dialUDP := func(context.Context, string) (net.Conn, error) {
		return nil, errors.New("dialUDP must not be called past the ceiling")
	}
	r := newFlowRegistry(dialUDP, "127.0.0.1:1", sender, discardLogger(), "s1")
	defer r.closeAll()
	seedFlows(r, maxFlowsPerTunnel, time.Now())

	if err := r.forward(context.Background(), 42, []byte("x")); err != nil {
		t.Fatalf("forward() at ceiling = %v, want nil (dropped, not an error)", err)
	}

	r.mu.Lock()
	_, present := r.byID[42]
	r.mu.Unlock()
	if present {
		t.Fatal("flow 42 registered despite being past the ceiling")
	}
}

// A new flow id is admitted normally while the registry is below the
// ceiling.
func TestFlowRegistryForwardBelowCeilingSucceeds(t *testing.T) {
	geyser := newFakeGeyser(t)
	sender := &fakeSender{}
	dialUDP := func(context.Context, string) (net.Conn, error) { return net.Dial("udp", geyser.addr()) }
	r := newFlowRegistry(dialUDP, geyser.addr(), sender, discardLogger(), "s1")
	defer r.closeAll()
	seedFlows(r, maxFlowsPerTunnel-1, time.Now())

	if err := r.forward(context.Background(), 7, []byte("x")); err != nil {
		t.Fatalf("forward() below ceiling: %v", err)
	}

	r.mu.Lock()
	_, present := r.byID[7]
	r.mu.Unlock()
	if !present {
		t.Fatal("flow 7 not registered, want admitted (registry was below the ceiling)")
	}
}

// The ceiling-reached warning is logged exactly once per registry, even
// across many dropped datagrams for many distinct new flow ids -- not once
// per datagram, which would be a log-spam vector under a misbehaving relay.
func TestFlowRegistryForwardLogsCeilingOnceNotPerDatagram(t *testing.T) {
	var buf syncBuffer
	logger := slog.New(slog.NewTextHandler(&buf, nil))
	sender := &fakeSender{}
	dialUDP := func(context.Context, string) (net.Conn, error) {
		return nil, errors.New("dialUDP must not be called past the ceiling")
	}
	r := newFlowRegistry(dialUDP, "127.0.0.1:1", sender, logger, "s1")
	defer r.closeAll()
	seedFlows(r, maxFlowsPerTunnel, time.Now())

	for _, id := range []uint32{1, 2, 3} {
		if err := r.forward(context.Background(), id, []byte("x")); err != nil {
			t.Fatalf("forward(%d) at ceiling: %v", id, err)
		}
	}

	logged := buf.String()
	if got := strings.Count(logged, "max flows per tunnel reached"); got != 1 {
		t.Fatalf("ceiling warning logged %d times across 3 dropped flows, want exactly 1; log: %q", got, logged)
	}
}

// evictIdle freeing a slot lets a later new flow be admitted again.
func TestFlowRegistryEvictIdleFreesCeilingCapacity(t *testing.T) {
	geyser := newFakeGeyser(t)
	sender := &fakeSender{}
	dialUDP := func(context.Context, string) (net.Conn, error) { return net.Dial("udp", geyser.addr()) }
	r := newFlowRegistry(dialUDP, geyser.addr(), sender, discardLogger(), "s1")
	defer r.closeAll()
	seedFlows(r, maxFlowsPerTunnel, time.Now().Add(-flowIdleTimeout-time.Second))

	if err := r.forward(context.Background(), 99, []byte("x")); err != nil {
		t.Fatalf("forward() at ceiling: %v", err)
	}
	r.mu.Lock()
	_, presentBefore := r.byID[99]
	r.mu.Unlock()
	if presentBefore {
		t.Fatal("flow 99 admitted while still at the ceiling, want dropped")
	}

	r.evictIdle()

	if err := r.forward(context.Background(), 99, []byte("x")); err != nil {
		t.Fatalf("forward() after eviction: %v", err)
	}
	r.mu.Lock()
	_, presentAfter := r.byID[99]
	r.mu.Unlock()
	if !presentAfter {
		t.Fatal("flow 99 not admitted after eviction freed capacity")
	}
}

// readPump self-evicts the flow and closes its socket on a non-eviction read
// error, allowing forward to redial a fresh socket on the next datagram.
func TestFlowRegistryReadPumpErrorEvictsFlowAndRedials(t *testing.T) {
	geyser := newFakeGeyser(t)
	sender := &fakeSender{}
	var dials atomic.Int32
	dialUDP := func(_ context.Context, _ string) (net.Conn, error) {
		dials.Add(1)
		return net.Dial("udp", geyser.addr())
	}
	r := newFlowRegistry(dialUDP, geyser.addr(), sender, discardLogger(), "s1")
	defer r.closeAll()

	// First forward: dials socket #1, starts readPump.
	if err := r.forward(context.Background(), 1, []byte("a")); err != nil {
		t.Fatalf("forward(1): %v", err)
	}
	_ = sender.waitSent(t, 1) // wait for the echo reply to confirm pump is running
	if dials.Load() != 1 {
		t.Fatalf("dials = %d, want 1", dials.Load())
	}

	// Simulate a read error by closing the socket under readPump.
	r.mu.Lock()
	fs := r.byID[1]
	r.mu.Unlock()
	_ = fs.conn.Close()

	// readPump should self-evict: byID[1] disappears.
	waitFor(t, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		_, ok := r.byID[1]
		return !ok
	})

	// A second forward redials a fresh socket.
	if err := r.forward(context.Background(), 1, []byte("b")); err != nil {
		t.Fatalf("forward(1) after eviction: %v", err)
	}
	if dials.Load() != 2 {
		t.Fatalf("dials = %d after re-forward, want 2", dials.Load())
	}

	// The new pump echoes back.
	frames := sender.waitSent(t, 2)
	last := frames[len(frames)-1]
	id := binary.BigEndian.Uint32(last[:flowIDSize])
	payload := string(last[flowIDSize:])
	if id != 1 {
		t.Fatalf("reply flow id = %d, want 1", id)
	}
	if len(payload) < 5 || payload[:5] != "echo:" {
		t.Fatalf("reply payload = %q, want echo: prefix", payload)
	}
}

// readPump does NOT evict a replacement flow that was inserted for the same
// id after the original was already removed (pointer-identity guard).
func TestFlowRegistryReadPumpErrorDoesNotEvictReplacementFlow(t *testing.T) {
	// Build a registry; we won't use forward -- we drive readPump manually.
	sender := &fakeSender{}
	dialUDP := func(context.Context, string) (net.Conn, error) {
		return nil, errors.New("unused")
	}
	r := newFlowRegistry(dialUDP, "127.0.0.1:1", sender, discardLogger(), "s1")
	defer r.closeAll()

	// fs1: the old socket whose readPump will error.
	c1a, c1b := net.Pipe()
	fs1 := &flowSocket{conn: c1a, lastSeen: time.Now()}

	// fs2: a replacement already inserted under the same id.
	c2a, _ := net.Pipe()
	fs2 := &flowSocket{conn: c2a, lastSeen: time.Now()}

	r.mu.Lock()
	r.byID[7] = fs2 // replacement is already in place
	r.mu.Unlock()

	// Close fs1's read end so readPump returns immediately.
	_ = c1b.Close()

	// Run readPump synchronously for the OLD socket -- it should see the
	// pointer mismatch and leave fs2 untouched.
	r.readPump(7, fs1)

	r.mu.Lock()
	cur, ok := r.byID[7]
	r.mu.Unlock()
	if !ok {
		t.Fatal("byID[7] deleted, want replacement fs2 to survive")
	}
	if cur != fs2 {
		t.Fatal("byID[7] changed, want it to still be fs2")
	}
}

// closeAll closes every live flow socket, discarding all connection-scoped
// flow state (docs/app/BEDROCK_TUNNEL.md Section 5: required on redial).
func TestFlowRegistryCloseAllClosesEverySocket(t *testing.T) {
	geyser := newFakeGeyser(t)
	sender := &fakeSender{}
	dialUDP := func(_ context.Context, _ string) (net.Conn, error) { return net.Dial("udp", geyser.addr()) }
	r := newFlowRegistry(dialUDP, geyser.addr(), sender, discardLogger(), "s1")

	if err := r.forward(context.Background(), 1, []byte("a")); err != nil {
		t.Fatalf("forward: %v", err)
	}
	r.mu.Lock()
	fs := r.byID[1]
	r.mu.Unlock()

	r.closeAll()

	if _, err := fs.conn.Write([]byte("after-close")); err == nil {
		t.Fatal("write after closeAll succeeded, want the socket closed")
	}
}
