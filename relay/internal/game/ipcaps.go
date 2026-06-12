package game

import (
	"sync"
	"time"
)

// IPCaps enforces the per-IP hygiene limits on the unauthenticated game
// listener (RELAY.md Section 11): a cap on concurrent connections per source IP
// and a cap on the join (login) rate per source IP. It is hygiene, not DDoS
// protection. Safe for concurrent use.
type IPCaps struct {
	maxConns    uint32
	joinsPerSec uint32
	now         func() time.Time
	mu          sync.Mutex
	conns       map[string]uint32
	joinWindows map[string]*rateWindow
}

type rateWindow struct {
	windowStart time.Time
	count       uint32
}

// NewIPCaps builds the caps. now is injectable for tests; pass time.Now in
// production. A zero maxConns or joinsPerSec disables that particular cap.
func NewIPCaps(maxConns, joinsPerSec uint32, now func() time.Time) *IPCaps {
	if now == nil {
		now = time.Now
	}
	return &IPCaps{
		maxConns:    maxConns,
		joinsPerSec: joinsPerSec,
		now:         now,
		conns:       make(map[string]uint32),
		joinWindows: make(map[string]*rateWindow),
	}
}

// Acquire registers a new connection from ip. It returns false if ip is already
// at the concurrent-connection cap, in which case the connection must be
// dropped and Release must NOT be called.
func (c *IPCaps) Acquire(ip string) bool {
	if c.maxConns == 0 {
		return true
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.conns[ip] >= c.maxConns {
		return false
	}
	c.conns[ip]++
	return true
}

// Release drops one concurrent-connection count for ip. It must be called once
// per successful Acquire.
func (c *IPCaps) Release(ip string) {
	if c.maxConns == 0 {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if n := c.conns[ip]; n <= 1 {
		delete(c.conns, ip)
	} else {
		c.conns[ip] = n - 1
	}
}

// AllowJoin reports whether a join (login) from ip is within the per-second
// rate cap, counting this attempt. It uses a fixed one-second window per IP.
func (c *IPCaps) AllowJoin(ip string) bool {
	if c.joinsPerSec == 0 {
		return true
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.now()
	w := c.joinWindows[ip]
	if w == nil || now.Sub(w.windowStart) >= time.Second {
		c.joinWindows[ip] = &rateWindow{windowStart: now, count: 1}
		return true
	}
	if w.count >= c.joinsPerSec {
		return false
	}
	w.count++
	return true
}
