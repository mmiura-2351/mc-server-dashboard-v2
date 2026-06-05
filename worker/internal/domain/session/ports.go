package session

import (
	"context"
	"fmt"
	"time"
)

// Capabilities is what the Worker advertises at registration (FR-WRK-1,
// CONTROL_PLANE.md Section 4.1). It is a domain value; the adapter maps it onto
// the wire WorkerCapabilities message.
type Capabilities struct {
	WorkerID      string
	WorkerVersion string
	Drivers       []string
	MaxServers    uint32
}

// RegisterAck is the API's answer to a Register (CONTROL_PLANE.md Section 4.1).
type RegisterAck struct {
	Accepted          bool
	HeartbeatInterval time.Duration
	RejectionReason   string
}

// Command is an inbound API command, reduced to the fields the session and its
// CommandHandler need (CONTROL_PLANE.md Section 5). The lifecycle commands
// (StartServer/StopServer/RestartServer/ServerCommand) and the file commands
// (ReadFile/EditFile) are dispatched to the handler; Hydrate/Snapshot run their
// data-plane transfer. An unset command oneof is answered with an "unsupported"
// CommandResult, never silently dropped.
type Command struct {
	CommandID string
	ServerID  string
	// Kind is the command name, both for logging and for dispatch
	// (e.g. "StartServer"); empty when the command oneof is unset.
	Kind string
	// Driver is the requested execution backend for StartServer.
	Driver string
	// JarRelpath is the server JAR path for StartServer (relative to the working
	// set).
	JarRelpath string
	// MinecraftVersion drives Java runtime selection for StartServer.
	MinecraftVersion string
	// Force skips the graceful path for StopServer.
	Force bool
	// Line is the console/RCON line for ServerCommand.
	Line string
	// TransferURL addresses the API HTTP data plane for a HydrateTrigger /
	// SnapshotTrigger; the bulk bytes move there, off this stream (Section 5).
	TransferURL string
	// TransferToken is the short-lived credential authorizing one transfer.
	TransferToken string
	// Path is the working-set-relative path for ReadFile / EditFile (Section 7.2).
	Path string
	// Content is the bytes to write for EditFile.
	Content []byte
}

// CommandResult answers a Command. A failure carries an ErrorCode and message;
// a ServerCommand success carries Output, a ReadFile success carries
// FileContent, and a ListFiles success carries FileListing
// (CONTROL_PLANE.md Section 7).
type CommandResult struct {
	CommandID    string
	Success      bool
	Output       string
	FileContent  []byte
	FileListing  *FileListing
	ErrorCode    CommandErrorCode
	ErrorMessage string
}

// FileListing is a directory listing returned by a ListFiles command. Truncated
// is set when the directory held more entries than the per-listing cap and
// Entries was clipped to that cap.
type FileListing struct {
	Entries   []FileEntry
	Truncated bool
}

// FileEntry is one child of a listed directory. The shape mirrors the API's
// authoritative-Storage listing (name / is_dir / size) so a running-server
// listing unifies with an at-rest one.
type FileEntry struct {
	Name  string
	IsDir bool
	Size  uint64
}

// CommandErrorCode mirrors the wire CommandErrorCode classes the session can
// emit (CONTROL_PLANE.md Section 7); the adapter maps each to the generated enum.
type CommandErrorCode int

const (
	// CommandErrorInternal is the unclassified-failure code (also used for the
	// not-yet-supported commands).
	CommandErrorInternal CommandErrorCode = iota
	// CommandErrorServerNotFound marks a command targeting a server unknown to
	// this Worker.
	CommandErrorServerNotFound
	// CommandErrorInvalidState marks a command invalid for the current state.
	CommandErrorInvalidState
	// CommandErrorDriverUnavailable marks a requested driver this Worker does not
	// offer.
	CommandErrorDriverUnavailable
	// CommandErrorTransferFailed marks a failed hydrate/snapshot data-plane
	// transfer (CONTROL_PLANE.md Section 7).
	CommandErrorTransferFailed
	// CommandErrorFileAccessDenied marks a rejected file access: a traversal-unsafe
	// path or an oversized read/edit (FR-FILE-4, CONTROL_PLANE.md Section 7).
	CommandErrorFileAccessDenied
	// CommandErrorPortConflict marks a StartServer whose driver could not publish a
	// host port already in use (issue #225). The container driver classifies it
	// from the docker start error; the raw daemon text stays in Worker logs.
	CommandErrorPortConflict
	// CommandErrorImageMissing marks a StartServer whose driver could not find or
	// pull the server's container image (issue #225). The container driver
	// classifies it from the docker create error; the raw daemon text stays in
	// Worker logs.
	CommandErrorImageMissing
)

// String renders the error code as a stable name for logs (issue #194).
func (c CommandErrorCode) String() string {
	switch c {
	case CommandErrorInternal:
		return "internal"
	case CommandErrorServerNotFound:
		return "server_not_found"
	case CommandErrorInvalidState:
		return "invalid_state"
	case CommandErrorDriverUnavailable:
		return "driver_unavailable"
	case CommandErrorTransferFailed:
		return "transfer_failed"
	case CommandErrorFileAccessDenied:
		return "file_access_denied"
	case CommandErrorPortConflict:
		return "port_conflict"
	case CommandErrorImageMissing:
		return "image_missing"
	default:
		return fmt.Sprintf("CommandErrorCode(%d)", int(c))
	}
}

// StatusEvent is an observed server-state transition the session emits as a
// StatusChange event (CONTROL_PLANE.md Section 6). State is the wire state name
// (e.g. "running"); the adapter maps it to the generated ServerState enum.
type StatusEvent struct {
	ServerID string
	State    string
	Detail   string
}

// LogStream identifies which output stream a LogEvent came from; the adapter
// maps it to the wire LogStream enum.
type LogStream int

const (
	// LogStreamStdout is the server process's standard output.
	LogStreamStdout LogStream = iota
	// LogStreamStderr is the server process's standard error.
	LogStreamStderr
)

// LogEvent is one captured line of a server's console output the session emits
// as a LogLine event (FR-MON-2). Logs are transient relay-only at M1: the
// Worker streams them and does not store them (REQUIREMENTS.md Section 6.13).
type LogEvent struct {
	ServerID string
	Line     string
	Stream   LogStream
}

// MetricsEvent is a best-effort runtime sample the session emits as a Metrics
// event (FR-MON-3). A field the Worker cannot measure is zero; emitting at all
// signals the server is up.
type MetricsEvent struct {
	ServerID    string
	CPUMillis   uint32
	MemoryBytes uint64
	PlayerCount uint32
}

// CommandHandler executes the lifecycle/console commands and surfaces observed
// state transitions, log lines, and metrics. It is the application-layer
// instance manager; the session stays transport-neutral and forwards its
// results and events onto the stream.
type CommandHandler interface {
	// Handle executes cmd and returns the result keyed to cmd.CommandID. It must
	// not block on long-running work beyond the command's own semantics.
	Handle(ctx context.Context, cmd Command) CommandResult
	// Events streams observed state transitions for all managed servers. The
	// session forwards each as a StatusChange event.
	Events() <-chan StatusEvent
	// Logs streams captured console output for all managed servers. The session
	// forwards each as a LogLine event (FR-MON-2).
	Logs() <-chan LogEvent
	// Metrics streams periodic runtime samples for all running servers. The
	// session forwards each as a Metrics event (FR-MON-3).
	Metrics() <-chan MetricsEvent
}

// Transport is the Port over a single live control-plane stream. One Transport
// value corresponds to one open Session stream; the run loop discards it on any
// error and dials a fresh one to reconnect (CONTROL_PLANE.md Section 4.4). The
// adapters layer implements it over gRPC; tests implement it in memory.
type Transport interface {
	// SendRegister sends the opening Register message; it MUST be the first
	// thing sent on a fresh stream (CONTROL_PLANE.md Section 4.1).
	SendRegister(ctx context.Context, caps Capabilities) error
	// RecvRegisterAck blocks for the API's RegisterAck.
	RecvRegisterAck(ctx context.Context) (RegisterAck, error)
	// SendHeartbeat emits one Heartbeat event (CONTROL_PLANE.md Section 4.3).
	SendHeartbeat(ctx context.Context) error
	// SendCommandResult replies to an inbound command.
	SendCommandResult(ctx context.Context, result CommandResult) error
	// SendStatusChange emits one StatusChange event (CONTROL_PLANE.md Section 6).
	SendStatusChange(ctx context.Context, event StatusEvent) error
	// SendLogLine emits one LogLine event (FR-MON-2).
	SendLogLine(ctx context.Context, event LogEvent) error
	// SendMetrics emits one Metrics event (FR-MON-3).
	SendMetrics(ctx context.Context, event MetricsEvent) error
	// RecvCommand blocks for the next inbound API command. It returns an error
	// when the stream ends (clean close or transport failure).
	RecvCommand(ctx context.Context) (Command, error)
	// Close releases the stream.
	Close() error
}

// Dialer opens a fresh Transport (one Session stream). The run loop calls it on
// startup and on every reconnect.
type Dialer interface {
	Dial(ctx context.Context) (Transport, error)
}

// Clock is the injectable time source so the heartbeat cadence and backoff are
// tested with a fake clock rather than wall time (TESTING.md Section 4).
type Clock interface {
	// Now reports the current time.
	Now() time.Time
	// After returns a channel that fires once after d elapses, like time.After.
	After(d time.Duration) <-chan time.Time
}
