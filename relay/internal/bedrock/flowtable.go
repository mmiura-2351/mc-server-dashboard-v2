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

	// ingress counts client->worker datagrams observed on this flow (both the
	// Create datagram and every later Lookup hit). Once it reaches
	// flowPromoteThreshold the flow is a real RakNet connection rather than
	// ping/scan churn, and the relay reports it to the API as a live session
	// (issue #1904). promoted records that Start has already fired; sessionID is
	// the reporter-minted id, set by Promote, so Evict / DrainPromoted can End
	// the matching session.
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

// Lookup returns the existing flow id for addr, refreshes its idle deadline, and
// counts the client->worker datagram for session promotion (issue #1904). ok is
// false when addr has no flow yet -- the caller (after applying any admission
// checks, e.g. ipcaps) creates one via Create. promote is true on the single
// datagram that carries the flow across flowPromoteThreshold; the caller then
// mints a session id (outside this lock) and stores it back via Promote.
func (t *FlowTable) Lookup(addr *net.UDPAddr) (id uint32, ok, promote bool) {
	key := addr.String()
	t.mu.Lock()
	defer t.mu.Unlock()
	e, ok := t.byAddr[key]
	if !ok {
		return 0, false, false
	}
	e.lastSeen = t.now()
	e.ingress++
	// == (not >=) fires exactly once: ingress increases monotonically and a
	// single reader goroutine drives it, so Start is reported at most once per
	// flow.
	return e.id, true, e.ingress == flowPromoteThreshold
}

// Create allocates a new flow id for addr and returns it. The caller must not
// already hold a flow for addr (check via Lookup first); Create does not
// re-check.
func (t *FlowTable) Create(addr *net.UDPAddr) uint32 {
	t.mu.Lock()
	defer t.mu.Unlock()
	// nextID wraps at 2^32 without a liveness check; unreachable in practice
	// (the per-tunnel ipcaps global ceiling bounds live flows to ~10k, and a
	// collision needs a >4-billion-flow-old entry still alive).
	id := t.nextID
	t.nextID++
	// ingress starts at 1 for the datagram that created the flow; promotion is
	// detected on a later Lookup hit, so flowPromoteThreshold must be >= 2.
	e := &flowEntry{id: id, addr: addr, lastSeen: t.now(), ingress: 1}
	t.byAddr[addr.String()] = e
	t.byID[id] = e
	return id
}

// Promote records that the flow with id has been reported to the session
// reporter as a live session, storing the reporter-minted sessionID so a later
// Evict or DrainPromoted can End it (issue #1904). The caller decides to promote
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

// DrainPromoted clears the promoted flag on every currently-promoted flow and
// returns their session ids, so tunnel teardown can End sessions still open when
// the tunnel goes away (issue #1904). Clearing the flag keeps a concurrent or
// later Evict from double-Ending the same session -- both paths run under this
// lock, so each promoted session is surfaced by exactly one of them.
func (t *FlowTable) DrainPromoted() []string {
	t.mu.Lock()
	defer t.mu.Unlock()
	var ids []string
	for _, e := range t.byAddr {
		if e.promoted {
			ids = append(ids, e.sessionID)
			e.promoted = false
		}
	}
	return ids
}

// Len returns the current number of live flows (test/diagnostic use).
func (t *FlowTable) Len() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return len(t.byAddr)
}
