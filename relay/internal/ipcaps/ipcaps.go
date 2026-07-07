// Package ipcaps provides the per-IP hygiene caps shared by the relay's
// internet-exposed listeners (RELAY.md Section 11). The game listener uses both
// the concurrent-connection cap and the join-rate cap (login attempts and
// status-cache-miss resolves share the same budget); the tunnel listener uses
// only the concurrent-connection cap (its rate is naturally bounded by token
// issuance). Sharing the type keeps the connection-cap logic in one place so the
// two listeners cannot diverge.
package ipcaps

import (
	"log/slog"
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
	logger      *slog.Logger
	mu          sync.Mutex
	conns       map[string]uint32
	joinWindows map[string]*rateWindow
	lastSweep   time.Time
	// ceilingLogged latches once AllowJoin has logged the current
	// maxJoinWindowEntries episode, so a sustained flood logs once rather than
	// once per denied attempt. Reset when joinWindows has room again.
	ceilingLogged bool
}

// joinWindowSweepInterval bounds how often AllowJoin opportunistically evicts
// expired rate windows, so the joinWindows map tracks only recently-active IPs
// rather than every IP that ever attempted a login (hostile churn would
// otherwise grow it without bound).
const joinWindowSweepInterval = time.Minute

// maxJoinWindowEntries bounds how many distinct source IPs joinWindows tracks
// at once, independent of sweepExpired's periodic reclaim. Without this, a
// source that can trivially spoof its address (e.g. the UDP source IPs the
// Bedrock tunnel's per-flow ipcaps instance checks via AllowJoin -- a TCP
// source cannot spoof past the handshake, but UDP can) could grow the map by
// one entry per distinct source IP for up to joinWindowSweepInterval before
// the next sweep reclaims space (issue #1566). The value mirrors
// DefaultGlobalMax's order of magnitude: this product runs one relay per
// self-hosted deployment (RELAY.md Section 1), so no legitimate install comes
// close to 10,000 distinct source IPs attempting a join within one sweep
// interval, while 10,000 *rateWindow entries is a trivial, bounded memory
// cost even if an attacker fills it.
const maxJoinWindowEntries = 10_000

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
// zero applies DefaultGlobalMax, negative disables the global cap. logger
// receives one warning each time AllowJoin starts denying brand-new source
// IPs because joinWindows is at maxJoinWindowEntries (issue #1566); nil
// disables this logging.
func NewIPCaps(maxConns, joinsPerSec uint32, globalMax int64, now func() time.Time, logger *slog.Logger) *IPCaps {
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
		logger:      logger,
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

// AllowJoin reports whether a join (login or status-resolve) from ip is within
// the per-second rate cap, counting this attempt. It uses a fixed one-second window per IP.
// A brand-new source IP is denied without being tracked once joinWindows
// holds maxJoinWindowEntries entries (issue #1566); an IP already tracked is
// unaffected by the ceiling and keeps renewing its own window as before.
func (c *IPCaps) AllowJoin(ip string) bool {
	if c.joinsPerSec == 0 {
		return true
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.now()
	c.sweepExpired(now)
	if len(c.joinWindows) < maxJoinWindowEntries {
		c.ceilingLogged = false
	}
	w := c.joinWindows[ip]
	if w == nil {
		if len(c.joinWindows) >= maxJoinWindowEntries {
			c.logCeilingReached()
			return false
		}
		c.joinWindows[ip] = &rateWindow{windowStart: now, count: 1}
		return true
	}
	if now.Sub(w.windowStart) >= time.Second {
		c.joinWindows[ip] = &rateWindow{windowStart: now, count: 1}
		return true
	}
	if w.count >= c.joinsPerSec {
		return false
	}
	w.count++
	return true
}

// logCeilingReached logs once per maxJoinWindowEntries episode that AllowJoin
// has started denying brand-new source IPs, rather than once per denied
// attempt -- under a spoofed-source flood that could be many times a second.
// Caller must hold c.mu.
func (c *IPCaps) logCeilingReached() {
	if c.logger == nil || c.ceilingLogged {
		return
	}
	c.ceilingLogged = true
	c.logger.Warn("ipcaps: joinWindows at capacity, denying new source IPs", "ceiling", maxJoinWindowEntries)
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
