package instancemanager

import (
	"context"
	"errors"
	"runtime"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// orphanInstance is a fakeInstance whose Stop fails until stopAfter calls have
// been made, modelling the #211 case where a driver Stop cannot confirm
// termination (process/container survives Kill) until a later retry succeeds.
//
// It faithfully models the real drivers' stopping-latch contract (issue #253):
// Stop latches `stopping` on entry and, if already latched (terminal or a
// concurrent/repeat stop), short-circuits to a no-op nil — exactly the behavior
// that turned the orphan retry into a false success. The latch is reset only on
// the failure return so a subsequent Stop re-runs the termination attempt; a
// successful stop keeps it latched (the instance is gone). Without this faithful
// model the manager suite could not have caught the bug.
type orphanInstance struct {
	*fakeInstance
	stopAfter int // number of leading Stop calls that fail
	stopCalls int
	stopping  bool
}

func (i *orphanInstance) Stop(ctx context.Context, graceful bool, preFallback ...func(context.Context) bool) error {
	i.mu.Lock()
	// Mirror the driver entry guard: a Stop while already stopping is a no-op nil.
	if i.stopping {
		i.mu.Unlock()
		return nil
	}
	i.stopping = true
	i.stopCalls++
	fail := i.stopCalls <= i.stopAfter
	i.mu.Unlock()
	if fail {
		// Survived-kill failure path: reset the latch so a retry re-attempts.
		i.mu.Lock()
		i.stopping = false
		i.mu.Unlock()
		return errors.New("driver: process survived kill")
	}
	// Confirmed termination: keep stopping latched and finalize via the base fake.
	return i.fakeInstance.Stop(ctx, graceful, preFallback...)
}

func (i *orphanInstance) stopCount() int {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.stopCalls
}

// orphanDriver hands out a single orphanInstance with a configurable failing
// Stop, so tests can drive the failed-stop -> retry path.
type orphanDriver struct {
	inst      *orphanInstance
	stopAfter int
}

func (d *orphanDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.inst = &orphanInstance{fakeInstance: newFakeInstance(spec.ServerID), stopAfter: d.stopAfter}
	return d.inst, nil
}

// A failed driver Stop records the instance as an orphan; a retry StopServer
// re-attempts the driver Stop against the same instance and returns success only
// once termination is confirmed (issue #251).
func TestFailedStopThenRetryTerminatesOrphan(t *testing.T) {
	d := &orphanDriver{stopAfter: 1} // first Stop fails, second succeeds
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())

	first := m.Handle(context.Background(), session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})
	if first.Success {
		t.Fatalf("first stop = %+v, want failure (driver could not confirm termination)", first)
	}
	if first.ErrorCode == session.CommandErrorServerNotFound {
		t.Fatalf("first stop error = %v, want a stop-failure code, not SERVER_NOT_FOUND", first.ErrorCode)
	}

	retry := m.Handle(context.Background(), session.Command{CommandID: "stop2", ServerID: "s1", Kind: "StopServer"})
	if !retry.Success {
		t.Fatalf("retry stop = %+v, want success once termination is confirmed", retry)
	}
	if d.inst.stopCount() != 2 {
		t.Fatalf("driver Stop called %d times, want 2 (initial + retry)", d.inst.stopCount())
	}
}

// A retry stop that still cannot confirm termination returns the same
// stop-failure error, never SERVER_NOT_FOUND: the orphan is known and may still
// be lingering, so the API must keep the assignment (issue #251).
func TestRetryStopStillFailingKeepsStopFailure(t *testing.T) {
	d := &orphanDriver{stopAfter: 2} // both the initial stop and the retry fail
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())

	_ = m.Handle(context.Background(), session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})
	retry := m.Handle(context.Background(), session.Command{CommandID: "stop2", ServerID: "s1", Kind: "StopServer"})
	if retry.Success {
		t.Fatalf("retry stop = %+v, want failure", retry)
	}
	if retry.ErrorCode == session.CommandErrorServerNotFound {
		t.Fatalf("retry stop error = %v, want a stop-failure code, not SERVER_NOT_FOUND", retry.ErrorCode)
	}
}

// A genuinely unknown server id still returns SERVER_NOT_FOUND: that code stays
// reserved for ids the worker never tracked, not for failed-stop orphans.
func TestStopUnknownStillServerNotFound(t *testing.T) {
	m := newManager(t, &orphanDriver{}, nil)
	res := m.Handle(context.Background(), session.Command{CommandID: "c", ServerID: "ghost", Kind: "StopServer"})
	if res.Success || res.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("stop unknown = %+v, want SERVER_NOT_FOUND", res)
	}
}

// A successful retry forgets the orphan: a subsequent stop for the id is now a
// genuinely unknown server (SERVER_NOT_FOUND), and the id can be started again.
func TestOrphanClearedAfterSuccessfulRetry(t *testing.T) {
	d := &orphanDriver{stopAfter: 1}
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())
	_ = m.Handle(context.Background(), session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})
	if retry := m.Handle(context.Background(), session.Command{CommandID: "stop2", ServerID: "s1", Kind: "StopServer"}); !retry.Success {
		t.Fatalf("retry stop = %+v, want success", retry)
	}

	again := m.Handle(context.Background(), session.Command{CommandID: "stop3", ServerID: "s1", Kind: "StopServer"})
	if again.Success || again.ErrorCode != session.CommandErrorServerNotFound {
		t.Fatalf("stop after cleared orphan = %+v, want SERVER_NOT_FOUND", again)
	}
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start after cleared orphan = %+v, want success", res)
	}
}

// StartServer for an orphaned id must NOT launch a second instance over the
// lingering orphan; it is rejected as INVALID_STATE (the same family as
// "already running"), pending termination (issue #251).
func TestStartOverOrphanRejected(t *testing.T) {
	d := &orphanDriver{stopAfter: 1}
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())
	_ = m.Handle(context.Background(), session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})

	res := m.Handle(context.Background(), startCmd())
	if res.Success || res.ErrorCode != session.CommandErrorInvalidState {
		t.Fatalf("start over orphan = %+v, want INVALID_STATE", res)
	}
	if d.inst.stopCount() != 1 {
		t.Fatalf("start over orphan should not Stop the orphan; stop calls = %d", d.inst.stopCount())
	}
}

// HydrateTrigger for an orphaned id gets the same protection as a running
// server: hydrating would replace the working set out from under a process that
// may still be alive, so it is rejected as INVALID_STATE (issue #251).
func TestHydrateOverOrphanRejected(t *testing.T) {
	d := &orphanDriver{stopAfter: 1}
	m := newManager(t, d, nil).WithTransfer(&fakeTransfer{})
	_ = m.Handle(context.Background(), startCmd())
	_ = m.Handle(context.Background(), session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})

	res := m.Handle(context.Background(), session.Command{CommandID: "h", ServerID: "s1", Kind: "HydrateTrigger"})
	if res.Success || res.ErrorCode != session.CommandErrorInvalidState {
		t.Fatalf("hydrate over orphan = %+v, want INVALID_STATE", res)
	}
}

// If the orphan finally exits on its own, the instance's status pump clears the
// orphan record: a later stop for the id is then a genuinely unknown server.
func TestOrphanClearedWhenInstanceExitsOnItsOwn(t *testing.T) {
	// Every retry stop keeps failing (the driver cannot confirm termination); only
	// the instance exiting on its own clears the orphan, via the pump.
	d := &orphanDriver{stopAfter: 1000}
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())
	_ = m.Handle(context.Background(), session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})

	// The lingering process finally dies: the instance emits a terminal event and
	// closes its channel, which the pump observes.
	d.inst.events <- execution.StatusEvent{ServerID: "s1", State: execution.StateStopped}
	close(d.inst.events)

	// Anchor on the pump's own progress rather than a fixed sleep: the pump
	// forwards the terminal stopped status onto the merged stream as the last
	// action of its event loop, then the loop exits and the deferred
	// forgetOrphanIf clears the orphan (issue #253). Draining that terminal event
	// off m.Events() deterministically advances the pump past its final send, so
	// the remaining window before the orphan is cleared is only the deferred call
	// scheduling — collapsing the old dependency on a fixed deadline that an
	// overloaded -race runner could exceed (issue #330).
	overall := time.After(2 * time.Second)
drain:
	for {
		select {
		case ev := <-m.Events():
			if ev.ServerID == "s1" && ev.State == execution.StateStopped.String() {
				break drain
			}
		case <-overall:
			t.Fatal("pump did not forward the terminal stopped status")
		}
	}

	// Event-driven fast path with a generous overall deadline: poll the observable
	// (the orphan is cleared once a stop for the id is SERVER_NOT_FOUND), which
	// converges within microseconds of the drain above once the deferred
	// forgetOrphanIf runs.
	for {
		res := m.Handle(context.Background(), session.Command{CommandID: "stop2", ServerID: "s1", Kind: "StopServer"})
		if res.ErrorCode == session.CommandErrorServerNotFound {
			break
		}
		select {
		case <-overall:
			t.Fatalf("orphan not cleared after instance exit; last = %+v", res)
		default:
		}
		runtime.Gosched()
	}
}

// flushOrphanInstance combines the orphanInstance latch (fail-then-succeed Stop)
// with the rconFailInstance preFallback invocation, so a test can exercise the
// pre-stop RCON flush across the orphan retry path.
type flushOrphanInstance struct {
	*fakeInstance
	stopAfter int
	stopCalls int
	stopping  bool
}

func (i *flushOrphanInstance) Stop(ctx context.Context, graceful bool, preFallback ...func(context.Context) bool) error {
	// Call the pre-fallback hook (the flush) before terminate, just as the real
	// containerdriver does on the graceful path (#1007).
	if graceful && len(preFallback) > 0 && preFallback[0] != nil {
		_ = preFallback[0](ctx)
	}
	i.mu.Lock()
	if i.stopping {
		i.mu.Unlock()
		return nil
	}
	i.stopping = true
	i.stopCalls++
	fail := i.stopCalls <= i.stopAfter
	i.mu.Unlock()
	if fail {
		i.mu.Lock()
		i.stopping = false
		i.mu.Unlock()
		return errors.New("driver: process survived kill")
	}
	return i.fakeInstance.Stop(ctx, graceful)
}

// flushOrphanDriver hands out flushOrphanInstances.
type flushOrphanDriver struct {
	inst      *flushOrphanInstance
	stopAfter int
}

func (d *flushOrphanDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.inst = &flushOrphanInstance{fakeInstance: newFakeInstance(spec.ServerID), stopAfter: d.stopAfter}
	return d.inst, nil
}

// A retried stop for a failed-stop orphan must pass the correct driver name to
// openControl so the RCON flush resolves the container's address — not the
// loopback host. On a docker-network topology (RCON not published to the host)
// an empty driver makes the dial fail and the flush is silently skipped
// (issue #1712).
func TestOrphanRetryStopPassesDriverToFlush(t *testing.T) {
	d := &flushOrphanDriver{stopAfter: 1} // first Stop fails, second succeeds
	var drivers []string
	scratch := t.TempDir()
	m := New(map[string]execution.ExecutionDriver{"container": d}, scratch,
		func(_ context.Context, _ string, driver string) (execution.ServerControl, error) {
			drivers = append(drivers, driver)
			return &fakeControl{reply: "ok"}, nil
		})
	m.settlePollInterval = 0

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("seed running instance: %+v", res)
	}

	// Stop #1: driver is captured before take — flush runs with the correct driver.
	first := m.Handle(context.Background(), session.Command{CommandID: "stop1", ServerID: "s1", Kind: "StopServer"})
	if first.Success {
		t.Fatalf("first stop = %+v, want failure (driver could not confirm termination)", first)
	}

	// Stop #2 (retry): the orphan retry must still pass "container" to openControl,
	// not an empty string.
	drivers = nil // reset so we observe only the retry's flush
	retry := m.Handle(context.Background(), session.Command{CommandID: "stop2", ServerID: "s1", Kind: "StopServer"})
	if !retry.Success {
		t.Fatalf("retry stop = %+v, want success", retry)
	}
	if len(drivers) == 0 {
		t.Fatal("retry stop did not call openControl (flush was skipped entirely)")
	}
	for i, got := range drivers {
		if got != "container" {
			t.Fatalf("retry openControl call %d driver = %q, want %q (orphan must retain the driver)", i, got, "container")
		}
	}
}

// A restart whose internal stop fails leaves the same orphan record: it does not
// relaunch, and a retry stop can still terminate the orphan (issue #251).
func TestRestartStopFailureLeavesOrphan(t *testing.T) {
	d := &orphanDriver{stopAfter: 1}
	m := newManager(t, d, nil)
	_ = m.Handle(context.Background(), startCmd())

	res := m.Handle(context.Background(), session.Command{CommandID: "r", ServerID: "s1", Kind: "RestartServer"})
	if res.Success {
		t.Fatalf("restart with failing stop = %+v, want failure", res)
	}
	if res.ErrorCode == session.CommandErrorServerNotFound {
		t.Fatalf("restart stop error = %v, want a stop-failure code, not SERVER_NOT_FOUND", res.ErrorCode)
	}

	retry := m.Handle(context.Background(), session.Command{CommandID: "stop2", ServerID: "s1", Kind: "StopServer"})
	if !retry.Success {
		t.Fatalf("retry stop after failed restart = %+v, want success", retry)
	}
}
