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
	// HeldServers is the set of working sets this Worker already holds in its
	// persistent local scratch at registration, each tagged with the generation the
	// local working set is at (issue #763). The adapter maps it onto the wire
	// Register.held_servers; the API uses it to skip the destructive hydrate on a
	// same-worker restart ONLY when the held generation is fresh enough (a hydrate
	// would clobber the Worker's live, newer working set with the last authoritative
	// snapshot). It generalizes the presence-only HeldServerIDs of issue #696.
	HeldServers []HeldServer
	// Resources advertises the host's hardware resources (CPU cores and total
	// memory) for the API's placement logic (FR-WRK-3, issue #1218). When
	// MemoryBytes is 0 the API skips the memory hard-gate, so this must be
	// populated from the actual host values at startup.
	Resources HostResources
}

// HostResources is a coarse description of a Worker host's hardware resources,
// used by the API's placement logic to enforce memory/CPU gates (issue #1218).
type HostResources struct {
	// CPUCores is the number of logical CPUs available to the Worker.
	CPUCores uint32
	// MemoryBytes is the total physical memory available to the Worker.
	MemoryBytes uint64
}

// HeldServer is one working set this Worker holds in local scratch, with the
// generation it is at (issue #763). The adapter maps it onto the wire HeldServer
// message; the generation is the authoritative store generation the working set
// was last hydrated from or snapshotted to.
type HeldServer struct {
	ServerID   string
	Generation uint64
}

// RegisterAck is the API's answer to a Register (CONTROL_PLANE.md Section 4.1).
type RegisterAck struct {
	Accepted          bool
	HeartbeatInterval time.Duration
	// TransferDeadline bounds a single data-plane transfer (snapshot upload /
	// hydrate download) Worker-side (issue #874). The API derives it from its
	// hydrate/snapshot budgets plus a margin, so it is always >= the API budget:
	// the API-side timeout fires first and this is the cleanup backstop that
	// closes the unbounded-upload case (#869). A non-positive value (an older
	// API that does not set the field) leaves the transfer unbounded as before.
	TransferDeadline time.Duration
	RejectionReason  string
	// UnknownHeldServerIDs is the subset of Register.held_servers whose server
	// no longer exists in the API (deleted while the scratch was live, issue
	// #924). The Worker reclaims the scratch dir and .hydrate-<id>-* leftovers
	// for each id listed here. .displaced-<id> trees are NOT reclaimed (issue
	// #911). An empty list (or an older API that does not set the field) means
	// nothing to reclaim.
	UnknownHeldServerIDs []string
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
	// LaunchMode is the requested launch shape for StartServer (issue #305). It is
	// the wire LaunchMode name ("jar" / "forge-argsfile"); empty (an unset field)
	// is treated as the JAR launch, the historical behavior.
	LaunchMode string
	// JarRelpath is the server JAR path for StartServer (relative to the working
	// set).
	JarRelpath string
	// MinecraftVersion drives Java runtime selection for StartServer.
	MinecraftVersion string
	// MemoryLimitBytes is the per-server memory ceiling for StartServer (the
	// operator-declared limit, issue #706). 0 means unset — the driver picks a
	// default heap. The instance manager converts it to MiB for the InstanceSpec.
	MemoryLimitBytes uint64
	// CPUMillis is the per-server CPU allocation in millicores for StartServer (the
	// operator-declared soft share, issue #723). 0 means unset — the driver applies
	// its default weight. Carried as-is onto the InstanceSpec; no derivation.
	CPUMillis uint32
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
	// TunnelEndpoint is the relay tunnel endpoint to dial for a TunnelDial,
	// host:port (RELAY.md Section 5).
	TunnelEndpoint string
	// TunnelToken is the single-use session token presented to the relay after the
	// TLS handshake for a TunnelDial (RELAY.md Section 5).
	TunnelToken string
	// TunnelCAPEM is the optional PEM CA bundle to verify the relay's tunnel
	// certificate for a TunnelDial; empty means system roots (RELAY.md Section 5).
	TunnelCAPEM string
	// BedrockRelayEndpoint is the relay's Bedrock tunnel endpoint to dial for an
	// OpenBedrockTunnel, host:port (docs/app/BEDROCK_TUNNEL.md Section 3).
	BedrockRelayEndpoint string
	// BedrockPort is the public UDP port the relay binds for this server for an
	// OpenBedrockTunnel (docs/app/BEDROCK_TUNNEL.md Section 3).
	BedrockPort uint32
	// BedrockToken is the credential presented on the Worker's QUIC dial-out for
	// an OpenBedrockTunnel; unlike TunnelToken it is valid for the whole tunnel
	// lifetime, not single-use (docs/app/BEDROCK_TUNNEL.md Section 3).
	BedrockToken string
	// BedrockCAPEM is the optional PEM CA bundle to verify the relay's Bedrock
	// tunnel QUIC certificate for an OpenBedrockTunnel; empty means system roots
	// (docs/app/BEDROCK_TUNNEL.md Section 4).
	BedrockCAPEM string
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
	// FileAccessReason refines a CommandErrorFileAccessDenied failure into the
	// specific condition (is-a-directory, symlink refusal, oversized payload,
	// etc.) so the API surfaces an honest problem reason rather than collapsing
	// every file denial into "invalid path" (issue #548). It is meaningful only
	// when ErrorCode is CommandErrorFileAccessDenied; the zero value
	// (FileAccessReasonUnspecified) is the generic path denial.
	FileAccessReason FileAccessReason
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
	// CommandErrorBusy marks a command refused because another mutating lifecycle
	// command is already in flight for the id (the reservation race, issue #824).
	// Distinct from CommandErrorInvalidState: the in-flight command's outcome is
	// not yet known, so the API must keep its assignment/intent and retry on a
	// later tick rather than converge an observed state.
	CommandErrorBusy
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
	case CommandErrorBusy:
		return "busy"
	default:
		return fmt.Sprintf("CommandErrorCode(%d)", int(c))
	}
}

// FileAccessReason refines a CommandErrorFileAccessDenied failure (issue #548).
// The Worker emits FileAccessDenied for several conditions that are NOT
// path-syntax problems; this value carries which one so the adapter sets the
// wire FileAccessReason and the API maps each to an honest problem reason. The
// adapter maps each value to the generated enum.
type FileAccessReason int

const (
	// FileAccessReasonUnspecified is the zero value: a generic path denial (a
	// traversal-unsafe path, or a resolution refusal that is not one of the
	// refined cases below). The API maps it to 422 invalid_path.
	FileAccessReasonUnspecified FileAccessReason = iota
	// FileAccessReasonIsADirectory marks a read/edit whose path is a directory.
	FileAccessReasonIsADirectory
	// FileAccessReasonNotADirectory marks a list whose path is a regular file.
	FileAccessReasonNotADirectory
	// FileAccessReasonSymlinkRefused marks a refused final/intermediate symlink.
	FileAccessReasonSymlinkRefused
	// FileAccessReasonPayloadTooLarge marks an oversized read result or edit
	// payload (the control-plane file cap).
	FileAccessReasonPayloadTooLarge
)

// String renders the file-access reason as a stable name for logs.
func (r FileAccessReason) String() string {
	switch r {
	case FileAccessReasonUnspecified:
		return "unspecified"
	case FileAccessReasonIsADirectory:
		return "is_a_directory"
	case FileAccessReasonNotADirectory:
		return "not_a_directory"
	case FileAccessReasonSymlinkRefused:
		return "symlink_refused"
	case FileAccessReasonPayloadTooLarge:
		return "payload_too_large"
	default:
		return fmt.Sprintf("FileAccessReason(%d)", int(r))
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

// TransferDeadlineSetter is an optional CommandHandler capability: the session
// pushes the RegisterAck's data-plane transfer bound onto the handler after
// registration so it can apply a per-transfer deadline (issue #874). It is a
// separate interface because the bound arrives from the ack, not on each
// command; a handler that does not implement it simply runs transfers unbounded.
type TransferDeadlineSetter interface {
	// SetTransferDeadline records the bound for one data-plane transfer (snapshot
	// upload / hydrate download). A non-positive value leaves transfers unbounded.
	SetTransferDeadline(d time.Duration)
}

// ScratchReclaimer is an optional CommandHandler capability: after registration
// the session hands the handler the list of held server ids the API reports as
// deleted (issue #924). The handler reclaims the scratch dir and hydrate
// leftovers for each id, but NOT .displaced-<id> trees (issue #911).
type ScratchReclaimer interface {
	// ReclaimDeletedScratches removes scratch dirs for server ids the API
	// confirmed no longer exist. It runs asynchronously and must not block
	// heartbeats or command dispatch.
	ReclaimDeletedScratches(serverIDs []string)
}

// StatusResyncer is an optional CommandHandler capability: after a successful
// (re-)register the session asks the handler to re-emit the current state of
// every instance it still holds (issue #985). On an API restart the worker
// process stays alive with its servers running, but the API resets their
// observed state to unknown on boot; without this re-emit they sit at unknown
// for the full reconciler grace window and the relay treats them as stopped.
// Re-emitting moves them out of unknown within seconds. A handler that does not
// implement it simply skips the resync (the prior behavior).
type StatusResyncer interface {
	// ResyncStatus re-emits a StatusChange for each currently-held instance. It
	// must be a no-op when the handler holds no instances (e.g. a fresh process).
	ResyncStatus()
}

// HeldServerProvider is an optional CommandHandler capability: before each
// registration the session asks the handler for the current held-server
// inventory so re-registrations advertise fresh generations instead of the
// stale boot-time snapshot (issue #1711). A handler that does not implement it
// keeps the original caps unchanged (the prior behavior).
type HeldServerProvider interface {
	// HeldServers returns the working sets this Worker currently holds in its
	// local scratch, each tagged with its recorded generation.
	HeldServers() []HeldServer
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
	// NewTimer returns a persistent, resettable timer (like time.NewTimer). The
	// heartbeat deadline uses it so the cadence stays independent of event traffic:
	// the timer is armed once and reset only after a heartbeat is sent, rather than
	// re-armed via After on every select iteration (issue #341).
	NewTimer(d time.Duration) Timer
}

// Timer is a single-shot deadline that can be re-armed, mirroring time.Timer.
// It is the seam that keeps the heartbeat cadence deterministic under a fake
// clock while staying independent of inbound event traffic (issue #341).
type Timer interface {
	// C is the channel on which the tick is delivered when the deadline elapses.
	// Unlike After, the channel is stable across Reset.
	C() <-chan time.Time
	// Reset re-arms the timer to fire after d. It is called only after a heartbeat
	// is sent, so other message types never re-arm the deadline.
	Reset(d time.Duration)
	// Stop halts the timer when the session tears down.
	Stop()
}
