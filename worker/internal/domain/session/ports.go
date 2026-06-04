package session

import (
	"context"
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

// Command is an inbound API command, reduced to the fields the session needs to
// acknowledge its protocol shape. Real command handling is epic #7; this
// milestone only replies with an "unsupported" CommandResult (CONTROL_PLANE.md
// Section 5), never silently dropping a command.
type Command struct {
	CommandID string
	ServerID  string
	// Kind is a human-readable command name for the log line and the error
	// message (e.g. "StartServer"); empty when the command oneof is unset.
	Kind string
}

// CommandResult answers a Command. Until epic #7 every result is an
// "unsupported" error keyed to the originating CommandID (CONTROL_PLANE.md
// Section 7).
type CommandResult struct {
	CommandID    string
	Success      bool
	ErrorCode    CommandErrorCode
	ErrorMessage string
}

// CommandErrorCode mirrors the wire CommandErrorCode classes the session can
// emit. This milestone only emits Internal for the not-yet-supported commands;
// the adapter maps it to the generated enum.
type CommandErrorCode int

const (
	// CommandErrorInternal is the unclassified-failure code used for commands
	// this milestone does not yet implement (CONTROL_PLANE.md Section 7).
	CommandErrorInternal CommandErrorCode = iota
)

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
