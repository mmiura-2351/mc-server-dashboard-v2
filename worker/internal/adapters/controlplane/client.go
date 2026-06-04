// Package controlplane is the gRPC adapter for the Worker's control-plane
// session Port (internal/domain/session). It dials the API, attaches the Worker
// credential, opens the bidirectional Session stream, and translates between the
// domain's transport-neutral types and the generated control-plane messages.
//
// Authentication: the Worker credential travels as gRPC call metadata
// ("authorization: Bearer <credential>"), not as a proto field — the Register
// message carries no credential (CONTROL_PLANE.md Sections 2 and 4.1; the
// credential is configuration, Section 6.1). Transport security (TLS/mTLS) sits
// below this contract; the wiring layer builds the gRPC credentials (cmd/worker
// dial).
package controlplane

import (
	"context"
	"fmt"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
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
		return nil, fmt.Errorf("controlplane: open session: %w", classify(err))
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
	msg, err := t.recvClassified()
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
	cr := &controlplanev1.CommandResult{Success: result.Success}
	if result.Success {
		// A successful ServerCommand carries its console output; other successes
		// have no payload (CONTROL_PLANE.md Section 5).
		if result.Output != "" {
			cr.Result = &controlplanev1.CommandResult_CommandOutput{CommandOutput: result.Output}
		}
	} else {
		cr.Error = &controlplanev1.CommandError{
			Code:    mapErrorCode(result.ErrorCode),
			Message: result.ErrorMessage,
		}
	}
	msg := &controlplanev1.WorkerMessage{
		// correlation_id MUST equal the originating command_id (CONTROL_PLANE.md
		// Section 3) so the API pairs the result to its command.
		CorrelationId: result.CommandID,
		EmittedAt:     timestamppb.New(t.clock.Now()),
		Payload:       &controlplanev1.WorkerMessage_CommandResult{CommandResult: cr},
	}
	if err := t.stream.Send(msg); err != nil {
		return fmt.Errorf("controlplane: send command result: %w", err)
	}
	return nil
}

func (t *transport) SendStatusChange(_ context.Context, event session.StatusEvent) error {
	msg := &controlplanev1.WorkerMessage{
		EmittedAt: timestamppb.New(t.clock.Now()),
		Payload: &controlplanev1.WorkerMessage_Event{
			Event: &controlplanev1.Event{
				ServerId: event.ServerID,
				Event: &controlplanev1.Event_StatusChange{
					StatusChange: &controlplanev1.StatusChange{
						State:  mapServerState(event.State),
						Detail: event.Detail,
					},
				},
			},
		},
	}
	if err := t.stream.Send(msg); err != nil {
		return fmt.Errorf("controlplane: send status change: %w", err)
	}
	return nil
}

func (t *transport) RecvCommand(_ context.Context) (session.Command, error) {
	for {
		msg, err := t.recvClassified()
		if err != nil {
			return session.Command{}, err
		}
		cmd := msg.GetApiCommand()
		if cmd == nil {
			// Ignore non-command API messages (none defined besides RegisterAck
			// today); keep reading rather than treating it as a stream error.
			continue
		}
		return toCommand(cmd), nil
	}
}

func (t *transport) Close() error {
	return t.stream.CloseSend()
}

// classify maps a gRPC stream error to the domain's terminal/transient
// distinction. The API aborts the stream with a status code rather than sending
// RegisterAck{accepted=false} for a bad/missing credential or a protocol
// violation (CONTROL_PLANE.md Section 4.1): those codes are terminal so the run
// loop stops instead of reconnecting forever with the same rejected input. All
// other failures (UNAVAILABLE, DEADLINE_EXCEEDED, mid-stream drops) stay
// transient and keep the backoff-reconnect path. err must be non-nil.
func classify(err error) error {
	switch status.Code(err) {
	case codes.Unauthenticated, codes.PermissionDenied, codes.FailedPrecondition, codes.InvalidArgument:
		return fmt.Errorf("%w: %w", session.ErrTerminal, err)
	default:
		return err
	}
}

// recvClassified reads the next stream message, classifying any error so the run
// loop can distinguish a terminal abort from a transient drop (see classify).
func (t *transport) recvClassified() (*controlplanev1.ApiMessage, error) {
	msg, err := t.stream.Recv()
	if err != nil {
		return nil, classify(err)
	}
	return msg, nil
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

// mapErrorCode translates a domain error code to the wire enum (CONTROL_PLANE.md
// Section 7).
func mapErrorCode(code session.CommandErrorCode) controlplanev1.CommandErrorCode {
	switch code {
	case session.CommandErrorServerNotFound:
		return controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_SERVER_NOT_FOUND
	case session.CommandErrorInvalidState:
		return controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_INVALID_STATE
	case session.CommandErrorDriverUnavailable:
		return controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_DRIVER_UNAVAILABLE
	default:
		return controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_INTERNAL
	}
}

// mapServerState translates a domain status name to the wire ServerState enum
// (CONTROL_PLANE.md Section 6).
func mapServerState(state string) controlplanev1.ServerState {
	switch state {
	case "starting":
		return controlplanev1.ServerState_SERVER_STATE_STARTING
	case "running":
		return controlplanev1.ServerState_SERVER_STATE_RUNNING
	case "stopping":
		return controlplanev1.ServerState_SERVER_STATE_STOPPING
	case "stopped":
		return controlplanev1.ServerState_SERVER_STATE_STOPPED
	case "restarting":
		return controlplanev1.ServerState_SERVER_STATE_RESTARTING
	case "crashed":
		return controlplanev1.ServerState_SERVER_STATE_CRASHED
	default:
		return controlplanev1.ServerState_SERVER_STATE_UNSPECIFIED
	}
}

// toCommand maps a wire ApiCommand to the domain Command, extracting the
// payload fields the handled commands need.
func toCommand(cmd *controlplanev1.ApiCommand) session.Command {
	out := session.Command{
		CommandID: cmd.GetCommandId(),
		ServerID:  cmd.GetServerId(),
		Kind:      commandKind(cmd),
	}
	switch c := cmd.GetCommand().(type) {
	case *controlplanev1.ApiCommand_Start:
		out.Driver = driverName(c.Start.GetDriver())
		out.JarRelpath = c.Start.GetJarRelpath()
		out.MinecraftVersion = c.Start.GetMinecraftVersion()
	case *controlplanev1.ApiCommand_Stop:
		out.Force = c.Stop.GetForce()
	case *controlplanev1.ApiCommand_ServerCommand:
		out.Line = c.ServerCommand.GetLine()
	}
	return out
}

// driverName maps the wire driver enum to the configured driver name used by the
// handler and capability config.
func driverName(kind controlplanev1.ExecutionDriverKind) string {
	switch kind {
	case controlplanev1.ExecutionDriverKind_EXECUTION_DRIVER_KIND_HOST_PROCESS:
		return "host-process"
	case controlplanev1.ExecutionDriverKind_EXECUTION_DRIVER_KIND_CONTAINER:
		return "container"
	default:
		return ""
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
