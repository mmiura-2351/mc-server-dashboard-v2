// Package execution defines backend-neutral lifecycle, control, and telemetry contracts.
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

// CrashReason mirrors the wire enum for explained crashes; zero leaves the transition unclassified.
type CrashReason int

const (
	// CrashReasonUnspecified is an unclassified crash (a runtime crash of the
	// server process).
	CrashReasonUnspecified CrashReason = iota
	// CrashReasonForgeInstallFailed is a failed supervised Forge install that is
	// not one of the specific reasons below.
	CrashReasonForgeInstallFailed
	// CrashReasonForgeInstallOutOfMemory is a Forge install that ran out of
	// memory: OOM-killed at the memory limit, or out of Java heap.
	CrashReasonForgeInstallOutOfMemory
	// CrashReasonForgeInstallJavaIncompatible is a Forge installer that cannot run
	// on the Java runtime selected for the server (FR-EXE-5).
	CrashReasonForgeInstallJavaIncompatible
)

// String renders a CrashReason as its wire name; unspecified is the empty string.
func (r CrashReason) String() string {
	switch r {
	case CrashReasonForgeInstallFailed:
		return "forge_install_failed"
	case CrashReasonForgeInstallOutOfMemory:
		return "forge_install_out_of_memory"
	case CrashReasonForgeInstallJavaIncompatible:
		return "forge_install_java_incompatible"
	default:
		return ""
	}
}

// LaunchMode comes from the command, never inferred from scratch contents; zero selects JAR launch.
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

// InstanceSpec is backend-neutral; hydrate prepares WorkingDir before launch.
type InstanceSpec struct {
	// ServerID is the API's identifier for the server, used to scope status
	// events and to key the instance in the manager.
	ServerID string
	// WorkingDir is the absolute path to the server's working set, the process
	// working directory (CONFIGURATION.md worker.scratch_dir root).
	WorkingDir string
	// MinecraftVersion drives Java runtime selection (FR-EXE-5) and the charset server.properties is read in for
	// RCON.
	MinecraftVersion string
	// JarRelpath is the server JAR path relative to WorkingDir (StartServer
	// carries it; the API ships the JAR via hydrate, ARCHITECTURE.md Section 7.3).
	// In LaunchModeForgeArgsfile it is the Forge installer JAR used for the
	// supervised install step.
	JarRelpath string
	// LaunchMode selects the launch command shape (JAR vs Forge args file). The zero value is the historical JAR
	// launch.
	LaunchMode LaunchMode
	// MemoryLimitMB is the total ceiling, including JVM headroom; zero leaves the heap unset.
	MemoryLimitMB uint32
	// CPUMillis is a relative CPU share in millicores, not a hard quota; zero uses the driver default.
	CPUMillis uint32
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
	// CrashReason classifies a StateCrashed transition the driver can explain;
	// maps to StatusChange.crash_reason. Zero for every other transition.
	CrashReason CrashReason
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

// LogEvent is transient console output forwarded to the control plane; the Worker does not persist it.
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

// LogSource is optional; its channel closes when no further instance logs will arrive.
type LogSource interface {
	Logs() <-chan LogEvent
}

// StatsSource is optional; unavailable fields stay zero and a sample error makes the manager emit up-only
// metrics.
type StatsSource interface {
	// Sample reads the instance's current resource usage. An error means no
	// honest measurement was available this tick.
	Sample(ctx context.Context) (MetricsSample, error)
}

// ExecutionDriver starts backend-specific Instance handles that own lifecycle and status.
// The Port name is fixed by the execution contract.
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
	// Stop confirms termination or returns an error; a graceful stop runs at most one preFallback callback.
	// The callback has a separate budget; true skips shutdown saves and uses SIGKILL to preserve flushed files.
	Stop(ctx context.Context, graceful bool, preFallback ...func(context.Context) bool) error
	// Status reports the last observed state.
	Status() ServerState
	// ProbeAlive observes the process directly, independently of cached status; errors mean unknown.
	// It performs one probe; the caller owns retries.
	ProbeAlive(ctx context.Context) (alive bool, err error)
	// Events streams state transitions for this instance until it terminates.
	Events() <-chan StatusEvent
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

// ErrNoRuntime is returned by the container driver's image selector when no
// configured image matches the Java major the requested Minecraft version needs.
var ErrNoRuntime = errors.New("execution: no Java runtime for Minecraft version")

// ErrUnknownServer is returned by the instance manager when a command targets a
// server it is not running.
var ErrUnknownServer = errors.New("execution: unknown server")

// ErrInvalidState is returned when a command is invalid for the instance's
// current state (e.g. start an already-running server).
var ErrInvalidState = errors.New("execution: invalid state for command")

// ErrPortConflict is wrapped into a driver's Start error when a server could not be launched because a host port
// it must publish is already in use. The instance manager matches it with errors.Is to emit a sanitized
// port_conflict failure code instead of the generic internal one.
var ErrPortConflict = errors.New("execution: host port already in use")

// ErrImageMissing is wrapped into a driver's Start error when a server could not be launched because its
// container image is absent and could not be pulled. The instance manager matches it with errors.Is to emit a
// sanitized image_missing failure code instead of the generic internal one.
var ErrImageMissing = errors.New("execution: container image missing")
