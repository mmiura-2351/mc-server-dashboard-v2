package bedrock

import (
	"context"
	"encoding/binary"
	"fmt"
	"log/slog"
	"net"
	"sync"
	"time"

	"github.com/quic-go/quic-go"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/netutil"
)

// flowIdleTimeout bounds how long a Bedrock client's flow entry survives
// without activity in either direction before the relay reclaims it and its
// ipcaps slot (docs/app/BEDROCK_TUNNEL.md).
const flowIdleTimeout = 60 * time.Second

// flowSweepInterval is how often a Tunnel checks for idle flows to evict.
const flowSweepInterval = 15 * time.Second

// udpReadBufferSize is sized generously above maxDatagramPayload so an
// oversized inbound UDP datagram is read in full (and then dropped by the MTU
// gate in pumpUDPToQUIC) rather than silently truncated by a too-small buffer.
const udpReadBufferSize = 2048

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
	logger   *slog.Logger

	closeOnce sync.Once
}

// bind opens the public UDP port for bedrockPort and wires it to quicConn. A
// bind failure (most likely the port already held by a not-yet-unbound prior
// connection for the same server, e.g. a Worker redial racing its old QUIC
// connection's idle timeout -- docs/app/BEDROCK_TUNNEL.md) is returned as-is
// for the caller to treat as a handshake rejection.
func bind(bedrockPort uint32, quicConn *quic.Conn, caps *ipcaps.IPCaps, logger *slog.Logger) (*Tunnel, error) {
	udpConn, err := net.ListenPacket("udp", fmt.Sprintf(":%d", bedrockPort))
	if err != nil {
		return nil, err
	}
	return &Tunnel{
		udpConn:  udpConn,
		quicConn: quicConn,
		flows:    NewFlowTable(flowIdleTimeout, nil),
		caps:     caps,
		logger:   logger,
	}, nil
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

	var udpDone sync.WaitGroup
	udpDone.Add(1)
	go func() {
		defer udpDone.Done()
		t.pumpUDPToQUIC()
	}()

	// Blocks until runCtx is cancelled, ReceiveDatagram reports the
	// connection is gone, or an external takeover (close, below) force-closes
	// the QUIC connection.
	t.pumpQUICToUDP(runCtx)

	// close is idempotent: if a takeover (#1565) already force-closed this
	// tunnel concurrently, this is a no-op; otherwise it is this tunnel's own
	// natural teardown, closing the QUIC connection (visible to the Worker
	// immediately rather than waiting out its idle timeout) and the UDP
	// socket (which unblocks pumpUDPToQUIC's blocking ReadFrom).
	t.close("tunnel closing")
	udpDone.Wait()
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
			continue // malformed frame: drop
		}
		id := binary.BigEndian.Uint32(data[:FlowIDSize])
		addr, ok := t.flows.AddrByID(id)
		if !ok {
			// Unknown or evicted flow (e.g. the relay reclaimed it for
			// inactivity after the Worker's last reply): drop
			// (docs/app/BEDROCK_TUNNEL.md).
			continue
		}
		if _, err := t.udpConn.WriteTo(data[FlowIDSize:], addr); err != nil {
			t.logger.Debug("bedrock: UDP write failed", "addr", addr, "error", err)
		}
	}
}

// pumpUDPToQUIC reads RakNet datagrams from Bedrock clients on the bound
// public UDP port, assigns/reuses a flow id per source address, and forwards
// them to the Worker as QUIC DATAGRAM frames. It returns once the UDP socket
// is closed (unbind, called after pumpQUICToUDP ends).
func (t *Tunnel) pumpUDPToQUIC() {
	buf := make([]byte, udpReadBufferSize)
	for {
		n, addr, err := t.udpConn.ReadFrom(buf)
		if err != nil {
			return
		}
		udpAddr, ok := addr.(*net.UDPAddr)
		if !ok {
			continue
		}
		if n > maxDatagramPayload {
			// Oversized for our conservative datagram budget: drop rather than
			// forward. RakNet's own MTU discovery reads this the same way it
			// reads any other unanswered probe -- try a smaller candidate
			// (docs/app/BEDROCK_TUNNEL.md).
			continue
		}

		id, ok := t.flows.Lookup(udpAddr)
		if !ok {
			ip := netutil.HostOf(udpAddr)
			// New-flow rate cap, then the concurrent-flow cap -- both per
			// source IP (RakNet unconnected-ping amplification hygiene,
			// docs/app/BEDROCK_TUNNEL.md). When AllowJoin passes but Acquire
			// fails, the rate-window count was already consumed -- strictly
			// conservative (the flow was not admitted), so acceptable.
			if !t.caps.AllowJoin(ip) || !t.caps.Acquire(ip) {
				continue
			}
			id = t.flows.Create(udpAddr)
		}

		frame := make([]byte, FlowIDSize+n)
		binary.BigEndian.PutUint32(frame[:FlowIDSize], id)
		copy(frame[FlowIDSize:], buf[:n])
		if err := t.quicConn.SendDatagram(frame); err != nil {
			t.logger.Debug("bedrock: SendDatagram failed", "error", err)
		}
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
			for _, addr := range t.flows.Evict() {
				t.caps.Release(netutil.HostOf(addr))
			}
		}
	}
}
