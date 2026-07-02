package bedrocktunnel

import (
	"context"
	"encoding/binary"
	"log/slog"
	"net"
	"sync"
	"time"
)

// flowIdleTimeout / flowSweepInterval bound how long a per-flow local UDP
// socket survives without activity before this Worker closes it — purely a
// local resource-hygiene measure; it is the relay's own flow table (with the
// same defaults, docs/app/BEDROCK_TUNNEL.md Section 7) that actually decides
// when a Bedrock client is gone, and simply stops sending datagrams for that
// flow id once it does.
const flowIdleTimeout = 60 * time.Second
const flowSweepInterval = 15 * time.Second

// udpReadBufferSize is sized generously above the relay's 1200-byte RakNet
// payload budget so a Geyser reply is read in full (docs/app/BEDROCK_TUNNEL.md
// Section 6).
const udpReadBufferSize = 2048

// datagramSender is the subset of *quic.Conn a flow's reply pump needs.
type datagramSender interface {
	SendDatagram(p []byte) error
}

// flowRegistry maps relay-assigned flow ids to a dedicated local UDP socket
// dialed to the server's Geyser port — "one local UDP socket per relay flow
// id" (docs/app/BEDROCK_TUNNEL.md Section 5). It is entirely connection-scoped:
// pump creates one fresh registry per dial/handshake attempt and closeAll runs
// when that connection ends, since flow ids restart from zero on every new
// QUIC connection and carrying one across a reconnect would misroute the
// relay's flow ids onto the wrong local socket.
type flowRegistry struct {
	dialUDP  func(ctx context.Context, addr string) (net.Conn, error)
	target   string
	sender   datagramSender
	logger   *slog.Logger
	serverID string

	mu    sync.Mutex
	byID  map[uint32]*flowSocket
	sweep chan struct{} // closed by closeAll to stop the sweep goroutine
}

// flowSocket is one flow's local UDP socket to the container's Geyser port,
// plus the idle-eviction bookkeeping.
type flowSocket struct {
	conn     net.Conn
	lastSeen time.Time
}

// newFlowRegistry builds a registry whose flows dial target (the resolved
// Geyser address for one server) via dialUDP, and whose replies are sent back
// over sender with the same flow id the relay assigned. It starts the idle
// sweep goroutine; the caller must call closeAll to stop it and release every
// socket.
func newFlowRegistry(dialUDP func(context.Context, string) (net.Conn, error), target string, sender datagramSender, logger *slog.Logger, serverID string) *flowRegistry {
	r := &flowRegistry{
		dialUDP:  dialUDP,
		target:   target,
		sender:   sender,
		logger:   logger,
		serverID: serverID,
		byID:     map[uint32]*flowSocket{},
		sweep:    make(chan struct{}),
	}
	go r.sweepLoop()
	return r
}

// forward writes payload for flow id to its local UDP socket, dialing a fresh
// one on first sight of id — the relay assigns flow ids, the Worker only ever
// mints a *local* socket for one, never a flow id of its own
// (docs/app/BEDROCK_TUNNEL.md Section 5). It is called serially from the
// connection's single receive loop, so the check-then-create below never races
// itself.
func (r *flowRegistry) forward(ctx context.Context, id uint32, payload []byte) error {
	r.mu.Lock()
	fs, ok := r.byID[id]
	r.mu.Unlock()
	if !ok {
		conn, err := r.dialUDP(ctx, r.target)
		if err != nil {
			return err
		}
		fs = &flowSocket{conn: conn, lastSeen: time.Now()}
		r.mu.Lock()
		r.byID[id] = fs
		r.mu.Unlock()
		go r.readPump(id, fs)
	}
	r.mu.Lock()
	fs.lastSeen = time.Now()
	r.mu.Unlock()
	_, err := fs.conn.Write(payload)
	return err
}

// readPump reads Geyser's replies for one flow and forwards them back over the
// QUIC connection, prefixed with the same flow id the relay assigned
// (docs/app/BEDROCK_TUNNEL.md Section 5: "the Worker only ever echoes back the
// flow id"). It exits once the flow's socket is closed (idle eviction or
// closeAll).
func (r *flowRegistry) readPump(id uint32, fs *flowSocket) {
	buf := make([]byte, udpReadBufferSize)
	for {
		n, err := fs.conn.Read(buf)
		if err != nil {
			return
		}
		r.mu.Lock()
		fs.lastSeen = time.Now()
		r.mu.Unlock()

		frame := make([]byte, flowIDSize+n)
		binary.BigEndian.PutUint32(frame[:flowIDSize], id)
		copy(frame[flowIDSize:], buf[:n])
		if err := r.sender.SendDatagram(frame); err != nil {
			r.logger.Debug("bedrock tunnel: SendDatagram failed",
				"server_id", r.serverID, "flow_id", id, "error", err)
		}
	}
}

// sweepLoop periodically evicts flows idle for at least flowIdleTimeout, until
// closeAll closes r.sweep.
func (r *flowRegistry) sweepLoop() {
	ticker := time.NewTicker(flowSweepInterval)
	defer ticker.Stop()
	for {
		select {
		case <-r.sweep:
			return
		case <-ticker.C:
			r.evictIdle()
		}
	}
}

// evictIdle closes and forgets every flow idle for at least flowIdleTimeout,
// which unblocks its readPump goroutine.
func (r *flowRegistry) evictIdle() {
	now := time.Now()
	r.mu.Lock()
	var stale []net.Conn
	for id, fs := range r.byID {
		if now.Sub(fs.lastSeen) >= flowIdleTimeout {
			stale = append(stale, fs.conn)
			delete(r.byID, id)
		}
	}
	r.mu.Unlock()
	for _, c := range stale {
		_ = c.Close()
	}
}

// closeAll stops the sweep loop and closes every live flow socket, unblocking
// their readPump goroutines — the connection-scoped flow-state discard
// docs/app/BEDROCK_TUNNEL.md Section 5 requires on redial.
func (r *flowRegistry) closeAll() {
	close(r.sweep)
	r.mu.Lock()
	conns := make([]net.Conn, 0, len(r.byID))
	for _, fs := range r.byID {
		conns = append(conns, fs.conn)
	}
	r.byID = map[uint32]*flowSocket{}
	r.mu.Unlock()
	for _, c := range conns {
		_ = c.Close()
	}
}
