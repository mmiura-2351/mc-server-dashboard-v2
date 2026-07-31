package controlplane

import (
	"context"
	"io"
	"testing"
	"time"

	"google.golang.org/grpc/metadata"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// capturingStream is a WorkerService_SessionClient double that records every
// message the transport sends, so a test can assert on the wire shape directly.
type capturingStream struct {
	sent []*controlplanev1.WorkerMessage
}

func (s *capturingStream) Send(msg *controlplanev1.WorkerMessage) error {
	s.sent = append(s.sent, msg)
	return nil
}

func (*capturingStream) Recv() (*controlplanev1.ApiMessage, error) { return nil, io.EOF }
func (*capturingStream) Header() (metadata.MD, error)              { return nil, nil }
func (*capturingStream) Trailer() metadata.MD                      { return nil }
func (*capturingStream) CloseSend() error                          { return nil }
func (*capturingStream) Context() context.Context                  { return context.Background() }
func (*capturingStream) SendMsg(any) error                         { return nil }
func (*capturingStream) RecvMsg(any) error                         { return nil }

// newCapturingTransport wires a transport over the recording stream and the
// manual clock (declared in register_ack_deadline_test.go).
func newCapturingTransport() (*transport, *capturingStream) {
	stream := &capturingStream{}
	return &transport{
		stream: stream,
		clock:  manualClock{make(chan time.Time, 1)},
		cancel: func() {},
	}, stream
}

// sentCommandResult returns the single CommandResult the transport put on the
// wire, failing the test if the shape is anything else.
func sentCommandResult(t *testing.T, stream *capturingStream) *controlplanev1.CommandResult {
	t.Helper()
	if len(stream.sent) != 1 {
		t.Fatalf("sent %d messages, want 1", len(stream.sent))
	}
	cr := stream.sent[0].GetCommandResult()
	if cr == nil {
		t.Fatalf("message is not a CommandResult: %v", stream.sent[0])
	}
	return cr
}

// TestCommandResultCarriesTheDeclaredHeldGeneration pins the wire mapping for the
// Worker's retention declaration (issue #2481). The API records a held generation
// from this field alone, so a declaration the Worker made must reach it intact.
func TestCommandResultCarriesTheDeclaredHeldGeneration(t *testing.T) {
	tr, stream := newCapturingTransport()
	gen := uint64(12)

	if err := tr.SendCommandResult(context.Background(), session.CommandResult{
		CommandID: "p1", Success: true, HeldGeneration: &gen,
	}); err != nil {
		t.Fatal(err)
	}

	cr := sentCommandResult(t, stream)
	if cr.HeldGeneration == nil {
		t.Fatal("held_generation absent on the wire: the Worker declared it holds generation 12, " +
			"but the API will read 'declared nothing' and hydrate unnecessarily (issue #2481)")
	}
	if got := cr.GetHeldGeneration(); got != 12 {
		t.Fatalf("held_generation = %d, want 12", got)
	}
}

// TestCommandResultOmitsHeldGenerationWhenNothingIsDeclared is the direction that
// carries the data-safety weight. A snapshot that GC'd the scratch (the stopped-id
// branch) or whose marker stamp was refused declares nothing, and the wire must
// say ABSENT rather than 0: the API records only on presence, so a spuriously
// present field would let a start skip a hydrate it needs (issue #696 class).
func TestCommandResultOmitsHeldGenerationWhenNothingIsDeclared(t *testing.T) {
	tr, stream := newCapturingTransport()

	if err := tr.SendCommandResult(context.Background(), session.CommandResult{
		CommandID: "p1", Success: true,
	}); err != nil {
		t.Fatal(err)
	}

	if cr := sentCommandResult(t, stream); cr.HeldGeneration != nil {
		t.Fatalf("held_generation = %d present though the Worker declared nothing", cr.GetHeldGeneration())
	}
}
