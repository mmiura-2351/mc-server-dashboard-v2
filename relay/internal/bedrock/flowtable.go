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

// Lookup returns the existing flow id for addr and refreshes its idle
// deadline. ok is false when addr has no flow yet -- the caller (after
// applying any admission checks, e.g. ipcaps) creates one via Create.
func (t *FlowTable) Lookup(addr *net.UDPAddr) (id uint32, ok bool) {
	key := addr.String()
	t.mu.Lock()
	defer t.mu.Unlock()
	e, ok := t.byAddr[key]
	if !ok {
		return 0, false
	}
	e.lastSeen = t.now()
	return e.id, true
}

// Create allocates a new flow id for addr and returns it. The caller must not
// already hold a flow for addr (check via Lookup first); Create does not
// re-check.
func (t *FlowTable) Create(addr *net.UDPAddr) uint32 {
	t.mu.Lock()
	defer t.mu.Unlock()
	id := t.nextID
	t.nextID++
	e := &flowEntry{id: id, addr: addr, lastSeen: t.now()}
	t.byAddr[addr.String()] = e
	t.byID[id] = e
	return id
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
// (e.g. an ipcaps concurrent-flow slot).
func (t *FlowTable) Evict() []*net.UDPAddr {
	now := t.now()
	t.mu.Lock()
	defer t.mu.Unlock()
	var evicted []*net.UDPAddr
	for key, e := range t.byAddr {
		if now.Sub(e.lastSeen) >= t.idleTTL {
			delete(t.byAddr, key)
			delete(t.byID, e.id)
			evicted = append(evicted, e.addr)
		}
	}
	return evicted
}

// Len returns the current number of live flows (test/diagnostic use).
func (t *FlowTable) Len() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return len(t.byAddr)
}
