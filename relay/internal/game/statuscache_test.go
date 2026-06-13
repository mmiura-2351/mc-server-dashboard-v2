package game

import (
	"testing"
	"time"
)

func TestStatusCacheTTL(t *testing.T) {
	now := time.Unix(0, 0)
	cache := NewStatusCache(5*time.Second, func() time.Time { return now })

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
	cache := NewStatusCache(time.Minute, nil)
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
	cache := NewStatusCache(5*time.Second, func() time.Time { return now })

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
	cache := NewStatusCache(5*time.Second, func() time.Time { return now })

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
