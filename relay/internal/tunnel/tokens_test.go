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
