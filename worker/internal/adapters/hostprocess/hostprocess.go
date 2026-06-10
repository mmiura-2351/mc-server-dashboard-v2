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
// Readiness posture: a successful spawn enters StateStarting; the instance is
// held there until the server logs its startup-complete "Done (X.XXXs)! For
// help" line (by which point RCON is listening), then transitions to running. A
// bounded fallback timeout reports running anyway if the marker never appears,
// so a server whose log format omits it never sticks in starting (issue #345).
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
	// ReadinessTimeout bounds how long the driver holds StateStarting waiting for
	// the server's startup-complete log marker before falling back to running
	// (issue #345). Zero uses defaultReadinessTimeout.
	ReadinessTimeout time.Duration
}

const defaultStopTimeout = 30 * time.Second

// defaultReadinessTimeout bounds how long the driver holds StateStarting waiting
// for the server's startup-complete log marker before falling back to running
// (issue #345). It is generous enough for a modded server's boot (tens of
// seconds) while never leaving a server stuck in starting when its log format
// omits the marker.
const defaultReadinessTimeout = 5 * time.Minute

// Driver is the host-process ExecutionDriver.
type Driver struct {
	selector         execution.JavaRuntimeSelector
	spawn            spawnFunc
	openControl      controlFunc
	stopTimeout      time.Duration
	readinessTimeout time.Duration
}

// New builds a host-process Driver. selector picks the Java runtime; spawn is the
// process-creation seam; openControl opens RCON for graceful stop.
func New(selector execution.JavaRuntimeSelector, spawn spawnFunc, openControl controlFunc, opts Options) *Driver {
	timeout := opts.StopTimeout
	if timeout <= 0 {
		timeout = defaultStopTimeout
	}
	readinessTimeout := opts.ReadinessTimeout
	if readinessTimeout <= 0 {
		readinessTimeout = defaultReadinessTimeout
	}
	return &Driver{
		selector:         selector,
		spawn:            spawn,
		openControl:      openControl,
		stopTimeout:      timeout,
		readinessTimeout: readinessTimeout,
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
		spec:             spec,
		javaPath:         javaPath,
		spawn:            d.spawn,
		openControl:      d.openControl,
		stopTimeout:      d.stopTimeout,
		readinessTimeout: d.readinessTimeout,
		events:           make(chan execution.StatusEvent, 8),
		exited:           make(chan struct{}),
		state:            execution.StateStarting,
		logPump:          execution.NewLogPump(spec.ServerID, logBufferLines),
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
	// readinessTimeout bounds the hold-on-starting wait before falling back to
	// running (issue #345).
	readinessTimeout time.Duration

	events chan execution.StatusEvent
	// exited is closed by supervise once the process has reached a terminal
	// state (stopped or crashed); waitExit selects on it.
	exited chan struct{}

	// logPump captures stdout/stderr; logWG tracks the two scan goroutines so
	// supervise closes the pump only after both have finished.
	logPump *execution.LogPump
	logWG   sync.WaitGroup

	// beforeLaunch is a test-only hook fired inside superviseInstall after the
	// re-plan but immediately before the latch-check-and-spawn critical section, so
	// a test can drive a Stop into the exact install-exit→launch window the section
	// must close (issue #306). Nil in production.
	beforeLaunch func()

	// beforeSurvivedReset is a test-only hook fired inside Stop after the post-kill
	// confirm wait times out but before re-acquiring the lock to reset the latch, so
	// a test can drive the process exit (and supervise) into the exact window the
	// survived-kill restore must not stomp (issue #392). Nil in production.
	beforeSurvivedReset func()

	mu       sync.Mutex
	state    execution.ServerState
	stopping bool
	// stopRequested is a sticky record that a Stop was ever requested. Unlike
	// stopping (which the survived-kill failure path resets so a retry re-runs the
	// escalation, issue #253), it is never cleared, so supervise reports the
	// eventual exit as stopped — not a spurious crash — even when the process
	// survived the kill window and then died after the latch reset (issue #257).
	stopRequested bool
	// exitObserved is set by supervise under the lock the moment it observes the
	// process exit, before recording the terminal state. The survived-kill restore
	// checks it under the same lock and skips the reset when set, so it cannot stomp
	// a terminal state supervise reached during the post-kill wait window (#392).
	exitObserved bool
	closed       bool

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
// path as a plain start (issue #305). The caller has already published proc as the
// current process (setProc), so a Stop racing this tail acts on the live launch.
func (i *instance) beginLaunch(proc process) {
	i.setProc(proc)
	i.beginLaunchTail(proc)
}

// beginLaunchTail wires log capture, starts the exit supervisor, and holds
// StateStarting until the server reports readiness (the startup-complete log
// marker) before transitioning to running (issue #345). superviseInstall calls
// it after publishing the launch under the latch lock, so the publish and the
// stopping re-check stay one critical section (issue #306).
func (i *instance) beginLaunchTail(proc process) {
	// Capture stdout/stderr into the per-instance log pump. The scan goroutines
	// end at EOF (process exit closes the pipes); supervise waits on logWG before
	// closing the pump so the consumer's range over Logs() terminates cleanly.
	i.logWG.Add(2)
	go i.scan(proc.Stdout(), execution.LogStreamStdout)
	go i.scan(proc.Stderr(), execution.LogStreamStderr)

	go i.supervise()
	go i.awaitReady()
}

// awaitReady holds StateStarting until the server's startup-complete log marker
// appears (RCON is listening by then), the readiness fallback elapses, or the
// process exits first; only the first two transition to running (issue #345).
// The transition is gated under the lock on the instance still being in
// StateStarting, so a process that crashed while booting (supervise set crashed)
// or a Stop that latched stopping is never overwritten with running.
func (i *instance) awaitReady() {
	if !execution.WaitReady(i.logPump.Ready(), i.exited, i.readinessTimeout) {
		return // the process exited first; supervise owns the terminal state.
	}
	i.mu.Lock()
	if i.state != execution.StateStarting {
		i.mu.Unlock()
		return
	}
	i.state = execution.StateRunning
	i.mu.Unlock()
	i.emit(execution.StateRunning, "")
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

	if i.beforeLaunch != nil {
		i.beforeLaunch()
	}

	// The latch re-check and the launch handoff are one critical section (issue
	// #306). The cheap glob/re-plan above ran outside the lock; here the lock is
	// held across spawn (which is exec.Cmd.Start, cheap) so a Stop racing this
	// window either wins the lock first — observed below, aborting the launch — or
	// blocks briefly until the launch process is published and then signals the
	// live launch. There is no publish-before-start sub-window: spawn starts the
	// process under the lock, so Stop never sees a published-but-unstarted handle.
	i.mu.Lock()
	if i.stopping {
		// A Stop won the latch after the installer exited but before the launch:
		// abort the launch entirely and report stopped (issue #306). The installer
		// has already exited, so there is nothing to terminate; Stop's waitExit is
		// released by finishTerminal closing i.exited.
		i.mu.Unlock()
		i.finishTerminal(execution.StateStopped, "")
		return
	}
	proc, err := i.spawn(context.Background(), i.javaPath, plan.LaunchArgs, i.spec.WorkingDir)
	if err != nil {
		i.mu.Unlock()
		i.finishTerminal(execution.StateCrashed, installFailDetail("forge launch after install failed", err))
		return
	}
	i.proc = proc
	i.mu.Unlock()

	i.beginLaunchTail(proc)
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
	// Record the stop intent stickily so supervise reports the eventual exit as
	// stopped even if the survived-kill failure path later clears stopping (#257).
	i.stopRequested = true
	// Capture the pre-stop state before overwriting it with stopping: the
	// survived-kill failure path (below) restores it rather than hardcoding running,
	// so a stop escalation that hits the survived-kill error while the instance is
	// still starting — Stop is reachable from starting since the readiness gating of
	// issue #350 holds starting through the MC boot — does not relabel a still-booting
	// process as running and misreport it to the control plane (issue #352).
	prior := i.state
	i.state = execution.StateStopping
	// Capture the current process under the same lock that latches stopping, so the
	// install→launch handoff (which only proceeds when stopping is unset) cannot
	// race this read: Stop signals whichever process is current, and a concurrent
	// install supervisor sees stopping set and launches nothing (issue #305).
	proc := i.proc
	i.mu.Unlock()
	i.emit(execution.StateStopping, "")

	// Once a stop has begun, detach the escalation from the caller's context. A
	// graceful stop usually runs on a per-server lane whose ctx is the gRPC
	// session stream's serveCtx; if the stream drops mid-stop (RCON "stop" already
	// accepted, the MC server saving), a cancelled ctx would make waitExit return
	// false instantly — twice — and Stop would escalate straight to SIGTERM and a
	// zero-grace SIGKILL, truncating .mca files mid-save (the #703 data-safety
	// line). Decouple instead: run the escalation against a detached context bound
	// by stopDeadline so the process keeps its full grace period regardless of the
	// caller, while the bound still caps a hung RCON call. The post-Kill confirm
	// (waitExitDone) already ignores caller cancellation, so it stays as is.
	stopCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), i.stopDeadline())
	defer cancel()

	// A graceful RCON stop only makes sense for a running server; during the
	// install phase RCON is not listening, so it falls through to signalling the
	// installer process directly.
	if graceful && i.tryRCONStop(stopCtx) && i.waitExit(stopCtx, i.stopTimeout) {
		return nil
	}

	_ = proc.Signal(syscall.SIGTERM)
	if i.waitExit(stopCtx, i.stopTimeout) {
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
		if i.beforeSurvivedReset != nil {
			i.beforeSurvivedReset()
		}
		// The process survived the kill, so this Stop failed but the process is
		// still alive. Reset the stopping latch (and the recorded state back to its
		// pre-stop value, since the process is still alive) so a subsequent Stop
		// re-runs the full graceful→SIGTERM→SIGKILL→confirm sequence instead of short-
		// circuiting on the entry guard and returning a false success (issue #253).
		// Restoring the prior state rather than hardcoding running keeps a
		// still-starting instance labelled starting (issue #352). The reset is
		// confined to this failure path: a successful stop keeps stopping latched so
		// concurrent stops still dedupe.
		//
		// But the process can exit during the wait above, between waitExitDone
		// timing out and re-acquiring the lock: supervise then sets a terminal state
		// and the reset would stomp it back to prior, misreporting a dead process as
		// alive (issue #392). Skip the reset entirely when supervise has observed the
		// exit — the process is gone, supervise owns the terminal state, and there is
		// nothing to retry. stopRequested stays set regardless, so supervise records
		// stopped rather than a spurious crash (issue #257).
		i.mu.Lock()
		if i.exitObserved {
			i.mu.Unlock()
			return nil
		}
		i.stopping = false
		i.state = prior
		i.mu.Unlock()
		return fmt.Errorf("hostprocess: process survived SIGKILL after %s", i.stopTimeout)
	}
	return nil
}

// stopDeadlineGrace pads the detached stop-escalation deadline beyond the two
// stopTimeout-bounded waits, leaving headroom for the RCON open/"stop" call so a
// healthy stop never trips the bound; it only caps a hung RCON call.
const stopDeadlineGrace = 10 * time.Second

// stopDeadline bounds the detached escalation context (issue #770). The
// graceful path runs at most two stopTimeout-bounded waits (RCON→SIGTERM,
// SIGTERM→SIGKILL), so 2*stopTimeout plus a grace for the RCON call covers the
// whole sequence without cutting an in-progress stop short.
func (i *instance) stopDeadline() time.Duration {
	return 2*i.stopTimeout + stopDeadlineGrace
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
	// Mark the exit observed before recording the terminal state so the
	// survived-kill restore, re-acquiring the lock, skips its reset rather than
	// stomping the terminal state set below (issue #392). Read the sticky stop
	// intent here too: a process that survived the kill window and then died after
	// the latch was reset is still a requested stop, so report stopped (issue #257).
	i.exitObserved = true
	stopping := i.stopRequested
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

// emit publishes a status event without ever blocking supervision. When the
// buffer is full it coalesces latest-state-wins: the oldest buffered event is
// discarded to make room for this one, mirroring the manager's coalescing (issue
// #96) so the terminal event — always the last emit — is never dropped (issue
// #790). It returns silently once the stream is closed.
func (i *instance) emit(state execution.ServerState, detail string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.closed {
		return
	}
	ev := execution.StatusEvent{ServerID: i.spec.ServerID, State: state, Detail: detail}
	for {
		select {
		case i.events <- ev:
			return
		default:
		}
		// Buffer full: drop the oldest buffered event and retry so the latest
		// state wins. The retry can race a concurrent reader that just freed a
		// slot, in which case the drain misses and we loop back to the send.
		select {
		case <-i.events:
		default:
		}
	}
}

// instance implements the optional log/metrics capabilities the instance manager
// type-asserts (FR-MON-2, FR-MON-3).
var (
	_ execution.LogSource   = (*instance)(nil)
	_ execution.StatsSource = (*instance)(nil)
)
