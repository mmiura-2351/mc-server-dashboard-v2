// Package hostprocess implements the execution.ExecutionDriver Port by running
// the server's java process directly on the Worker host (FR-EXE-2a). Process
// spawning sits behind a small seam (the process interface and spawnFunc) so
// unit tests substitute a fake process and no real java/Minecraft runs in CI.
//
// Stop semantics (CONTROL_PLANE.md Section 5, ARCHITECTURE.md Section 5.2): a
// graceful stop prefers the in-band RCON "stop" command; if RCON is unavailable
// or fails it falls back to SIGTERM, then escalates to SIGKILL after the
// configured stop timeout. A forced stop skips RCON and signals directly.
//
// Readiness posture (M1): a successful spawn transitions starting→running
// immediately. Log-based "Done" readiness detection is deliberately out of scope
// for this milestone (it lands with log streaming, FR-MON-2).
package hostprocess

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// process is the supervision seam over a spawned OS process. The real adapter
// wraps *exec.Cmd; tests substitute a fake. Wait blocks until the process exits.
type process interface {
	Wait() error
	Signal(os.Signal) error
	Kill() error
}

// spawnFunc launches a process for the given command in dir. It is the test seam
// for process creation.
type spawnFunc func(ctx context.Context, name string, args []string, dir string) (process, error)

// controlFunc opens an execution.ServerControl (RCON) for an instance, used for
// the graceful-stop "stop" command. It returns an error when RCON is unavailable;
// the driver then falls back to signals.
type controlFunc func(ctx context.Context, spec execution.InstanceSpec) (execution.ServerControl, error)

// Options tunes the driver.
type Options struct {
	// StopTimeout bounds each escalation step of a graceful stop (RCON→SIGTERM,
	// SIGTERM→SIGKILL). Zero uses defaultStopTimeout.
	StopTimeout time.Duration
}

const defaultStopTimeout = 30 * time.Second

// Driver is the host-process ExecutionDriver.
type Driver struct {
	selector    execution.JavaRuntimeSelector
	spawn       spawnFunc
	openControl controlFunc
	stopTimeout time.Duration
}

// New builds a host-process Driver. selector picks the Java runtime; spawn is the
// process-creation seam; openControl opens RCON for graceful stop.
func New(selector execution.JavaRuntimeSelector, spawn spawnFunc, openControl controlFunc, opts Options) *Driver {
	timeout := opts.StopTimeout
	if timeout <= 0 {
		timeout = defaultStopTimeout
	}
	return &Driver{
		selector:    selector,
		spawn:       spawn,
		openControl: openControl,
		stopTimeout: timeout,
	}
}

// Start selects the Java runtime, spawns the server process in its working dir,
// and returns the running Instance. It emits starting then running; a successful
// return means the process was spawned.
func (d *Driver) Start(ctx context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	javaPath, err := d.selector.Select(spec.MinecraftVersion)
	if err != nil {
		return nil, fmt.Errorf("hostprocess: select java: %w", err)
	}

	args := javaArgs(spec)
	proc, err := d.spawn(ctx, javaPath, args, spec.WorkingDir)
	if err != nil {
		return nil, fmt.Errorf("hostprocess: spawn java: %w", err)
	}

	inst := &instance{
		spec:        spec,
		proc:        proc,
		openControl: d.openControl,
		stopTimeout: d.stopTimeout,
		events:      make(chan execution.StatusEvent, 8),
		exited:      make(chan struct{}),
		state:       execution.StateStarting,
	}
	inst.emit(execution.StateStarting, "")
	inst.set(execution.StateRunning)
	inst.emit(execution.StateRunning, "")

	go inst.supervise()
	return inst, nil
}

// javaArgs builds the JVM command line for a server. Heap flags are set from
// MemoryMB when provided; the JAR runs headless (nogui).
func javaArgs(spec execution.InstanceSpec) []string {
	var args []string
	if spec.MemoryMB > 0 {
		heap := fmt.Sprintf("%dM", spec.MemoryMB)
		args = append(args, "-Xms"+heap, "-Xmx"+heap)
	}
	args = append(args, "-jar", filepath.Join(spec.WorkingDir, spec.JarRelpath), "nogui")
	return args
}

// instance is one running host process.
type instance struct {
	spec        execution.InstanceSpec
	proc        process
	openControl controlFunc
	stopTimeout time.Duration

	events chan execution.StatusEvent
	// exited is closed by supervise once the process has reached a terminal
	// state (stopped or crashed); waitExit selects on it.
	exited chan struct{}

	mu       sync.Mutex
	state    execution.ServerState
	stopping bool
	closed   bool
}

func (i *instance) Status() execution.ServerState {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.state
}

func (i *instance) Events() <-chan execution.StatusEvent { return i.events }

// Stop ends the instance. A graceful stop tries RCON "stop", then SIGTERM, then
// SIGKILL after stopTimeout; a forced stop skips the RCON step.
func (i *instance) Stop(ctx context.Context, graceful bool) error {
	i.mu.Lock()
	// Any terminal state (stopped or crashed) makes Stop a no-op success: the
	// process is already gone, so signalling it would spin waitExit's timeout and
	// can surface a spurious Kill() error. This also covers a Stop racing the
	// crash-eviction window.
	if i.stopping || isTerminal(i.state) {
		i.mu.Unlock()
		return nil
	}
	i.stopping = true
	i.state = execution.StateStopping
	i.mu.Unlock()
	i.emit(execution.StateStopping, "")

	if graceful && i.tryRCONStop(ctx) && i.waitExit(ctx, i.stopTimeout) {
		return nil
	}

	_ = i.proc.Signal(syscall.SIGTERM)
	if i.waitExit(ctx, i.stopTimeout) {
		return nil
	}

	if err := i.proc.Kill(); err != nil {
		return fmt.Errorf("hostprocess: kill: %w", err)
	}
	i.waitExit(ctx, i.stopTimeout)
	return nil
}

// isTerminal reports whether s is a state the process can no longer leave.
func isTerminal(s execution.ServerState) bool {
	return s == execution.StateStopped || s == execution.StateCrashed
}

// tryRCONStop opens RCON and sends "stop", reporting whether the in-band stop was
// issued successfully. A failure (no RCON, send error) returns false so Stop
// falls back to signals.
func (i *instance) tryRCONStop(ctx context.Context) bool {
	ctrl, err := i.openControl(ctx, i.spec)
	if err != nil {
		return false
	}
	defer func() { _ = ctrl.Close() }()
	if _, err := ctrl.Execute(ctx, "stop"); err != nil {
		return false
	}
	return true
}

// waitExit reports whether the process reached a terminal state within d. The
// supervisor goroutine observes the actual exit and closes i.exited, so any
// terminal state (stopped or, when a process crashes mid-graceful-stop, crashed)
// satisfies the wait. It returns false if the stop timeout elapses or ctx is
// cancelled first.
func (i *instance) waitExit(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-i.exited:
		return true
	case <-ctx.Done():
		return false
	case <-timer.C:
		return false
	}
}

// supervise blocks on the process exit and emits the terminal state: stopped when
// a stop was requested, crashed otherwise (FR-SRV-4). It then closes the event
// stream.
func (i *instance) supervise() {
	waitErr := i.proc.Wait()

	i.mu.Lock()
	stopping := i.stopping
	i.mu.Unlock()

	if stopping {
		i.set(execution.StateStopped)
		i.emit(execution.StateStopped, "")
	} else {
		detail := "process exited unexpectedly"
		if waitErr != nil {
			detail = waitErr.Error()
		}
		i.set(execution.StateCrashed)
		i.emit(execution.StateCrashed, detail)
	}
	// Release any in-flight waitExit now the terminal state is set.
	close(i.exited)

	i.mu.Lock()
	i.closed = true
	close(i.events)
	i.mu.Unlock()
}

func (i *instance) set(s execution.ServerState) {
	i.mu.Lock()
	i.state = s
	i.mu.Unlock()
}

// emit publishes a status event, dropping it if the stream is closed or full so a
// slow consumer never blocks supervision.
func (i *instance) emit(state execution.ServerState, detail string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.closed {
		return
	}
	select {
	case i.events <- execution.StatusEvent{ServerID: i.spec.ServerID, State: state, Detail: detail}:
	default:
	}
}
