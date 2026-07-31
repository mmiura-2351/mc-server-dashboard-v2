package controlplane

import (
	"context"
	"testing"
	"time"

	"google.golang.org/grpc/metadata"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// ackStream is a WorkerService_SessionClient double whose Recv blocks until the
// test releases it and then answers with a RegisterAck. It lets a test place the
// ack at an exact point relative to the register-ack deadline instead of hoping
// to hit the boundary by timing (issue #2020).
type ackStream struct {
	entered chan struct{}
	release chan struct{}
}

func (s *ackStream) Recv() (*controlplanev1.ApiMessage, error) {
	close(s.entered)
	<-s.release
	return &controlplanev1.ApiMessage{
		Payload: &controlplanev1.ApiMessage_RegisterAck{RegisterAck: &controlplanev1.RegisterAck{}},
	}, nil
}

func (*ackStream) Send(*controlplanev1.WorkerMessage) error { return nil }
func (*ackStream) Header() (metadata.MD, error)             { return nil, nil }
func (*ackStream) Trailer() metadata.MD                     { return nil }
func (*ackStream) CloseSend() error                         { return nil }
func (*ackStream) Context() context.Context                 { return context.Background() }
func (*ackStream) SendMsg(any) error                        { return nil }
func (*ackStream) RecvMsg(any) error                        { return nil }

// manualClock hands out timers that fire only when the test writes to tick, so
// the deadline expiry is a step the test takes rather than a wall-clock event.
type manualClock struct{ tick chan time.Time }

func (manualClock) Now() time.Time                         { return time.Time{} }
func (c manualClock) After(time.Duration) <-chan time.Time { return c.tick }
func (c manualClock) NewTimer(time.Duration) session.Timer { return manualTimer{c.tick} }

type manualTimer struct{ ch chan time.Time }

func (t manualTimer) C() <-chan time.Time { return t.ch }
func (manualTimer) Reset(time.Duration)   {}
func (manualTimer) Stop()                 {}

// newAckTransport wires a transport over the blocking ack stream and the manual
// clock. The returned channels expose the deadline tick and the stream-context
// cancellation the deadline watcher performs.
func newAckTransport() (tr *transport, stream *ackStream, tick chan time.Time, canceled chan struct{}) {
	stream = &ackStream{entered: make(chan struct{}), release: make(chan struct{})}
	tick = make(chan time.Time, 1)
	canceled = make(chan struct{}, 1)
	tr = &transport{
		stream: stream,
		clock:  manualClock{tick},
		cancel: func() { canceled <- struct{}{} },
	}
	return tr, stream, tick, canceled
}

// TestRecvRegisterAckReportsTimeoutWhenDeadlineWins pins the losing half of the
// boundary race (issue #2020): once the deadline watcher has cancelled the
// per-stream context, an ack that lands immediately afterwards must be reported
// as a timeout. Returning it as a success would hand the run loop a registered
// session on a context it no longer owns, and the next Send/Recv would fail with
// codes.Canceled — a pointless disconnect/backoff/re-register cycle.
func TestRecvRegisterAckReportsTimeoutWhenDeadlineWins(t *testing.T) {
	tr, stream, tick, canceled := newAckTransport()

	result := make(chan error, 1)
	go func() {
		_, err := tr.RecvRegisterAck(context.Background())
		result <- err
	}()

	<-stream.entered
	tick <- time.Time{}
	select {
	case <-canceled:
	case <-time.After(2 * time.Second):
		t.Fatal("the deadline watcher never cancelled the stream context")
	}

	// The ack lands right at the boundary, after the watcher already won.
	close(stream.release)

	select {
	case err := <-result:
		if err == nil {
			t.Fatal("RecvRegisterAck returned a successful ack on a stream the deadline watcher had already cancelled")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("RecvRegisterAck did not return")
	}
}

// TestRecvRegisterAckDoesNotCancelWhenAckWins pins the winning half of the same
// race: once the ack has been received, a deadline tick arriving at the boundary
// must not cancel the stream of a session that just registered.
func TestRecvRegisterAckDoesNotCancelWhenAckWins(t *testing.T) {
	tr, stream, tick, canceled := newAckTransport()

	result := make(chan error, 1)
	go func() {
		_, err := tr.RecvRegisterAck(context.Background())
		result <- err
	}()

	<-stream.entered
	close(stream.release)
	select {
	case err := <-result:
		if err != nil {
			t.Fatalf("RecvRegisterAck: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("RecvRegisterAck did not return")
	}

	// The deadline expires right at the boundary, after the ack already won.
	tick <- time.Time{}
	select {
	case <-canceled:
		t.Fatal("the deadline watcher cancelled the stream context of a session that had already registered")
	case <-time.After(50 * time.Millisecond):
	}
}
