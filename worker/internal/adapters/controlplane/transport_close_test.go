package controlplane

import (
	"context"
	"net"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// sysClock is the production Clock; these transport-level tests use real time so
// the registerAckTimeout drives a genuine deadline.
type sysClock struct{}

func (sysClock) Now() time.Time                         { return time.Now() }
func (sysClock) After(d time.Duration) <-chan time.Time { return time.After(d) }
func (sysClock) NewTimer(d time.Duration) session.Timer { return sysTimer{time.NewTimer(d)} }

type sysTimer struct{ t *time.Timer }

func (t sysTimer) C() <-chan time.Time   { return t.t.C }
func (t sysTimer) Reset(d time.Duration) { t.t.Reset(d) }
func (t sysTimer) Stop()                 { t.t.Stop() }

// silentServer accepts the Session stream but never sends a RegisterAck and never
// returns: it models an API that wedges after opening the stream (issue #786). It
// blocks until its context is cancelled so the client side, not the server, must
// be the one to release the stream.
type silentServer struct {
	controlplanev1.UnimplementedWorkerServiceServer
}

func (silentServer) Session(stream controlplanev1.WorkerService_SessionServer) error {
	<-stream.Context().Done()
	return stream.Context().Err()
}

// dialSilent wires a silentServer onto a bufconn and returns a Dialer over it.
func dialSilent(t *testing.T) *Dialer {
	t.Helper()
	lis := bufconn.Listen(1024 * 1024)
	gs := grpc.NewServer()
	controlplanev1.RegisterWorkerServiceServer(gs, silentServer{})
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
	return NewDialer(conn, "the-secret", sysClock{})
}

// TestCloseUnblocksLingeringRecv proves that Close cancels the per-stream context
// so a Recv stranded on a stream the server never tears down returns promptly
// (issue #786). Before the fix, Close was CloseSend-only and RecvCommand could
// linger until process shutdown.
func TestCloseUnblocksLingeringRecv(t *testing.T) {
	transport, err := dialSilent(t).Dial(context.Background())
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}

	recvDone := make(chan error, 1)
	go func() {
		_, rerr := transport.RecvCommand(context.Background())
		recvDone <- rerr
	}()

	// Let the Recv block on the silent stream before closing.
	time.Sleep(20 * time.Millisecond)
	if err := transport.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	select {
	case rerr := <-recvDone:
		if rerr == nil {
			t.Fatal("RecvCommand returned nil; want a stream/context error after Close")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("RecvCommand did not return after Close; the stream is still lingering")
	}
}

// TestRecvRegisterAckTimesOut proves that an API which accepts the stream but
// never acks fails RecvRegisterAck within the deadline instead of wedging the run
// loop forever (issue #786).
func TestRecvRegisterAckTimesOut(t *testing.T) {
	prev := registerAckTimeout
	registerAckTimeout = 100 * time.Millisecond
	t.Cleanup(func() { registerAckTimeout = prev })

	transport, err := dialSilent(t).Dial(context.Background())
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	t.Cleanup(func() { _ = transport.Close() })

	if err := transport.SendRegister(context.Background(), session.Capabilities{WorkerID: "w1"}); err != nil {
		t.Fatalf("SendRegister: %v", err)
	}

	ackDone := make(chan error, 1)
	go func() {
		_, aerr := transport.RecvRegisterAck(context.Background())
		ackDone <- aerr
	}()

	select {
	case aerr := <-ackDone:
		if aerr == nil {
			t.Fatal("RecvRegisterAck returned nil; want a timeout error when no ack arrives")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("RecvRegisterAck did not return within the deadline; no ack bound is in effect")
	}
}
