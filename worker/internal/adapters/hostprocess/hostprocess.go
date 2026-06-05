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
	"io"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// process is the supervision seam over a spawned OS process. The real adapter
// wraps *exec.Cmd; tests substitute a fake. Wait blocks until the process exits.
// Stdout/Stderr return the process's output pipes for log capture (FR-MON-2);
// Pid is the OS pid for /proc-based metrics (FR-MON-3).
type process interface {
	Wait() error
	Signal(os.Signal) error
	Kill() error
	Stdout() io.Reader
	Stderr() io.Reader
	Pid() int
}

// logBufferLines bounds the per-instance captured-log buffer. Under heavier
// output the pump drops the oldest line and emits a dropped-count marker (issue
// #96 posture); 256 lines absorbs a normal startup burst without unbounded
// growth.
const logBufferLines = 256

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

// Start selects the Java runtime and launches the server process in its working
// dir, returning the running Instance. For a Forge args-file launch whose working
// set is not yet installed it spawns the supervised installer first and returns
// immediately; a supervisor goroutine runs the install to completion then execs
// the launch as the SAME instance, so the exit-watcher contract sees one instance
// throughout (issue #305). It emits starting then running (or crashed if the
// install fails); a successful return means the install or launch was spawned.
func (d *Driver) Start(ctx context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	javaPath, err := d.selector.Select(spec.MinecraftVersion)
	if err != nil {
		return nil, fmt.Errorf("hostprocess: select java: %w", err)
	}

	plan, err := execution.BuildLaunchPlan(spec, spec.WorkingDir, hostPathResolver(spec.WorkingDir))
	if err != nil {
		return nil, fmt.Errorf("hostprocess: plan launch: %w", err)
	}

	inst := &instance{
		spec:        spec,
		javaPath:    javaPath,
		spawn:       d.spawn,
		openControl: d.openControl,
		stopTimeout: d.stopTimeout,
		events:      make(chan execution.StatusEvent, 8),
		exited:      make(chan struct{}),
		state:       execution.StateStarting,
		logPump:     execution.NewLogPump(spec.ServerID, logBufferLines),
	}
	inst.emit(execution.StateStarting, "")

	if plan.NeedsInstall {
		// Supervised install phase: the installer runs to completion, then the
		// supervisor re-plans and launches as the SAME instance (issue #305).
		proc, err := d.spawn(ctx, javaPath, plan.InstallArgs, spec.WorkingDir)
		if err != nil {
			return nil, fmt.Errorf("hostprocess: spawn forge installer: %w", err)
		}
		inst.setProc(proc)
		go inst.superviseInstall(proc)
		return inst, nil
	}

	proc, err := d.spawn(ctx, javaPath, plan.LaunchArgs, spec.WorkingDir)
	if err != nil {
		return nil, fmt.Errorf("hostprocess: spawn java: %w", err)
	}
	inst.beginLaunch(proc)
	return inst, nil
}

// hostPathResolver maps working-set-relative paths onto host-absolute paths under
// workingDir and checks existence there, for execution.BuildLaunchPlan.
func hostPathResolver(workingDir string) execution.PathResolver {
	return execution.PathResolver{
		Resolve: func(rel string) string { return filepath.Join(workingDir, filepath.FromSlash(rel)) },
		Exists: func(rel string) bool {
			_, err := os.Stat(filepath.Join(workingDir, filepath.FromSlash(rel)))
			return err == nil
		},
	}
}

// instance is one running host process. Across a Forge install+launch it owns two
// processes in succession (installer, then server); proc is the current one,
// guarded by mu (issue #305).
type instance struct {
	spec        execution.InstanceSpec
	javaPath    string
	spawn       spawnFunc
	proc        process
	openControl controlFunc
	stopTimeout time.Duration

	events chan execution.StatusEvent
	// exited is closed by supervise once the process has reached a terminal
	// state (stopped or crashed); waitExit selects on it.
	exited chan struct{}

	// logPump captures stdout/stderr; logWG tracks the two scan goroutines so
	// supervise closes the pump only after both have finished.
	logPump *execution.LogPump
	logWG   sync.WaitGroup

	mu       sync.Mutex
	state    execution.ServerState
	stopping bool
	closed   bool

	// cpu carries the previous CPU reading so Sample reports a rate (cpu_millis,
	// thousandths of a core) over the interval between two samples.
	cpuMu     sync.Mutex
	lastTicks uint64
	lastTime  time.Time
}

func (i *instance) Status() execution.ServerState {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.state
}

// setProc records the instance's current process under the lock (the installer
// during the install phase, the server after launch; issue #305).
func (i *instance) setProc(p process) {
	i.mu.Lock()
	i.proc = p
	i.mu.Unlock()
}

// currentProc returns the instance's current process under the lock.
func (i *instance) currentProc() process {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.proc
}

// beginLaunch wires the launch process's log capture, marks the instance running,
// and starts the exit supervisor. It is the shared tail of a direct launch and a
// post-install launch, so a Forge install+launch reaches running through the same
// path as a plain start (issue #305).
func (i *instance) beginLaunch(proc process) {
	i.setProc(proc)
	// Capture stdout/stderr into the per-instance log pump. The scan goroutines
	// end at EOF (process exit closes the pipes); supervise waits on logWG before
	// closing the pump so the consumer's range over Logs() terminates cleanly.
	i.logWG.Add(2)
	go i.scan(proc.Stdout(), execution.LogStreamStdout)
	go i.scan(proc.Stderr(), execution.LogStreamStderr)

	i.set(execution.StateRunning)
	i.emit(execution.StateRunning, "")

	go i.supervise()
}

// superviseInstall runs the supervised Forge installer to completion, then execs
// the launch as the SAME instance (issue #305). The installer's combined output
// is written to logs/forge-install.log in the working dir so an operator can read
// it via the files API. On a non-zero install exit (or a copy error) the instance
// goes crashed and no launch is spawned; a Stop that terminated the installer
// reports stopped. On success it re-plans (the args file is now present) and hands
// off to beginLaunch.
func (i *instance) superviseInstall(installer process) {
	copyErr := i.captureInstallOutput(installer)
	waitErr := installer.Wait()

	i.mu.Lock()
	stopping := i.stopping
	i.mu.Unlock()

	if stopping {
		// A Stop terminated the installer: report stopped and launch nothing.
		i.finishTerminal(execution.StateStopped, "")
		return
	}
	if waitErr != nil {
		i.finishTerminal(execution.StateCrashed, installFailDetail("forge install failed", waitErr))
		return
	}
	if copyErr != nil {
		i.finishTerminal(execution.StateCrashed, installFailDetail("forge install log capture failed", copyErr))
		return
	}

	plan, err := execution.BuildLaunchPlan(i.spec, i.spec.WorkingDir, hostPathResolver(i.spec.WorkingDir))
	if err != nil || plan.NeedsInstall {
		detail := "forge install produced no args file"
		if err != nil {
			detail = installFailDetail("forge re-plan after install failed", err)
		}
		i.finishTerminal(execution.StateCrashed, detail)
		return
	}

	proc, err := i.spawn(context.Background(), i.javaPath, plan.LaunchArgs, i.spec.WorkingDir)
	if err != nil {
		i.finishTerminal(execution.StateCrashed, installFailDetail("forge launch after install failed", err))
		return
	}
	i.beginLaunch(proc)
}

// captureInstallOutput appends the installer's stdout and stderr to the
// working-dir install log. The log is operator-readable via the files API; a
// failure to open it is surfaced so the install is reported crashed rather than
// silently losing diagnostics.
func (i *instance) captureInstallOutput(installer process) error {
	logPath := filepath.Join(i.spec.WorkingDir, filepath.FromSlash(execution.ForgeInstallLogRelpath))
	if err := os.MkdirAll(filepath.Dir(logPath), 0o750); err != nil {
		return fmt.Errorf("create install log dir: %w", err)
	}
	f, err := os.Create(logPath) //nolint:gosec // logPath is the server's own working dir, not user-controlled.
	if err != nil {
		return fmt.Errorf("create install log: %w", err)
	}
	defer func() { _ = f.Close() }()

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); _, _ = io.Copy(f, installer.Stdout()) }()
	go func() { defer wg.Done(); _, _ = io.Copy(f, installer.Stderr()) }()
	wg.Wait()
	return nil
}

// finishTerminal records a terminal state reached during the install phase (no
// launch happened), emits it, and closes the event/exited channels so the
// manager's pump and any in-flight Stop wait observe the end (issue #305). The
// log pump never captured anything in this path, so it is closed directly.
func (i *instance) finishTerminal(state execution.ServerState, detail string) {
	i.set(state)
	i.emit(state, detail)
	close(i.exited)
	i.logPump.Close()
	i.mu.Lock()
	i.closed = true
	close(i.events)
	i.mu.Unlock()
}

// installFailDetail builds a status detail from a context label and an error.
func installFailDetail(label string, err error) string {
	return label + ": " + err.Error()
}

// Logs streams captured console output (execution.LogSource).
func (i *instance) Logs() <-chan execution.LogEvent { return i.logPump.Logs() }

// scan feeds one output stream into the log pump, then signals logWG.
func (i *instance) scan(r io.Reader, stream execution.LogStream) {
	defer i.logWG.Done()
	i.logPump.Scan(r, stream)
}

// Sample reads the process's resident memory and CPU usage from /proc
// (execution.StatsSource, FR-MON-3). RSS is a one-shot read; CPU is a rate
// (cpu_millis, thousandths of a core) computed from the delta in consumed CPU
// ticks since the previous Sample. It is cheap and Linux-specific; on a platform
// without /proc or for an exited process it returns an error and the manager
// falls back to an up-only sample. The first Sample reports cpu_millis=0 (no
// prior reading to diff against).
func (i *instance) Sample(_ context.Context) (execution.MetricsSample, error) {
	rss, ticks, hz, err := readProcStats(i.currentProc().Pid())
	if err != nil {
		return execution.MetricsSample{}, err
	}

	now := time.Now()
	i.cpuMu.Lock()
	var cpuMillis uint32
	if !i.lastTime.IsZero() && hz > 0 {
		elapsed := now.Sub(i.lastTime).Seconds()
		if elapsed > 0 && ticks >= i.lastTicks {
			cores := (float64(ticks-i.lastTicks) / float64(hz)) / elapsed
			cpuMillis = uint32(cores * 1000)
		}
	}
	i.lastTicks = ticks
	i.lastTime = now
	i.cpuMu.Unlock()

	return execution.MetricsSample{
		ServerID:    i.spec.ServerID,
		CPUMillis:   cpuMillis,
		MemoryBytes: rss,
	}, nil
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
	// Capture the current process under the same lock that latches stopping, so the
	// install→launch handoff (which only proceeds when stopping is unset) cannot
	// race this read: Stop signals whichever process is current, and a concurrent
	// install supervisor sees stopping set and launches nothing (issue #305).
	proc := i.proc
	i.mu.Unlock()
	i.emit(execution.StateStopping, "")

	// A graceful RCON stop only makes sense for a running server; during the
	// install phase RCON is not listening, so it falls through to signalling the
	// installer process directly.
	if graceful && i.tryRCONStop(ctx) && i.waitExit(ctx, i.stopTimeout) {
		return nil
	}

	_ = proc.Signal(syscall.SIGTERM)
	if i.waitExit(ctx, i.stopTimeout) {
		return nil
	}

	if err := proc.Kill(); err != nil {
		return fmt.Errorf("hostprocess: kill: %w", err)
	}
	// Confirm the kill actually terminated the process. A process that survives
	// SIGKILL leaves this final wait timing out; report it as a stop failure so the
	// manager reports the command failed, the API keeps the assignment, and the
	// reconciler retries (issue #211). Reporting success here would let the API
	// unassign while the process lingers. The instance was already evicted from the
	// manager's map (handleStop's take()), so the linger is owned by the startup
	// sweep and the reconciler, not re-tracked here.
	//
	// This wait does not honor ctx cancellation: the kill is already issued, so a
	// cancelled caller context must not be read as a lingering process when the
	// process did in fact exit. Only the timeout means it survived.
	if !i.waitExitDone(i.stopTimeout) {
		// The process survived the kill, so this Stop failed but the process is
		// still alive. Reset the stopping latch (and the recorded state back to
		// running, since the process is still alive) so a subsequent Stop re-runs
		// the full graceful→SIGTERM→SIGKILL→confirm sequence instead of short-
		// circuiting on the entry guard and returning a false success (issue #253).
		// The reset is confined to this failure path: a successful stop or a
		// terminal state keeps stopping latched so supervise reports the eventual
		// exit as stopped and concurrent stops still dedupe. supervise has not run
		// here (the process has not exited), so clearing stopping is safe.
		i.mu.Lock()
		i.stopping = false
		i.state = execution.StateRunning
		i.mu.Unlock()
		return fmt.Errorf("hostprocess: process survived SIGKILL after %s", i.stopTimeout)
	}
	return nil
}

// waitExitDone reports whether the process reached a terminal state within d,
// observing only the exit and the timeout (not caller-context cancellation). It
// confirms a kill terminated the process (issue #211).
func (i *instance) waitExitDone(d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-i.exited:
		return true
	case <-timer.C:
		return false
	}
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
// supervisor goroutine observes the actual exit and closes i.exited; the wait is
// released by that close regardless of the recorded terminal state (stopped when
// a stop is in flight, otherwise crashed). It returns false if the stop timeout
// elapses or ctx is cancelled first.
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
// and log streams.
func (i *instance) supervise() {
	waitErr := i.currentProc().Wait()

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

	// The process has exited, so its pipes are at EOF; wait for both scan
	// goroutines to drain them, then close the pump so Logs() consumers finish.
	i.logWG.Wait()
	i.logPump.Close()

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

// instance implements the optional log/metrics capabilities the instance manager
// type-asserts (FR-MON-2, FR-MON-3).
var (
	_ execution.LogSource   = (*instance)(nil)
	_ execution.StatsSource = (*instance)(nil)
)
