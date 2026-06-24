// Package ipcaps provides the per-IP hygiene caps shared by the relay's
// internet-exposed listeners (RELAY.md Section 11). The game listener uses both
// the concurrent-connection cap and the join-rate cap; the tunnel listener uses
// only the concurrent-connection cap (its rate is naturally bounded by token
// issuance). Sharing the type keeps the connection-cap logic in one place so the
// two listeners cannot diverge.
package ipcaps

import (
	"sync"
	"sync/atomic"
	"time"
)

// IPCaps enforces the per-IP hygiene limits on an unauthenticated listener
// (RELAY.md Section 11): a cap on concurrent connections per source IP and a cap
// on the join (login) rate per source IP. It is hygiene, not DDoS protection.
// Safe for concurrent use.
type IPCaps struct {
	maxConns    uint32
	joinsPerSec uint32
	globalMax   int64
	globalConns atomic.Int64
	now         func() time.Time
	mu          sync.Mutex
	conns       map[string]uint32
	joinWindows map[string]*rateWindow
	lastSweep   time.Time
}

// joinWindowSweepInterval bounds how often AllowJoin opportunistically evicts
// expired rate windows, so the joinWindows map tracks only recently-active IPs
// rather than every IP that ever attempted a login (hostile churn would
// otherwise grow it without bound).
const joinWindowSweepInterval = time.Minute

type rateWindow struct {
	windowStart time.Time
	count       uint32
}

// DefaultGlobalMax is the default global connection ceiling used when callers
// pass zero for globalMax. It is defense-in-depth against distributed source
// exhaustion (RELAY.md Section 16 defers volumetric DDoS to the provider).
const DefaultGlobalMax int64 = 10_000

// NewIPCaps builds the caps. now is injectable for tests; pass time.Now in
// production. A zero maxConns or joinsPerSec disables that particular cap.
// globalMax sets the hard ceiling on total concurrent connections across all IPs;
// zero applies DefaultGlobalMax, negative disables the global cap.
func NewIPCaps(maxConns, joinsPerSec uint32, globalMax int64, now func() time.Time) *IPCaps {
	if now == nil {
		now = time.Now
	}
	if globalMax == 0 {
		globalMax = DefaultGlobalMax
	}
	return &IPCaps{
		maxConns:    maxConns,
		joinsPerSec: joinsPerSec,
		globalMax:   globalMax,
		now:         now,
		conns:       make(map[string]uint32),
		joinWindows: make(map[string]*rateWindow),
	}
}

// Acquire registers a new connection from ip. It returns false if ip is already
// at the concurrent-connection cap or the global connection ceiling has been
// reached, in which case the connection must be dropped and Release must NOT be
// called.
func (c *IPCaps) Acquire(ip string) bool {
	// Check global cap first (lock-free fast path).
	if c.globalMax > 0 && c.globalConns.Load() >= c.globalMax {
		return false
	}
	// Reserve a global slot atomically. Add(1) returns the new value; if it
	// exceeds the ceiling another goroutine raced past the fast path, so undo.
	if c.globalMax > 0 {
		if c.globalConns.Add(1) > c.globalMax {
			c.globalConns.Add(-1)
			return false
		}
	}
	if c.maxConns == 0 {
		return true
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.conns[ip] >= c.maxConns {
		// Per-IP cap hit — release the global slot we just reserved.
		if c.globalMax > 0 {
			c.globalConns.Add(-1)
		}
		return false
	}
	c.conns[ip]++
	return true
}

// Release drops one concurrent-connection count for ip. It must be called once
// per successful Acquire.
func (c *IPCaps) Release(ip string) {
	if c.globalMax > 0 {
		c.globalConns.Add(-1)
	}
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
	c.sweepExpired(now)
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

// sweepExpired evicts rate windows whose one-second window has elapsed, at most
// once per joinWindowSweepInterval. Caller must hold c.mu. This bounds the
// joinWindows map to recently-active source IPs.
func (c *IPCaps) sweepExpired(now time.Time) {
	if now.Sub(c.lastSweep) < joinWindowSweepInterval {
		return
	}
	c.lastSweep = now
	for ip, w := range c.joinWindows {
		if now.Sub(w.windowStart) >= time.Second {
			delete(c.joinWindows, ip)
		}
	}
}
