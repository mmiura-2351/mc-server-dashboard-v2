package bedrock

import (
	"context"
	"encoding/binary"
	"fmt"
	"log/slog"
	"net"
	"sync"
	"syscall"
	"time"

	"github.com/quic-go/quic-go"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/metrics"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/netutil"
)

// flowIdleTimeout bounds how long a Bedrock client's flow entry survives
// without activity in either direction before the relay reclaims it and its
// ipcaps slot (docs/app/BEDROCK_TUNNEL.md).
const flowIdleTimeout = 60 * time.Second

// flowSweepInterval is how often a Tunnel checks for idle flows to evict.
const flowSweepInterval = 15 * time.Second

// flowPingsPerSecond caps how many RakNet unconnected-pings (first byte 0x01)
// the relay forwards per flow per second (FlowTable.AllowPing). It is a fixed
// const, not a config knob: legitimate Bedrock clients send only a few
// unconnected-pings for the server-list MOTD, far below this, while a
// reflection source must sustain them -- so the cap bounds the relay's
// amplification exposure for a single continuously-refreshed spoofed-victim
// flow (issue #1604) without breaking the ping/MOTD or touching gameplay
// (connected RakNet packets, first byte 0x80+).
const flowPingsPerSecond = 5

// flowPromoteThreshold is the number of CONNECTED (RakNet FLAG_VALID, first byte
// >= 0x80) client->worker datagrams a Bedrock flow must send before the relay
// reports it to the API as a live player session (issue #1904). The relay never
// parses RakNet, so a UDP flow -- not an authenticated player -- is the only
// unit it can observe; counting only connected datagrams (never offline
// unconnected-ping / server-scan / handshake packets, all first byte < 0x80)
// separates a real connection -- which sends many connected datagrams within
// ~1s -- from a client re-pinging a pinned server, which must not mint a session
// row. It is a fixed const, not a config knob, mirroring flowPingsPerSecond.
const flowPromoteThreshold = 5

// udpReadBufferSize is sized generously above maxDatagramPayload so an
// oversized inbound UDP datagram is read in full (and then dropped by the MTU
// gate in pumpUDPToQueue) rather than silently truncated by a too-small buffer.
const udpReadBufferSize = 2048

// udpRecvBufferBytes is the socket receive buffer (SO_RCVBUF) the relay requests
// for each bound Bedrock port. The default socket buffer (~208 KiB) holds only a
// couple hundred max-size datagrams, so a brief stall in the relay->Worker QUIC
// send path lets the kernel drop inbound datagrams for every flow on the port
// (issue #1721). A few MiB gives the reader headroom to keep draining the socket
// across a transient stall. The OS may clamp this to its own maximum
// (net.core.rmem_max on Linux); setUDPRecvBuffer logs but does not fail then.
const udpRecvBufferBytes = 4 << 20 // 4 MiB

// sendQueueDepth bounds the buffered channel that decouples the UDP reader
// (pumpUDPToQueue) from the QUIC sender (pumpQueueToQUIC). A congested tunnel
// backs the channel up to this depth and then the reader drops -- explicitly and
// per-datagram -- instead of blocking, so one congested flow cannot stall the
// shared reader for every other flow on the port (issue #1721). A single shared
// queue is sufficient: the QUIC connection is one serialization point anyway,
// and the per-flow ping cap (issue #1604) already bounds the amplifying ping.
const sendQueueDepth = 1024

// Tunnel is one bound Bedrock server: a public UDP port mapped to a Worker's
// authenticated QUIC connection, with a per-client flow table and per-IP abuse
// caps (docs/app/BEDROCK_TUNNEL.md). The authenticated QUIC dial-out IS the
// registration -- a Tunnel holds no state beyond this live mapping, and there
// is no separate server table anywhere in the relay.
type Tunnel struct {
	udpConn  net.PacketConn
	quicConn *quic.Conn
	flows    *FlowTable
	caps     *ipcaps.IPCaps
	serverID string
	sessions SessionRecorder
	metrics  *metrics.Metrics
	logger   *slog.Logger

	// framePool recycles the flow-id-prefixed frame buffers that cross the
	// reader->sender channel, removing the per-datagram allocation on the ingress
	// hot path. Each buffer holds one full-size frame (FlowIDSize + a max-size
	// payload); the reader gets one, and the sender returns it after
	// SendDatagram (which copies the payload synchronously) returns, or the
	// reader returns it directly on the drop path (issue #1721).
	framePool sync.Pool

	closeOnce sync.Once
}

// bind opens the public UDP port for bedrockPort and wires it to quicConn. A
// bind failure (most likely the port already held by a not-yet-unbound prior
// connection for the same server, e.g. a Worker redial racing its old QUIC
// connection's idle timeout -- docs/app/BEDROCK_TUNNEL.md) is returned as-is
// for the caller to treat as a handshake rejection.
func bind(bedrockPort uint32, serverID string, quicConn *quic.Conn, caps *ipcaps.IPCaps, sessions SessionRecorder, m *metrics.Metrics, logger *slog.Logger) (*Tunnel, error) {
	udpConn, err := net.ListenPacket("udp", fmt.Sprintf(":%d", bedrockPort))
	if err != nil {
		m.BedrockBindFailure()
		return nil, err
	}
	if uc, ok := udpConn.(*net.UDPConn); ok {
		setUDPRecvBuffer(uc, logger)
	}
	t := &Tunnel{
		udpConn:  udpConn,
		quicConn: quicConn,
		flows:    NewFlowTable(flowIdleTimeout, nil),
		caps:     caps,
		serverID: serverID,
		sessions: sessions,
		metrics:  m,
		logger:   logger,
	}
	t.framePool.New = func() any {
		b := make([]byte, FlowIDSize+maxDatagramPayload)
		return &b
	}
	m.BedrockTunnelBound()
	return t, nil
}

// setUDPRecvBuffer enlarges the socket receive buffer to udpRecvBufferBytes so
// the reader has kernel headroom to keep draining inbound datagrams across a
// transient stall in the QUIC send path (issue #1721). Failure is not fatal: if
// SetReadBuffer errors, or the OS clamped the request below what we asked for
// (net.core.rmem_max on Linux), we log and carry on with whatever the kernel
// granted. The read-back value is the kernel's doubled bookkeeping size on
// Linux, so a smaller-than-requested read-back reliably flags a genuine clamp.
func setUDPRecvBuffer(conn *net.UDPConn, logger *slog.Logger) {
	if err := conn.SetReadBuffer(udpRecvBufferBytes); err != nil {
		logger.Warn("bedrock: could not enlarge UDP receive buffer", "want", udpRecvBufferBytes, "error", err)
		return
	}
	effective, err := readUDPRecvBuffer(conn)
	if err != nil {
		// Best-effort diagnostics only: SetReadBuffer above already succeeded.
		return
	}
	if effective < udpRecvBufferBytes {
		logger.Warn("bedrock: UDP receive buffer clamped below requested size",
			"want", udpRecvBufferBytes, "effective", effective)
	}
}

// readUDPRecvBuffer queries the socket's effective receive buffer size
// (SO_RCVBUF) so setUDPRecvBuffer can detect an OS clamp.
func readUDPRecvBuffer(conn *net.UDPConn) (int, error) {
	raw, err := conn.SyscallConn()
	if err != nil {
		return 0, err
	}
	var size int
	var sockErr error
	if err := raw.Control(func(fd uintptr) {
		size, sockErr = syscall.GetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF)
	}); err != nil {
		return 0, err
	}
	return size, sockErr
}

// Addr returns the bound public UDP address (test/diagnostic use).
func (t *Tunnel) Addr() net.Addr { return t.udpConn.LocalAddr() }

// run pumps RakNet datagrams both directions until ctx is cancelled or the
// QUIC connection closes, then unbinds the UDP port. It blocks; the caller
// runs it in the accepted connection's own goroutine.
func (t *Tunnel) run(ctx context.Context) {
	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	// The Tunnel's lifetime is bounded by whichever ends first: the caller's
	// ctx (relay shutdown) or the QUIC connection's own Context() (Worker
	// disconnect).
	go func() {
		select {
		case <-t.quicConn.Context().Done():
		case <-runCtx.Done():
		}
		cancel()
	}()

	go t.sweepLoop(runCtx)

	// Ingress is split across two goroutines joined by a bounded channel so the
	// UDP reader never blocks on the QUIC send path (issue #1721): the reader
	// drains the socket and enqueues, the sender drains the channel into the
	// QUIC connection. The reader closes sendCh on return so the sender then
	// drains what is left and exits.
	sendCh := make(chan *[]byte, sendQueueDepth)
	var udpDone sync.WaitGroup
	udpDone.Add(2)
	go func() {
		defer udpDone.Done()
		defer close(sendCh)
		t.pumpUDPToQueue(sendCh)
	}()
	go func() {
		defer udpDone.Done()
		t.pumpQueueToQUIC(sendCh)
	}()

	// Blocks until runCtx is cancelled, ReceiveDatagram reports the
	// connection is gone, or an external takeover (close, below) force-closes
	// the QUIC connection.
	t.pumpQUICToUDP(runCtx)

	// close is idempotent: if a takeover (#1565) already force-closed this
	// tunnel concurrently, this is a no-op; otherwise it is this tunnel's own
	// natural teardown, closing the QUIC connection (visible to the Worker
	// immediately rather than waiting out its idle timeout) and the UDP
	// socket (which unblocks pumpUDPToQueue's blocking ReadFrom).
	t.close("tunnel closing")
	udpDone.Wait()

	// End any sessions still open when the tunnel tears down (Worker disconnect,
	// takeover, or relay shutdown) so promoted Bedrock flows do not strand
	// (issue #1904), and decrement the active-flows gauge by the whole abandoned
	// flow table so it does not leak (issue #1909). The reader has stopped
	// (udpDone), so no new flow can race this; the sweep may still run until the
	// deferred cancel fires, but it and Drain both go through the FlowTable under
	// its lock and Drain removes the entries, so each flow (and each session) is
	// surfaced by exactly one of them.
	endedSessions, drained := t.flows.Drain()
	t.metrics.BedrockFlowsDrained(drained)
	for _, sid := range endedSessions {
		t.sessions.End(sid)
	}
}

// close force-closes the QUIC connection and unbinds the UDP port. It is
// idempotent (sync.Once) and safe to call concurrently, so both this
// Tunnel's own natural teardown (run, above) and an external takeover of its
// port by a new connection (Listener.bindOrTakeover, #1565) can call it
// without risking a double-close or a use-after-close: only the first caller
// does any work.
func (t *Tunnel) close(reason string) {
	t.closeOnce.Do(func() {
		_ = t.quicConn.CloseWithError(0, reason)
		t.unbind()
		t.metrics.BedrockTunnelTornDown()
	})
}

// unbind closes the bound UDP port.
func (t *Tunnel) unbind() {
	_ = t.udpConn.Close()
}

// pumpQUICToUDP reads RakNet datagrams the Worker forwards from the container
// and writes them to the originating Bedrock client. It returns when ctx is
// cancelled or ReceiveDatagram errors (the QUIC connection closed).
func (t *Tunnel) pumpQUICToUDP(ctx context.Context) {
	for {
		data, err := t.quicConn.ReceiveDatagram(ctx)
		if err != nil {
			return
		}
		if len(data) < FlowIDSize {
			t.metrics.BedrockDatagramDropped(metrics.DirectionOut, metrics.BedrockDropShortFrame)
			continue // malformed frame: drop
		}
		id := binary.BigEndian.Uint32(data[:FlowIDSize])
		addr, ok := t.flows.AddrByID(id)
		if !ok {
			// Unknown or evicted flow (e.g. the relay reclaimed it for
			// inactivity after the Worker's last reply): drop
			// (docs/app/BEDROCK_TUNNEL.md).
			t.metrics.BedrockDatagramDropped(metrics.DirectionOut, metrics.BedrockDropUnknownFlow)
			continue
		}
		if _, err := t.udpConn.WriteTo(data[FlowIDSize:], addr); err != nil {
			t.metrics.BedrockDatagramDropped(metrics.DirectionOut, metrics.BedrockDropUDPWrite)
			t.logger.Debug("bedrock: UDP write failed", "addr", addr, "error", err)
			continue
		}
		t.metrics.BedrockDatagram(metrics.DirectionOut)
	}
}

// pumpUDPToQueue reads RakNet datagrams from Bedrock clients on the bound public
// UDP port, assigns/reuses a flow id per source address, applies the ingress
// admission checks, and hands each flow-id-prefixed frame to the sender via
// sendCh. It never blocks on the QUIC send path: when sendCh is full (a
// congested tunnel), it drops the datagram explicitly rather than stalling, so
// one congested flow cannot starve the shared reader for every other flow on the
// port (issue #1721). It returns once the UDP socket is closed (unbind, called
// after pumpQUICToUDP ends).
func (t *Tunnel) pumpUDPToQueue(sendCh chan<- *[]byte) {
	buf := make([]byte, udpReadBufferSize)
	for {
		n, addr, err := t.udpConn.ReadFrom(buf)
		if err != nil {
			return
		}
		t.metrics.BedrockDatagram(metrics.DirectionIn)
		udpAddr, ok := addr.(*net.UDPAddr)
		if !ok {
			continue
		}
		if n > maxDatagramPayload {
			// Oversized for our conservative datagram budget: drop rather than
			// forward. RakNet's own MTU discovery reads this the same way it
			// reads any other unanswered probe -- try a smaller candidate
			// (docs/app/BEDROCK_TUNNEL.md).
			t.metrics.BedrockDatagramDropped(metrics.DirectionIn, metrics.BedrockDropOversized)
			continue
		}

		// Only connected RakNet datagrams (FLAG_VALID, first byte >= 0x80) advance
		// session promotion; offline packets (unconnected ping 0x01, pong, the
		// connection handshake) refresh the flow but must not promote it, so a
		// client re-pinging a pinned server from a stable source port never mints
		// a phantom session (issue #1904). buf[0] is read here, before the frame
		// is copied out below; the n > 0 guard keeps a zero-length datagram from
		// indexing buf[0].
		connected := n > 0 && buf[0] >= 0x80
		id, ok, promote := t.flows.Lookup(udpAddr, connected)
		if !ok {
			ip := netutil.HostOf(udpAddr)
			// New-flow rate cap, then the concurrent-flow cap -- both per
			// source IP (RakNet unconnected-ping amplification hygiene,
			// docs/app/BEDROCK_TUNNEL.md), recorded on the shared per-IP
			// rejection counter with listener="bedrock" (issue #1909). When
			// AllowJoin passes but Acquire fails, the rate-window count was
			// already consumed -- strictly conservative (the flow was not
			// admitted), so acceptable.
			if !t.caps.AllowJoin(ip) {
				t.metrics.IPCapsReject(metrics.ListenerBedrock, metrics.CapKindRate)
				continue
			}
			if !t.caps.Acquire(ip) {
				t.metrics.IPCapsReject(metrics.ListenerBedrock, metrics.CapKindConn)
				continue
			}
			id = t.flows.Create(udpAddr, connected)
			t.metrics.BedrockFlowCreated()
		}

		// Promote the flow to a reported session once it crosses
		// flowPromoteThreshold client->worker datagrams: it is a real RakNet
		// connection, not ping/scan churn (issue #1904). The promotion decision
		// was made under the FlowTable lock inside Lookup; Start is called here,
		// outside that lock (it only buffers the event -- session/reporter.go),
		// and the minted id is stored back on the flow for the matching End on
		// eviction / teardown. The relay cannot see Floodgate identity, so the
		// username/uuid are empty; playerIP is the client's true UDP source.
		if promote {
			sid := t.sessions.Start(t.serverID, "", netutil.HostOf(udpAddr), "", "")
			t.flows.Promote(id, sid)
		}

		// Rate-limit forwarded RakNet unconnected-ping (first byte 0x01) per
		// flow so a single continuously-refreshed flow cannot drive Geyser's
		// amplifying unconnected-pong replies at line rate, turning the relay
		// into a reflection source (issue #1604). This stays on the ingress side,
		// before the enqueue, so ping-flood drops never consume a channel slot.
		// Only buf[0] is inspected -- the relay never parses RakNet beyond the
		// first byte; the n > 0 guard keeps a zero-length datagram from indexing
		// buf[0]. buf[0] is read here, before the frame is copied out below, so a
		// later read cannot overwrite it first.
		if n > 0 && buf[0] == 0x01 && !t.flows.AllowPing(id) {
			t.metrics.BedrockDatagramDropped(metrics.DirectionIn, metrics.BedrockDropPingRateCap)
			continue
		}

		// Copy the frame out of the shared read buffer into a pooled buffer so it
		// can cross to the sender without sharing mutable state with the next
		// read.
		bufp := t.framePool.Get().(*[]byte)
		frame := (*bufp)[:FlowIDSize+n]
		binary.BigEndian.PutUint32(frame[:FlowIDSize], id)
		copy(frame[FlowIDSize:], buf[:n])
		*bufp = frame
		select {
		case sendCh <- bufp:
		default:
			// Single, per-datagram drop site: the tunnel is congestion-limited
			// or in loss recovery and the send queue is full. Dropping here
			// (rather than blocking the reader) is what keeps one congested flow
			// from stalling ingress for every flow on the port (issue #1721). The
			// drop-count metric hangs off exactly this point (issue #1909).
			t.metrics.BedrockDatagramDropped(metrics.DirectionIn, metrics.BedrockDropQueueFull)
			*bufp = (*bufp)[:cap(*bufp)]
			t.framePool.Put(bufp)
		}
	}
}

// pumpQueueToQUIC drains the frames pumpUDPToQueue enqueues and forwards each to
// the Worker as a QUIC DATAGRAM frame. It runs in its own goroutine so a stalled
// SendDatagram (a congestion-limited or loss-recovering tunnel) backs pressure up
// through the channel to the reader's drop path instead of blocking the reader
// itself. It returns once sendCh is closed (the reader has stopped).
func (t *Tunnel) pumpQueueToQUIC(sendCh <-chan *[]byte) {
	for bufp := range sendCh {
		if err := t.quicConn.SendDatagram(*bufp); err != nil {
			t.metrics.BedrockDatagramDropped(metrics.DirectionIn, metrics.BedrockDropQUICSend)
			t.logger.Debug("bedrock: SendDatagram failed", "error", err)
		}
		// SendDatagram copied the payload synchronously, so the buffer is free to
		// recycle for the next read.
		*bufp = (*bufp)[:cap(*bufp)]
		t.framePool.Put(bufp)
	}
}

// sweepLoop periodically evicts idle flows and releases their ipcaps slots,
// until ctx is cancelled.
func (t *Tunnel) sweepLoop(ctx context.Context) {
	ticker := time.NewTicker(flowSweepInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			t.sweepOnce()
		}
	}
}

// sweepOnce evicts every idle flow, releasing its ipcaps slot and Ending its
// reported session, if any (issue #1904). reporter.End is called outside the
// FlowTable lock, matching the promotion path.
func (t *Tunnel) sweepOnce() {
	addrs, endedSessions := t.flows.Evict()
	for _, addr := range addrs {
		t.caps.Release(netutil.HostOf(addr))
	}
	t.metrics.BedrockFlowsEvicted(len(addrs))
	for _, sid := range endedSessions {
		t.sessions.End(sid)
	}
}
