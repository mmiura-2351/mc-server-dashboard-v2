package controlplane

import (
	"context"
	"io"
	"testing"
	"time"

	"google.golang.org/grpc/metadata"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
)

// stallStream is a WorkerService_SessionClient double whose Send blocks until
// the test releases it and then reports success. It lets a test place the
// completion of a Send at an exact point relative to the stall deadline instead
// of hoping to hit the boundary by timing (issue #2397).
type stallStream struct {
	entered chan struct{}
	release chan struct{}
}

func (s *stallStream) Send(*controlplanev1.WorkerMessage) error {
	close(s.entered)
	<-s.release
	return nil
}

func (*stallStream) Recv() (*controlplanev1.ApiMessage, error) { return nil, io.EOF }
func (*stallStream) Header() (metadata.MD, error)              { return nil, nil }
func (*stallStream) Trailer() metadata.MD                      { return nil }
func (*stallStream) CloseSend() error                          { return nil }
func (*stallStream) Context() context.Context                  { return context.Background() }
func (*stallStream) SendMsg(any) error                         { return nil }
func (*stallStream) RecvMsg(any) error                         { return nil }

// newStallTransport wires a transport over the blocking send stream and the
// manual clock (declared in register_ack_deadline_test.go). The returned
// channels expose the stall tick and the stream-context cancellation the stall
// watchdog performs.
func newStallTransport() (tr *transport, stream *stallStream, tick chan time.Time, canceled chan struct{}) {
	stream = &stallStream{entered: make(chan struct{}), release: make(chan struct{})}
	tick = make(chan time.Time, 1)
	canceled = make(chan struct{}, 1)
	tr = &transport{
		stream: stream,
		clock:  manualClock{tick},
		cancel: func() { canceled <- struct{}{} },
	}
	return tr, stream, tick, canceled
}

// TestSendBoundedReportsStallWhenDeadlineWins pins the losing half of the
// boundary race (issue #2397): once the stall watchdog has cancelled the
// per-stream context, a Send that completes immediately afterwards must be
// reported as a stall. Returning success would hand the run loop a healthy
// verdict on a stream that is already cancelled, and the next Send/Recv would
// fail with codes.Canceled somewhere with less context.
func TestSendBoundedReportsStallWhenDeadlineWins(t *testing.T) {
	tr, stream, tick, canceled := newStallTransport()

	result := make(chan error, 1)
	go func() {
		result <- tr.sendBounded(&controlplanev1.WorkerMessage{})
	}()

	<-stream.entered
	tick <- time.Time{}
	select {
	case <-canceled:
	case <-time.After(2 * time.Second):
		t.Fatal("the stall watchdog never cancelled the stream context")
	}

	// The Send completes right at the boundary, after the watchdog already won.
	close(stream.release)

	select {
	case err := <-result:
		if err == nil {
			t.Fatal("sendBounded reported success on a stream the stall watchdog had already cancelled")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("sendBounded did not return")
	}
}

// TestSendBoundedDoesNotCancelWhenSendWins pins the winning half of the same
// race: once the Send has completed, a stall tick arriving at the boundary must
// not cancel the stream of a transport that is sending fine — that costs a
// pointless disconnect/backoff/reconnect cycle.
func TestSendBoundedDoesNotCancelWhenSendWins(t *testing.T) {
	tr, stream, tick, canceled := newStallTransport()

	result := make(chan error, 1)
	go func() {
		result <- tr.sendBounded(&controlplanev1.WorkerMessage{})
	}()

	<-stream.entered
	close(stream.release)
	select {
	case err := <-result:
		if err != nil {
			t.Fatalf("sendBounded: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("sendBounded did not return")
	}

	// The stall deadline expires right at the boundary, after the Send already
	// won.
	tick <- time.Time{}
	select {
	case <-canceled:
		t.Fatal("the stall watchdog cancelled the stream context of a send that had already completed")
	case <-time.After(50 * time.Millisecond):
	}
}
