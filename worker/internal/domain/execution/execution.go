// Package execution holds the Worker's execution-backend core: the
// ExecutionDriver Port that realizes logical start/stop for a server instance,
// the ServerControl Port, and the value types they exchange. It depends on the
// standard library only (ARCHITECTURE.md Section 2, Section 5.2). Concrete drivers
// (container, future k8s) live in the adapters layer and implement these
// interfaces, so the application and session layers never know which backend runs
// a server (FR-EXE-1, FR-EXE-4).
package execution

import (
	"context"
	"errors"
	"time"
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

// CrashReason classifies a StateCrashed transition a driver can explain (issue
// #1093). It mirrors the wire CrashReason (CONTROL_PLANE.md Section 6) and rides
// the StatusEvent beside the free-text Detail, so a client can show a specific
// message without parsing that text. The zero value is every crash a driver does
// not classify.
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
// backend-neutral: a now-removed host-process, container, or future k8s driver all consume
// the same spec (FR-EXE-4). The working set under WorkingDir is prepared by the
// hydrate path (epic #8); this milestone runs against an empty/pre-seeded dir.
type InstanceSpec struct {
	// ServerID is the API's identifier for the server, used to scope status
	// events and to key the instance in the manager.
	ServerID string
	// WorkingDir is the absolute path to the server's working set, the process
	// working directory (CONFIGURATION.md worker.scratch_dir root).
	WorkingDir string
	// MinecraftVersion drives Java runtime selection (FR-EXE-5) and the charset
	// server.properties is read in for RCON (issue #3116).
	MinecraftVersion string
	// JarRelpath is the server JAR path relative to WorkingDir (StartServer
	// carries it; the API ships the JAR via hydrate, ARCHITECTURE.md Section 7.3).
	// In LaunchModeForgeArgsfile it is the Forge installer JAR used for the
	// supervised install step.
	JarRelpath string
	// LaunchMode selects the launch command shape (JAR vs Forge args file). The
	// zero value is the historical JAR launch (issue #305).
	LaunchMode LaunchMode
	// MemoryLimitMB is the per-server memory ceiling in mebibytes (the
	// operator-declared limit carried from StartServer, issue #706). It is the
	// LIMIT, not the heap: the JVM heap (`-Xmx`) is derived from it (limit minus
	// headroom, see heapArgs), and the enforcement drivers (#707/#708) consume this
	// ceiling to cap the container/process. 0 means unset — the driver/JVM picks a
	// default heap, preserving the pre-#706 launch.
	MemoryLimitMB uint32
	// CPUMillis is the per-server CPU allocation in millicores (1000 = one core),
	// the operator-declared SOFT relative share carried from StartServer (issue
	// #723). Unlike MemoryLimitMB there is NO derivation: it is carried as-is and
	// the enforcement driver (#724) turns it into a relative CPUShares weight. 0
	// means unset — the driver applies its default weight.
	CPUMillis uint32
	// WorkingSetLock is this server's working-set lock, which a driver that
	// re-owns the working set holds while it walks the tree (issue #3371). A walk
	// can happen long after Start returned, which is why the lock travels with the
	// spec.
	WorkingSetLock *WorkingSetLock
	// WorldProgress fingerprints the server's on-disk world for a graceful stop's
	// wait (issue #3436): a fingerprint that keeps changing between polls is a
	// shutdown save still writing, so the driver waits instead of escalating to the
	// next signal. An error means the scan could not tell, which the driver counts
	// as progress (its hard cap still bounds the wait). Nil gives the driver nothing
	// to observe: it escalates after its idle window, as if nothing changed.
	WorldProgress func() (WorldProgress, error)
}

// WorldProgress is a cheap fingerprint of a world on disk: the count, total size
// and newest modification time of its region files (issue #3436). Every chunk
// write moves Newest (and a new chunk also Bytes), so two equal fingerprints mean
// nothing was written between them. It is built from metadata alone, never file
// contents.
type WorldProgress struct {
	Files  int
	Bytes  int64
	Newest time.Time
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
	// It returns once the process has exited or the stop deadline elapsed. A
	// graceful stop waits on each step while InstanceSpec.WorldProgress shows the
	// world still being written (issue #3436).
	//
	// preFallback, when supplied, is called always before stop on the graceful
	// path (before tryRCONStop and before SIGTERM) so the caller can flush
	// the live world to disk while the process is still alive. It is NOT called
	// on a force stop (graceful=false). At most one callback is accepted.
	// When the callback returns true the world data is fully flushed; Stop then
	// skips RCON "stop" and SIGTERM (both trigger MC's own shutdown save, which
	// can overwrite the flushed data if killed mid-write) and terminates the
	// process with SIGKILL so the flushed region files stay intact.
	// The callback gets its own budget, which runs before the stop deadline
	// starts rather than out of it, so a slow flush never shortens the shutdown
	// grace; the two are additive in the worst case (issue #2622).
	Stop(ctx context.Context, graceful bool, preFallback ...func(context.Context) bool) error
	// Status reports the last observed state.
	Status() ServerState
	// ProbeAlive reports whether the instance's underlying process/container is
	// currently alive, by direct observation (not cached state): neither Status
	// nor Events can answer it, since both Stop failure paths reset the cached
	// state to its pre-stop value and Events only carries transitions the driver
	// could confirm. err != nil means the answer is genuinely unavailable (e.g.
	// the backend daemon is unreachable) — never a guessed false. It is
	// single-shot: the caller owns the retry cadence.
	//
	// A core method rather than an optional capability (LogSource/StatsSource):
	// converging on a container that outlived a failed stop is a correctness
	// obligation of every driver, not an optional nicety (issue #2467).
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

// ErrConnBroken is returned by ServerControl.Execute when a prior round trip
// failed mid-stream (timeout or cancel), which can leave the connection
// mis-framed. The connection is poisoned on such a failure so reuse fails fast and
// forces a redial rather than reading a stale response off the broken stream.
var ErrConnBroken = errors.New("rcon: connection poisoned by a prior I/O error")

// ErrCommandDropped is returned by ServerControl.Execute when the server closed
// the connection (EOF or reset) after the command was written and before a single
// response byte arrived. That is how vanilla answers a request packet that did not
// arrive in one read: it drops the connection without running the command (issue
// #3366, CONTROL_PLANE.md Section 3.1). A server that ran the command and died
// before answering looks the same, so whether it ran stays uncertain. It is told
// apart from a hung server (a timeout), from a reply cut short (some bytes
// arrived), and from a failure to connect or authenticate.
var ErrCommandDropped = errors.New("rcon: server closed the connection without replying to the command")

// ErrNoRuntime is returned by the container driver's image selector when no
// configured image matches the Java major the requested Minecraft version needs.
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

// ErrServerPropertiesUnreadable is wrapped into the error of a read of a server's
// server.properties that refused the file itself: a symlink, a FIFO, socket,
// device or directory, or a file larger than the read cap (issue #3335). The
// server's own code can put any of those at the name. The instance manager
// matches it with errors.Is to emit a server_properties_unreadable failure code
// for every command the refusal fails (a start, a restart's relaunch, a console
// command, a running snapshot, a tunnel dial), so the user is told to restore a
// regular file instead of seeing a generic failure (issue #3347).
// An absent file is not this error.
var ErrServerPropertiesUnreadable = errors.New("execution: server.properties is not a regular file or is too large")
