package bedrock

import (
	"context"
	"encoding/binary"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"
	"github.com/quic-go/quic-go"

	bedrocktunnelv1 "github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/genproto/mcsd/bedrocktunnel/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/metrics"
)

// newBedrockMetrics builds a fresh registry and Metrics handle for a metrics
// assertion test.
func newBedrockMetrics(t *testing.T) (*metrics.Metrics, *prometheus.Registry) {
	t.Helper()
	reg := prometheus.NewRegistry()
	return metrics.New(reg, "test"), reg
}

// seriesValue returns the value of the counter/gauge series named name whose
// labels match want exactly, or 0 if the series has not been emitted.
func seriesValue(t *testing.T, reg *prometheus.Registry, name string, want map[string]string) float64 {
	t.Helper()
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, f := range families {
		if f.GetName() != name {
			continue
		}
		for _, m := range f.GetMetric() {
			if !labelsMatch(m.GetLabel(), want) {
				continue
			}
			switch {
			case m.Counter != nil:
				return m.GetCounter().GetValue()
			case m.Gauge != nil:
				return m.GetGauge().GetValue()
			}
		}
	}
	return 0
}

func labelsMatch(pairs []*dto.LabelPair, want map[string]string) bool {
	if len(pairs) != len(want) {
		return false
	}
	for _, p := range pairs {
		if want[p.GetName()] != p.GetValue() {
			return false
		}
	}
	return true
}

// waitForSeries polls seriesValue until it reaches at least want or a short
// deadline elapses, returning the final value. Used for drop metrics whose
// increment is observed asynchronously (the reader/pumps run in goroutines).
func waitForSeries(t *testing.T, reg *prometheus.Registry, name string, labels map[string]string, want float64) float64 {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		got := seriesValue(t, reg, name, labels)
		if got >= want || time.Now().After(deadline) {
			return got
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// runInstrumentedTunnel binds a Tunnel wired to m and runs it, returning the
// tunnel, the loopback dial address, and a stop func (cf. runTunnel).
func runInstrumentedTunnel(t *testing.T, server *quic.Conn, caps *ipcaps.IPCaps, m *metrics.Metrics) (*Tunnel, *net.UDPAddr, func()) {
	t.Helper()
	tun, err := bind(0, testServerID, server, caps, noopRecorder{}, m, testLogger())
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
	return tun, dialAddr, stop
}

// TestBedrockActiveTunnelsBindAndTeardown asserts the active-tunnels gauge is
// +1 on bind and -1 on teardown.
func TestBedrockActiveTunnelsBindAndTeardown(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, _ := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)

	tun, err := bind(0, testServerID, server, caps, noopRecorder{}, m, testLogger())
	if err != nil {
		t.Fatalf("bind: %v", err)
	}
	if got := seriesValue(t, reg, "relay_bedrock_active_tunnels", nil); got != 1 {
		t.Errorf("active_tunnels after bind = %v, want 1", got)
	}

	tun.close("done")
	if got := seriesValue(t, reg, "relay_bedrock_active_tunnels", nil); got != 0 {
		t.Errorf("active_tunnels after teardown = %v, want 0", got)
	}
}

// TestBedrockFlowCreateAndEvictTrackGaugeAndCounters asserts a flow create
// moves active_flows +1 and flows_created_total +1, and an idle eviction moves
// active_flows -1 and flows_evicted_total +1.
func TestBedrockFlowCreateAndEvictTrackGaugeAndCounters(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)

	tun, err := bind(0, testServerID, server, caps, noopRecorder{}, m, testLogger())
	if err != nil {
		t.Fatalf("bind: %v", err)
	}
	// Injected clock so idle eviction is deterministic (swapped before run()).
	now := time.Now()
	tun.flows = NewFlowTable(flowIdleTimeout, func() time.Time { return now })

	bound := tun.Addr().(*net.UDPAddr)
	dialAddr := &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: bound.Port}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { tun.run(ctx); close(done) }()

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	// One datagram from one source creates exactly one flow.
	sendGameplay(t, fakeClient, dialAddr, client)
	if got := waitForSeries(t, reg, "relay_bedrock_active_flows", nil, 1); got != 1 {
		t.Errorf("active_flows after create = %v, want 1", got)
	}
	if got := seriesValue(t, reg, "relay_bedrock_flows_created_total", nil); got != 1 {
		t.Errorf("flows_created = %v, want 1", got)
	}

	// Idle the flow past the timeout and sweep: the gauge drops back and the
	// evicted counter ticks.
	now = now.Add(flowIdleTimeout + time.Second)
	tun.sweepOnce()
	if got := seriesValue(t, reg, "relay_bedrock_active_flows", nil); got != 0 {
		t.Errorf("active_flows after eviction = %v, want 0", got)
	}
	if got := seriesValue(t, reg, "relay_bedrock_flows_evicted_total", nil); got != 1 {
		t.Errorf("flows_evicted = %v, want 1", got)
	}

	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("tun.run did not return")
	}
}

// TestBedrockTeardownReturnsActiveFlowsToZero is the anti-leak case: flows still
// live when the tunnel tears down must be removed from the active-flows gauge
// (driven off the teardown Drain seam), and teardown must NOT be counted as
// eviction.
func TestBedrockTeardownReturnsActiveFlowsToZero(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, client := quicConnPair(t)
	// No caps and a real 60 s idle TTL: nothing evicts within the test, so the
	// only thing that can zero the gauge is teardown.
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	_, dialAddr, stop := runInstrumentedTunnel(t, server, caps, m)

	const flows = 3
	sources := make([]net.PacketConn, flows)
	for i := range sources {
		c, err := net.ListenPacket("udp", "127.0.0.1:0")
		if err != nil {
			t.Fatalf("ListenPacket: %v", err)
		}
		defer func() { _ = c.Close() }()
		sources[i] = c
		sendGameplay(t, c, dialAddr, client)
	}
	if got := waitForSeries(t, reg, "relay_bedrock_active_flows", nil, flows); got != flows {
		t.Fatalf("active_flows before teardown = %v, want %d", got, flows)
	}

	// Teardown (no idle eviction) must return the gauge to 0 without leaking.
	stop()
	if got := seriesValue(t, reg, "relay_bedrock_active_flows", nil); got != 0 {
		t.Errorf("active_flows after teardown = %v, want 0 (gauge leaked)", got)
	}
	// Teardown abandonment is not eviction.
	if got := seriesValue(t, reg, "relay_bedrock_flows_evicted_total", nil); got != 0 {
		t.Errorf("flows_evicted after teardown = %v, want 0 (teardown is not eviction)", got)
	}
}

// TestBedrockDatagramsInAndOut asserts a forwarded client datagram increments
// udp_datagrams_total{in} and a delivered reply increments {out}.
func TestBedrockDatagramsInAndOut(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	_, dialAddr, stop := runInstrumentedTunnel(t, server, caps, m)
	defer stop()

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	// One ingress datagram, forwarded to the worker.
	payload := []byte{0x84, 0x01, 0x02}
	if _, err := fakeClient.WriteTo(payload, dialAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	rctx, rcancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer rcancel()
	frame, err := client.ReceiveDatagram(rctx)
	if err != nil {
		t.Fatalf("ReceiveDatagram: %v", err)
	}
	if got := waitForSeries(t, reg, "relay_bedrock_udp_datagrams_total", map[string]string{"direction": metrics.DirectionIn}, 1); got != 1 {
		t.Errorf("datagrams{in} = %v, want 1", got)
	}

	// Worker replies, echoing the flow id: the relay writes it back out.
	if err := client.SendDatagram(frame); err != nil {
		t.Fatalf("SendDatagram: %v", err)
	}
	_ = fakeClient.SetReadDeadline(time.Now().Add(5 * time.Second))
	buf := make([]byte, 2048)
	if _, _, err := fakeClient.ReadFrom(buf); err != nil {
		t.Fatalf("ReadFrom: %v", err)
	}
	if got := waitForSeries(t, reg, "relay_bedrock_udp_datagrams_total", map[string]string{"direction": metrics.DirectionOut}, 1); got != 1 {
		t.Errorf("datagrams{out} = %v, want 1", got)
	}
}

// TestBedrockOversizedDatagramDropMetric asserts an oversized ingress datagram
// increments datagrams_dropped_total{in,oversized}.
func TestBedrockOversizedDatagramDropMetric(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, _ := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	_, dialAddr, stop := runInstrumentedTunnel(t, server, caps, m)
	defer stop()

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	oversized := make([]byte, maxDatagramPayload+1)
	oversized[0] = 0x84
	if _, err := fakeClient.WriteTo(oversized, dialAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	if got := waitForSeries(t, reg, "relay_bedrock_datagrams_dropped_total",
		map[string]string{"direction": metrics.DirectionIn, "reason": metrics.BedrockDropOversized}, 1); got != 1 {
		t.Errorf("dropped{in,oversized} = %v, want 1", got)
	}
}

// TestBedrockQueueFullDropMetric asserts the #1721 bounded-channel drop
// increments datagrams_dropped_total{in,queue_full}. Driven with a tiny,
// undrained send channel so the reader's non-blocking enqueue takes the drop
// path (cf. TestReaderDoesNotStallWhenSendQueueFull).
func TestBedrockQueueFullDropMetric(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, _ := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	tun, err := bind(0, testServerID, server, caps, noopRecorder{}, m, testLogger())
	if err != nil {
		t.Fatalf("bind: %v", err)
	}
	bound := tun.Addr().(*net.UDPAddr)
	dialAddr := &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: bound.Port}

	sendCh := make(chan *[]byte, 4) // never drained
	readerDone := make(chan struct{})
	go func() { tun.pumpUDPToQueue(sendCh); close(readerDone) }()

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	// One flow, many datagrams: after the queue fills, every further datagram
	// is dropped at the queue_full site. Keep sending until the metric ticks.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		for i := 0; i < 50; i++ {
			if _, err := fakeClient.WriteTo([]byte{0x84, 0x00}, dialAddr); err != nil {
				t.Fatalf("WriteTo: %v", err)
			}
		}
		if seriesValue(t, reg, "relay_bedrock_datagrams_dropped_total",
			map[string]string{"direction": metrics.DirectionIn, "reason": metrics.BedrockDropQueueFull}) > 0 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}

	if got := seriesValue(t, reg, "relay_bedrock_datagrams_dropped_total",
		map[string]string{"direction": metrics.DirectionIn, "reason": metrics.BedrockDropQueueFull}); got == 0 {
		t.Error("dropped{in,queue_full} = 0, want > 0")
	}

	tun.unbind()
	select {
	case <-readerDone:
	case <-time.After(5 * time.Second):
		t.Fatal("pumpUDPToQueue did not return after the socket was closed")
	}
}

// TestBedrockPingRateCapDropMetric asserts the #1604 per-flow unconnected-ping
// rate cap increments datagrams_dropped_total{in,ping_rate_cap}.
func TestBedrockPingRateCapDropMetric(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, _ := quicConnPair(t)
	// Generous ipcaps so only the per-flow ping cap gates.
	caps := ipcaps.NewIPCaps(100, 100, -1, nil, nil)
	_, dialAddr, stop := runInstrumentedTunnel(t, server, caps, m)
	defer stop()

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	// A burst of unconnected-pings (first byte 0x01) from one source within one
	// second: all but flowPingsPerSecond are dropped at the ping-cap site.
	ping := []byte{0x01, 0xaa}
	for i := 0; i < 50; i++ {
		if _, err := fakeClient.WriteTo(ping, dialAddr); err != nil {
			t.Fatalf("WriteTo: %v", err)
		}
	}
	if got := waitForSeries(t, reg, "relay_bedrock_datagrams_dropped_total",
		map[string]string{"direction": metrics.DirectionIn, "reason": metrics.BedrockDropPingRateCap}, 1); got == 0 {
		t.Error("dropped{in,ping_rate_cap} = 0, want > 0")
	}
}

// TestBedrockQUICSendErrorDropMetric asserts a failed relay->Worker SendDatagram
// increments datagrams_dropped_total{in,quic_send_error}. Any send error hits
// the same instrumentation line; a frame far above the peer's max datagram size
// makes SendDatagram fail deterministically, with no connection-close timing.
func TestBedrockQUICSendErrorDropMetric(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, _ := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	tun, err := bind(0, testServerID, server, caps, noopRecorder{}, m, testLogger())
	if err != nil {
		t.Fatalf("bind: %v", err)
	}
	defer tun.unbind()

	// Far larger than any negotiated QUIC datagram size -> SendDatagram errors.
	frame := make([]byte, 64<<10)
	sendCh := make(chan *[]byte, 1)
	sendCh <- &frame
	close(sendCh)
	tun.pumpQueueToQUIC(sendCh)

	if got := seriesValue(t, reg, "relay_bedrock_datagrams_dropped_total",
		map[string]string{"direction": metrics.DirectionIn, "reason": metrics.BedrockDropQUICSend}); got != 1 {
		t.Errorf("dropped{in,quic_send_error} = %v, want 1", got)
	}
}

// TestBedrockShortFrameDropMetric asserts a worker->client frame shorter than
// the flow-id prefix increments datagrams_dropped_total{out,short_frame}.
func TestBedrockShortFrameDropMetric(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	_, _, stop := runInstrumentedTunnel(t, server, caps, m)
	defer stop()

	if err := client.SendDatagram([]byte{0x01, 0x02}); err != nil {
		t.Fatalf("SendDatagram: %v", err)
	}
	if got := waitForSeries(t, reg, "relay_bedrock_datagrams_dropped_total",
		map[string]string{"direction": metrics.DirectionOut, "reason": metrics.BedrockDropShortFrame}, 1); got != 1 {
		t.Errorf("dropped{out,short_frame} = %v, want 1", got)
	}
}

// TestBedrockUnknownFlowDropMetric asserts a worker->client frame for an unknown
// flow id increments datagrams_dropped_total{out,unknown_flow}.
func TestBedrockUnknownFlowDropMetric(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	_, _, stop := runInstrumentedTunnel(t, server, caps, m)
	defer stop()

	frame := make([]byte, FlowIDSize+2)
	binary.BigEndian.PutUint32(frame[:FlowIDSize], 0xDEADBEEF) // no such flow
	if err := client.SendDatagram(frame); err != nil {
		t.Fatalf("SendDatagram: %v", err)
	}
	if got := waitForSeries(t, reg, "relay_bedrock_datagrams_dropped_total",
		map[string]string{"direction": metrics.DirectionOut, "reason": metrics.BedrockDropUnknownFlow}, 1); got != 1 {
		t.Errorf("dropped{out,unknown_flow} = %v, want 1", got)
	}
}

// TestBedrockUDPWriteErrorDropMetric asserts a failed reply write (closed UDP
// socket) increments datagrams_dropped_total{out,udp_write_error}. The flow is
// registered first, then the socket is closed, then a reply for that flow is
// delivered so AddrByID hits but WriteTo fails.
func TestBedrockUDPWriteErrorDropMetric(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	tun, err := bind(0, testServerID, server, caps, noopRecorder{}, m, testLogger())
	if err != nil {
		t.Fatalf("bind: %v", err)
	}

	// Register a flow by hand, then close the socket so its reply write fails.
	id := tun.flows.Create(&net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: 65000}, false)
	tun.unbind()

	ctx, cancel := context.WithCancel(context.Background())
	pumpDone := make(chan struct{})
	go func() { tun.pumpQUICToUDP(ctx); close(pumpDone) }()

	frame := make([]byte, FlowIDSize+2)
	binary.BigEndian.PutUint32(frame[:FlowIDSize], id)
	if err := client.SendDatagram(frame); err != nil {
		t.Fatalf("SendDatagram: %v", err)
	}
	if got := waitForSeries(t, reg, "relay_bedrock_datagrams_dropped_total",
		map[string]string{"direction": metrics.DirectionOut, "reason": metrics.BedrockDropUDPWrite}, 1); got != 1 {
		t.Errorf("dropped{out,udp_write_error} = %v, want 1", got)
	}

	cancel()
	_ = server.CloseWithError(0, "test done")
	select {
	case <-pumpDone:
	case <-time.After(5 * time.Second):
		t.Fatal("pumpQUICToUDP did not return")
	}
}

// TestBedrockBindFailureMetric asserts a UDP ListenPacket failure increments
// bind_failures_total.
func TestBedrockBindFailureMetric(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, _ := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)

	// Occupy a port so bind's ListenPacket for it fails.
	occupied, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = occupied.Close() }()
	port := uint32(occupied.LocalAddr().(*net.UDPAddr).Port)

	if _, err := bind(port, testServerID, server, caps, noopRecorder{}, m, testLogger()); err == nil {
		t.Fatal("bind on an occupied port should fail")
	}
	if got := seriesValue(t, reg, "relay_bedrock_bind_failures_total", nil); got != 1 {
		t.Errorf("bind_failures = %v, want 1", got)
	}
}

// newInstrumentedListener runs a Listener wired to m with unlimited pre-auth
// caps.
func newInstrumentedListener(t *testing.T, validator Validator, m *metrics.Metrics) (*Listener, func()) {
	t.Helper()
	newCaps := func() *ipcaps.IPCaps { return ipcaps.NewIPCaps(0, 0, 0, nil, nil) }
	ln, err := NewListener("127.0.0.1:0", selfSignedTLS(t), validator, ipcaps.NewIPCaps(0, 0, 0, nil, nil), newCaps, noopRecorder{}, m, testLogger())
	if err != nil {
		t.Fatalf("NewListener: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = ln.Serve(ctx); close(done) }()
	stop := func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Fatal("Serve did not return after ctx cancel")
		}
	}
	return ln, stop
}

// TestBedrockTunnelOpenedMetric asserts an accepted handshake increments
// tunnels_opened_total.
func TestBedrockTunnelOpenedMetric(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	ln, stop := newInstrumentedListener(t, &fakeValidator{valid: true}, m)
	defer stop()

	// BedrockPort 0 -> OS-assigned bind, so this cannot collide with a busy port.
	_, ack := doHandshake(t, ln, &bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: 0, Token: "tok"})
	if !ack.GetAccepted() {
		t.Fatalf("handshake rejected: %q", ack.GetRejectReason())
	}
	if got := waitForSeries(t, reg, "relay_bedrock_tunnels_opened_total", nil, 1); got != 1 {
		t.Errorf("tunnels_opened = %v, want 1", got)
	}
}

// TestBedrockHandshakeRejectionMetric asserts an invalid-credential handshake
// increments tunnels_rejected_total{invalid_credential}.
func TestBedrockHandshakeRejectionMetric(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	ln, stop := newInstrumentedListener(t, &fakeValidator{valid: false}, m)
	defer stop()

	_, ack := doHandshake(t, ln, &bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: 25710, Token: "wrong"})
	if ack.GetAccepted() {
		t.Fatal("accepted = true, want false for an invalid credential")
	}
	if got := waitForSeries(t, reg, "relay_bedrock_tunnels_rejected_total",
		map[string]string{"reason": metrics.BedrockRejectInvalidCredential}, 1); got != 1 {
		t.Errorf("tunnels_rejected{invalid_credential} = %v, want 1", got)
	}
}

// TestBedrockNewFlowConnCapRejectionUsesBedrockListener asserts an over-cap new
// flow (concurrent-flow cap) increments ipcaps_rejections_total{bedrock,conn}.
func TestBedrockNewFlowConnCapRejectionUsesBedrockListener(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, client := quicConnPair(t)
	// One concurrent flow per source IP; generous join rate.
	caps := ipcaps.NewIPCaps(1, 100, -1, nil, nil)
	_, dialAddr, stop := runInstrumentedTunnel(t, server, caps, m)
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

	// First flow admitted and forwarded.
	sendGameplay(t, c1, dialAddr, client)
	// Second flow from the same IP: over the concurrent cap.
	if _, err := c2.WriteTo([]byte{0x84, 0x00}, dialAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	if got := waitForSeries(t, reg, "relay_ipcaps_rejections_total",
		map[string]string{"listener": metrics.ListenerBedrock, "kind": metrics.CapKindConn}, 1); got != 1 {
		t.Errorf("ipcaps_rejections{bedrock,conn} = %v, want 1", got)
	}
}

// TestBedrockNewFlowRateCapRejectionUsesBedrockListener asserts an over-cap new
// flow (new-flow rate cap) increments ipcaps_rejections_total{bedrock,rate}.
func TestBedrockNewFlowRateCapRejectionUsesBedrockListener(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, client := quicConnPair(t)
	// Generous concurrent cap, but only one new flow per second per IP.
	caps := ipcaps.NewIPCaps(100, 1, -1, nil, nil)
	_, dialAddr, stop := runInstrumentedTunnel(t, server, caps, m)
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

	sendGameplay(t, c1, dialAddr, client)
	if _, err := c2.WriteTo([]byte{0x84, 0x00}, dialAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	if got := waitForSeries(t, reg, "relay_ipcaps_rejections_total",
		map[string]string{"listener": metrics.ListenerBedrock, "kind": metrics.CapKindRate}, 1); got != 1 {
		t.Errorf("ipcaps_rejections{bedrock,rate} = %v, want 1", got)
	}
}

// TestBedrockMetricsCarryNoSourceAddressLabel is the cardinality guardrail
// exercised through the real datagram path: after driving ingress, egress, and a
// drop, no gathered Bedrock series may carry a label whose name is
// source-identifying or whose value parses as an IP address.
func TestBedrockMetricsCarryNoSourceAddressLabel(t *testing.T) {
	m, reg := newBedrockMetrics(t)
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	_, dialAddr, stop := runInstrumentedTunnel(t, server, caps, m)
	defer stop()

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	// Ingress + forward, a reply (egress), and an oversized drop -- materialising
	// several labelled Bedrock series.
	if _, err := fakeClient.WriteTo([]byte{0x84, 0x00}, dialAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	rctx, rcancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer rcancel()
	frame, err := client.ReceiveDatagram(rctx)
	if err != nil {
		t.Fatalf("ReceiveDatagram: %v", err)
	}
	if err := client.SendDatagram(frame); err != nil {
		t.Fatalf("SendDatagram: %v", err)
	}
	oversized := make([]byte, maxDatagramPayload+1)
	if _, err := fakeClient.WriteTo(oversized, dialAddr); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	waitForSeries(t, reg, "relay_bedrock_datagrams_dropped_total",
		map[string]string{"direction": metrics.DirectionIn, "reason": metrics.BedrockDropOversized}, 1)

	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	forbidden := []string{"ip", "addr", "address", "source", "src", "remote", "client", "host", "port"}
	for _, f := range families {
		for _, metric := range f.GetMetric() {
			for _, lp := range metric.GetLabel() {
				name := strings.ToLower(lp.GetName())
				for _, bad := range forbidden {
					if strings.Contains(name, bad) {
						t.Errorf("%s carries a source-identifying label %q", f.GetName(), lp.GetName())
					}
				}
				if net.ParseIP(lp.GetValue()) != nil {
					t.Errorf("%s label %q carries an IP-address value %q", f.GetName(), lp.GetName(), lp.GetValue())
				}
			}
		}
	}
}
