package controlplane_test

import (
	"context"
	"io"
	"log/slog"
	"net"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
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

// fakeServer is an in-process WorkerService implementing the API side of the
// stream lifecycle (CONTROL_PLANE.md Section 4) just enough to exercise the
// client: it checks the credential metadata, answers Register with a
// configurable ack, records heartbeats, and can drop the stream after the first
// heartbeat to drive a reconnect.
type fakeServer struct {
	controlplanev1.UnimplementedWorkerServiceServer

	wantCredential string
	accept         bool
	heartbeatEvery time.Duration
	dropAfterFirst bool

	mu          sync.Mutex
	registers   int
	heartbeats  int
	lastCmdResp *controlplanev1.CommandResult
	authedOK    bool
}

func (s *fakeServer) Session(stream controlplanev1.WorkerService_SessionServer) error {
	if !s.checkAuth(stream.Context()) {
		s.mu.Lock()
		s.authedOK = false
		s.mu.Unlock()
		return stream.Send(rejectAck("bad credential"))
	}
	s.mu.Lock()
	s.authedOK = true
	s.mu.Unlock()

	// First message must be Register.
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	if first.GetRegister() == nil {
		return stream.Send(rejectAck("first message was not Register"))
	}
	s.mu.Lock()
	s.registers++
	s.mu.Unlock()

	if !s.accept {
		return stream.Send(rejectAck("registration refused"))
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
		if ev := msg.GetEvent(); ev != nil && ev.GetHeartbeat() != nil {
			s.mu.Lock()
			s.heartbeats++
			drop := s.dropAfterFirst
			s.mu.Unlock()
			if drop {
				return nil // close the stream; client must reconnect
			}
		}
		if res := msg.GetCommandResult(); res != nil {
			s.mu.Lock()
			s.lastCmdResp = res
			s.mu.Unlock()
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

func rejectAck(reason string) *controlplanev1.ApiMessage {
	return &controlplanev1.ApiMessage{
		Payload: &controlplanev1.ApiMessage_RegisterAck{
			RegisterAck: &controlplanev1.RegisterAck{Accepted: false, RejectionReason: reason},
		},
	}
}

func acceptAck(every time.Duration) *controlplanev1.ApiMessage {
	return &controlplanev1.ApiMessage{
		Payload: &controlplanev1.ApiMessage_RegisterAck{
			RegisterAck: &controlplanev1.RegisterAck{
				Accepted:          true,
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
		Drivers:       []string{"host-process"},
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
		accept:         true,
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

func TestAuthRejectStopsRunner(t *testing.T) {
	srv := &fakeServer{
		wantCredential: "the-secret",
		accept:         true,
		heartbeatEvery: 50 * time.Millisecond,
	}
	conn := startServer(t, srv)

	// Wrong credential: the server rejects before registration.
	dialer := controlplane.NewDialer(conn, "wrong-secret", realClock{})
	runner := session.NewRunner(dialer, testCaps(), realClock{}, testLogger())

	errCh := make(chan error, 1)
	go func() { errCh <- runner.Run(context.Background()) }()

	select {
	case err := <-errCh:
		if err == nil {
			t.Fatal("Run() returned nil on auth reject, want ErrRejected")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run() did not return after auth reject")
	}
}

func TestServerDropTriggersReconnect(t *testing.T) {
	srv := &fakeServer{
		wantCredential: "the-secret",
		accept:         true,
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
