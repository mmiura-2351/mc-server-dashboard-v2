// Package controlplane translates session types to gRPC messages.
// The Worker credential is bearer metadata, never a Register field; TLS is configured by the wiring layer.
package controlplane

import (
	"context"
	"fmt"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
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

// Bound the opening ack wait, which runs before heartbeat monitoring begins.
// Tests can lower this timeout.
var registerAckTimeout = 30 * time.Second

// Bound sends blocked by HTTP/2 flow control so heartbeats cannot be starved indefinitely.
var sendStallTimeout = 10 * time.Second

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

// Dial opens Session with bearer metadata attached to its stream context.
func (d *Dialer) Dial(ctx context.Context) (session.Transport, error) {
	client := controlplanev1.NewWorkerServiceClient(d.conn)
	// Use a per-stream context so Close can unblock Recv independently of the Runner lifetime.
	streamCtx, cancel := context.WithCancel(ctx)
	authCtx := metadata.AppendToOutgoingContext(streamCtx, authMetadataKey, "Bearer "+d.credential)

	stream, err := client.Session(authCtx)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("controlplane: open session: %w", classify(err))
	}
	return &transport{stream: stream, clock: d.clock, cancel: cancel}, nil
}

// transport uses stream cancellation to bound blocked sends; gRPC ignores per-call contexts.
// Connection keepalive detects silent path loss, while sendBounded handles flow-control stalls.
type transport struct {
	stream controlplanev1.WorkerService_SessionClient
	clock  session.Clock
	// cancel tears down the per-stream context; Close calls it after CloseSend so an in-flight Recv returns instead
	// of lingering.
	cancel context.CancelFunc
}

func (t *transport) SendRegister(_ context.Context, caps session.Capabilities) error {
	msg := &controlplanev1.WorkerMessage{
		CorrelationId: uuid.New().String(),
		EmittedAt:     timestamppb.New(t.clock.Now()),
		Payload: &controlplanev1.WorkerMessage_Register{
			Register: &controlplanev1.Register{
				WorkerId:      caps.WorkerID,
				WorkerVersion: caps.WorkerVersion,
				// Advertise held generations so the API skips hydrate only for a sufficiently fresh working set.
				HeldServers: mapHeldServers(caps.HeldServers),
				Capabilities: &controlplanev1.WorkerCapabilities{
					Drivers:    mapDrivers(caps.Drivers),
					MaxServers: caps.MaxServers,
					Resources: &controlplanev1.HostResources{
						CpuCores:    caps.Resources.CPUCores,
						MemoryBytes: caps.Resources.MemoryBytes,
					},
				},
			},
		},
	}
	if err := t.sendBounded(msg); err != nil {
		return fmt.Errorf("controlplane: send register: %w", err)
	}
	return nil
}

func (t *transport) RecvRegisterAck(_ context.Context) (session.RegisterAck, error) {
	// The CAS selects one winner between ack and timeout; Stop cannot retract an already buffered tick.
	var settled atomic.Bool
	deadline := t.clock.NewTimer(registerAckTimeout)
	defer deadline.Stop()
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-deadline.C():
			if settled.CompareAndSwap(false, true) {
				t.cancel()
			}
		case <-done:
		}
	}()

	msg, err := t.recvClassified()
	if err != nil {
		return session.RegisterAck{}, fmt.Errorf("controlplane: recv register ack: %w", err)
	}
	if !settled.CompareAndSwap(false, true) {
		return session.RegisterAck{}, fmt.Errorf("controlplane: recv register ack: no ack within %s", registerAckTimeout)
	}
	ack := msg.GetRegisterAck()
	if ack == nil {
		return session.RegisterAck{}, fmt.Errorf("controlplane: first API message was not a RegisterAck")
	}
	return session.RegisterAck{
		HeartbeatInterval:    ack.GetHeartbeatInterval().AsDuration(),
		TransferDeadline:     ack.GetTransferDeadline().AsDuration(),
		UnknownHeldServerIDs: ack.GetUnknownHeldServerIds(),
	}, nil
}

func (t *transport) SendHeartbeat(_ context.Context) error {
	msg := &controlplanev1.WorkerMessage{
		CorrelationId: uuid.New().String(),
		EmittedAt:     timestamppb.New(t.clock.Now()),
		Payload: &controlplanev1.WorkerMessage_Event{
			Event: &controlplanev1.Event{
				Event: &controlplanev1.Event_Heartbeat{Heartbeat: &controlplanev1.Heartbeat{}},
			},
		},
	}
	if err := t.sendBounded(msg); err != nil {
		return fmt.Errorf("controlplane: send heartbeat: %w", err)
	}
	return nil
}

func (t *transport) SendCommandResult(_ context.Context, result session.CommandResult) error {
	// Preserve nil held_generation: it declares nothing when scratch was removed or its marker was not stamped.
	cr := &controlplanev1.CommandResult{Success: result.Success, HeldGeneration: result.HeldGeneration}
	if result.Success {
		// Check non-nil file results first so empty files and directories retain their result oneof arm.
		switch {
		case result.FileListing != nil:
			cr.Result = &controlplanev1.CommandResult_FileListing{
				FileListing: toFileListing(result.FileListing),
			}
		case result.FileContent != nil:
			cr.Result = &controlplanev1.CommandResult_FileContent{FileContent: result.FileContent}
		case result.Output != "":
			cr.Result = &controlplanev1.CommandResult_CommandOutput{CommandOutput: result.Output}
		}
	} else {
		cr.Error = &controlplanev1.CommandError{
			Code:    mapErrorCode(result.ErrorCode),
			Message: result.ErrorMessage,
			// FileAccessReason refines a FILE_ACCESS_DENIED failure; it is UNSPECIFIED for every other code, the proto3
			// default.
			FileAccessReason: mapFileAccessReason(result.FileAccessReason),
		}
	}
	msg := &controlplanev1.WorkerMessage{
		// correlation_id MUST equal the originating command_id (CONTROL_PLANE.md
		// Section 3) so the API pairs the result to its command.
		CorrelationId: result.CommandID,
		EmittedAt:     timestamppb.New(t.clock.Now()),
		Payload:       &controlplanev1.WorkerMessage_CommandResult{CommandResult: cr},
	}
	if err := t.sendBounded(msg); err != nil {
		return fmt.Errorf("controlplane: send command result: %w", err)
	}
	return nil
}

func (t *transport) SendStatusChange(_ context.Context, event session.StatusEvent) error {
	msg := &controlplanev1.WorkerMessage{
		CorrelationId: uuid.New().String(),
		EmittedAt:     timestamppb.New(t.clock.Now()),
		Payload: &controlplanev1.WorkerMessage_Event{
			Event: &controlplanev1.Event{
				ServerId: event.ServerID,
				Event: &controlplanev1.Event_StatusChange{
					StatusChange: &controlplanev1.StatusChange{
						State:       mapServerState(event.State),
						Detail:      event.Detail,
						CrashReason: mapCrashReason(event.CrashReason),
					},
				},
			},
		},
	}
	if err := t.sendBounded(msg); err != nil {
		return fmt.Errorf("controlplane: send status change: %w", err)
	}
	return nil
}

func (t *transport) SendLogLine(_ context.Context, event session.LogEvent) error {
	msg := &controlplanev1.WorkerMessage{
		CorrelationId: uuid.New().String(),
		EmittedAt:     timestamppb.New(t.clock.Now()),
		Payload: &controlplanev1.WorkerMessage_Event{
			Event: &controlplanev1.Event{
				ServerId: event.ServerID,
				Event: &controlplanev1.Event_LogLine{
					LogLine: &controlplanev1.LogLine{
						Line:   event.Line,
						Stream: mapLogStream(event.Stream),
					},
				},
			},
		},
	}
	if err := t.sendBounded(msg); err != nil {
		return fmt.Errorf("controlplane: send log line: %w", err)
	}
	return nil
}

func (t *transport) SendMetrics(_ context.Context, event session.MetricsEvent) error {
	msg := &controlplanev1.WorkerMessage{
		CorrelationId: uuid.New().String(),
		EmittedAt:     timestamppb.New(t.clock.Now()),
		Payload: &controlplanev1.WorkerMessage_Event{
			Event: &controlplanev1.Event{
				ServerId: event.ServerID,
				Event: &controlplanev1.Event_Metrics{
					Metrics: &controlplanev1.Metrics{
						CpuMillis:   event.CPUMillis,
						MemoryBytes: event.MemoryBytes,
						PlayerCount: event.PlayerCount,
					},
				},
			},
		},
	}
	if err := t.sendBounded(msg); err != nil {
		return fmt.Errorf("controlplane: send metrics: %w", err)
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

// Close half-closes sending, then cancels the stream to unblock any pending Recv.
func (t *transport) Close() error {
	err := t.stream.CloseSend()
	t.cancel()
	return err
}

// classify marks credential and protocol refusals terminal; other failures reconnect.
// err must be non-nil.
func classify(err error) error {
	switch status.Code(err) {
	case codes.Unauthenticated, codes.PermissionDenied, codes.FailedPrecondition, codes.InvalidArgument:
		return fmt.Errorf("%w: %w", session.ErrTerminal, err)
	default:
		return err
	}
}

// sendBounded cancels a stalled stream and returns a transient reconnect error.
// The CAS selects one winner between completion and timeout, even with a buffered timer tick.
func (t *transport) sendBounded(msg *controlplanev1.WorkerMessage) error {
	var settled atomic.Bool
	deadline := t.clock.NewTimer(sendStallTimeout)
	defer deadline.Stop()
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-deadline.C():
			if settled.CompareAndSwap(false, true) {
				t.cancel()
			}
		case <-done:
		}
	}()
	if err := t.stream.Send(msg); err != nil {
		return err
	}
	if !settled.CompareAndSwap(false, true) {
		return fmt.Errorf("send stalled: not completed within %s", sendStallTimeout)
	}
	return nil
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
		case "container":
			out = append(out, controlplanev1.ExecutionDriverKind_EXECUTION_DRIVER_KIND_CONTAINER)
		default:
			out = append(out, controlplanev1.ExecutionDriverKind_EXECUTION_DRIVER_KIND_UNSPECIFIED)
		}
	}
	return out
}

// mapHeldServers translates the held working sets to the wire HeldServer messages the API reads on Register.
func mapHeldServers(held []session.HeldServer) []*controlplanev1.HeldServer {
	out := make([]*controlplanev1.HeldServer, 0, len(held))
	for _, h := range held {
		out = append(out, &controlplanev1.HeldServer{
			ServerId:   h.ServerID,
			Generation: h.Generation,
		})
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
	case session.CommandErrorTransferFailed:
		return controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_TRANSFER_FAILED
	case session.CommandErrorFileAccessDenied:
		return controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_FILE_ACCESS_DENIED
	case session.CommandErrorPortConflict:
		return controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_PORT_CONFLICT
	case session.CommandErrorImageMissing:
		return controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_IMAGE_MISSING
	case session.CommandErrorBusy:
		return controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_BUSY
	default:
		return controlplanev1.CommandErrorCode_COMMAND_ERROR_CODE_INTERNAL
	}
}

// Unrecognized file-access reasons map to UNSPECIFIED.
func mapFileAccessReason(reason session.FileAccessReason) controlplanev1.FileAccessReason {
	switch reason {
	case session.FileAccessReasonIsADirectory:
		return controlplanev1.FileAccessReason_FILE_ACCESS_REASON_IS_A_DIRECTORY
	case session.FileAccessReasonNotADirectory:
		return controlplanev1.FileAccessReason_FILE_ACCESS_REASON_NOT_A_DIRECTORY
	case session.FileAccessReasonSymlinkRefused:
		return controlplanev1.FileAccessReason_FILE_ACCESS_REASON_SYMLINK_REFUSED
	case session.FileAccessReasonPayloadTooLarge:
		return controlplanev1.FileAccessReason_FILE_ACCESS_REASON_PAYLOAD_TOO_LARGE
	default:
		return controlplanev1.FileAccessReason_FILE_ACCESS_REASON_UNSPECIFIED
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
	case "unknown":
		// UNKNOWN must reach the API; UNSPECIFIED is dropped and would leave a stale observed state.
		return controlplanev1.ServerState_SERVER_STATE_UNKNOWN
	default:
		return controlplanev1.ServerState_SERVER_STATE_UNSPECIFIED
	}
}

// Empty or unrecognized crash reasons map to UNSPECIFIED.
func mapCrashReason(reason string) controlplanev1.CrashReason {
	switch reason {
	case "forge_install_failed":
		return controlplanev1.CrashReason_CRASH_REASON_FORGE_INSTALL_FAILED
	case "forge_install_out_of_memory":
		return controlplanev1.CrashReason_CRASH_REASON_FORGE_INSTALL_OUT_OF_MEMORY
	case "forge_install_java_incompatible":
		return controlplanev1.CrashReason_CRASH_REASON_FORGE_INSTALL_JAVA_INCOMPATIBLE
	default:
		return controlplanev1.CrashReason_CRASH_REASON_UNSPECIFIED
	}
}

// mapLogStream translates a domain log stream to the wire LogStream enum.
func mapLogStream(stream session.LogStream) controlplanev1.LogStream {
	switch stream {
	case session.LogStreamStderr:
		return controlplanev1.LogStream_LOG_STREAM_STDERR
	default:
		return controlplanev1.LogStream_LOG_STREAM_STDOUT
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
		out.LaunchMode = launchModeName(c.Start.GetLaunchMode())
		out.JarRelpath = c.Start.GetJarRelpath()
		out.MinecraftVersion = c.Start.GetMinecraftVersion()
		out.MemoryLimitBytes = c.Start.GetMemoryLimitBytes()
		out.CPUMillis = c.Start.GetCpuMillis()
	case *controlplanev1.ApiCommand_Stop:
		out.Force = c.Stop.GetForce()
	case *controlplanev1.ApiCommand_ServerCommand:
		out.Line = c.ServerCommand.GetLine()
	case *controlplanev1.ApiCommand_Hydrate:
		out.TransferURL = c.Hydrate.GetTransferUrl()
		out.TransferToken = c.Hydrate.GetTransferToken()
	case *controlplanev1.ApiCommand_Snapshot:
		out.TransferURL = c.Snapshot.GetTransferUrl()
		out.TransferToken = c.Snapshot.GetTransferToken()
	case *controlplanev1.ApiCommand_ReadFile:
		out.Path = c.ReadFile.GetPath()
	case *controlplanev1.ApiCommand_EditFile:
		out.Path = c.EditFile.GetPath()
		out.Content = c.EditFile.GetContent()
	case *controlplanev1.ApiCommand_ListFiles:
		out.Path = c.ListFiles.GetPath()
	case *controlplanev1.ApiCommand_TunnelDial:
		out.TunnelEndpoint = c.TunnelDial.GetEndpoint()
		out.TunnelToken = c.TunnelDial.GetToken()
		out.TunnelCAPEM = c.TunnelDial.GetTlsCaPem()
	case *controlplanev1.ApiCommand_OpenBedrockTunnel:
		out.BedrockRelayEndpoint = c.OpenBedrockTunnel.GetRelayEndpoint()
		out.BedrockPort = c.OpenBedrockTunnel.GetBedrockPort()
		out.BedrockToken = c.OpenBedrockTunnel.GetToken()
		out.BedrockCAPEM = c.OpenBedrockTunnel.GetTlsCaPem()
	}
	return out
}

// toFileListing maps the domain listing onto the wire FileListing message.
func toFileListing(listing *session.FileListing) *controlplanev1.FileListing {
	entries := make([]*controlplanev1.FileEntry, 0, len(listing.Entries))
	for _, e := range listing.Entries {
		entries = append(entries, &controlplanev1.FileEntry{
			Name:  e.Name,
			IsDir: e.IsDir,
			Size:  e.Size,
		})
	}
	return &controlplanev1.FileListing{Entries: entries, Truncated: listing.Truncated}
}

// driverName maps the wire driver enum to the configured driver name used by the
// handler and capability config.
func driverName(kind controlplanev1.ExecutionDriverKind) string {
	switch kind {
	case controlplanev1.ExecutionDriverKind_EXECUTION_DRIVER_KIND_CONTAINER:
		return "container"
	default:
		return ""
	}
}

// Unset launch modes preserve the default JAR launch.
func launchModeName(mode controlplanev1.LaunchMode) string {
	switch mode {
	case controlplanev1.LaunchMode_LAUNCH_MODE_JAR:
		return "jar"
	case controlplanev1.LaunchMode_LAUNCH_MODE_FORGE_ARGSFILE:
		return "forge-argsfile"
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
	case *controlplanev1.ApiCommand_ListFiles:
		return "ListFiles"
	case *controlplanev1.ApiCommand_TunnelDial:
		return "TunnelDial"
	case *controlplanev1.ApiCommand_OpenBedrockTunnel:
		return "OpenBedrockTunnel"
	case *controlplanev1.ApiCommand_CloseBedrockTunnel:
		return "CloseBedrockTunnel"
	default:
		return "unknown"
	}
}
