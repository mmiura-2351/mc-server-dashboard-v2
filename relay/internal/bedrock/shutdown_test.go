package bedrock

import (
	"context"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
	bedrocktunnelv1 "github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/genproto/mcsd/bedrocktunnel/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/session"
)

// recordingReportClient captures the SessionEnd events the reporter flushes, so
// a test can assert an ended_at was reported through the normal flush path
// rather than left to orphan-healing. Safe for concurrent use: the reporter
// flushes from its own Run goroutine while the test goroutine reads.
type recordingReportClient struct {
	mu   sync.Mutex
	ends []apiclient.SessionEnd
}

func (c *recordingReportClient) ReportSessions(_ context.Context, _ []apiclient.SessionStart, ends []apiclient.SessionEnd) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.ends = append(c.ends, ends...)
	return nil
}

func (c *recordingReportClient) endedIDs() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	ids := make([]string, len(c.ends))
	for i, e := range c.ends {
		ids[i] = e.SessionID
	}
	return ids
}

// gatedRecorder wraps a SessionRecorder and blocks inside End until released, so
// a test can hold a tunnel's teardown open across a Drain call and observe that
// Drain does not return until that End completes -- modelling a teardown slow
// enough that, without the drain barrier, the reporter could stop first and lose
// the event.
type gatedRecorder struct {
	inner      SessionRecorder
	endEntered chan string   // buffered(1): the id End was called with
	release    chan struct{} // closed to let a blocked End return
}

func (g *gatedRecorder) Start(serverID, slug, playerIP, username, playerUUID string, source apiclient.Source) string {
	return g.inner.Start(serverID, slug, playerIP, username, playerUUID, source)
}

func (g *gatedRecorder) End(id string) {
	g.endEntered <- id
	<-g.release
	g.inner.End(id)
}

// TestListenerDrainBlocksUntilBedrockSessionEndReachesReporter covers issue
// #1926: on a clean relay shutdown the Bedrock listener's Drain must block until
// every in-flight tunnel handler has torn down and delivered its promoted
// session's End to the reporter -- so main.go can flush ended_at through the
// normal path instead of relying on the reporter's orphan-healing fallback. The
// teardown End is held open with a gate so the barrier's blocking is observable:
// without it, Drain would return while the End is still in flight and the
// reporter (stopped right after Drain) could lose the event.
func TestListenerDrainBlocksUntilBedrockSessionEndReachesReporter(t *testing.T) {
	// Real reporter over a fake API client, with periodic flushing disabled so
	// the only flush is the one Run performs on shutdown -- the teardown End must
	// already be buffered by then for it to be reported.
	api := &recordingReportClient{}
	reporter := session.NewReporter(api, testLogger(), nil, nil).WithFlushInterval(time.Hour)
	reporterCtx, reporterStop := context.WithCancel(context.Background())
	reporterDone := make(chan struct{})
	go func() {
		reporter.Run(reporterCtx)
		close(reporterDone)
	}()

	// Wire the listener with a gated recorder in front of the real reporter.
	gate := &gatedRecorder{inner: reporter, endEntered: make(chan string, 1), release: make(chan struct{})}
	validator := &fakeValidator{valid: true}
	newCaps := func() *ipcaps.IPCaps { return ipcaps.NewIPCaps(0, 0, 0, nil, nil) }
	ln, err := NewListener("127.0.0.1:0", selfSignedTLS(t), validator, ipcaps.NewIPCaps(0, 0, 0, nil, nil), newCaps, gate, nil, testLogger())
	if err != nil {
		t.Fatalf("NewListener: %v", err)
	}
	serveCtx, serveCancel := context.WithCancel(context.Background())
	serveDone := make(chan struct{})
	go func() {
		_ = ln.Serve(serveCtx)
		close(serveDone)
	}()

	// A Worker dials out and binds a tunnel on a concrete port (BedrockPort 0
	// would be OS-assigned and unknowable to the client below).
	port := freeUDPPort(t)
	conn, ack := doHandshake(t, ln, &bedrocktunnelv1.TunnelHello{ServerId: "srv-1", BedrockPort: port, Token: "tok"})
	if !ack.GetAccepted() {
		t.Fatalf("handshake rejected: %q", ack.GetRejectReason())
	}
	udpAddr := &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: int(port)}

	// One client flow crosses flowPromoteThreshold connected datagrams, so the
	// reporter mints a live session for it.
	fakeClient, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer func() { _ = fakeClient.Close() }()
	for i := 0; i < flowPromoteThreshold; i++ {
		sendGameplay(t, fakeClient, udpAddr, conn)
	}
	openID := waitForOpenSession(t, reporter)

	// Begin the clean shutdown in main.go's order: stop accepting, wait for Serve
	// to return (so every in-flight handler was counted on the WaitGroup before
	// Drain waits), then Drain the listener in the background.
	serveCancel()
	<-serveDone
	drainReturned := make(chan bool, 1)
	go func() { drainReturned <- ln.Drain(5 * time.Second) }()

	// The tunnel's teardown reaches End for the still-open session ...
	select {
	case gotID := <-gate.endEntered:
		if gotID != openID {
			t.Fatalf("teardown called End(%q), want End(%q)", gotID, openID)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("tunnel teardown never called End on shutdown")
	}

	// ... and while that End is held, Drain must NOT have returned: the barrier
	// is still waiting on the in-flight handler.
	select {
	case <-drainReturned:
		t.Fatal("Drain returned before the in-flight tunnel finished delivering its session End")
	case <-time.After(100 * time.Millisecond):
	}

	// Release the teardown; End now reaches the reporter and the handler finishes,
	// so the barrier lifts.
	close(gate.release)
	select {
	case ok := <-drainReturned:
		if !ok {
			t.Fatal("Drain timed out; in-flight Bedrock tunnels did not finish")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Drain did not return after the teardown completed")
	}

	// Only now -- after Drain -- is the reporter stopped, exactly as main.go
	// sequences it. The End was buffered before the stop, so it flushes on
	// shutdown rather than being lost, and no session is left for orphan-healing.
	reporterStop()
	select {
	case <-reporterDone:
	case <-time.After(5 * time.Second):
		t.Fatal("reporter.Run did not return")
	}

	if ids := api.endedIDs(); len(ids) != 1 || ids[0] != openID {
		t.Fatalf("reporter flushed SessionEnd ids %v, want exactly [%s] (the promoted Bedrock session)", ids, openID)
	}
	if open := reporter.ActiveSessionIDs(); len(open) != 0 {
		t.Errorf("sessions still open after clean shutdown: %v -- ended_at would rely on orphan-healing", open)
	}
}

// waitForOpenSession polls the reporter until exactly one session is open and
// returns its id, or fails after a short deadline. The promotion Start is made
// on the reader goroutine, so a brief poll keeps the test deterministic without
// reaching into reporter internals.
func waitForOpenSession(t *testing.T, reporter *session.Reporter) string {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		if ids := reporter.ActiveSessionIDs(); len(ids) == 1 {
			return ids[0]
		}
		if time.Now().After(deadline) {
			t.Fatalf("expected exactly one open session before shutdown, have %v", reporter.ActiveSessionIDs())
		}
		time.Sleep(10 * time.Millisecond)
	}
}
