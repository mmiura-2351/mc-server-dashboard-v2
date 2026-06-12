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
