package bedrock

import (
	"net"
	"sync"
	"time"
)

// FlowIDSize is the size, in bytes, of the big-endian flow id prefix on every
// QUIC DATAGRAM carrying Bedrock traffic (docs/app/BEDROCK_TUNNEL.md).
const FlowIDSize = 4

// FlowTable maps Bedrock client source addresses to compact flow ids and back,
// for one bound bedrock_port (RakNet has no session concept the relay can key
// on, so this is a NAT-style mapping keyed on source address). The relay
// assigns ids; the Worker only ever echoes one back on a reply datagram
// (docs/app/BEDROCK_TUNNEL.md). Idle entries are evicted via Evict so a churn
// of distinct clients does not grow the table without bound. Safe for
// concurrent use.
type FlowTable struct {
	idleTTL time.Duration
	now     func() time.Time

	mu     sync.Mutex
	nextID uint32
	byAddr map[string]*flowEntry
	byID   map[uint32]*flowEntry
}

type flowEntry struct {
	id       uint32
	addr     *net.UDPAddr
	lastSeen time.Time

	// pingWindowStart / pingCount rate-limit forwarded RakNet unconnected-pings
	// on this flow within a fixed one-second window (AllowPing), mirroring the
	// per-IP rate window in relay/internal/ipcaps. This bounds the relay's
	// reflection/amplification exposure on a single continuously-refreshed flow
	// (issue #1604), which the per-IP caps -- gating only new-flow creation --
	// do not.
	pingWindowStart time.Time
	pingCount       uint32

	// ingress counts CONNECTED client->worker datagrams (RakNet FLAG_VALID, first
	// byte >= 0x80) observed on this flow via Lookup. Offline packets
	// (unconnected ping/pong, the connection handshake) refresh the flow but do
	// not count, so a client re-pinging a pinned server never promotes. Once
	// ingress reaches flowPromoteThreshold the flow is a real connection and the
	// relay reports it to the API as a live session (issue #1904). promoted
	// records that Start has already fired; sessionID is the reporter-minted id,
	// set by Promote, so Evict / Drain can End the matching session.
	ingress   uint32
	promoted  bool
	sessionID string
}

// NewFlowTable builds a table whose entries are evicted once idle for idleTTL.
// now is injectable for tests; pass time.Now in production.
func NewFlowTable(idleTTL time.Duration, now func() time.Time) *FlowTable {
	if now == nil {
		now = time.Now
	}
	return &FlowTable{
		idleTTL: idleTTL,
		now:     now,
		byAddr:  make(map[string]*flowEntry),
		byID:    make(map[uint32]*flowEntry),
	}
}

// Lookup returns the existing flow id for addr and refreshes its idle deadline.
// counts marks whether this datagram advances session promotion -- true only for
// connected RakNet datagrams (FLAG_VALID, first byte >= 0x80); offline packets
// (unconnected ping/pong, the connection handshake) refresh the flow but must
// not promote it, so a client re-pinging a pinned server never mints a phantom
// session (issue #1904). ok is false when addr has no flow yet -- the caller
// (after applying any admission checks, e.g. ipcaps) creates one via Create.
// promote is true on the single connected datagram that carries the flow across
// flowPromoteThreshold; the caller then mints a session id (outside this lock)
// and stores it back via Promote.
func (t *FlowTable) Lookup(addr *net.UDPAddr, counts bool) (id uint32, ok, promote bool) {
	key := addr.String()
	t.mu.Lock()
	defer t.mu.Unlock()
	e, ok := t.byAddr[key]
	if !ok {
		return 0, false, false
	}
	// Any datagram is activity that refreshes the idle deadline, but only a
	// connected one advances promotion.
	e.lastSeen = t.now()
	if !counts {
		return e.id, true, false
	}
	e.ingress++
	// == (not >=) fires exactly once: ingress increases monotonically and a
	// single reader goroutine drives it, so Start is reported at most once per
	// flow.
	return e.id, true, e.ingress == flowPromoteThreshold
}

// Create allocates a new flow id for addr and returns it. counts marks whether
// the creating datagram is connected and so advances promotion, matching Lookup.
// The caller must not already hold a flow for addr (check via Lookup first);
// Create does not re-check.
func (t *FlowTable) Create(addr *net.UDPAddr, counts bool) uint32 {
	t.mu.Lock()
	defer t.mu.Unlock()
	// nextID wraps at 2^32 without a liveness check; unreachable in practice
	// (the per-tunnel ipcaps global ceiling bounds live flows to ~10k, and a
	// collision needs a >4-billion-flow-old entry still alive).
	id := t.nextID
	t.nextID++
	// A flow's first datagram is normally an offline RakNet handshake/ping
	// (counts=false, ingress 0), but a NAT-rebound mid-session client can create
	// a flow with a connected packet, which must count toward promotion just like
	// a connected Lookup hit (issue #1904).
	var ingress uint32
	if counts {
		ingress = 1
	}
	e := &flowEntry{id: id, addr: addr, lastSeen: t.now(), ingress: ingress}
	t.byAddr[addr.String()] = e
	t.byID[id] = e
	return id
}

// Promote records that the flow with id has been reported to the session
// reporter as a live session, storing the reporter-minted sessionID so a later
// Evict or Drain can End it (issue #1904). The caller decides to promote
// under Lookup's lock but calls the reporter (to mint the id) outside it, then
// stores the id here. It is a no-op if the flow no longer exists.
func (t *FlowTable) Promote(id uint32, sessionID string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if e, ok := t.byID[id]; ok {
		e.promoted = true
		e.sessionID = sessionID
	}
}

// AllowPing reports whether a RakNet unconnected-ping on flow id may be
// forwarded under a fixed one-second window (flowPingsPerSecond), counting this
// attempt. It returns false for an unknown id. This caps the relay's
// reflection/amplification exposure on a single continuously-refreshed flow --
// which the per-IP caps, gating only new-flow creation, do not bound (issue
// #1604). The window mirrors ipcaps.AllowJoin's per-IP rate window.
func (t *FlowTable) AllowPing(id uint32) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	e, ok := t.byID[id]
	if !ok {
		return false
	}
	now := t.now()
	if now.Sub(e.pingWindowStart) >= time.Second {
		e.pingWindowStart = now
		e.pingCount = 1
		return true
	}
	if e.pingCount >= flowPingsPerSecond {
		return false
	}
	e.pingCount++
	return true
}

// AddrByID returns the source address for a flow id and refreshes its idle
// deadline (a reply datagram keeps the flow alive just like a new client
// datagram does). ok is false for an unrecognized id -- e.g. the Worker
// replying after the relay already evicted the flow for inactivity; the
// caller drops the datagram (docs/app/BEDROCK_TUNNEL.md).
func (t *FlowTable) AddrByID(id uint32) (addr *net.UDPAddr, ok bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	e, ok := t.byID[id]
	if !ok {
		return nil, false
	}
	e.lastSeen = t.now()
	return e.addr, true
}

// Evict removes every flow idle for at least idleTTL and returns their
// addresses, so the caller can release any per-address resources tied to them
// (e.g. an ipcaps concurrent-flow slot), plus the session ids of any evicted
// flows that had been promoted, so the caller can End their reported sessions
// (issue #1904).
func (t *FlowTable) Evict() (addrs []*net.UDPAddr, endedSessions []string) {
	now := t.now()
	t.mu.Lock()
	defer t.mu.Unlock()
	for key, e := range t.byAddr {
		if now.Sub(e.lastSeen) >= t.idleTTL {
			delete(t.byAddr, key)
			delete(t.byID, e.id)
			addrs = append(addrs, e.addr)
			if e.promoted {
				endedSessions = append(endedSessions, e.sessionID)
			}
		}
	}
	return addrs, endedSessions
}

// Drain removes every flow from the table and returns the session ids of those
// that had been promoted (so tunnel teardown can End sessions still open when
// the tunnel goes away, issue #1904) plus the total number of flows removed (so
// teardown can decrement the active-flows metric and not leak the gauge, issue
// #1909). Removing the entries outright -- rather than only clearing the
// promoted flag -- keeps a concurrent or later Evict from also surfacing the
// same flow: both paths run under this lock, so each flow (and each promoted
// session) is surfaced by exactly one of them.
func (t *FlowTable) Drain() (endedSessions []string, removed int) {
	t.mu.Lock()
	defer t.mu.Unlock()
	removed = len(t.byAddr)
	for _, e := range t.byAddr {
		if e.promoted {
			endedSessions = append(endedSessions, e.sessionID)
		}
	}
	t.byAddr = make(map[string]*flowEntry)
	t.byID = make(map[uint32]*flowEntry)
	return endedSessions, removed
}

// Len returns the current number of live flows (test/diagnostic use).
func (t *FlowTable) Len() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return len(t.byAddr)
}
