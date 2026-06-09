package hostprocess

import (
	"context"
	"errors"
	"io"
	"os"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// fakeProcess is an in-memory stand-in for an OS process. Wait blocks until the
// test (or a signal) releases it, so no real java runs in CI.
type fakeProcess struct {
	mu      sync.Mutex
	done    chan struct{}
	waitErr error
	signals []os.Signal
	killed  bool
	// killNoExit models a process that survives SIGKILL: Kill records the call but
	// does not release Wait, so the post-Kill waitExit times out.
	killNoExit bool
	// killSurvive counts how many leading Kill calls survive (do not release Wait);
	// each Kill decrements it, and a Kill at zero exits the process. It models a
	// process that lingers through the first kill(s) and dies on a later retry,
	// driving the re-attemptable-Stop path (issue #253).
	killSurvive int
	killCalls   int
	startErr    error
	startedAt   bool
	stdout      io.Reader
	stderr      io.Reader
}

func newFakeProcess() *fakeProcess {
	return &fakeProcess{
		done:   make(chan struct{}),
		stdout: strings.NewReader(""),
		stderr: strings.NewReader(""),
	}
}

func (p *fakeProcess) Stdout() io.Reader {
	if p.stdout == nil {
		return strings.NewReader("")
	}
	return p.stdout
}

func (p *fakeProcess) Stderr() io.Reader {
	if p.stderr == nil {
		return strings.NewReader("")
	}
	return p.stderr
}

func (p *fakeProcess) Pid() int { return 0 }

func (p *fakeProcess) Wait() error {
	<-p.done
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.waitErr
}

func (p *fakeProcess) Signal(sig os.Signal) error {
	p.mu.Lock()
	p.signals = append(p.signals, sig)
	p.mu.Unlock()
	return nil
}

func (p *fakeProcess) Kill() error {
	p.mu.Lock()
	p.killed = true
	p.killCalls++
	survive := p.killNoExit || p.killSurvive > 0
	if p.killSurvive > 0 {
		p.killSurvive--
	}
	p.mu.Unlock()
	if !survive {
		p.exit(errors.New("killed"))
	}
	return nil
}

func (p *fakeProcess) killCount() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.killCalls
}

// exit releases Wait with the given error, simulating process termination.
func (p *fakeProcess) exit(err error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	select {
	case <-p.done:
	default:
		p.waitErr = err
		close(p.done)
	}
}

func (p *fakeProcess) gotSignal(sig os.Signal) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, s := range p.signals {
		if s == sig {
			return true
		}
	}
	return false
}

// fixedSelector returns a fixed java path for any version.
type fixedSelector struct{}

func (fixedSelector) Select(string) (string, error) { return "/jvm/21/bin/java", nil }

func newTestDriver(t *testing.T, proc *fakeProcess, ctrl execution.ServerControl, ctrlErr error) *Driver {
	t.Helper()
	// A short readiness fallback lets tests that do not feed a Done marker reach
	// running promptly via the timeout path (issue #345).
	return newReadinessTestDriver(t, proc, 20*time.Millisecond, ctrl, ctrlErr)
}

// newReadinessTestDriver builds a driver with an explicit readiness fallback
// timeout so the readiness tests (issue #345) can isolate the marker path (long
// timeout) from the fallback path (short timeout).
func newReadinessTestDriver(t *testing.T, proc *fakeProcess, readinessTimeout time.Duration, ctrl execution.ServerControl, ctrlErr error) *Driver {
	t.Helper()
	spawn := func(_ context.Context, _ string, _ []string, _ string) (process, error) {
		if proc.startErr != nil {
			return nil, proc.startErr
		}
		proc.startedAt = true
		return proc, nil
	}
	return New(fixedSelector{}, spawn, func(context.Context, execution.InstanceSpec) (execution.ServerControl, error) {
		return ctrl, ctrlErr
	}, Options{StopTimeout: 50 * time.Millisecond, ReadinessTimeout: readinessTimeout})
}

func spec() execution.InstanceSpec {
	return execution.InstanceSpec{ServerID: "s1", WorkingDir: "/scratch/s1", MinecraftVersion: "1.21"}
}

// awaitLogLine reads the Logs() stream until a line containing want surfaces. It
// is the deterministic synchronization point the hold-on-starting test relies on:
// once a benign boot line appears on Logs(), the scan goroutine has consumed the
// boot window, and since markReadyIfDone runs synchronously before a line is
// queued (logpump.go), any readiness marker present would already have fired
// Ready. So the marker has provably NOT been seen yet — no sleep needed.
func awaitLogLine(t *testing.T, ch <-chan execution.LogEvent, want string) {
	t.Helper()
	deadline := time.After(2 * time.Second)
	for {
		select {
		case ev, ok := <-ch:
			if !ok {
				t.Fatalf("log channel closed before reaching %q", want)
			}
			if strings.Contains(ev.Line, want) {
				return
			}
		case <-deadline:
			t.Fatalf("timed out waiting for log line %q", want)
		}
	}
}

// observedRunning reports whether any StateRunning event is currently buffered on
// ch. It drains non-blocking: every emit up to the caller's synchronization point
// has already completed, so a running event — if one was wrongly emitted before
// readiness — is sitting in the buffer to be observed here.
func observedRunning(ch <-chan execution.StatusEvent) bool {
	for {
		select {
		case ev := <-ch:
			if ev.State == execution.StateRunning {
				return true
			}
		default:
			return false
		}
	}
}

// drainTo collects status events until it sees want or times out.
func drainTo(t *testing.T, ch <-chan execution.StatusEvent, want execution.ServerState) {
	t.Helper()
	deadline := time.After(2 * time.Second)
	for {
		select {
		case ev, ok := <-ch:
			if !ok {
				t.Fatalf("event channel closed before reaching %v", want)
			}
			if ev.State == want {
				return
			}
		case <-deadline:
			t.Fatalf("timed out waiting for %v", want)
		}
	}
}

func TestStartReachesRunning(t *testing.T) {
	proc := newFakeProcess()
	d := newTestDriver(t, proc, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
	if inst.Status() != execution.StateRunning {
		t.Fatalf("Status = %v, want running", inst.Status())
	}
}

// Running is reported only after the server logs its startup-complete "Done"
// line; until then the instance holds StateStarting so a client gating console
// input on running does not hit the RCON boot window (issue #345).
//
// This pins the PR's core invariant: running is never observed BEFORE the
// readiness marker. The stdout stream is held open (an io.Pipe) without the Done
// line and the readiness timeout is long, so neither the marker path nor the
// fallback can fire. A benign boot line driven through to Logs() is the
// deterministic synchronization point (see awaitLogLine): once it surfaces, the
// instance must still be starting with no running event emitted; only after the
// Done line is written does running arrive. Re-introducing the pre-fix immediate
// StateRunning emit in beginLaunchTail makes the negative assertions below fail.
func TestStartHoldsStartingUntilReadyMarker(t *testing.T) {
	pr, pw := io.Pipe()
	proc := newFakeProcess()
	proc.stdout = pr
	// A long readiness timeout means only the Done marker can drive running here;
	// the fallback path is covered by TestStartReachesRunningViaFallbackTimeout.
	d := newReadinessTestDriver(t, proc, 10*time.Second, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	src, ok := inst.(execution.LogSource)
	if !ok {
		t.Fatal("host-process instance should be a LogSource")
	}

	// Drive a benign boot line (NOT the marker) and wait for it on Logs(). Its
	// arrival proves the scan goroutine consumed the boot window without seeing the
	// marker, so awaitReady cannot have transitioned to running.
	if _, err := io.WriteString(pw,
		"[12:00:00] [Server thread/INFO]: Starting minecraft server\n"); err != nil {
		t.Fatalf("write boot line: %v", err)
	}
	awaitLogLine(t, src.Logs(), "Starting minecraft server")

	// The negative assertions: the instance is still starting and no running event
	// was emitted before the readiness marker.
	if got := inst.Status(); got != execution.StateStarting {
		t.Fatalf("Status = %v before the readiness marker, want starting", got)
	}
	if observedRunning(inst.Events()) {
		t.Fatal("running was emitted before the readiness marker")
	}

	// Now feed the marker; running must arrive.
	if _, err := io.WriteString(pw,
		`[12:00:03] [Server thread/INFO]: Done (3.210s)! For help, type "help"`+"\n"); err != nil {
		t.Fatalf("write done line: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
	_ = pw.Close()
}

// With no readiness marker in the logs, the instance still reaches running once
// the fallback timeout elapses, so a server whose log format differs never
// sticks in starting forever (issue #345).
func TestStartReachesRunningViaFallbackTimeout(t *testing.T) {
	proc := newFakeProcess() // empty stdout/stderr: the Done marker never appears
	d := newReadinessTestDriver(t, proc, 30*time.Millisecond, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)
}

// A process that exits while still starting (before any readiness marker)
// surfaces as crashed, not running: a boot crash (e.g. eula=false) must not be
// masked by the readiness wait (issue #345).
func TestStartExitDuringStartingReportsCrashed(t *testing.T) {
	proc := newFakeProcess()
	// A long readiness timeout: the exit, not the fallback, must drive the state.
	d := newReadinessTestDriver(t, proc, 10*time.Second, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	// The process exits before any Done line; the instance must go crashed.
	proc.exit(errors.New("exit status 1"))
	drainTo(t, inst.Events(), execution.StateCrashed)
	if inst.Status() == execution.StateRunning {
		t.Fatal("instance reported running after a boot crash")
	}
}

func TestCrashEmitsCrashed(t *testing.T) {
	proc := newFakeProcess()
	d := newTestDriver(t, proc, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	// Process dies unexpectedly → crashed.
	proc.exit(errors.New("exit status 1"))
	drainTo(t, inst.Events(), execution.StateCrashed)
}

// A graceful stop prefers RCON "stop"; when it succeeds the process exits and the
// instance reaches stopped without a SIGTERM.
func TestGracefulStopViaRCON(t *testing.T) {
	proc := newFakeProcess()
	ctrl := &fakeControl{onStop: func() { proc.exit(nil) }}
	d := newTestDriver(t, proc, ctrl, nil)

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	if !ctrl.stopCalled {
		t.Fatal("expected RCON stop to be called")
	}
	if proc.gotSignal(syscall.SIGTERM) {
		t.Fatal("SIGTERM should not be sent when RCON stop succeeds")
	}
}

// When RCON is unavailable, a graceful stop falls back to SIGTERM.
func TestGracefulStopFallsBackToSIGTERM(t *testing.T) {
	proc := newFakeProcess()
	d := newTestDriver(t, proc, nil, errors.New("rcon dial failed"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	// SIGTERM handler in the real world exits; the fake exits on the signal too.
	go func() {
		for !proc.gotSignal(syscall.SIGTERM) {
			time.Sleep(time.Millisecond)
		}
		proc.exit(nil)
	}()

	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	if !proc.gotSignal(syscall.SIGTERM) {
		t.Fatal("expected SIGTERM fallback")
	}
}

// When the process ignores SIGTERM past the stop timeout, the driver escalates to
// SIGKILL.
func TestGracefulStopEscalatesToSIGKILL(t *testing.T) {
	proc := newFakeProcess()
	d := newTestDriver(t, proc, nil, errors.New("rcon dial failed"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	// Process never exits on SIGTERM; the driver's Kill() releases Wait.
	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	if !proc.killed {
		t.Fatal("expected SIGKILL escalation")
	}
}

// A process that survives SIGKILL leaves the post-Kill waitExit timing out. Stop
// must report this as a failure so the manager reports the command failed, the
// API keeps the assignment, and the reconciler retries (issue #211); reporting
// success here would let the API unassign while the process lingers.
func TestStopFailsWhenProcessSurvivesKill(t *testing.T) {
	proc := newFakeProcess()
	proc.killNoExit = true
	d := newTestDriver(t, proc, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if err := inst.Stop(context.Background(), true); err == nil {
		t.Fatal("expected Stop to fail when the process survives SIGKILL")
	}
	if !proc.killed {
		t.Fatal("expected SIGKILL escalation")
	}
}

// A stop escalation that hits the survived-SIGKILL failure path while the
// instance is still starting must not relabel the still-booting process as
// running. Stop is reachable from starting because readiness gating holds
// starting through the MC boot (issue #350); the survived-kill reset restores the
// pre-stop state, so a starting instance stays starting rather than misreporting
// running to the control plane (issue #352).
func TestSurvivedKillFromStartingDoesNotReportRunning(t *testing.T) {
	pr, pw := io.Pipe()
	defer func() { _ = pw.Close() }()
	proc := newFakeProcess()
	proc.stdout = pr
	proc.killNoExit = true // the process survives SIGKILL
	// A long readiness timeout with no Done marker holds the instance in starting.
	d := newReadinessTestDriver(t, proc, 10*time.Second, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	src, ok := inst.(execution.LogSource)
	if !ok {
		t.Fatal("host-process instance should be a LogSource")
	}

	// Synchronize on a benign boot line to prove the boot window was consumed
	// without the readiness marker, so the instance is provably still starting.
	if _, err := io.WriteString(pw,
		"[12:00:00] [Server thread/INFO]: Starting minecraft server\n"); err != nil {
		t.Fatalf("write boot line: %v", err)
	}
	awaitLogLine(t, src.Logs(), "Starting minecraft server")
	if got := inst.Status(); got != execution.StateStarting {
		t.Fatalf("Status = %v before Stop, want starting", got)
	}

	if err := inst.Stop(context.Background(), true); err == nil {
		t.Fatal("expected Stop to fail when the process survives SIGKILL")
	}
	if got := inst.Status(); got != execution.StateStarting {
		t.Fatalf("Status = %v after survived-kill Stop, want starting (not running)", got)
	}
}

// The process can exit during the post-kill confirm wait, in the window between
// waitExitDone timing out and the survived-kill restore re-acquiring the lock:
// supervise sets the terminal state, and the restore must not stomp it back to
// the pre-stop state (issue #392). The beforeSurvivedReset hook drives the exit
// and supervise into that exact window, then the restore runs.
func TestSurvivedKillRestoreDoesNotStompTerminalState(t *testing.T) {
	proc := newFakeProcess()
	proc.killNoExit = true // the first confirm wait times out: the kill "survived"
	d := newTestDriver(t, proc, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	in := inst.(*instance)
	in.beforeSurvivedReset = func() {
		// The process exits in the window; wait for supervise to record the terminal
		// state before the restore re-acquires the lock.
		proc.exit(errors.New("killed late"))
		drainTo(t, inst.Events(), execution.StateStopped)
	}

	// The process did exit (during the window), so Stop succeeds rather than
	// reporting a survived-kill failure.
	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("Stop = %v, want nil once the process exits in the wait window", err)
	}
	if got := inst.Status(); got != execution.StateStopped {
		t.Fatalf("Status = %v after the exit-in-window Stop, want stopped (not stomped back)", got)
	}
}

// A process that survives the kill for the whole timeout and then dies after the
// survived-kill restore reset the stopping latch must still be recorded stopped,
// not a spurious crash: a stop was requested (issue #257). The reset clears
// stopping (so a retry can run), but the sticky stop intent makes supervise
// report the operator-requested stop correctly.
func TestSurvivedKillThenLateExitRecordsStopped(t *testing.T) {
	proc := newFakeProcess()
	proc.killNoExit = true // the kill survives for the whole confirm wait
	d := newTestDriver(t, proc, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	// The kill survives the whole timeout, so Stop fails with the survived-kill
	// error and the latch is reset.
	if err := inst.Stop(context.Background(), true); err == nil {
		t.Fatal("expected Stop to fail when the process survives SIGKILL")
	}

	// The orphan dies later; supervise must record it stopped, not crashed, because
	// a stop was requested.
	proc.exit(errors.New("died after reset"))
	drainTo(t, inst.Events(), execution.StateStopped)
	if got := inst.Status(); got != execution.StateStopped {
		t.Fatalf("Status = %v after the late exit, want stopped (not a spurious crash)", got)
	}
}

// After a Stop that fails because the process survives SIGKILL, a retry Stop must
// re-run the kill-and-confirm sequence rather than short-circuit on the stopping
// latch. When the process then dies on the retry kill, the retry returns success
// (issue #253). Without the latch reset the retry would return a false nil.
func TestStopReattemptableAfterSurvivedKill(t *testing.T) {
	proc := newFakeProcess()
	proc.killSurvive = 1 // first kill survives, the next kill exits the process
	d := newTestDriver(t, proc, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if err := inst.Stop(context.Background(), true); err == nil {
		t.Fatal("expected first Stop to fail when the process survives SIGKILL")
	}

	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("retry Stop = %v, want success once the process dies on the retry kill", err)
	}
	if proc.killCount() != 2 {
		t.Fatalf("Kill called %d times, want 2 (initial + retry re-issues the kill)", proc.killCount())
	}
	drainTo(t, inst.Events(), execution.StateStopped)
}

// A retry Stop while the process is STILL surviving the kill must fail again,
// never return a false nil: the orphan is still alive and the API must keep the
// assignment (issue #253).
func TestRetryStopStillSurvivingFailsAgain(t *testing.T) {
	proc := newFakeProcess()
	proc.killNoExit = true // every kill survives
	d := newTestDriver(t, proc, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if err := inst.Stop(context.Background(), true); err == nil {
		t.Fatal("expected first Stop to fail")
	}
	if err := inst.Stop(context.Background(), true); err == nil {
		t.Fatal("retry Stop returned nil while the process still survives; want a failure")
	}
	if proc.killCount() != 2 {
		t.Fatalf("Kill called %d times, want 2 (the retry must re-issue the kill, not short-circuit)", proc.killCount())
	}
}

// Stopping a crashed instance is a prompt no-op success: the process is already
// dead, so Stop must not signal it, must not spin waitExit's timeout, and must
// not surface a Kill() error.
func TestStopOnCrashedIsPromptNoOp(t *testing.T) {
	proc := newFakeProcess()
	d := newTestDriver(t, proc, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	// Crash the process and wait until the terminal state is observed.
	proc.exit(errors.New("exit status 1"))
	drainTo(t, inst.Events(), execution.StateCrashed)

	// Stop must return well under two stop-timeout escalation steps (~100ms here).
	done := make(chan error, 1)
	start := time.Now()
	go func() { done <- inst.Stop(context.Background(), true) }()
	select {
	case stopErr := <-done:
		if stopErr != nil {
			t.Fatalf("Stop on crashed instance: %v", stopErr)
		}
	case <-time.After(time.Second):
		t.Fatal("Stop on crashed instance did not return promptly")
	}
	if elapsed := time.Since(start); elapsed >= 100*time.Millisecond {
		t.Fatalf("Stop spun the timeout: took %v", elapsed)
	}
	if proc.gotSignal(syscall.SIGTERM) {
		t.Fatal("Stop should not signal an already-dead process")
	}
}

// A process that exits mid-graceful-stop releases the Stop wait via close(exited)
// rather than timing out. The stop is already in flight, so supervise records the
// terminal state as stopped.
func TestStopWaitSatisfiedByCrash(t *testing.T) {
	proc := newFakeProcess()
	// RCON "stop" does not exit the process immediately; the process exits shortly
	// after, and waitExit completes when supervise closes exited.
	ctrl := &fakeControl{onStop: func() {
		go func() {
			time.Sleep(5 * time.Millisecond)
			proc.exit(errors.New("exit status 1"))
		}()
	}}
	d := newTestDriver(t, proc, ctrl, nil)

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	start := time.Now()
	if err := inst.Stop(context.Background(), true); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if elapsed := time.Since(start); elapsed >= 50*time.Millisecond {
		t.Fatalf("Stop timed out instead of completing on crash: took %v", elapsed)
	}
	if proc.gotSignal(syscall.SIGTERM) {
		t.Fatal("Stop should not escalate to SIGTERM when the process crashes during the wait")
	}
}

// waitExit honours the caller's context: a cancelled ctx unblocks Stop before
// the stop timeout, and the driver then escalates to Kill().
func TestStopHonoursContextCancellation(t *testing.T) {
	proc := newFakeProcess()
	d := newTestDriver(t, proc, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	// The process never exits on SIGTERM; with ctx already cancelled, waitExit
	// returns immediately and Stop escalates to Kill(), which releases Wait.
	if err := inst.Stop(ctx, true); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	if !proc.killed {
		t.Fatal("expected Kill() escalation after context cancellation")
	}
}

func TestStartSpawnFailure(t *testing.T) {
	proc := newFakeProcess()
	proc.startErr = errors.New("exec: java not found")
	d := newTestDriver(t, proc, nil, nil)

	_, err := d.Start(context.Background(), spec())
	if err == nil {
		t.Fatal("expected Start to fail when spawn fails")
	}
}

// A spec carrying MemoryLimitMB launches java with the derived -Xms/-Xmx heap
// flags; an unset limit launches with none. The host-process driver enforces no
// hard memory ceiling — the derived heap (best-effort, issue #708) is its only
// memory control — so this asserts the launch command, the seam where that heap
// reaches the JVM.
func TestStartFeedsDerivedHeapFromMemoryLimit(t *testing.T) {
	cases := []struct {
		name      string
		limitMB   uint32
		wantHeap  bool
		wantXmx   string
		wantNoXmx bool
	}{
		{name: "limit set derives heap", limitMB: 2048, wantHeap: true, wantXmx: "-Xmx1639M"},
		{name: "unset limit no heap", limitMB: 0, wantNoXmx: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			proc := newFakeProcess()
			var gotArgs []string
			spawn := func(_ context.Context, _ string, args []string, _ string) (process, error) {
				gotArgs = args
				return proc, nil
			}
			d := New(fixedSelector{}, spawn, func(context.Context, execution.InstanceSpec) (execution.ServerControl, error) {
				return nil, errors.New("no rcon")
			}, Options{StopTimeout: 50 * time.Millisecond, ReadinessTimeout: 20 * time.Millisecond})

			s := spec()
			s.JarRelpath = "server.jar"
			s.MemoryLimitMB = tc.limitMB
			if _, err := d.Start(context.Background(), s); err != nil {
				t.Fatalf("Start: %v", err)
			}
			proc.exit(nil)

			hasXmx := false
			for _, a := range gotArgs {
				if strings.HasPrefix(a, "-Xmx") {
					hasXmx = true
					if tc.wantHeap && a != tc.wantXmx {
						t.Fatalf("launch args = %v, want -Xmx %q", gotArgs, tc.wantXmx)
					}
				}
			}
			if tc.wantHeap && !hasXmx {
				t.Fatalf("launch args = %v, want a derived -Xmx flag", gotArgs)
			}
			if tc.wantNoXmx && hasXmx {
				t.Fatalf("launch args = %v, want no -Xmx flag for an unset limit", gotArgs)
			}
		})
	}
}

// CPUMillis carries no host-process enforcement (container-only, issue #725):
// unlike MemoryLimitMB there is no derived launch flag to feed, so a CPU
// allocation must not alter the launch command. This asserts the launch args are
// identical with and without CPUMillis set — the host-process driver leaves a
// server CPU-unconstrained. Hard enforcement (cgroup v2) is tracked in #718.
func TestStartDoesNotAlterLaunchForCPUMillis(t *testing.T) {
	launchArgs := func(cpuMillis uint32) []string {
		proc := newFakeProcess()
		var gotArgs []string
		spawn := func(_ context.Context, _ string, args []string, _ string) (process, error) {
			gotArgs = args
			return proc, nil
		}
		d := New(fixedSelector{}, spawn, func(context.Context, execution.InstanceSpec) (execution.ServerControl, error) {
			return nil, errors.New("no rcon")
		}, Options{StopTimeout: 50 * time.Millisecond, ReadinessTimeout: 20 * time.Millisecond})

		s := spec()
		s.JarRelpath = "server.jar"
		s.CPUMillis = cpuMillis
		if _, err := d.Start(context.Background(), s); err != nil {
			t.Fatalf("Start: %v", err)
		}
		proc.exit(nil)
		return gotArgs
	}

	withoutCPU := strings.Join(launchArgs(0), " ")
	withCPU := strings.Join(launchArgs(2000), " ")
	if withoutCPU != withCPU {
		t.Fatalf("CPUMillis altered launch args: without = %q, with = %q", withoutCPU, withCPU)
	}
}

// Captured stdout/stderr flow through to the Logs() stream as LogEvents tagged
// with the originating stream; the log channel closes when the process exits.
func TestLogCaptureFlowsToLogs(t *testing.T) {
	proc := newFakeProcess()
	proc.stdout = strings.NewReader("hello world\nsecond line\n")
	proc.stderr = strings.NewReader("a warning\n")
	d := newTestDriver(t, proc, nil, errors.New("no rcon"))

	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	src, ok := inst.(execution.LogSource)
	if !ok {
		t.Fatal("host-process instance should be a LogSource")
	}

	// The readers reach EOF immediately; exit the process so supervise closes the
	// log pump after the scan goroutines drain.
	proc.exit(nil)

	var stdout, stderr []string
	for ev := range src.Logs() {
		if ev.ServerID != "s1" {
			t.Fatalf("ServerID = %q", ev.ServerID)
		}
		switch ev.Stream {
		case execution.LogStreamStdout:
			stdout = append(stdout, ev.Line)
		case execution.LogStreamStderr:
			stderr = append(stderr, ev.Line)
		}
	}
	if len(stdout) != 2 || stdout[0] != "hello world" || stdout[1] != "second line" {
		t.Fatalf("stdout lines = %v", stdout)
	}
	if len(stderr) != 1 || stderr[0] != "a warning" {
		t.Fatalf("stderr lines = %v", stderr)
	}
}

// Sample errors for a non-existent pid; the manager treats this as "no
// measurement" and emits an up-only sample. (Pid 0 / process-less fake has no
// /proc entry.)
func TestSampleErrorsForUnknownPid(t *testing.T) {
	proc := newFakeProcess()
	d := newTestDriver(t, proc, nil, errors.New("no rcon"))
	inst, err := d.Start(context.Background(), spec())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer proc.exit(nil)

	stats, ok := inst.(execution.StatsSource)
	if !ok {
		t.Fatal("host-process instance should be a StatsSource")
	}
	if _, err := stats.Sample(context.Background()); err == nil {
		t.Fatal("expected Sample to error for an unknown pid")
	}
}

// fakeControl is an in-memory ServerControl.
type fakeControl struct {
	stopCalled bool
	onStop     func()
}

func (c *fakeControl) Execute(_ context.Context, line string) (string, error) {
	if line == "stop" {
		c.stopCalled = true
		if c.onStop != nil {
			c.onStop()
		}
	}
	return "", nil
}

func (c *fakeControl) Close() error { return nil }
