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

// Ordering of the orphan converger's reports against the newer events for the
// same id (issue #3596). The API stamps each report on receipt, so a report the
// converger queues after the orphan's own terminal, or after a replacement's
// events, overwrites them with a stale state.

// exitOnItsOwn models the orphan's process exiting while nobody stops it: the
// instance sets its terminal state and then emits it, as drivers do. closed also
// closes the event stream, after which the pump retires the orphan record.
func exitOnItsOwn(inst *fakeInstance, closed bool) {
	inst.end(execution.StatusEvent{ServerID: inst.serverID, State: execution.StateStopped})
	if closed {
		close(inst.events)
	}
}

// A probe that fails while the orphan exits on its own must not end in an
// `unknown` queued after the pump's terminal `stopped`. "retired" is the exit
// the pump has finished with, record and all. "held" is the exit whose terminal
// is on the stream while the record is still there, which is what the pump's
// forward-then-retire order leaves for a moment.
func TestConvergersUnknownIsNotQueuedAfterTheOrphansOwnExit(t *testing.T) {
	for _, tc := range []struct {
		name    string
		retired bool
	}{{"retired", true}, {"held", false}} {
		t.Run(tc.name, func(t *testing.T) {
			m := newManager(t, &fakeDriver{}, nil)
			shrinkOrphanConverger(m)
			inst := newGatedProbeInstance(s1)
			inst.setAlive(false, errors.New("driver: daemon unreachable"))
			startPumpsFor(m, s1, inst)
			drainStatuses(t, m, 1) // the fake's own running
			m.recordOrphan(s1, inst, "container", "1.21")
			awaitEnter(t, inst.probeEntered)

			// The orphan exits while the probe is out, and the probe then fails.
			exitOnItsOwn(inst.fakeInstance, tc.retired)
			awaitStatus(t, m, s1, execution.StateStopped.String())
			if tc.retired {
				waitFor(t, func() bool {
					m.mu.Lock()
					defer m.mu.Unlock()
					_, ok := m.orphans[s1]
					return !ok
				})
			}
			close(inst.probeRelease)
			// Wait for the round that probed to end: the converger leaves once it
			// finds the record gone, and otherwise probes again.
			if tc.retired {
				waitFor(t, func() bool {
					m.mu.Lock()
					defer m.mu.Unlock()
					return !m.converging[s1]
				})
			} else {
				awaitEnter(t, inst.probeEntered)
			}

		drain:
			for {
				select {
				case ev := <-m.Events():
					if ev.ServerID == s1 && ev.State == orphanUnknownState {
						t.Fatalf("%s reported %q after its own exit's stopped", s1, ev.State)
					}
				default:
					break drain
				}
			}
		})
	}
}

// A stranded orphan's retirement and a replacement start for the id, with the
// replacement run at the point where the converger has retired the record and
// has yet to queue its `stopped` (beforeOrphanReport). Where the replacement can
// take mu there, as it could between the old retire's unlock and its send, it
// runs to its end and the converger goes on only once the replacement's running
// is on the stream. Where mu is held, it runs concurrently and has to wait for
// the converger. Either way the old `stopped` must precede the replacement's
// running.
func TestStrandedOrphansStoppedPrecedesAReplacementsEvents(t *testing.T) {
	m := newManager(t, &fakeDriver{}, nil)
	shrinkOrphanConverger(m)
	seedScratch(t, m, s1)

	// One reader for the whole stream, so the hook can wait on what it sees.
	var mu sync.Mutex
	var states []string
	quit, collected := make(chan struct{}), make(chan struct{})
	go func() {
		defer close(collected)
		for {
			select {
			case ev := <-m.Events():
				if ev.ServerID == s1 {
					mu.Lock()
					states = append(states, ev.State)
					mu.Unlock()
				}
			case <-quit:
				return
			}
		}
	}()
	seen := func(state string) bool {
		mu.Lock()
		defer mu.Unlock()
		for _, s := range states {
			if s == state {
				return true
			}
		}
		return false
	}

	var res session.CommandResult
	replaced := make(chan struct{})
	var once sync.Once
	m.beforeOrphanReport = func(id string) {
		if id != s1 {
			return
		}
		once.Do(func() {
			replace := func() { res = m.Handle(context.Background(), startCmd()) }
			if !m.mu.TryLock() {
				go func() {
					defer close(replaced)
					replace()
				}()
				return
			}
			m.mu.Unlock()
			replace()
			close(replaced)
			deadline := time.Now().Add(5 * time.Second)
			for !seen(execution.StateRunning.String()) && time.Now().Before(deadline) {
				time.Sleep(time.Millisecond)
			}
		})
	}

	// Stranded: the instance has exited and its pump retired nothing, so only the
	// converger can clear the record (issue #2468 item 4).
	inst := newFakeInstance(s1)
	close(inst.events)
	inst.setAlive(false, nil)
	m.recordOrphan(s1, inst, "container", "1.21")

	select {
	case <-replaced:
	case <-time.After(5 * time.Second):
		t.Fatal("the converger never retired the stranded record")
	}
	if !res.Success {
		t.Fatalf("replacement start = %+v, want success", res)
	}
	waitFor(t, func() bool { return seen(execution.StateRunning.String()) && seen(execution.StateStopped.String()) })
	close(quit)
	<-collected

	if want := []string{"stopped", "running"}; len(states) != 2 || states[0] != want[0] || states[1] != want[1] {
		t.Fatalf("%s reported %v, want %v: the retired orphan's stopped followed the replacement's events", s1, states, want)
	}
}
