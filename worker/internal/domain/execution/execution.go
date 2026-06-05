// Package execution holds the Worker's execution-backend core: the
// ExecutionDriver Port that realizes logical start/stop for a server instance,
// the JavaRuntimeSelector and ServerControl Ports, and the value types they
// exchange. It depends on the standard library only (ARCHITECTURE.md Section 2,
// Section 5.2). Concrete drivers (host process, container, future k8s) live in
// the adapters layer and implement these interfaces, so the application and
// session layers never know which backend runs a server (FR-EXE-1, FR-EXE-4).
package execution

import (
	"context"
	"errors"
)

// ServerState is the observed runtime state of a server instance. It mirrors the
// wire ServerState (CONTROL_PLANE.md Section 6); the session adapter maps it onto
// the generated enum. The driver reports these; the API holds desired state.
type ServerState int

const (
	// StateStarting is the launch-in-progress state, reported the moment a Start
	// is accepted and before the process is confirmed running.
	StateStarting ServerState = iota
	// StateRunning is the steady state of a live server process.
	StateRunning
	// StateStopping is the graceful-shutdown-in-progress state.
	StateStopping
	// StateStopped is a clean, intentional exit.
	StateStopped
	// StateCrashed is an unexpected exit: the process died without an operator
	// stop request (FR-SRV-4).
	StateCrashed
)

// String renders a ServerState for logs and StatusEvent.Detail context.
func (s ServerState) String() string {
	switch s {
	case StateStarting:
		return "starting"
	case StateRunning:
		return "running"
	case StateStopping:
		return "stopping"
	case StateStopped:
		return "stopped"
	case StateCrashed:
		return "crashed"
	default:
		return "unknown"
	}
}

// LaunchMode selects how a driver builds a server's launch command (issue #305).
// It is carried explicitly on the start command, never inferred from the
// working-set contents. The zero value (LaunchModeJar) is the historical
// behavior (a `java -jar <jar> nogui` launch), so an unset launch mode launches
// exactly as before this field existed.
type LaunchMode int

const (
	// LaunchModeJar runs the server JAR directly: `java -jar <jar> nogui`. It is
	// the zero value, so an unspecified launch mode is byte-for-byte the original
	// launch (vanilla, Paper, etc.).
	LaunchModeJar LaunchMode = iota
	// LaunchModeForgeArgsfile launches Forge via its generated unix args file
	// (libraries/net/minecraftforge/forge/*/unix_args.txt). When that args file is
	// absent the working set is uninstalled, so the driver first runs the installer
	// (`java -jar <jar> --installServer`) as a supervised phase of the start.
	LaunchModeForgeArgsfile
)

// InstanceSpec is everything a driver needs to launch one server instance. It is
// backend-neutral: a host-process, container, or future k8s driver all consume
// the same spec (FR-EXE-4). The working set under WorkingDir is prepared by the
// hydrate path (epic #8); this milestone runs against an empty/pre-seeded dir.
type InstanceSpec struct {
	// ServerID is the API's identifier for the server, used to scope status
	// events and to key the instance in the manager.
	ServerID string
	// WorkingDir is the absolute path to the server's working set, the process
	// working directory (CONFIGURATION.md worker.scratch_dir root).
	WorkingDir string
	// MinecraftVersion drives Java runtime selection (FR-EXE-5).
	MinecraftVersion string
	// JarRelpath is the server JAR path relative to WorkingDir (StartServer
	// carries it; the API ships the JAR via hydrate, ARCHITECTURE.md Section 7.3).
	// In LaunchModeForgeArgsfile it is the Forge installer JAR used for the
	// supervised install step.
	JarRelpath string
	// LaunchMode selects the launch command shape (JAR vs Forge args file). The
	// zero value is the historical JAR launch (issue #305).
	LaunchMode LaunchMode
	// MemoryMB is the JVM heap size in mebibytes; 0 lets the driver pick a
	// proportionate default.
	MemoryMB uint32
}

// StatusEvent is an observed state transition for a server instance. The
// instance manager forwards these onto the control plane as StatusChange events
// (CONTROL_PLANE.md Section 6).
type StatusEvent struct {
	ServerID string
	State    ServerState
	// Detail optionally explains the transition (e.g. a crash reason); maps to
	// StatusChange.detail.
	Detail string
}

// LogStream identifies which output stream a LogLine came from (mirrors the wire
// LogStream enum, proto LogLine).
type LogStream int

const (
	// LogStreamStdout is the process's standard output.
	LogStreamStdout LogStream = iota
	// LogStreamStderr is the process's standard error.
	LogStreamStderr
)

// LogEvent is one captured line of a server's console output (FR-MON-2). The
// instance manager forwards these onto the control plane as LogLine events.
// Logs are transient relay-only at M1: the Worker streams them and does not
// store them (REQUIREMENTS.md Section 6.13).
type LogEvent struct {
	ServerID string
	Line     string
	Stream   LogStream
}

// MetricsSample is a best-effort runtime measurement for a running server
// (FR-MON-3). Fields a driver cannot measure cheaply are left zero; emitting a
// sample at all signals the server is up. The instance manager forwards these
// as Metrics events.
type MetricsSample struct {
	ServerID    string
	CPUMillis   uint32
	MemoryBytes uint64
	PlayerCount uint32
}

// LogSource is an optional capability an Instance may implement to stream its
// captured console output. The instance manager type-asserts it; a driver that
// cannot capture logs simply does not implement it (keeping the core Instance
// interface unchanged, FR-EXE-4). The channel closes when the instance
// terminates and no further lines will arrive.
type LogSource interface {
	Logs() <-chan LogEvent
}

// StatsSource is an optional capability an Instance may implement to report a
// one-shot metrics sample on demand. The instance manager polls it on the
// configured interval while the instance runs. A driver that cannot measure a
// given stat cheaply leaves it zero; one that cannot measure any returns an
// error and the manager emits an up-only sample.
type StatsSource interface {
	// Sample reads the instance's current resource usage. An error means no
	// honest measurement was available this tick.
	Sample(ctx context.Context) (MetricsSample, error)
}

// ExecutionDriver realizes logical lifecycle operations for one execution
// backend (FR-EXE-1). Start launches an instance and returns a handle; the
// handle owns stop, status, and the crash-notification stream. Keeping per-
// instance operations on the returned Instance (rather than driver methods keyed
// by server id) lets a container or k8s driver hold backend-specific handle
// state without changing this interface (FR-EXE-4).
//
// The ExecutionDriver name is the contract term fixed by REQUIREMENTS.md
// FR-EXE-1 and ARCHITECTURE.md Section 5.2; the documented Port name is kept
// despite the package-stutter lint.
type ExecutionDriver interface { //nolint:revive // documented Port name (FR-EXE-1)
	// Start launches the server described by spec and returns its Instance. It
	// errors if the instance cannot be launched (e.g. no Java runtime, spawn
	// failure); a successful return means the process was spawned and is
	// transitioning through StateStarting.
	Start(ctx context.Context, spec InstanceSpec) (Instance, error)
}

// Instance is a launched server handle. Its Events channel delivers state
// transitions including the crash notification (process exit → StateCrashed
// unless a Stop is in flight). The channel closes when the instance reaches a
// terminal state and no further events will arrive.
type Instance interface {
	// Stop ends the instance. A graceful stop tries the in-band shutdown (RCON
	// "stop") and falls back to signals; a non-graceful stop forces termination.
	// It returns once the process has exited or the stop deadline elapsed.
	Stop(ctx context.Context, graceful bool) error
	// Status reports the last observed state.
	Status() ServerState
	// Events streams state transitions for this instance until it terminates.
	Events() <-chan StatusEvent
}

// JavaRuntimeSelector picks the local Java runtime path for a server's Minecraft
// version (FR-EXE-5). Selection is the Worker's concern, never the API's
// (ARCHITECTURE.md Section 7.3).
type JavaRuntimeSelector interface {
	// Select returns the absolute path to the java binary for mcVersion, or an
	// error if no configured runtime satisfies the version.
	Select(mcVersion string) (string, error)
}

// ServerControl is the RCON seam over a running server (ARCHITECTURE.md Section
// 5.2): forward console/RCON commands (FR-SRV-5) and issue save-all / stop for
// the graceful-stop path.
type ServerControl interface {
	// Execute sends one command line over RCON and returns the server's reply.
	Execute(ctx context.Context, line string) (string, error)
	// Close releases the RCON connection.
	Close() error
}

// ErrNoRuntime is returned by a JavaRuntimeSelector when no configured runtime
// matches the requested Minecraft version.
var ErrNoRuntime = errors.New("execution: no Java runtime for Minecraft version")

// ErrUnknownServer is returned by the instance manager when a command targets a
// server it is not running.
var ErrUnknownServer = errors.New("execution: unknown server")

// ErrInvalidState is returned when a command is invalid for the instance's
// current state (e.g. start an already-running server).
var ErrInvalidState = errors.New("execution: invalid state for command")

// ErrPortConflict is wrapped into a driver's Start error when a server could not
// be launched because a host port it must publish is already in use (issue
// #225). The instance manager matches it with errors.Is to emit a sanitized
// port_conflict failure code instead of the generic internal one.
var ErrPortConflict = errors.New("execution: host port already in use")

// ErrImageMissing is wrapped into a driver's Start error when a server could not
// be launched because its container image is absent and could not be pulled
// (issue #225). The instance manager matches it with errors.Is to emit a
// sanitized image_missing failure code instead of the generic internal one.
var ErrImageMissing = errors.New("execution: container image missing")
