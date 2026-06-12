package game

import (
	"net"
	"testing"
	"time"
)

func TestIPCapsMaxConns(t *testing.T) {
	caps := NewIPCaps(2, 0, nil)
	first := caps.Acquire("1.1.1.1")
	second := caps.Acquire("1.1.1.1")
	if !first || !second {
		t.Fatal("first two acquires should succeed")
	}
	if caps.Acquire("1.1.1.1") {
		t.Error("third acquire should be capped")
	}
	// A different IP is unaffected.
	if !caps.Acquire("2.2.2.2") {
		t.Error("other IP should not be capped")
	}
	// Releasing frees a slot.
	caps.Release("1.1.1.1")
	if !caps.Acquire("1.1.1.1") {
		t.Error("acquire after release should succeed")
	}
}

func TestIPCapsJoinRate(t *testing.T) {
	now := time.Unix(100, 0)
	caps := NewIPCaps(0, 3, func() time.Time { return now })

	for i := 0; i < 3; i++ {
		if !caps.AllowJoin("1.1.1.1") {
			t.Fatalf("join %d should be allowed", i)
		}
	}
	if caps.AllowJoin("1.1.1.1") {
		t.Error("4th join in the window should be denied")
	}

	// New one-second window resets the count.
	now = now.Add(time.Second)
	if !caps.AllowJoin("1.1.1.1") {
		t.Error("join in a new window should be allowed")
	}
}

// TestIPCapsJoinWindowsBounded asserts the joinWindows map does not grow
// without bound under hostile churn: each unique source IP joins once and never
// returns, but the opportunistic sweep evicts windows whose one-second window
// has elapsed, so the map collapses back to (about) the current second's
// active IPs rather than retaining every IP forever.
func TestIPCapsJoinWindowsBounded(t *testing.T) {
	now := time.Unix(0, 0)
	caps := NewIPCaps(0, 10, func() time.Time { return now })

	// 1000 unique IPs each join once at t=0.
	for i := 0; i < 1000; i++ {
		caps.AllowJoin(uniqueIP(i))
	}
	if got := len(caps.joinWindows); got != 1000 {
		t.Fatalf("after first burst, joinWindows = %d, want 1000", got)
	}

	// Advance well past the window and the sweep interval, then a single fresh
	// join triggers the sweep, evicting all the now-stale windows.
	now = now.Add(2 * joinWindowSweepInterval)
	caps.AllowJoin("fresh")
	if got := len(caps.joinWindows); got > 1 {
		t.Errorf("after sweep, joinWindows = %d, want <= 1 (stale windows evicted)", got)
	}
}

func uniqueIP(i int) string {
	return net.IPv4(10, byte(i/65536%256), byte(i/256%256), byte(i%256)).String()
}

func TestIPCapsZeroDisables(t *testing.T) {
	caps := NewIPCaps(0, 0, nil)
	for i := 0; i < 100; i++ {
		if !caps.Acquire("x") || !caps.AllowJoin("x") {
			t.Fatal("zero caps should never block")
		}
	}
}
