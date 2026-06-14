package game

import (
	"context"
	"sync"
	"time"
)

// cacheSweepInterval is how often the background sweep evicts expired entries.
const cacheSweepInterval = time.Minute

// StatusCache is a per-slug cache of Status Response JSON with a fixed TTL
// (RELAY.md Section 7). Clients ping every saved server on each multiplayer
// refresh; the cache absorbs that fan-out so a status ping rarely reaches a
// Worker. It is safe for concurrent use.
type StatusCache struct {
	ttl        time.Duration
	maxEntries int
	now        func() time.Time

	mu      sync.Mutex
	entries map[string]statusEntry
}

type statusEntry struct {
	json    string
	expires time.Time
}

// NewStatusCache builds a cache with the given TTL and a maximum entry count.
// When Put would exceed maxEntries, the oldest entry (by expiry) is evicted.
// now is injectable for tests; pass time.Now in production.
func NewStatusCache(ttl time.Duration, maxEntries int, now func() time.Time) *StatusCache {
	if now == nil {
		now = time.Now
	}
	if maxEntries <= 0 {
		maxEntries = 1024
	}
	return &StatusCache{
		ttl:        ttl,
		maxEntries: maxEntries,
		now:        now,
		entries:    make(map[string]statusEntry),
	}
}

// Get returns the cached status JSON for slug if present and not expired.
// Expired entries are deleted on access (lazy eviction).
func (c *StatusCache) Get(slug string) (string, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.entries[slug]
	if !ok {
		return "", false
	}
	if !c.now().Before(e.expires) {
		delete(c.entries, slug)
		return "", false
	}
	return e.json, true
}

// Put stores status JSON for slug, expiring after the cache TTL. If inserting
// a new key would exceed maxEntries, the oldest entry (by expiry) is evicted.
func (c *StatusCache) Put(slug, statusJSON string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	_, exists := c.entries[slug]
	c.entries[slug] = statusEntry{json: statusJSON, expires: c.now().Add(c.ttl)}
	if !exists && len(c.entries) > c.maxEntries {
		c.evictOldest(slug)
	}
}

// evictOldest removes the entry with the earliest expiry, skipping skip.
// Caller must hold c.mu.
func (c *StatusCache) evictOldest(skip string) {
	var oldestSlug string
	var oldestExp time.Time
	first := true
	for s, e := range c.entries {
		if s == skip {
			continue
		}
		if first || e.expires.Before(oldestExp) {
			oldestSlug = s
			oldestExp = e.expires
			first = false
		}
	}
	if !first {
		delete(c.entries, oldestSlug)
	}
}

// StartSweep runs a background goroutine that periodically removes expired
// entries. This complements the lazy delete-on-miss in Get: entries that are
// never re-read are still evicted. The goroutine exits when ctx is cancelled.
func (c *StatusCache) StartSweep(ctx context.Context) {
	go c.sweepLoop(ctx)
}

func (c *StatusCache) sweepLoop(ctx context.Context) {
	ticker := time.NewTicker(cacheSweepInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			c.sweepExpired()
		}
	}
}

// sweepExpired removes all entries whose expiry has passed.
func (c *StatusCache) sweepExpired() {
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	for slug, e := range c.entries {
		if !now.Before(e.expires) {
			delete(c.entries, slug)
		}
	}
}
