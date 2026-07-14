package bedrock

import (
	"context"
	"fmt"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
)

// testServerID is the server id threaded through bind in tests that do not
// assert on it.
const testServerID = "srv-test"

// noopRecorder is a SessionRecorder that records nothing, for tests whose flows
// never promote (or that do not care about session reporting).
type noopRecorder struct{}

func (noopRecorder) Start(_, _, _, _, _ string) string { return "" }
func (noopRecorder) End(_ string)                      {}

// startCall captures one SessionRecorder.Start invocation and the id it minted.
type startCall struct {
	serverID   string
	slug       string
	playerIP   string
	username   string
	playerUUID string
	id         string
}

// fakeRecorder is a SessionRecorder test double that captures every Start / End
// call. Safe for concurrent use: the tunnel's reader and sweep goroutines call
// it while the test goroutine reads via snapshot.
type fakeRecorder struct {
	mu      sync.Mutex
	starts  []startCall
	ends    []string
	counter int
}

func (f *fakeRecorder) Start(serverID, slug, playerIP, username, playerUUID string) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.counter++
	id := fmt.Sprintf("sess-%d", f.counter)
	f.starts = append(f.starts, startCall{serverID, slug, playerIP, username, playerUUID, id})
	return id
}

func (f *fakeRecorder) End(id string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ends = append(f.ends, id)
}

func (f *fakeRecorder) snapshot() (starts []startCall, ends []string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]startCall(nil), f.starts...), append([]string(nil), f.ends...)
}

// sendGameplay writes one connected-RakNet-shaped datagram (first byte 0x84, so
// the per-flow unconnected-ping cap never interferes) from src to dst and waits
// for it to be forwarded as a QUIC DATAGRAM on conn. Receiving the forwarded
// frame proves the reader processed the datagram (counting it for promotion),
// keeping the test deterministic without draining timers.
func sendGameplay(t *testing.T, src net.PacketConn, dst *net.UDPAddr, conn quicReceiver) {
	t.Helper()
	if _, err := src.WriteTo([]byte{0x84, 0x00}, dst); err != nil {
		t.Fatalf("WriteTo: %v", err)
	}
	rctx, rcancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer rcancel()
	if _, err := conn.ReceiveDatagram(rctx); err != nil {
		t.Fatalf("gameplay datagram was not forwarded: %v", err)
	}
}

// TestBedrockFlowPromotesToSessionAndEndsOnEviction covers issue #1904's core
// contract: a flow that sends flowPromoteThreshold client->worker datagrams is
// reported to the session recorder exactly once, with the client's true UDP
// source IP and empty (relay-invisible) identity, and its idle eviction Ends
// that same session.
func TestBedrockFlowPromotesToSessionAndEndsOnEviction(t *testing.T) {
	server, client := quicConnPair(t)
	// No ingress caps so the flow is admitted freely; only promotion is under
	// test.
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	rec := &fakeRecorder{}

	tun, err := bind(0, "srv-1", server, caps, rec, testLogger())
	if err != nil {
		t.Fatalf("bind: %v", err)
	}
	// Swap in a flow table with an injected clock so eviction is deterministic
	// (the production 15 s sweep never fires within the test). Swapped before
	// run() starts any goroutine, matching TestFlowEvictionRacesPumps.
	var clkMu sync.Mutex
	nowT := time.Now()
	tun.flows = NewFlowTable(flowIdleTimeout, func() time.Time {
		clkMu.Lock()
		defer clkMu.Unlock()
		return nowT
	})

	bound, ok := tun.Addr().(*net.UDPAddr)
	if !ok {
		t.Fatalf("Addr() = %T, want *net.UDPAddr", tun.Addr())
	}
	dialAddr := &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: bound.Port}

	ctx, cancel := context.WithCancel(context.Background())
	runDone := make(chan struct{})
	go func() {
		tun.run(ctx)
		close(runDone)
	}()

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	// Drive exactly flowPromoteThreshold datagrams from one source; the flow
	// promotes on the last one.
	for i := 0; i < flowPromoteThreshold; i++ {
		sendGameplay(t, fakeClient, dialAddr, client)
	}

	starts := waitForStarts(t, rec, 1)
	if len(starts) != 1 {
		t.Fatalf("Start called %d times, want exactly 1", len(starts))
	}
	s := starts[0]
	if s.serverID != "srv-1" || s.slug != "" || s.username != "" || s.playerUUID != "" {
		t.Errorf("Start(%+v), want serverID=srv-1 with empty slug/username/uuid", s)
	}
	if s.playerIP != "127.0.0.1" {
		t.Errorf("Start playerIP = %q, want the true client source 127.0.0.1", s.playerIP)
	}

	// Idle the flow past the timeout and run one sweep: eviction Ends the session
	// with the same id.
	clkMu.Lock()
	nowT = nowT.Add(flowIdleTimeout + time.Second)
	clkMu.Unlock()
	tun.sweepOnce()

	_, ends := rec.snapshot()
	if len(ends) != 1 || ends[0] != s.id {
		t.Errorf("End calls = %v, want exactly [%s]", ends, s.id)
	}

	cancel()
	select {
	case <-runDone:
	case <-time.After(5 * time.Second):
		t.Fatal("tun.run did not return after ctx cancel")
	}
}

// TestBedrockShortFlowDoesNotPromote covers issue #1904's noise gate: a flow
// that stays below flowPromoteThreshold (a server-list ping / scan) reports
// nothing -- no Start on ingress, no End on teardown.
func TestBedrockShortFlowDoesNotPromote(t *testing.T) {
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	rec := &fakeRecorder{}
	dialAddr, stop := runTunnelRec(t, server, caps, "srv-1", rec)

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	// One below the threshold: never promotes.
	for i := 0; i < flowPromoteThreshold-1; i++ {
		sendGameplay(t, fakeClient, dialAddr, client)
	}

	if starts, ends := rec.snapshot(); len(starts) != 0 || len(ends) != 0 {
		t.Fatalf("a sub-threshold flow reported starts=%d ends=%d, want 0/0", len(starts), len(ends))
	}

	// Teardown must not conjure a session either.
	stop()
	if starts, ends := rec.snapshot(); len(starts) != 0 || len(ends) != 0 {
		t.Errorf("after teardown starts=%d ends=%d, want 0/0", len(starts), len(ends))
	}
}

// TestBedrockTeardownEndsOpenSessions covers issue #1904's strand guard: a
// promoted flow still open when the tunnel tears down (Worker disconnect,
// takeover, or relay shutdown) is Ended so its session does not strand.
func TestBedrockTeardownEndsOpenSessions(t *testing.T) {
	server, client := quicConnPair(t)
	caps := ipcaps.NewIPCaps(0, 0, 0, nil, nil)
	rec := &fakeRecorder{}
	// Real clock / 60 s idle TTL: nothing evicts within the test, so the only
	// End can come from teardown.
	dialAddr, stop := runTunnelRec(t, server, caps, "srv-1", rec)

	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()

	for i := 0; i < flowPromoteThreshold; i++ {
		sendGameplay(t, fakeClient, dialAddr, client)
	}
	starts := waitForStarts(t, rec, 1)
	if len(starts) != 1 {
		t.Fatalf("Start called %d times, want exactly 1", len(starts))
	}

	// Teardown (no idle eviction) Ends the still-open session.
	stop()

	_, ends := rec.snapshot()
	if len(ends) != 1 || ends[0] != starts[0].id {
		t.Errorf("End calls after teardown = %v, want exactly [%s]", ends, starts[0].id)
	}
}

// quicReceiver is the subset of *quic.Conn sendGameplay needs; declared so the
// helper reads naturally without importing quic-go directly here.
type quicReceiver interface {
	ReceiveDatagram(ctx context.Context) ([]byte, error)
}

// waitForStarts polls the recorder until it has at least want Start calls or a
// short deadline elapses, then returns the captured starts.
func waitForStarts(t *testing.T, rec *fakeRecorder, want int) []startCall {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		starts, _ := rec.snapshot()
		if len(starts) >= want || time.Now().After(deadline) {
			return starts
		}
		time.Sleep(10 * time.Millisecond)
	}
}
