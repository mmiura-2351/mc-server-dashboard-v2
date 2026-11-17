package containerdriver

import (
	"sync"
	"sync/atomic"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// newFinalizeInstance is an instance with an open event stream roomy enough
// that a second terminal event could not displace the first by coalescing.
func newFinalizeInstance() *instance {
	return &instance{
		spec:    execution.InstanceSpec{ServerID: "s1"},
		events:  make(chan execution.StatusEvent, 8),
		exited:  make(chan struct{}),
		logPump: execution.NewLogPump("s1", 8),
	}
}

// bufferedEvents drains what the open stream holds right now.
func bufferedEvents(inst *instance) []execution.StatusEvent {
	var evs []execution.StatusEvent
	for {
		select {
		case ev := <-inst.events:
			evs = append(evs, ev)
		default:
			return evs
		}
	}
}

// assertFinalizedAs fails unless Status, Terminal, the stream and exited all
// report want as the instance's one terminal (issue #3652).
func assertFinalizedAs(t *testing.T, inst *instance, evs []execution.StatusEvent, want execution.StatusEvent) {
	t.Helper()
	if got := inst.Status(); got != want.State {
		t.Fatalf("Status = %v, want the first terminal %v", got, want.State)
	}
	if got, ok := inst.Terminal(); !ok || got != want {
		t.Fatalf("Terminal = %+v, %v, want the first terminal %+v", got, ok, want)
	}
	if len(evs) != 1 || evs[0] != want {
		t.Fatalf("stream = %+v, want the one terminal event %+v", evs, want)
	}
	select {
	case <-inst.exited:
	default:
		t.Fatal("exited is not closed after the terminal")
	}
}

// A second terminate with a different terminal state changes nothing: the
// first terminal stays Status, Terminal and the stream's only terminal event,
// and exited is closed exactly once (issue #3652).
func TestSecondTerminateIsANoOp(t *testing.T) {
	inst := newFinalizeInstance()

	inst.terminate(execution.StateStopped, "", execution.CrashReasonUnspecified)
	inst.terminate(execution.StateCrashed, "boom", execution.CrashReasonForgeInstallFailed)

	assertFinalizedAs(t, inst, bufferedEvents(inst),
		execution.StatusEvent{ServerID: "s1", State: execution.StateStopped})
}

// A second install-phase finish is a no-op rather than a panic on the second
// close of exited or of the event stream (issue #3652).
func TestSecondFinishIsANoOp(t *testing.T) {
	inst := newFinalizeInstance()

	inst.finishTerminal(execution.StateStopped, "")
	inst.finishInstallCrash(execution.CrashReasonForgeInstallFailed, "boom")

	var evs []execution.StatusEvent
	for ev := range inst.events {
		evs = append(evs, ev)
	}
	assertFinalizedAs(t, inst, evs, execution.StatusEvent{ServerID: "s1", State: execution.StateStopped})
}

// Concurrent terminates elect exactly one winner, and Status, Terminal, the
// stream and exited all agree on its terminal (issue #3652).
func TestConcurrentTerminatesElectOneTerminal(t *testing.T) {
	inst := newFinalizeInstance()
	start := make(chan struct{})
	var wg sync.WaitGroup
	var wins atomic.Int32
	for n := range 16 {
		state := execution.StateStopped
		if n%2 == 1 {
			state = execution.StateCrashed
		}
		wg.Go(func() {
			<-start
			if inst.terminate(state, "", execution.CrashReasonUnspecified) {
				wins.Add(1)
			}
		})
	}
	close(start)
	wg.Wait()

	if got := wins.Load(); got != 1 {
		t.Fatalf("terminate won %d times, want exactly once", got)
	}
	want, ok := inst.Terminal()
	if !ok {
		t.Fatal("no Terminal after the terminates")
	}
	assertFinalizedAs(t, inst, bufferedEvents(inst), want)
}
