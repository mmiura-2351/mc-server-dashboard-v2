package hostprocess

import (
	"context"
	"errors"
	"os"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// fakeProcess is an in-memory stand-in for an OS process. Wait blocks until the
// test (or a signal) releases it, so no real java runs in CI.
type fakeProcess struct {
	mu        sync.Mutex
	done      chan struct{}
	waitErr   error
	signals   []os.Signal
	killed    bool
	startErr  error
	startedAt bool
}

func newFakeProcess() *fakeProcess {
	return &fakeProcess{done: make(chan struct{})}
}

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
	p.mu.Unlock()
	p.exit(errors.New("killed"))
	return nil
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
	spawn := func(_ context.Context, _ string, _ []string, _ string) (process, error) {
		if proc.startErr != nil {
			return nil, proc.startErr
		}
		proc.startedAt = true
		return proc, nil
	}
	return New(fixedSelector{}, spawn, func(context.Context, execution.InstanceSpec) (execution.ServerControl, error) {
		return ctrl, ctrlErr
	}, Options{StopTimeout: 50 * time.Millisecond})
}

func spec() execution.InstanceSpec {
	return execution.InstanceSpec{ServerID: "s1", WorkingDir: "/scratch/s1", MinecraftVersion: "1.21"}
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
