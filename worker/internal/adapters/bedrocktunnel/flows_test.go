package bedrocktunnel

import (
	"context"
	"encoding/binary"
	"errors"
	"net"
	"sync"
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
