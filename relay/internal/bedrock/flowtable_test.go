package bedrock

import (
	"net"
	"testing"
	"time"
)

func udpAddr(t *testing.T, s string) *net.UDPAddr {
	t.Helper()
	addr, err := net.ResolveUDPAddr("udp", s)
	if err != nil {
		t.Fatalf("ResolveUDPAddr(%q): %v", s, err)
	}
	return addr
}

func TestFlowTableCreateThenLookup(t *testing.T) {
	ft := NewFlowTable(time.Minute, nil)
	a := udpAddr(t, "203.0.113.1:12345")

	if _, ok, _ := ft.Lookup(a, true); ok {
		t.Fatal("Lookup should miss before Create")
	}
	id := ft.Create(a, false)

	got, ok, _ := ft.Lookup(a, true)
	if !ok {
		t.Fatal("Lookup should hit after Create")
	}
	if got != id {
		t.Errorf("Lookup id = %d, want %d", got, id)
	}
}

func TestFlowTableMultipleClientsGetDistinctIDs(t *testing.T) {
	ft := NewFlowTable(time.Minute, nil)
	a1 := udpAddr(t, "203.0.113.1:1")
	a2 := udpAddr(t, "203.0.113.2:2")

	id1 := ft.Create(a1, false)
	id2 := ft.Create(a2, false)
	if id1 == id2 {
		t.Errorf("distinct clients got the same flow id %d", id1)
	}
	if ft.Len() != 2 {
		t.Errorf("Len() = %d, want 2", ft.Len())
	}
}

func TestFlowTableAddrByID(t *testing.T) {
	ft := NewFlowTable(time.Minute, nil)
	a := udpAddr(t, "203.0.113.1:12345")
	id := ft.Create(a, false)

	got, ok := ft.AddrByID(id)
	if !ok {
		t.Fatal("AddrByID should hit for a known id")
	}
	if got.String() != a.String() {
		t.Errorf("AddrByID = %v, want %v", got, a)
	}
}

func TestFlowTableAddrByIDUnknown(t *testing.T) {
	ft := NewFlowTable(time.Minute, nil)
	if _, ok := ft.AddrByID(999); ok {
		t.Error("AddrByID should miss for an unknown id")
	}
}

func TestFlowTableEvictIdle(t *testing.T) {
	now := time.Now()
	clock := func() time.Time { return now }
	ft := NewFlowTable(time.Minute, clock)

	a := udpAddr(t, "203.0.113.1:12345")
	ft.Create(a, false)

	// Not idle yet: no eviction.
	if evicted, _ := ft.Evict(); len(evicted) != 0 {
		t.Fatalf("evicted %d entries before idleTTL elapsed", len(evicted))
	}

	now = now.Add(2 * time.Minute)
	evicted, _ := ft.Evict()
	if len(evicted) != 1 {
		t.Fatalf("evicted = %d, want 1", len(evicted))
	}
	if evicted[0].String() != a.String() {
		t.Errorf("evicted addr = %v, want %v", evicted[0], a)
	}
	if ft.Len() != 0 {
		t.Errorf("Len() after eviction = %d, want 0", ft.Len())
	}
	if _, ok, _ := ft.Lookup(a, true); ok {
		t.Error("Lookup should miss after eviction")
	}
}

func TestFlowTableActivityResetsIdleClock(t *testing.T) {
	now := time.Now()
	clock := func() time.Time { return now }
	ft := NewFlowTable(time.Minute, clock)

	a := udpAddr(t, "203.0.113.1:12345")
	id := ft.Create(a, false)

	// Halfway through the idle window, a lookup (fresh datagram) refreshes it.
	now = now.Add(30 * time.Second)
	if _, ok, _ := ft.Lookup(a, true); !ok {
		t.Fatal("Lookup should hit")
	}

	// Another 45s (75s total since Create, but only 45s since the refresh):
	// still alive.
	now = now.Add(45 * time.Second)
	if evicted, _ := ft.Evict(); len(evicted) != 0 {
		t.Fatalf("evicted %d entries; activity should have reset the idle clock", len(evicted))
	}

	// AddrByID also counts as activity.
	if _, ok := ft.AddrByID(id); !ok {
		t.Fatal("AddrByID should hit")
	}
	now = now.Add(45 * time.Second)
	if evicted, _ := ft.Evict(); len(evicted) != 0 {
		t.Fatalf("evicted %d entries; AddrByID should have reset the idle clock", len(evicted))
	}

	// Finally let it go fully idle.
	now = now.Add(time.Minute)
	if evicted, _ := ft.Evict(); len(evicted) != 1 {
		t.Fatalf("evicted = %d, want 1 once truly idle", len(evicted))
	}
}

func TestFlowTableDefaultClock(t *testing.T) {
	ft := NewFlowTable(time.Minute, nil)
	a := udpAddr(t, "203.0.113.1:12345")
	ft.Create(a, false)
	if _, ok, _ := ft.Lookup(a, true); !ok {
		t.Fatal("Lookup should hit with the default (time.Now) clock")
	}
}

func TestFlowTableLookupPromotesAtThreshold(t *testing.T) {
	ft := NewFlowTable(time.Minute, nil)
	a := udpAddr(t, "203.0.113.1:12345")
	ft.Create(a, false) // the offline creating datagram does not advance promotion

	// Offline datagrams (counts=false) never advance promotion, no matter how
	// many arrive.
	for i := 0; i < flowPromoteThreshold*2; i++ {
		if _, ok, promote := ft.Lookup(a, false); !ok || promote {
			t.Fatalf("offline lookup %d: ok=%v promote=%v, want ok=true promote=false", i, ok, promote)
		}
	}

	// Connected datagrams up to (but not reaching) the threshold do not promote.
	for i := 1; i < flowPromoteThreshold; i++ {
		if _, ok, promote := ft.Lookup(a, true); !ok || promote {
			t.Fatalf("connected lookup %d: ok=%v promote=%v, want ok=true promote=false", i, ok, promote)
		}
	}
	// The connected datagram that carries the flow to the threshold promotes.
	if _, ok, promote := ft.Lookup(a, true); !ok || !promote {
		t.Fatalf("connected lookup at threshold: ok=%v promote=%v, want ok=true promote=true", ok, promote)
	}
	// Promotion fires exactly once: past the threshold it never repeats.
	if _, _, promote := ft.Lookup(a, true); promote {
		t.Error("connected lookup past the threshold promoted again; want promote exactly once")
	}
}

func TestFlowTableIngressSaturatesAtThreshold(t *testing.T) {
	ft := NewFlowTable(time.Minute, nil)
	a := udpAddr(t, "203.0.113.1:12345")
	ft.Create(a, false)

	// Drive the flow to exactly the promotion threshold.
	for i := 1; i <= flowPromoteThreshold; i++ {
		_, ok, promote := ft.Lookup(a, true)
		if !ok {
			t.Fatalf("Lookup %d: want ok=true", i)
		}
		if i < flowPromoteThreshold && promote {
			t.Fatalf("Lookup %d: premature promote", i)
		}
		if i == flowPromoteThreshold && !promote {
			t.Fatalf("Lookup %d: expected promote=true at threshold", i)
		}
	}

	// Many more connected lookups past the threshold: promote must never fire
	// again, even if the counter were to wrap a uint32. We simulate by
	// hammering well beyond the threshold.
	for i := 0; i < flowPromoteThreshold*3; i++ {
		if _, _, promote := ft.Lookup(a, true); promote {
			t.Fatalf("promote fired again at lookup %d past threshold", i)
		}
	}

	// The counter itself must have been clamped at the threshold — on unfixed
	// code it would be flowPromoteThreshold + flowPromoteThreshold*3 (i.e. 20),
	// not 5.
	ft.mu.Lock()
	got := ft.byAddr[a.String()].ingress
	ft.mu.Unlock()
	if got != flowPromoteThreshold {
		t.Errorf("ingress = %d after saturation, want %d (clamped at threshold)", got, flowPromoteThreshold)
	}
}

func TestFlowTableEvictReturnsPromotedSessions(t *testing.T) {
	now := time.Now()
	clock := func() time.Time { return now }
	ft := NewFlowTable(time.Minute, clock)

	promoted := udpAddr(t, "203.0.113.1:1")
	ft.Promote(ft.Create(promoted, false), "sess-A")

	plain := udpAddr(t, "203.0.113.2:2")
	ft.Create(plain, false) // never promoted

	now = now.Add(2 * time.Minute)
	addrs, ended := ft.Evict()
	if len(addrs) != 2 {
		t.Fatalf("evicted %d addrs, want 2 (both idle flows)", len(addrs))
	}
	if len(ended) != 1 || ended[0] != "sess-A" {
		t.Errorf("ended sessions = %v, want [sess-A] (only the promoted flow)", ended)
	}
}

func TestFlowTableDrain(t *testing.T) {
	ft := NewFlowTable(time.Minute, nil)
	ft.Promote(ft.Create(udpAddr(t, "203.0.113.1:1"), false), "sess-1")
	ft.Promote(ft.Create(udpAddr(t, "203.0.113.2:2"), false), "sess-2")
	ft.Create(udpAddr(t, "203.0.113.3:3"), false) // not promoted

	ids, removed := ft.Drain()
	got := make(map[string]bool, len(ids))
	for _, id := range ids {
		got[id] = true
	}
	if len(ids) != 2 || !got["sess-1"] || !got["sess-2"] {
		t.Errorf("Drain ended sessions = %v, want {sess-1, sess-2}", ids)
	}
	// Drain removes every flow (promoted or not), so the removed count is all 3.
	if removed != 3 {
		t.Errorf("Drain removed = %d, want 3 (all flows)", removed)
	}
	// The table is emptied, so a second drain (or a later Evict) reports nothing.
	if ft.Len() != 0 {
		t.Errorf("Len() after Drain = %d, want 0", ft.Len())
	}
	if again, removedAgain := ft.Drain(); len(again) != 0 || removedAgain != 0 {
		t.Errorf("second Drain = (%v, %d), want empty", again, removedAgain)
	}
}
