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

// TestTunnelCloseUnblocksRunAndFreesPort exercises the mechanism takeover
// (#1565) relies on: an external close() call -- exactly what
// Listener.bindOrTakeover does to a stale tunnel it is displacing -- must
// force-close the QUIC connection, unblock run()'s pumps so the goroutine it
// runs in actually returns (no leak), and release the UDP port so a new bind
// on the same port succeeds immediately after. It also exercises close's
// idempotency: run()'s own teardown calls close() again once pumpQUICToUDP
// unblocks, which must be a harmless no-op (no double-close panic/error).
func TestTunnelCloseUnblocksRunAndFreesPort(t *testing.T) {
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)

	tun, err := bind(0, server, caps, testLogger())
	if err != nil {
		t.Fatalf("bind: %v", err)
	}
	udpAddr, ok := tun.Addr().(*net.UDPAddr)
	if !ok {
		t.Fatalf("Addr() = %T, want *net.UDPAddr", tun.Addr())
	}

	runDone := make(chan struct{})
	go func() {
		tun.run(context.Background())
		close(runDone)
	}()

	// Simulate an external takeover displacing this tunnel while it is live.
	tun.close("displaced by new connection")

	select {
	case <-runDone:
	case <-time.After(5 * time.Second):
		t.Fatal("run() did not return after close() -- goroutine leak")
	}

	select {
	case <-client.Context().Done():
	case <-time.After(5 * time.Second):
		t.Fatal("expected the QUIC connection to be closed after close()")
	}

	// The UDP socket must be released, not just the QUIC connection.
	ln, err := net.ListenPacket("udp", udpAddr.String())
	if err != nil {
		t.Errorf("expected the port to be free after close(): %v", err)
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

// TestPumpRateLimitsUnconnectedPingPerFlow proves the per-flow forward cap on
// RakNet unconnected-ping (first byte 0x01): once a flow is established, a burst
// of unconnected-pings from the same source within one second is forwarded at
// most flowPingsPerSecond times. This bounds the relay's exposure as a
// reflection/amplification source even for a single continuously-refreshed flow,
// which the per-IP caps -- gating only new-flow creation -- do not (issue #1604).
func TestPumpRateLimitsUnconnectedPingPerFlow(t *testing.T) {
	server, client := quicConnPair(t)
	// Generous ipcaps (concurrent-flow + new-flow rate) so only the per-flow
	// unconnected-ping cap is under test.
	caps := ipcaps.NewIPCaps(100, 100, -1, nil, nil)
	udpAddr, stop := runTunnel(t, server, caps)
	defer stop()

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	ping := []byte{0x01, 0xaa, 0xbb}

	// First unconnected-ping establishes the flow and is forwarded.
	if _, err := fakeClient.WriteTo(ping, udpAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	rctx, rcancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer rcancel()
	if _, err := client.ReceiveDatagram(rctx); err != nil {
		t.Fatalf("first unconnected-ping should be forwarded: %v", err)
	}
	forwarded := 1

	// A burst of further unconnected-pings from the SAME source within the same
	// one-second window: the per-flow cap must gate all but flowPingsPerSecond
	// of them (counting the first, already forwarded above).
	const burst = 50
	for i := 0; i < burst; i++ {
		if _, err := fakeClient.WriteTo(ping, udpAddr); err != nil {
			t.Fatalf("WriteTo: %v", err)
		}
	}

	// Drain whatever the relay forwarded; count until the QUIC side goes quiet.
	for {
		dctx, dcancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
		_, err := client.ReceiveDatagram(dctx)
		dcancel()
		if err != nil {
			break
		}
		forwarded++
	}

	if forwarded > flowPingsPerSecond {
		t.Errorf("forwarded %d unconnected-pings, want at most %d (per-flow cap)", forwarded, flowPingsPerSecond)
	}
}

// TestPumpForwardsAllGameplayDatagramsPerFlow is the control for the per-flow
// unconnected-ping cap: connected RakNet gameplay traffic (first byte 0x80+,
// here 0x84) is not rate-limited, so a burst well above flowPingsPerSecond from
// one flow is forwarded in full -- the cap must neuter reflection without
// throttling gameplay (issue #1604).
func TestPumpForwardsAllGameplayDatagramsPerFlow(t *testing.T) {
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(100, 100, -1, nil, nil)
	udpAddr, stop := runTunnel(t, server, caps)
	defer stop()

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	// Well above the unconnected-ping cap, all from the same flow. Send and
	// receive one at a time: connected packets carry no rate window, so this is
	// deterministic regardless of timing.
	const burst = 4 * flowPingsPerSecond
	gameplay := []byte{0x84, 0x00, 0x01}
	for i := 0; i < burst; i++ {
		if _, err := fakeClient.WriteTo(gameplay, udpAddr); err != nil {
			t.Fatalf("WriteTo: %v", err)
		}
		rctx, rcancel := context.WithTimeout(context.Background(), 5*time.Second)
		_, err := client.ReceiveDatagram(rctx)
		rcancel()
		if err != nil {
			t.Fatalf("gameplay datagram %d/%d should be forwarded, not rate-limited: %v", i+1, burst, err)
		}
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
