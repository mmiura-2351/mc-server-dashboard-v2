package instancemanager

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// managerFrames names the background goroutines a Manager owns: the ones New and
// startPumps launch, the metrics pump's teardown watcher, and the deleted-scratch
// reclaim ReclaimDeletedScratches launches (issue #2878). A goroutine dump
// is the only evidence that says whether they are still there once the manager
// that started them is gone, which is how issue #2777 was found — the package's
// own test run left ~91k of them behind. The trailing "(" is load-bearing: it
// keeps ".metricsPump(" from also matching ".metricsPump.func1(".
//
// Convergers are not listed: PR #2775 already joins them, and orphan_test.go
// pins that directly.
var managerFrames = []string{
	"instancemanager.(*Manager).pump(",
	"instancemanager.(*Manager).metricsPump(",
	"instancemanager.(*Manager).metricsPump.func1(",
	"instancemanager.(*Manager).logPump(",
	"instancemanager.(*Manager).statusDispatcher(",
	"instancemanager.(*Manager).reclaimDeletedScratches(",
}

// liveManagerGoroutines counts the manager-owned background goroutines currently
// in the runtime and returns the dump they were counted from. Each stack lists a
// given frame at most once, so counting occurrences counts goroutines.
func liveManagerGoroutines() (int, []byte) {
	buf := make([]byte, 1<<20)
	for {
		n := runtime.Stack(buf, true)
		if n < len(buf) {
			buf = buf[:n]
			break
		}
		buf = make([]byte, 2*len(buf))
	}
	live := 0
	for _, f := range managerFrames {
		live += bytes.Count(buf, []byte(f))
	}
	return live, buf
}

// settleManagerGoroutines polls until want manager-owned goroutines are live, or
// the budget runs out, and reports the last count with the stacks behind it.
//
// It polls rather than sampling once because a goroutine Close has joined is
// past its WaitGroup Done but has not necessarily left its frame yet — a residue
// that clears in microseconds. A leak never clears, so the poll cannot turn one
// green; it only keeps a busy host's scheduling from turning a clean shutdown
// red.
func settleManagerGoroutines(want int, budget time.Duration) (int, string) {
	deadline := time.Now().Add(budget)
	for {
		live, dump := liveManagerGoroutines()
		if live == want || time.Now().After(deadline) {
			return live, managerStacks(dump)
		}
		time.Sleep(time.Millisecond)
	}
}

// managerStacks reduces a full goroutine dump to the blocks that hold a manager
// frame, so a failure names the leaked goroutines instead of every goroutine in
// the binary.
func managerStacks(dump []byte) string {
	var kept []string
	for _, block := range strings.Split(string(dump), "\n\n") {
		for _, f := range managerFrames {
			if strings.Contains(block, f) {
				kept = append(kept, block)
				break
			}
		}
	}
	return strings.Join(kept, "\n\n")
}

// awaitManagerGoroutines asserts that exactly want manager-owned goroutines are
// live, allowing the same settling window the other waits in this package use.
func awaitManagerGoroutines(t *testing.T, want int) {
	t.Helper()
	if live, stacks := settleManagerGoroutines(want, 5*time.Second); live != want {
		t.Fatalf("%d manager background goroutine(s) live, want %d:\n%s", live, want, stacks)
	}
}

// TestMain runs the package and then asserts the acceptance criterion of issue
// #2777 directly: once every test has finished — and with it every t.Cleanup
// Close — no goroutine the Manager started may still be running. Before the fix
// this dump held ~30k status pumps, ~30k metrics pumps and their watchers, and
// one status dispatcher per manager ever built.
//
// It is a package-level check because the leak is: no single test leaks
// visibly, and the cost (a `-race` run paying for tens of thousands of parked
// goroutines) only shows in the aggregate. It runs only on a green suite, so a
// failing test is never buried under a leak report caused by its own abort.
func TestMain(m *testing.M) {
	code := m.Run()
	if code == 0 {
		if live, stacks := settleManagerGoroutines(0, 5*time.Second); live != 0 {
			fmt.Fprintf(os.Stderr,
				"%d manager background goroutine(s) outlived the package's tests (issue #2777):\n%s\n",
				live, stacks)
			code = 1
		}
	}
	os.Exit(code)
}

// A running instance's status pump parks on the instance's event channel and the
// metrics pump runs off the clock until the status pump releases it. Nothing
// closed either: an instance that never reaches a terminal state — a server
// still up when the Worker goes down, and every fake in this package — left the
// pair (and the metrics pump's cancel watcher) parked for the life of the
// process, against a manager nobody owned any more (issue #2777). Close ends
// them.
func TestCloseEndsThePumpsOfALiveInstance(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	m := newManager(t, &fakeDriver{}, nil)
	seedScratch(t, m, "s1")
	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start failed: %+v", res)
	}
	// The dispatcher, the status pump, the metrics pump and its cancel watcher.
	awaitManagerGoroutines(t, 4)

	m.Close()

	awaitManagerGoroutines(t, 0)
}

// statusDispatcher is started by New and parks on statusNotify, which nothing
// ever closes — so every manager ever built left one behind, one per test in
// this package (issue #2777).
func TestCloseEndsTheStatusDispatcher(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	m := newManager(t, &fakeDriver{}, nil)
	awaitManagerGoroutines(t, 1)

	m.Close()

	awaitManagerGoroutines(t, 0)
}

// The dispatcher's send is BLOCKING by design: coalesced status is state, not a
// stream, so backpressure is absorbed rather than dropped (issue #96). By the
// time Close runs, nothing drains the merged stream any more — main.go closes
// the manager only after the session runner has returned — so a dispatcher
// observing the shutdown only between events would hold Close forever on a full
// sink. It observes it on the send too, and the parked status is dropped.
func TestCloseEndsAStatusDispatcherBlockedOnAFullSink(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	m := newManager(t, &fakeDriver{}, nil)

	// Fill the sink through the fast path, then park one more status behind it:
	// with events full, sendStatus routes through the pending slot and wakes the
	// dispatcher, which takes the entry and blocks on the send.
	for i := 0; i < cap(m.Events()); i++ {
		m.sendStatus(session.StatusEvent{ServerID: fmt.Sprintf("filler-%d", i), State: "running"})
	}
	m.sendStatus(session.StatusEvent{ServerID: "s1", State: "running"})
	waitFor(t, func() bool {
		m.statusMu.Lock()
		defer m.statusMu.Unlock()
		return m.coalescing["s1"] && len(m.dirtyStatus) == 0 && len(m.pendingStatus) == 0
	})

	closed := make(chan struct{})
	go func() { m.Close(); close(closed) }()
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not return: the dispatcher is still blocked sending into a sink nobody drains")
	}

	awaitManagerGoroutines(t, 0)
}

// A start that lands after Close registers its instance but starts no pumps.
// Close has already run the Wait, so a pump spawned afterwards is both a
// goroutine nobody joins and — an Add racing a Wait that has reached zero — a
// panic away. It mirrors recordOrphan's guard for convergers (issue #2493).
func TestStartAfterCloseSpawnsNoPumps(t *testing.T) {
	awaitManagerGoroutines(t, 0)
	m := newManager(t, &fakeDriver{}, nil)
	seedScratch(t, m, "s1")
	m.Close()

	if res := m.Handle(context.Background(), startCmd()); !res.Success {
		t.Fatalf("start failed: %+v", res)
	}

	// The guard itself, asserted directly: a spawn on a closed manager is REFUSED,
	// not merely short-lived. Watching the goroutines alone cannot see this — one
	// started here observes the already-cancelled shutdown and leaves within
	// microseconds — but the danger is not its lifetime, it is the Add: landing
	// beside a Wait still in progress, that is a panic in the Worker's shutdown
	// path, not a leak.
	if m.goBackground(func() {}) {
		t.Fatal("goBackground started a goroutine on a closed manager; Close has already run the Wait that counts it")
	}

	awaitManagerGoroutines(t, 0)
}
