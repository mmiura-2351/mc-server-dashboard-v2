// Package controlplane is the gRPC adapter for the Worker's control-plane
// session Port (internal/domain/session). It dials the API, attaches the Worker
// credential, opens the bidirectional Session stream, and translates between the
// domain's transport-neutral types and the generated control-plane messages.
//
// Authentication: the Worker credential travels as gRPC call metadata
// ("authorization: Bearer <credential>"), not as a proto field — the Register
// message carries no credential (CONTROL_PLANE.md Sections 2 and 4.1; the
// credential is configuration, Section 6.1). Transport security (TLS/mTLS) sits
// below this contract; an empty CA file selects an insecure dial for local use.
package controlplane

import (
	"context"
	"fmt"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// authMetadataKey is the metadata key carrying the Worker credential. The API's
// control-plane server reads it to authenticate the stream.
const authMetadataKey = "authorization"

// Dialer opens a fresh Session stream per Dial, implementing session.Dialer.
type Dialer struct {
	conn       grpc.ClientConnInterface
	credential string
	clock      session.Clock
}

// NewDialer builds a Dialer over an established client connection. The
// credential is attached to every stream's metadata; clock stamps emitted_at.
func NewDialer(conn grpc.ClientConnInterface, credential string, clock session.Clock) *Dialer {
	return &Dialer{conn: conn, credential: credential, clock: clock}
}

// Dial opens the bidirectional Session stream and returns it as a
// session.Transport. The credential is injected as outgoing metadata on the
// stream context.
func (d *Dialer) Dial(ctx context.Context) (session.Transport, error) {
	client := controlplanev1.NewWorkerServiceClient(d.conn)
	authCtx := metadata.AppendToOutgoingContext(ctx, authMetadataKey, "Bearer "+d.credential)

	stream, err := client.Session(authCtx)
	if err != nil {
		return nil, fmt.Errorf("controlplane: open session: %w", err)
	}
	return &transport{stream: stream, clock: d.clock}, nil
}

// transport adapts one open Session stream to session.Transport.
type transport struct {
	stream controlplanev1.WorkerService_SessionClient
	clock  session.Clock
}

func (t *transport) SendRegister(_ context.Context, caps session.Capabilities) error {
	msg := &controlplanev1.WorkerMessage{
		EmittedAt: timestamppb.New(t.clock.Now()),
		Payload: &controlplanev1.WorkerMessage_Register{
			Register: &controlplanev1.Register{
				WorkerId:      caps.WorkerID,
				WorkerVersion: caps.WorkerVersion,
				Capabilities: &controlplanev1.WorkerCapabilities{
					Drivers:    mapDrivers(caps.Drivers),
					MaxServers: caps.MaxServers,
				},
			},
		},
	}
	if err := t.stream.Send(msg); err != nil {
		return fmt.Errorf("controlplane: send register: %w", err)
	}
	return nil
}

func (t *transport) RecvRegisterAck(_ context.Context) (session.RegisterAck, error) {
	msg, err := t.stream.Recv()
	if err != nil {
		return session.RegisterAck{}, fmt.Errorf("controlplane: recv register ack: %w", err)
	}
	ack := msg.GetRegisterAck()
	if ack == nil {
		return session.RegisterAck{}, fmt.Errorf("controlplane: first API message was not a RegisterAck")
	}
	return session.RegisterAck{
		Accepted:          ack.GetAccepted(),
		HeartbeatInterval: ack.GetHeartbeatInterval().AsDuration(),
		RejectionReason:   ack.GetRejectionReason(),
	}, nil
}

func (t *transport) SendHeartbeat(_ context.Context) error {
	msg := &controlplanev1.WorkerMessage{
		EmittedAt: timestamppb.New(t.clock.Now()),
		Payload: &controlplanev1.WorkerMessage_Event{
			Event: &controlplanev1.Event{
				Event: &controlplanev1.Event_Heartbeat{Heartbeat: &controlplanev1.Heartbeat{}},
			},
		},
	}
	if err := t.stream.Send(msg); err != nil {
		return fmt.Errorf("controlplane: send heartbeat: %w", err)
	}
	return nil
}

func (t *transport) SendCommandResult(_ context.Context, result session.CommandResult) error {
	msg := &controlplanev1.WorkerMessage{
		// correlation_id MUST equal the originating command_id (CONTROL_PLANE.md
		// Section 3) so the API pairs the result to its command.
		CorrelationId: result.CommandID,
		EmittedAt:     timestamppb.New(t.clock.Now()),
		Payload: &controlplanev1.WorkerMessage_CommandResult{
			CommandResult: &controlplanev1.CommandResult{
				Success: result.Success,
				Error: &controlplanev1.CommandError{
					Code:    mapErrorCode(result.ErrorCode),
					Message: result.ErrorMessage,
				},
			},
		},
	}
	if err := t.stream.Send(msg); err != nil {
		return fmt.Errorf("controlplane: send command result: %w", err)
	}
	return nil
}

func (t *transport) RecvCommand(_ context.Context) (session.Command, error) {
	for {
		msg, err := t.stream.Recv()
		if err != nil {
			return session.Command{}, err
		}
		cmd := msg.GetApiCommand()
		if cmd == nil {
			// Ignore non-command API messages (none defined besides RegisterAck
			// today); keep reading rather than treating it as a stream error.
			continue
		}
		return session.Command{
			CommandID: cmd.GetCommandId(),
			ServerID:  cmd.GetServerId(),
			Kind:      commandKind(cmd),
		}, nil
	}
}

func (t *transport) Close() error {
	return t.stream.CloseSend()
}

// mapDrivers translates configured driver names to the wire enum. Unknown names
// are validated away in config; an unexpected one maps to UNSPECIFIED.
func mapDrivers(names []string) []controlplanev1.ExecutionDriverKind {
	out := make([]controlplanev1.ExecutionDriverKind, 0, len(names))
	for _, n := range names {
		switch n {
		case "host-process":
			out = append(out, controlplanev1.ExecutionDriverKind_EXECUTION_DRIVER_KIND_HOST_PROCESS)
		case "container":
			out = append(out, controlplanev1.ExecutionDriverKind_EXECUTION_DRIVER_KIND_CONTAINER)
		default:
			out = append(out, controlplanev1.ExecutionDriverKind_EXECUTION_DRIVER_KIND_UNSPECIFIED)
		}
	}
	return out
}

// mapErrorCode translates a domain error code to the wire enum. This milestone
// only emits the internal/unsupported code.
func mapErrorCode(code session.CommandErrorCode) controlplanev1.CommandErrorCode {
	switch code {
	case session.CommandErrorInternal:
		return controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_INTERNAL
	default:
		return controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_INTERNAL
	}
}

// commandKind names the command oneof for logging and the unsupported-error
// message (CONTROL_PLANE.md Section 5).
func commandKind(cmd *controlplanev1.ApiCommand) string {
	switch cmd.GetCommand().(type) {
	case *controlplanev1.ApiCommand_Start:
		return "StartServer"
	case *controlplanev1.ApiCommand_Stop:
		return "StopServer"
	case *controlplanev1.ApiCommand_Restart:
		return "RestartServer"
	case *controlplanev1.ApiCommand_ServerCommand:
		return "ServerCommand"
	case *controlplanev1.ApiCommand_Hydrate:
		return "HydrateTrigger"
	case *controlplanev1.ApiCommand_Snapshot:
		return "SnapshotTrigger"
	case *controlplanev1.ApiCommand_ReadFile:
		return "ReadFile"
	case *controlplanev1.ApiCommand_EditFile:
		return "EditFile"
	default:
		return "unknown"
	}
}
