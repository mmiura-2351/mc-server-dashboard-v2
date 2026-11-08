package instancemanager

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// Ordering of the resync's reports against the events of the commands that
// finish around it (issue #3587). The API stamps each report on receipt, so a
// resync report queued after a newer event for its id overwrites that event with
// the older state. Each report must reach the stream before any event newer than
// what it reports, or not at all.

// lastStateRacingResync runs a resync against step, which ends a command for
// serverID, in the order most hostile to the resync: at the point where the
// resync has read serverID's state and has yet to queue the report of it
// (beforeResyncReport). Where a command can take mu at that point, as one could
// between the old snapshot's unlock and its sends, step runs to its end there and
// the resync goes on only once step's want is on the stream. Where mu is held,
// step runs concurrently and has to wait for the resync. It returns the last state
// serverID reported: want, unless the resync's report followed step's event.
func lastStateRacingResync(t *testing.T, m *Manager, serverID, want string, step func()) string {
	t.Helper()
	last, seen := "", false
	next := func() {
		select {
		case ev := <-m.Events():
			if ev.ServerID == serverID {
				last, seen = ev.State, seen || ev.State == want
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("%s never reported %s; last %q", serverID, want, last)
		}
	}
	stepped := make(chan struct{})
	m.beforeResyncReport = func(id string) {
		if id != serverID {
			return
		}
		if !m.mu.TryLock() {
			go func() {
				defer close(stepped)
				step()
			}()
			return
		}
		m.mu.Unlock()
		step()
		close(stepped)
		for !seen {
			next()
		}
	}

	m.ResyncStatus()

	select {
	case <-stepped:
	case <-time.After(5 * time.Second):
		t.Fatalf("the command for %s never ended, or the resync never reported it", serverID)
	}
	for !seen {
		next()
	}
	for {
		select {
		case ev := <-m.Events():
			if ev.ServerID == serverID {
				last = ev.State
			}
		default:
			return last
		}
	}
}

// A stop that evicts the instance at the resync's report of it. The resync read
// the instance running, and that report must not follow the stop's stopped.
func TestResyncReportOfAnInstanceIsNotQueuedAfterItsStop(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{})
	startHeld(t, m)
	var res session.CommandResult
	last := lastStateRacingResync(t, m, s1, "stopped", func() {
		res = m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: s1, Kind: "StopServer"})
	})

	if !res.Success {
		t.Fatalf("stop = %+v, want success", res)
	}
	if last != "stopped" {
		t.Fatalf("last %s report = %q, want stopped: the resync's report followed the stop's", s1, last)
	}
}

// A retry stop that confirms a failed-stop orphan gone at the resync's report of
// it. The resync's unknown must not follow the retry's stopped.
func TestResyncReportOfAnOrphanIsNotQueuedAfterItsRetryStop(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil).WithTransfer(&fakeTransfer{})
	inst := newFakeInstance(s1)
	m.orphans[s1] = orphanEntry{inst: inst, driver: "container"}
	m.startPumps(s1, inst)
	drainStatuses(t, m, 1) // the fake's own running
	var res session.CommandResult
	last := lastStateRacingResync(t, m, s1, "stopped", func() {
		res = m.Handle(context.Background(), session.Command{CommandID: "stop", ServerID: s1, Kind: "StopServer"})
	})

	if !res.Success {
		t.Fatalf("retry stop = %+v, want success", res)
	}
	if last != "stopped" {
		t.Fatalf("last %s report = %q, want stopped: the resync's unknown followed the retry's stopped", s1, last)
	}
}

// A launch that ends at the resync's report of its reservation. The resync's
// starting must not follow what the launch's end reports: the corrective stopped
// of a failed launch (releaseFailedLaunch), or the registered instance's running.
func TestResyncReportOfAReservedLaunchIsNotQueuedAfterItsEnd(t *testing.T) {
	t.Run("fails", func(t *testing.T) {
		held := newHeldLaunchDriver(errors.New("test: launch failed"))
		m := newManager(t, held, nil)
		seedScratch(t, m, s1)
		done := make(chan session.CommandResult, 1)
		go func() { done <- m.Handle(context.Background(), startCmd()) }()
		awaitEnter(t, held.entered)
		var res session.CommandResult
		last := lastStateRacingResync(t, m, s1, "stopped", func() {
			close(held.release)
			res = <-done
		})

		if res.Success {
			t.Fatalf("start = %+v, want the launch's failure", res)
		}
		if last != "stopped" {
			t.Fatalf("last %s report = %q, want stopped: the resync's starting followed the correction", s1, last)
		}
	})
	t.Run("registers", func(t *testing.T) {
		d := newGatedDriver()
		m := newManager(t, d, nil)
		seedScratch(t, m, s1)
		done := make(chan session.CommandResult, 1)
		go func() { done <- m.Handle(context.Background(), startCmd()) }()
		awaitEnter(t, d.entered)
		var res session.CommandResult
		last := lastStateRacingResync(t, m, s1, "running", func() {
			close(d.release)
			res = <-done
		})

		if !res.Success {
			t.Fatalf("start = %+v, want success", res)
		}
		if last != "running" {
			t.Fatalf("last %s report = %q, want running: the resync's starting followed the instance's running", s1, last)
		}
	})
}

// stopThenHoldInstance reports its stop's terminal at once and then holds Stop
// until release closes: the window in which the instance's stopped is on the
// stream while the stop still holds the id.
type stopThenHoldInstance struct {
	*fakeInstance
	release chan struct{}
}

func (i *stopThenHoldInstance) Stop(ctx context.Context, graceful bool, preFallback ...func(context.Context) bool) error {
	err := i.fakeInstance.Stop(ctx, graceful, preFallback...)
	<-i.release
	return err
}

// stopThenHoldDriver launches a stopThenHoldInstance first and plain fakes after,
// so a restart's relaunch registers at once.
type stopThenHoldDriver struct {
	mu   sync.Mutex
	inst *stopThenHoldInstance
}

func (d *stopThenHoldDriver) Start(_ context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.inst == nil {
		d.inst = &stopThenHoldInstance{fakeInstance: newFakeInstance(spec.ServerID), release: make(chan struct{})}
		return d.inst, nil
	}
	return newFakeInstance(spec.ServerID), nil
}

// A resync that comes after a stop's terminal is on the stream but before the stop
// lets go of the id reports what the stopped instance says, not the stop's leg:
// stopping (or the orphan's unknown) there would overwrite the stopped already
// sent. The same holds for a restart's stop leg and for a retry stop of a
// failed-stop orphan.
func TestResyncAfterAStopsTerminalReportsTheTerminal(t *testing.T) {
	for _, verb := range []string{"StopServer", "RestartServer", "orphan StopServer"} {
		t.Run(verb, func(t *testing.T) {
			d := &stopThenHoldDriver{}
			m := newManager(t, d, nil).WithTransfer(&fakeTransfer{})
			kind := verb
			if verb == "orphan StopServer" {
				kind = "StopServer"
				inst, _ := d.Start(context.Background(), execution.InstanceSpec{ServerID: s1})
				m.orphans[s1] = orphanEntry{inst: inst, driver: "container"}
				m.startPumps(s1, inst)
				drainStatuses(t, m, 1) // the fake's own running
			} else {
				startHeld(t, m)
			}
			done := make(chan session.CommandResult, 1)
			go func() {
				done <- m.Handle(context.Background(), session.Command{CommandID: "c2", ServerID: s1, Kind: kind})
			}()
			if ev := drainStatuses(t, m, 1)[0]; ev.State != "stopped" {
				t.Fatalf("stop reported %+v, want stopped", ev)
			}

			m.ResyncStatus()

			if states := statesOf(t, m, 1); states[s1] != "stopped" {
				t.Fatalf("resync states = %v, want %s stopped", states, s1)
			}
			close(d.inst.release)
			if res := <-done; !res.Success {
				t.Fatalf("%s = %+v, want success", verb, res)
			}
		})
	}
}

// Pins existing behaviour (the PR #3585 r2 review item on issue #3587): a
// replacement command issued while a failed launch's corrective stopped is still
// waiting to be queued is refused BUSY, and claims the id only once the correction
// is on the stream. Releasing the reservation before the correction is sent would
// let the replacement's events overtake it.
func TestReplacementWaitsForAFailedLaunchsCorrection(t *testing.T) {
	held := newHeldLaunchDriver(errors.New("test: launch failed"))
	m := newManager(t, held, nil)
	seedScratch(t, m, s1)
	done := make(chan session.CommandResult, 1)
	go func() { done <- m.Handle(context.Background(), startCmd()) }()
	awaitEnter(t, held.entered)
	m.ResyncStatus()
	if states := statesOf(t, m, 1); states[s1] != "starting" {
		t.Fatalf("resync states = %v, want %s starting", states, s1)
	}

	// Hold the status stream so the correction cannot be queued, then end the launch.
	m.statusMu.Lock()
	close(held.release)
	waitFor(t, func() bool {
		m.mu.Lock()
		defer m.mu.Unlock()
		return m.reserved[s1].leg != legStarting
	})
	m.drivers["container"] = &fakeDriver{}
	replacement := m.Handle(context.Background(), startCmd())
	m.statusMu.Unlock()

	if replacement.Success || replacement.ErrorCode != session.CommandErrorBusy {
		t.Fatalf("replacement while the correction is pending = %+v, want BUSY", replacement)
	}
	if res := <-done; res.Success {
		t.Fatalf("start = %+v, want the launch's failure", res)
	}
	if ev := drainStatuses(t, m, 1)[0]; ev.ServerID != s1 || ev.State != "stopped" {
		t.Fatalf("status = %+v, want %s stopped", ev, s1)
	}
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("replacement after the correction = %+v, want success", res)
	}
}
