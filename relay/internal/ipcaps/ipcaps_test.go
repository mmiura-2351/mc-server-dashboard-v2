package ipcaps

import (
	"net"
	"testing"
	"time"
)

func TestIPCapsMaxConns(t *testing.T) {
	caps := NewIPCaps(2, 0, -1, nil)
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

// TestIPCapsConnsBounded asserts the concurrent-connection map does not retain
// per-IP entries once their connections close: 1000 distinct IPs each acquire
// and release a slot, and the conns map collapses back to empty. This is the
// eviction the tunnel listener relies on so hostile per-IP churn cannot grow the
// map without bound.
func TestIPCapsConnsBounded(t *testing.T) {
	caps := NewIPCaps(4, 0, -1, nil)
	for i := 0; i < 1000; i++ {
		ip := uniqueIP(i)
		if !caps.Acquire(ip) {
			t.Fatalf("acquire %d should succeed", i)
		}
		caps.Release(ip)
	}
	if got := len(caps.conns); got != 0 {
		t.Errorf("after 1000 acquire/release pairs, conns = %d, want 0 (entries evicted)", got)
	}
}

func TestIPCapsJoinRate(t *testing.T) {
	now := time.Unix(100, 0)
	caps := NewIPCaps(0, 3, -1, func() time.Time { return now })

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
	caps := NewIPCaps(0, 10, -1, func() time.Time { return now })

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
	caps := NewIPCaps(0, 0, -1, nil)
	for i := 0; i < 100; i++ {
		if !caps.Acquire("x") || !caps.AllowJoin("x") {
			t.Fatal("zero caps should never block")
		}
	}
}

// TestGlobalCapRejectsAtCeiling asserts that once the global connection ceiling
// is reached, Acquire rejects new connections even from distinct IPs that are
// individually under their per-IP cap.
func TestGlobalCapRejectsAtCeiling(t *testing.T) {
	caps := NewIPCaps(10, 0, 3, nil) // per-IP 10, global 3
	if !caps.Acquire("1.1.1.1") || !caps.Acquire("2.2.2.2") || !caps.Acquire("3.3.3.3") {
		t.Fatal("first three acquires from distinct IPs should succeed")
	}
	// Global cap reached — a fourth distinct IP is rejected.
	if caps.Acquire("4.4.4.4") {
		t.Error("acquire beyond global cap should be rejected")
	}
	// Releasing one slot allows the next acquire.
	caps.Release("2.2.2.2")
	if !caps.Acquire("5.5.5.5") {
		t.Error("acquire after release should succeed")
	}
}

// TestGlobalCapDefaultApplied asserts that passing zero for globalMax applies
// DefaultGlobalMax rather than disabling the cap.
func TestGlobalCapDefaultApplied(t *testing.T) {
	caps := NewIPCaps(0, 0, 0, nil)
	if caps.globalMax != DefaultGlobalMax {
		t.Errorf("globalMax = %d, want %d", caps.globalMax, DefaultGlobalMax)
	}
}

// TestGlobalCapNegativeDisables asserts that a negative globalMax disables the
// global cap entirely, matching pre-existing behavior.
func TestGlobalCapNegativeDisables(t *testing.T) {
	caps := NewIPCaps(0, 0, -1, nil)
	// With global cap disabled, even many acquires succeed (limited only by
	// per-IP cap, which is also disabled here).
	for i := 0; i < 200; i++ {
		if !caps.Acquire(uniqueIP(i)) {
			t.Fatalf("acquire %d should succeed with global cap disabled", i)
		}
	}
}

// TestGlobalCapWithPerIPCap asserts that per-IP rejection releases the global
// slot so the global counter stays accurate.
func TestGlobalCapWithPerIPCap(t *testing.T) {
	caps := NewIPCaps(1, 0, 5, nil) // per-IP 1, global 5
	// First acquire from an IP succeeds.
	if !caps.Acquire("1.1.1.1") {
		t.Fatal("first acquire should succeed")
	}
	// Second from the same IP is rejected by per-IP cap.
	if caps.Acquire("1.1.1.1") {
		t.Error("per-IP cap should reject second acquire")
	}
	// The global counter should still be 1 (the per-IP rejection did not leak a
	// global slot).
	if got := caps.globalConns.Load(); got != 1 {
		t.Errorf("globalConns = %d after per-IP rejection, want 1", got)
	}
}
