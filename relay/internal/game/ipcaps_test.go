package game

import (
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

func TestIPCapsZeroDisables(t *testing.T) {
	caps := NewIPCaps(0, 0, nil)
	for i := 0; i < 100; i++ {
		if !caps.Acquire("x") || !caps.AllowJoin("x") {
			t.Fatal("zero caps should never block")
		}
	}
}
