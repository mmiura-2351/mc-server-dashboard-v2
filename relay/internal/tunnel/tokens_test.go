package tunnel

import (
	"net"
	"testing"
	"time"
)

func TestTokenTableSingleUse(t *testing.T) {
	now := time.Unix(0, 0)
	table := NewTokenTable(10*time.Second, func() time.Time { return now })

	ch := table.Register("tok")
	c1, c2 := net.Pipe()
	defer func() { _ = c1.Close() }()
	defer func() { _ = c2.Close() }()

	if !table.Deliver("tok", c1) {
		t.Fatal("first deliver should match the waiter")
	}
	select {
	case got := <-ch:
		if got != c1 {
			t.Error("waiter received the wrong conn")
		}
	default:
		t.Fatal("waiter did not receive the conn")
	}

	// The token is consumed: a second dial-back with the same token finds no
	// waiter.
	if table.Deliver("tok", c2) {
		t.Error("reused token should not match")
	}
}

func TestTokenTableUnknownToken(t *testing.T) {
	table := NewTokenTable(10*time.Second, nil)
	c, _ := net.Pipe()
	defer func() { _ = c.Close() }()
	if table.Deliver("never-registered", c) {
		t.Error("unknown token should not match")
	}
}

func TestTokenTableExpiry(t *testing.T) {
	now := time.Unix(0, 0)
	table := NewTokenTable(10*time.Second, func() time.Time { return now })

	table.Register("tok")
	now = now.Add(11 * time.Second)

	c, _ := net.Pipe()
	defer func() { _ = c.Close() }()
	if table.Deliver("tok", c) {
		t.Error("expired token should not match")
	}
}

func TestTokenTableCancel(t *testing.T) {
	table := NewTokenTable(10*time.Second, nil)
	table.Register("tok")
	if !table.Cancel("tok") {
		t.Error("Cancel of a live waiter should report removed=true")
	}
	c, _ := net.Pipe()
	defer func() { _ = c.Close() }()
	if table.Deliver("tok", c) {
		t.Error("cancelled waiter should not match")
	}
}

// TestTokenTableDeliverOnExpiredKeepsWaiter is the regression for the
// expired-Deliver leak: a dial-back that lands after the token's TTL but while
// the waiter is still registered must (a) return false from Deliver and (b)
// NOT delete the entry, so the waiter's later Cancel still returns true. If
// Deliver deleted the entry, Cancel would return false — falsely signalling "a
// conn is en route on the channel" — and the waiter would block forever.
func TestTokenTableDeliverOnExpiredKeepsWaiter(t *testing.T) {
	now := time.Unix(0, 0)
	table := NewTokenTable(10*time.Second, func() time.Time { return now })

	ch := table.Register("tok")
	now = now.Add(11 * time.Second) // TTL elapsed, waiter still registered.

	c, _ := net.Pipe()
	defer func() { _ = c.Close() }()
	if table.Deliver("tok", c) {
		t.Fatal("expired token should not match")
	}
	// Nothing must have been sent on the channel (the waiter is not unblocked).
	select {
	case <-ch:
		t.Fatal("expired Deliver must not send on the channel")
	default:
	}
	// The entry survives, so the waiter's Cancel reclaims it (removed=true).
	if !table.Cancel("tok") {
		t.Error("Cancel after an expired Deliver should report removed=true")
	}
}

// TestTokenTableCancelAfterDeliver verifies the leak-guard contract: once
// Deliver has consumed a token, Cancel reports removed=false so the waiter
// knows a connection is en route and must be drained/closed.
func TestTokenTableCancelAfterDeliver(t *testing.T) {
	table := NewTokenTable(10*time.Second, nil)
	ch := table.Register("tok")
	c, _ := net.Pipe()
	defer func() { _ = c.Close() }()

	if !table.Deliver("tok", c) {
		t.Fatal("deliver should match")
	}
	if table.Cancel("tok") {
		t.Error("Cancel after Deliver should report removed=false")
	}
	// The delivered conn is waiting on the channel for the waiter to drain.
	if got := <-ch; got != c {
		t.Error("delivered conn not on the channel")
	}
}

// TestTokenTableSweepExpired verifies that sweepExpired removes waiters whose
// TTL has elapsed, preventing indefinite map growth when Cancel is never called.
func TestTokenTableSweepExpired(t *testing.T) {
	now := time.Unix(0, 0)
	table := NewTokenTable(10*time.Second, func() time.Time { return now })

	table.Register("alive")
	table.Register("expired")

	// Advance time past the TTL for "expired" but register "alive" fresh.
	now = now.Add(11 * time.Second)
	table.Register("alive") // re-register with a fresh expiry

	table.sweepExpired()

	table.mu.Lock()
	_, alivePresent := table.waiters["alive"]
	_, expiredPresent := table.waiters["expired"]
	table.mu.Unlock()

	if !alivePresent {
		t.Error("alive entry should survive the sweep")
	}
	if expiredPresent {
		t.Error("expired entry should be removed by the sweep")
	}
}

// TestTokenTableSweepLeavesDelivered verifies that sweep does not interfere
// with entries already consumed by Deliver (they are already deleted).
func TestTokenTableSweepDeliveredAlreadyGone(t *testing.T) {
	now := time.Unix(0, 0)
	table := NewTokenTable(10*time.Second, func() time.Time { return now })

	ch := table.Register("tok")
	c, _ := net.Pipe()
	defer func() { _ = c.Close() }()

	if !table.Deliver("tok", c) {
		t.Fatal("deliver should match")
	}
	<-ch // drain

	table.sweepExpired()

	table.mu.Lock()
	n := len(table.waiters)
	table.mu.Unlock()
	if n != 0 {
		t.Errorf("map should be empty after deliver+sweep, got %d entries", n)
	}
}
