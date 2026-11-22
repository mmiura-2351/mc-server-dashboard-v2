package controlplane_test

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"regexp"
	"slices"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/durationpb"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/controlplane"
	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// realClock is the production Clock; the integration test uses real time so the
// heartbeat interval drives genuine cadence over the wire.
type realClock struct{}

func (realClock) Now() time.Time                         { return time.Now() }
func (realClock) After(d time.Duration) <-chan time.Time { return time.After(d) }
func (realClock) NewTimer(d time.Duration) session.Timer { return realTimer{time.NewTimer(d)} }

// realTimer adapts *time.Timer to session.Timer for the integration test's clock.
type realTimer struct{ t *time.Timer }

func (t realTimer) C() <-chan time.Time   { return t.t.C }
func (t realTimer) Reset(d time.Duration) { t.t.Reset(d) }
func (t realTimer) Stop()                 { t.t.Stop() }

// fakeServer is an in-process WorkerService implementing the API side of the
// stream lifecycle (CONTROL_PLANE.md Section 4) just enough to exercise the
// client. It mirrors the real servicer (#83): a bad/missing credential aborts
// the stream with gRPC status UNAUTHENTICATED rather than a RegisterAck — a
// refusal is never carried in the ack. On success it answers Register with a
// RegisterAck, records heartbeats, and can drop the stream after the first
// heartbeat to drive a transient reconnect.
type fakeServer struct {
	controlplanev1.UnimplementedWorkerServiceServer

	wantCredential string
	heartbeatEvery time.Duration
	dropAfterFirst bool
	// refuseRegisters is how many Registers, from the first, the server refuses
	// with UNAVAILABLE, as the API does one its database did not answer in time
	// (issue #4050).
	refuseRegisters int

	mu          sync.Mutex
	registers   int
	heartbeats  int
	heldServers []*controlplanev1.HeldServer
	unheldIDs   []string
	resources   *controlplanev1.HostResources
	// messages records every WorkerMessage received on the stream, in order.
	messages []*controlplanev1.WorkerMessage
}

func (s *fakeServer) Session(stream controlplanev1.WorkerService_SessionServer) error {
	if !s.checkAuth(stream.Context()) {
		// Match #83: abort with a status code; a refusal is never carried in the ack.
		return status.Error(codes.Unauthenticated, "worker credential rejected")
	}

	// First message must be Register.
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	if first.GetRegister() == nil {
		return status.Error(codes.FailedPrecondition, "first message must be Register")
	}
	s.mu.Lock()
	s.registers++
	s.heldServers = first.GetRegister().GetHeldServers()
	s.unheldIDs = first.GetRegister().GetUnheldScratchServerIds()
	s.resources = first.GetRegister().GetCapabilities().GetResources()
	s.messages = append(s.messages, first)
	refuse := s.registers <= s.refuseRegisters
	s.mu.Unlock()

	if refuse {
		return status.Error(codes.Unavailable, "the database did not answer the registration in time")
	}
	if err := stream.Send(acceptAck(s.heartbeatEvery)); err != nil {
		return err
	}

	// Read worker->API messages (heartbeats, command results) until the stream
	// ends, optionally dropping after the first heartbeat to force a reconnect.
	for {
		msg, err := stream.Recv()
		if err != nil {
			return err
		}
		s.mu.Lock()
		s.messages = append(s.messages, msg)
		s.mu.Unlock()
		if ev := msg.GetEvent(); ev != nil && ev.GetHeartbeat() != nil {
			s.mu.Lock()
			s.heartbeats++
			drop := s.dropAfterFirst
			s.mu.Unlock()
			if drop {
				return nil // close the stream; client must reconnect
			}
		}
	}
}

func (s *fakeServer) checkAuth(ctx context.Context) bool {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return false
	}
	vals := md.Get("authorization")
	if len(vals) == 0 {
		return false
	}
	return vals[0] == "Bearer "+s.wantCredential
}

func (s *fakeServer) registerCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.registers
}

func (s *fakeServer) heartbeatCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.heartbeats
}

func (s *fakeServer) reportedHeldServers() []*controlplanev1.HeldServer {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.heldServers
}

func (s *fakeServer) reportedUnheldIDs() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.unheldIDs
}

func (s *fakeServer) reportedResources() *controlplanev1.HostResources {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.resources
}

func (s *fakeServer) recordedMessages() []*controlplanev1.WorkerMessage {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*controlplanev1.WorkerMessage, len(s.messages))
	copy(out, s.messages)
	return out
}

func acceptAck(every time.Duration) *controlplanev1.ApiMessage {
	return &controlplanev1.ApiMessage{
		Payload: &controlplanev1.ApiMessage_RegisterAck{
			RegisterAck: &controlplanev1.RegisterAck{
				HeartbeatInterval: durationpb.New(every),
			},
		},
	}
}

// startServer wires the fake server onto a bufconn and returns a client
// connection plus a cleanup func.
func startServer(t *testing.T, srv *fakeServer) *grpc.ClientConn {
	t.Helper()
	lis := bufconn.Listen(1024 * 1024)
	gs := grpc.NewServer()
	controlplanev1.RegisterWorkerServiceServer(gs, srv)
	go func() { _ = gs.Serve(lis) }()

	conn, err := grpc.NewClient(
		"passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("grpc.NewClient: %v", err)
	}
	t.Cleanup(func() {
		_ = conn.Close()
		gs.Stop()
	})
	return conn
}

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func testCaps() session.Capabilities {
	return session.Capabilities{
		WorkerID:      "worker-int",
		WorkerVersion: "test",
		Drivers:       []string{"container"},
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatal("condition not met before deadline")
}

func TestHappyPathRegisterAndHeartbeat(t *testing.T) {
	srv := &fakeServer{
		wantCredential: "the-secret",
		heartbeatEvery: 20 * time.Millisecond,
	}
	conn := startServer(t, srv)

	dialer := controlplane.NewDialer(conn, "the-secret", realClock{})
	runner := session.NewRunner(dialer, testCaps(), realClock{}, testLogger())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = runner.Run(ctx); close(done) }()

	waitFor(t, func() bool { return srv.registerCount() == 1 })
	waitFor(t, func() bool { return srv.heartbeatCount() >= 2 })

	cancel()
	<-done
}

// TestRegisterAdvertisesHeldServers proves the adapter maps the domain
// Capabilities.HeldServers (id + generation) onto the wire Register.held_servers
// (issue #763), so the API can skip the destructive hydrate on a same-worker
// restart only when the held generation is fresh enough.
func TestRegisterAdvertisesHeldServers(t *testing.T) {
	srv := &fakeServer{
		wantCredential: "the-secret",
		heartbeatEvery: 20 * time.Millisecond,
	}
	conn := startServer(t, srv)

	caps := testCaps()
	caps.HeldServers = []session.HeldServer{
		{ServerID: "server-a", Generation: 5},
		{ServerID: "server-b", Generation: 0},
	}
	dialer := controlplane.NewDialer(conn, "the-secret", realClock{})
	runner := session.NewRunner(dialer, caps, realClock{}, testLogger())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = runner.Run(ctx); close(done) }()

	waitFor(t, func() bool { return srv.registerCount() == 1 })
	got := srv.reportedHeldServers()
	cancel()
	<-done

	want := map[string]uint64{"server-a": 5, "server-b": 0}
	if len(got) != len(want) {
		t.Fatalf("held_servers = %v, want %v", got, want)
	}
	for _, h := range got {
		gen, ok := want[h.GetServerId()]
		if !ok || gen != h.GetGeneration() {
			t.Fatalf("held_servers = %v, want %v", got, want)
		}
	}
}

// TestRegisterListsUnheldScratchServerIDs proves the adapter maps the domain
// Capabilities.UnheldScratchServerIDs onto the wire
// Register.unheld_scratch_server_ids (issue #3794), so the API checks those
// servers for a delete too.
func TestRegisterListsUnheldScratchServerIDs(t *testing.T) {
	srv := &fakeServer{
		wantCredential: "the-secret",
		heartbeatEvery: 20 * time.Millisecond,
	}
	conn := startServer(t, srv)

	caps := testCaps()
	caps.UnheldScratchServerIDs = []string{"server-a", "server-b"}
	dialer := controlplane.NewDialer(conn, "the-secret", realClock{})
	runner := session.NewRunner(dialer, caps, realClock{}, testLogger())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = runner.Run(ctx); close(done) }()

	waitFor(t, func() bool { return srv.registerCount() == 1 })
	got := srv.reportedUnheldIDs()
	cancel()
	<-done

	if !slices.Equal(got, []string{"server-a", "server-b"}) {
		t.Fatalf("unheld_scratch_server_ids = %v, want [server-a server-b]", got)
	}
}

// TestAuthRejectStopsRunner proves a wrong-credential Worker STOPS instead of
// reconnecting forever: the server aborts the stream with UNAUTHENTICATED (as
// #83 does), the adapter classifies that as terminal, and the runner returns
// session.ErrTerminal without ever registering.
func TestAuthRejectStopsRunner(t *testing.T) {
	srv := &fakeServer{
		wantCredential: "the-secret",
		heartbeatEvery: 50 * time.Millisecond,
	}
	conn := startServer(t, srv)

	// Wrong credential: the server aborts with UNAUTHENTICATED before registration.
	dialer := controlplane.NewDialer(conn, "wrong-secret", realClock{})
	runner := session.NewRunner(
		dialer, testCaps(), realClock{}, testLogger(),
		// A small backoff would still apply if the runner wrongly retried; keep it
		// tiny so a buggy reconnect would be caught quickly by the register check.
		session.WithBackoff(session.Backoff{Initial: 5 * time.Millisecond, Max: 20 * time.Millisecond, Multiplier: 2}),
	)

	errCh := make(chan error, 1)
	go func() { errCh <- runner.Run(context.Background()) }()

	select {
	case err := <-errCh:
		if !errors.Is(err, session.ErrTerminal) {
			t.Fatalf("Run() error = %v, want session.ErrTerminal (terminal stop)", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run() did not stop after auth reject; it is reconnecting forever")
	}

	if got := srv.registerCount(); got != 0 {
		t.Errorf("registerCount = %d, want 0 (auth aborts before Register)", got)
	}
}

func TestServerDropTriggersReconnect(t *testing.T) {
	srv := &fakeServer{
		wantCredential: "the-secret",
		heartbeatEvery: 15 * time.Millisecond,
		dropAfterFirst: true,
	}
	conn := startServer(t, srv)

	dialer := controlplane.NewDialer(conn, "the-secret", realClock{})
	runner := session.NewRunner(
		dialer, testCaps(), realClock{}, testLogger(),
		// Small, deterministic backoff so the reconnect is quick in the test.
		session.WithBackoff(session.Backoff{Initial: 5 * time.Millisecond, Max: 20 * time.Millisecond, Multiplier: 2}),
	)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = runner.Run(ctx); close(done) }()

	// The server drops after the first heartbeat; the client must re-register on
	// a fresh stream (CONTROL_PLANE.md Section 4.4).
	waitFor(t, func() bool { return srv.registerCount() >= 2 })

	cancel()
	<-done
}

// backoffClock is the real clock, except that it records each delay the Runner
// backs off for and lets it pass at once.
type backoffClock struct {
	realClock

	mu     sync.Mutex
	delays []time.Duration
}

func (c *backoffClock) After(d time.Duration) <-chan time.Time {
	c.mu.Lock()
	c.delays = append(c.delays, d)
	c.mu.Unlock()
	ch := make(chan time.Time, 1)
	ch <- time.Now()
	return ch
}

func (c *backoffClock) backoffs() []time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	return slices.Clone(c.delays)
}

// TestAnUnavailableRegistrationIsRetriedWithBackoff: the API refuses a
// registration its database did not answer in time with UNAVAILABLE (issue
// #4050). The Worker must retry it, waiting longer after each refusal rather than
// re-registering at once against a database that is still down, and register once
// the API acks again.
func TestAnUnavailableRegistrationIsRetriedWithBackoff(t *testing.T) {
	srv := &fakeServer{
		wantCredential:  "the-secret",
		heartbeatEvery:  20 * time.Millisecond,
		refuseRegisters: 3,
	}
	conn := startServer(t, srv)

	clock := &backoffClock{}
	dialer := controlplane.NewDialer(conn, "the-secret", clock)
	// Half of each attempt's ceiling under the default backoff: 1 s, 2 s, 4 s.
	runner := session.NewRunner(dialer, testCaps(), clock, testLogger(),
		session.WithRandFloat(func() float64 { return 0.5 }))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	errCh := make(chan error, 1)
	go func() { errCh <- runner.Run(ctx) }()

	// A heartbeat on the fourth stream: its registration was acked.
	waitFor(t, func() bool { return srv.heartbeatCount() >= 1 })
	cancel()
	if err := <-errCh; err != nil {
		t.Fatalf("Run() = %v, want nil after the shutdown", err)
	}

	if got := srv.registerCount(); got != 4 {
		t.Errorf("registerCount = %d, want 4 (three refused, then the accepted one)", got)
	}
	want := []time.Duration{500 * time.Millisecond, time.Second, 2 * time.Second}
	if got := clock.backoffs(); !slices.Equal(got, want) {
		t.Errorf("backoffs = %v, want %v", got, want)
	}
}

// TestRegisterAdvertisesResources proves the adapter maps the domain
// Capabilities.Resources onto the wire WorkerCapabilities.resources
// (issue #1218), so the API's placement logic can enforce memory/CPU gates.
func TestRegisterAdvertisesResources(t *testing.T) {
	srv := &fakeServer{
		wantCredential: "the-secret",
		heartbeatEvery: 20 * time.Millisecond,
	}
	conn := startServer(t, srv)

	caps := testCaps()
	caps.Resources = session.HostResources{
		CPUCores:    8,
		MemoryBytes: 16 * 1024 * 1024 * 1024, // 16 GiB
	}
	dialer := controlplane.NewDialer(conn, "the-secret", realClock{})
	runner := session.NewRunner(dialer, caps, realClock{}, testLogger())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = runner.Run(ctx); close(done) }()

	waitFor(t, func() bool { return srv.registerCount() == 1 })
	got := srv.reportedResources()
	cancel()
	<-done

	if got == nil {
		t.Fatal("resources is nil, want non-nil HostResources")
	}
	if got.GetCpuCores() != 8 {
		t.Errorf("cpu_cores = %d, want 8", got.GetCpuCores())
	}
	if got.GetMemoryBytes() != 16*1024*1024*1024 {
		t.Errorf("memory_bytes = %d, want %d", got.GetMemoryBytes(), uint64(16*1024*1024*1024))
	}
}

// TestRegisterAndHeartbeatCarryCorrelationId proves the adapter stamps a fresh
// UUID correlation_id on Register and Heartbeat messages (NFR-OBS-1, issue #2002)
// so an operator can trace each event end to end.
func TestRegisterAndHeartbeatCarryCorrelationId(t *testing.T) {
	srv := &fakeServer{
		wantCredential: "the-secret",
		heartbeatEvery: 20 * time.Millisecond,
	}
	conn := startServer(t, srv)

	dialer := controlplane.NewDialer(conn, "the-secret", realClock{})
	runner := session.NewRunner(dialer, testCaps(), realClock{}, testLogger())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = runner.Run(ctx); close(done) }()

	waitFor(t, func() bool { return srv.registerCount() == 1 })
	waitFor(t, func() bool { return srv.heartbeatCount() >= 1 })

	cancel()
	<-done

	msgs := srv.recordedMessages()
	if len(msgs) < 2 {
		t.Fatalf("expected at least 2 messages (Register + Heartbeat), got %d", len(msgs))
	}

	// First message is Register; it must carry a non-empty UUID correlation_id.
	regID := msgs[0].GetCorrelationId()
	if regID == "" {
		t.Fatal("Register message has empty correlation_id, want a fresh UUID")
	}
	if _, err := parseUUID(regID); err != nil {
		t.Fatalf("Register correlation_id %q is not a valid UUID: %v", regID, err)
	}

	// Second message is Heartbeat; its correlation_id must also be non-empty and
	// different from the Register's (each event gets its own fresh id).
	hbID := msgs[1].GetCorrelationId()
	if hbID == "" {
		t.Fatal("Heartbeat message has empty correlation_id, want a fresh UUID")
	}
	if _, err := parseUUID(hbID); err != nil {
		t.Fatalf("Heartbeat correlation_id %q is not a valid UUID: %v", hbID, err)
	}
	if hbID == regID {
		t.Errorf("Heartbeat correlation_id equals Register's (%s); each event must get a fresh id", hbID)
	}
}

// uuidRe matches the canonical 8-4-4-4-12 UUID text form.
var uuidRe = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// parseUUID validates a string as a UUID without importing google/uuid in tests.
func parseUUID(s string) (string, error) {
	if !uuidRe.MatchString(s) {
		return "", fmt.Errorf("%q is not a valid UUID", s)
	}
	return s, nil
}
