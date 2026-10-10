package bedrocktunnel

import (
	"context"
	"encoding/binary"
	"log/slog"
	"net"
	"sync"
	"time"
)

// Evict idle local sockets; the relay's flow table determines when a client is gone.
const flowIdleTimeout = 60 * time.Second
const flowSweepInterval = 15 * time.Second

// Leave room above the relay's 1200-byte payload budget to read Geyser replies in full.
const udpReadBufferSize = 2048

// Cap local sockets independently of relay admission to contain a misbehaving relay.
// Log a capacity refusal once per connection; idle eviction makes room for new flows.
const maxFlowsPerTunnel = 4096

// datagramSender is the subset of *quic.Conn a flow's reply pump needs.
type datagramSender interface {
	SendDatagram(p []byte) error
}

// flowRegistry owns one local UDP socket per relay flow ID and is discarded on reconnect.
type flowRegistry struct {
	dialUDP  func(ctx context.Context, addr string) (net.Conn, error)
	target   string
	sender   datagramSender
	logger   *slog.Logger
	serverID string

	mu        sync.Mutex
	byID      map[uint32]*flowSocket
	sweep     chan struct{} // closed by closeAll to stop the sweep goroutine
	capLogged bool          // set once maxFlowsPerTunnel has been logged (logCapOnce)
}

// flowSocket is one flow's local UDP socket to the container's Geyser port,
// plus the idle-eviction bookkeeping.
type flowSocket struct {
	conn     net.Conn
	lastSeen time.Time
}

// newFlowRegistry starts idle eviction; the caller must closeAll to release its sockets and goroutine.
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

// forward creates sockets only for relay-assigned IDs, dropping new IDs at capacity.
// The single receive loop serializes creation.
func (r *flowRegistry) forward(ctx context.Context, id uint32, payload []byte) error {
	r.mu.Lock()
	fs, ok := r.byID[id]
	atCeiling := !ok && len(r.byID) >= maxFlowsPerTunnel
	r.mu.Unlock()
	if atCeiling {
		r.logCapOnce()
		return nil
	}
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

// Log once per connection so excess flow IDs cannot flood the log.
func (r *flowRegistry) logCapOnce() {
	r.mu.Lock()
	already := r.capLogged
	r.capLogged = true
	r.mu.Unlock()
	if already {
		return
	}
	r.logger.Warn("bedrock tunnel: max flows per tunnel reached; dropping new flow",
		"server_id", r.serverID, "max_flows_per_tunnel", maxFlowsPerTunnel)
}

// readPump echoes the relay's flow ID and evicts the socket on read failure so the next datagram can redial.
func (r *flowRegistry) readPump(id uint32, fs *flowSocket) {
	buf := make([]byte, udpReadBufferSize)
	for {
		n, err := fs.conn.Read(buf)
		if err != nil {
			r.mu.Lock()
			evicted := false
			if cur, ok := r.byID[id]; ok && cur == fs {
				delete(r.byID, id)
				evicted = true
			}
			r.mu.Unlock()
			if evicted {
				_ = fs.conn.Close()
			}
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

// closeAll stops eviction and closes sockets, unblocking their read pumps.
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
