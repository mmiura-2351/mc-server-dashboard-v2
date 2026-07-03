package bedrock

import (
	"bytes"
	"context"
	"encoding/binary"
	"io"
	"log/slog"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/quic-go/quic-go"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/netutil"
)

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// runTunnel binds a Tunnel on an OS-assigned UDP port over server (bind, like
// production, uses the wildcard address, so the returned dial address
// substitutes the concrete loopback IP -- a UDP wildcard bind is not itself a
// valid destination). It starts the tunnel running in the background and
// returns the loopback dial address plus a stop func that cancels and waits
// for run() to return.
func runTunnel(t *testing.T, server *quic.Conn, caps *ipcaps.IPCaps) (*net.UDPAddr, func()) {
	t.Helper()
	tun, err := bind(0, server, caps, testLogger())
	if err != nil {
		t.Fatalf("bind: %v", err)
	}
	bound, ok := tun.Addr().(*net.UDPAddr)
	if !ok {
		t.Fatalf("Addr() = %T, want *net.UDPAddr", tun.Addr())
	}
	dialAddr := &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: bound.Port}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		tun.run(ctx)
		close(done)
	}()

	stop := func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Fatal("tun.run did not return after ctx cancel")
		}
	}
	return dialAddr, stop
}

func TestBindUnbindLifecycle(t *testing.T) {
	server, _ := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)

	udpAddr, stop := runTunnel(t, server, caps)

	// The port should be unavailable to a second bind while the tunnel runs.
	if ln, err := net.ListenPacket("udp", udpAddr.String()); err == nil {
		_ = ln.Close()
		t.Error("expected the bound port to be unavailable while the tunnel runs")
	}

	stop()

	// The port should be free again once the tunnel has unbound.
	ln, err := net.ListenPacket("udp", udpAddr.String())
	if err != nil {
		t.Errorf("expected the bound port to be free after unbind: %v", err)
		return
	}
	_ = ln.Close()
}

func TestPumpFramingRoundTrip(t *testing.T) {
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	udpAddr, stop := runTunnel(t, server, caps)
	defer stop()

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	payload := []byte("raknet-open-connection-request-1")
	if _, err := fakeClient.WriteTo(payload, udpAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}

	rctx, rcancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer rcancel()
	frame, err := client.ReceiveDatagram(rctx)
	if err != nil {
		t.Fatalf("ReceiveDatagram: %v", err)
	}
	if len(frame) < FlowIDSize {
		t.Fatalf("frame too short: %d bytes", len(frame))
	}
	flowID := binary.BigEndian.Uint32(frame[:FlowIDSize])
	if got := frame[FlowIDSize:]; !bytes.Equal(got, payload) {
		t.Errorf("forwarded payload = %q, want %q", got, payload)
	}

	// Reply from the "Worker" side, echoing the flow id back.
	reply := []byte("raknet-open-connection-reply-1")
	replyFrame := make([]byte, FlowIDSize+len(reply))
	binary.BigEndian.PutUint32(replyFrame[:FlowIDSize], flowID)
	copy(replyFrame[FlowIDSize:], reply)
	if err := client.SendDatagram(replyFrame); err != nil {
		t.Fatalf("SendDatagram: %v", err)
	}

	_ = fakeClient.SetReadDeadline(time.Now().Add(5 * time.Second))
	buf := make([]byte, 2048)
	n, _, err := fakeClient.ReadFrom(buf)
	if err != nil {
		t.Fatalf("ReadFrom: %v", err)
	}
	if !bytes.Equal(buf[:n], reply) {
		t.Errorf("reply delivered to client = %q, want %q", buf[:n], reply)
	}
}

func TestPumpDropsOversizedDatagram(t *testing.T) {
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	udpAddr, stop := runTunnel(t, server, caps)
	defer stop()

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	oversized := bytes.Repeat([]byte{0x42}, maxDatagramPayload+1)
	if _, err := fakeClient.WriteTo(oversized, udpAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}

	rctx, rcancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer rcancel()
	if _, err := client.ReceiveDatagram(rctx); err == nil {
		t.Error("expected an oversized datagram to be dropped, not forwarded")
	}
}

func TestPumpSurvivesMalformedReplyFrame(t *testing.T) {
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	udpAddr, stop := runTunnel(t, server, caps)
	defer stop()

	// A frame shorter than the flow id prefix must be dropped without
	// disrupting the pump.
	if err := client.SendDatagram([]byte{0x01, 0x02}); err != nil {
		t.Fatalf("SendDatagram: %v", err)
	}

	// A subsequent, well-formed round trip must still work.
	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	payload := []byte("still-alive")
	if _, err := fakeClient.WriteTo(payload, udpAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	rctx, rcancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer rcancel()
	frame, err := client.ReceiveDatagram(rctx)
	if err != nil {
		t.Fatalf("ReceiveDatagram after malformed frame: %v", err)
	}
	if got := frame[FlowIDSize:]; !bytes.Equal(got, payload) {
		t.Errorf("payload = %q, want %q", got, payload)
	}
}

func TestPumpConcurrentFlowCapEnforced(t *testing.T) {
	server, client := quicConnPair(t)
	// Only one concurrent flow per source IP; generous join rate so the flow
	// cap alone is under test.
	caps := ipcaps.NewIPCaps(1, 100, -1, nil, nil)
	udpAddr, stop := runTunnel(t, server, caps)
	defer stop()

	c1, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = c1.Close() }()
	c2, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = c2.Close() }()

	if _, err := c1.WriteTo([]byte("client-1"), udpAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	rctx, rcancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer rcancel()
	if _, err := client.ReceiveDatagram(rctx); err != nil {
		t.Fatalf("first client's datagram should be forwarded: %v", err)
	}

	// Same source IP (127.0.0.1), a second distinct client: over the
	// concurrent-flow cap, so its datagram must be dropped.
	if _, err := c2.WriteTo([]byte("client-2"), udpAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	rctx2, rcancel2 := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer rcancel2()
	if _, err := client.ReceiveDatagram(rctx2); err == nil {
		t.Error("expected the over-cap second flow to be dropped")
	}
}

func TestPumpNewFlowRateCapEnforced(t *testing.T) {
	server, client := quicConnPair(t)
	// Generous concurrent-flow cap, but only one new flow per second per IP.
	caps := ipcaps.NewIPCaps(100, 1, -1, nil, nil)
	udpAddr, stop := runTunnel(t, server, caps)
	defer stop()

	c1, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = c1.Close() }()
	c2, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = c2.Close() }()

	if _, err := c1.WriteTo([]byte("client-1"), udpAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	rctx, rcancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer rcancel()
	if _, err := client.ReceiveDatagram(rctx); err != nil {
		t.Fatalf("first client's datagram should be forwarded: %v", err)
	}

	if _, err := c2.WriteTo([]byte("client-2"), udpAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	rctx2, rcancel2 := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer rcancel2()
	if _, err := client.ReceiveDatagram(rctx2); err == nil {
		t.Error("expected the second new flow within the same second to be rate-limited")
	}
}

// TestFlowEvictionRacesPumps drives flow eviction concurrently with the
// datagram pumps so the race detector actually covers Evict against
// Lookup/Create/AddrByID -- the production sweep fires only every
// flowSweepInterval (15 s), which no other test waits out.
func TestFlowEvictionRacesPumps(t *testing.T) {
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(100, 0, -1, nil, nil)
	tun, err := bind(0, server, caps, testLogger())
	if err != nil {
		t.Fatalf("bind: %v", err)
	}
	// Shrink the idle TTL (the production table uses flowIdleTimeout, 60 s)
	// so eviction genuinely removes entries mid-traffic; swapped before
	// run() starts any goroutine, so no pump ever sees the original table.
	tun.flows = NewFlowTable(time.Millisecond, nil)

	bound, ok := tun.Addr().(*net.UDPAddr)
	if !ok {
		t.Fatalf("Addr() = %T, want *net.UDPAddr", tun.Addr())
	}
	dialAddr := &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: bound.Port}

	ctx, cancel := context.WithCancel(context.Background())
	runDone := make(chan struct{})
	go func() {
		tun.run(ctx)
		close(runDone)
	}()

	// Evictor: hammer Evict + Release exactly as the production sweep does.
	evictCtx, evictCancel := context.WithCancel(context.Background())
	var evictDone sync.WaitGroup
	evictDone.Add(1)
	go func() {
		defer evictDone.Done()
		for evictCtx.Err() == nil {
			for _, addr := range tun.flows.Evict() {
				caps.Release(netutil.HostOf(addr))
			}
		}
	}()

	// Echo peer: bounce every forwarded frame straight back so the
	// QUIC-to-UDP pump's AddrByID races the eviction too.
	echoCtx, echoCancel := context.WithCancel(context.Background())
	var echoDone sync.WaitGroup
	echoDone.Add(1)
	go func() {
		defer echoDone.Done()
		for {
			frame, err := client.ReceiveDatagram(echoCtx)
			if err != nil {
				return
			}
			_ = client.SendDatagram(frame)
		}
	}()

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	// Spaced-out datagrams so flows go idle (1 ms TTL) and are evicted and
	// re-created repeatedly while the pumps run.
	for i := 0; i < 100; i++ {
		if _, err := fakeClient.WriteTo([]byte("ping"), dialAddr); err != nil {
			t.Fatalf("WriteTo: %v", err)
		}
		time.Sleep(2 * time.Millisecond)
	}

	echoCancel()
	echoDone.Wait()
	evictCancel()
	evictDone.Wait()
	cancel()
	select {
	case <-runDone:
	case <-time.After(5 * time.Second):
		t.Fatal("tun.run did not return after ctx cancel")
	}
}
