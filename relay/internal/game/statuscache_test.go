package game

import (
	"testing"
	"time"
)

func TestStatusCacheTTL(t *testing.T) {
	now := time.Unix(0, 0)
	cache := NewStatusCache(5*time.Second, 1024, func() time.Time { return now })

	if _, ok := cache.Get("amber"); ok {
		t.Fatal("empty cache should miss")
	}

	cache.Put("amber", `{"motd":"hi"}`)
	if v, ok := cache.Get("amber"); !ok || v != `{"motd":"hi"}` {
		t.Fatalf("fresh entry: got (%q, %v)", v, ok)
	}

	// Within TTL.
	now = now.Add(4 * time.Second)
	if _, ok := cache.Get("amber"); !ok {
		t.Error("entry should still be valid at 4s")
	}

	// Exactly at TTL is expired (not Before).
	now = now.Add(1 * time.Second)
	if _, ok := cache.Get("amber"); ok {
		t.Error("entry should be expired at 5s")
	}
}

func TestStatusCachePerSlug(t *testing.T) {
	cache := NewStatusCache(time.Minute, 1024, nil)
	cache.Put("a", "A")
	cache.Put("b", "B")
	if v, _ := cache.Get("a"); v != "A" {
		t.Errorf("slug a = %q", v)
	}
	if v, _ := cache.Get("b"); v != "B" {
		t.Errorf("slug b = %q", v)
	}
}

// TestStatusCacheGetDeletesExpired verifies that Get lazily deletes expired
// entries from the map (delete-on-miss), not just returning a miss.
func TestStatusCacheGetDeletesExpired(t *testing.T) {
	now := time.Unix(0, 0)
	cache := NewStatusCache(5*time.Second, 1024, func() time.Time { return now })

	cache.Put("slug", `{"motd":"hi"}`)
	now = now.Add(6 * time.Second) // past TTL

	if _, ok := cache.Get("slug"); ok {
		t.Fatal("expired entry should miss")
	}

	cache.mu.Lock()
	n := len(cache.entries)
	cache.mu.Unlock()
	if n != 0 {
		t.Errorf("expired entry should be deleted from the map, got %d entries", n)
	}
}

// TestStatusCacheSweepExpired verifies that sweepExpired removes all entries
// past their TTL, including entries never re-read by Get.
func TestStatusCacheSweepExpired(t *testing.T) {
	now := time.Unix(0, 0)
	cache := NewStatusCache(5*time.Second, 1024, func() time.Time { return now })

	cache.Put("alive", "A")
	cache.Put("expired", "B")

	now = now.Add(6 * time.Second)
	cache.Put("alive", "A2") // refresh "alive" with a new expiry

	cache.sweepExpired()

	cache.mu.Lock()
	_, alivePresent := cache.entries["alive"]
	_, expiredPresent := cache.entries["expired"]
	cache.mu.Unlock()

	if !alivePresent {
		t.Error("alive entry should survive the sweep")
	}
	if expiredPresent {
		t.Error("expired entry should be removed by the sweep")
	}
}

// TestStatusCacheEvictsOldestOnOverflow verifies that Put evicts the oldest
// entry (by expiry) when the cache exceeds maxEntries.
func TestStatusCacheEvictsOldestOnOverflow(t *testing.T) {
	now := time.Unix(0, 0)
	cache := NewStatusCache(10*time.Second, 2, func() time.Time { return now })

	// Insert two entries at different times so their expiry differs.
	cache.Put("first", "F")
	now = now.Add(1 * time.Second)
	cache.Put("second", "S")

	// A third insert should evict "first" (oldest expiry).
	now = now.Add(1 * time.Second)
	cache.Put("third", "T")

	cache.mu.Lock()
	n := len(cache.entries)
	_, firstPresent := cache.entries["first"]
	_, secondPresent := cache.entries["second"]
	_, thirdPresent := cache.entries["third"]
	cache.mu.Unlock()

	if n != 2 {
		t.Errorf("cache should have 2 entries, got %d", n)
	}
	if firstPresent {
		t.Error("first (oldest) should have been evicted")
	}
	if !secondPresent {
		t.Error("second should still be present")
	}
	if !thirdPresent {
		t.Error("third should still be present")
	}
}

// TestStatusCacheUpdateExistingDoesNotEvict verifies that updating an existing
// key does not trigger eviction (the entry count does not change).
func TestStatusCacheUpdateExistingDoesNotEvict(t *testing.T) {
	now := time.Unix(0, 0)
	cache := NewStatusCache(10*time.Second, 2, func() time.Time { return now })

	cache.Put("a", "A1")
	now = now.Add(1 * time.Second)
	cache.Put("b", "B1")

	// Update "a" — should NOT evict anything.
	now = now.Add(1 * time.Second)
	cache.Put("a", "A2")

	cache.mu.Lock()
	n := len(cache.entries)
	cache.mu.Unlock()

	if n != 2 {
		t.Errorf("cache should have 2 entries after update, got %d", n)
	}
	if v, ok := cache.Get("a"); !ok || v != "A2" {
		t.Errorf("updated entry a = (%q, %v)", v, ok)
	}
	if v, ok := cache.Get("b"); !ok || v != "B1" {
		t.Errorf("entry b = (%q, %v)", v, ok)
	}
}

// TestStatusCacheMaxEntriesDefaultsPositive verifies that a zero or negative
// maxEntries is clamped to a safe default.
func TestStatusCacheMaxEntriesDefaultsPositive(t *testing.T) {
	cache := NewStatusCache(time.Second, 0, nil)
	if cache.maxEntries <= 0 {
		t.Errorf("maxEntries should be positive, got %d", cache.maxEntries)
	}
}
