// Package goroutineleak holds the census of the background goroutines an
// instancemanager.Manager owns, and the assertions the test binaries that build
// Managers make against it: instancemanager's own tests, and the `-tags e2e`
// suite in worker/test/e2e that drives the same Manager against a real Docker
// daemon (issues #2777, #2875, #2881).
//
// It is an ordinary package rather than a _test.go file because a _test.go file
// is visible only to its own package's test binary, and both binaries have to
// assert the same thing. It sits beside instancemanager rather than in a
// test-support tree because the frames it names are that package's methods: a
// pump added to instancemanager.go has its census entry in the next directory
// over. Nothing outside a test binary imports it, and it imports nothing but the
// standard library, so the layering rule of internal/application/doc.go stands.
//
// One list for both binaries is the point of the package. Copied into
// worker/test/e2e the list could only drift into a FALSE GREEN — a pump added
// later checked in the unit binary and silently unchecked in the e2e one — so it
// stays unexported here and both binaries reach it only through the functions
// below.
package goroutineleak

import (
	"bytes"
	"fmt"
	"os"
	"runtime"
	"strings"
	"time"
)

// frames names the background goroutines a Manager owns: the ones New and
// startPumps launch, the metrics pump's teardown watcher, and the deleted-scratch
// reclaim ReclaimDeletedScratches launches (issue #2878). A goroutine dump
// is the only evidence that says whether they are still there once the manager
// that started them is gone, which is how issue #2777 was found — the
// instancemanager package's own test run left ~91k of them behind. The trailing
// "(" is load-bearing: it keeps ".metricsPump(" from also matching
// ".metricsPump.func1(".
//
// Convergers are not listed: PR #2775 already joins them, and orphan_test.go
// pins that directly.
var frames = []string{
	"instancemanager.(*Manager).pump(",
	"instancemanager.(*Manager).metricsPump(",
	"instancemanager.(*Manager).metricsPump.func1(",
	"instancemanager.(*Manager).logPump(",
	"instancemanager.(*Manager).statusDispatcher(",
	"instancemanager.(*Manager).reclaimDeletedScratches(",
}

// live counts the manager-owned background goroutines currently in the runtime
// and returns the dump they were counted from. Each stack lists a given frame at
// most once, so counting occurrences counts goroutines.
func live() (int, []byte) {
	buf := make([]byte, 1<<20)
	for {
		n := runtime.Stack(buf, true)
		if n < len(buf) {
			buf = buf[:n]
			break
		}
		buf = make([]byte, 2*len(buf))
	}
	count := 0
	for _, f := range frames {
		count += bytes.Count(buf, []byte(f))
	}
	return count, buf
}

// Settle polls until want manager-owned goroutines are live, or the budget runs
// out, and reports the last count with the stacks behind it.
//
// It polls rather than sampling once because a goroutine Close has joined is
// past its WaitGroup Done but has not necessarily left its frame yet — a residue
// that clears in microseconds. A leak never clears, so the poll cannot turn one
// green; it only keeps a busy host's scheduling from turning a clean shutdown
// red.
func Settle(want int, budget time.Duration) (int, string) {
	deadline := time.Now().Add(budget)
	for {
		count, dump := live()
		if count == want || time.Now().After(deadline) {
			return count, stacks(dump)
		}
		time.Sleep(time.Millisecond)
	}
}

// stacks reduces a full goroutine dump to the blocks that hold a manager frame,
// so a failure names the leaked goroutines instead of every goroutine in the
// binary.
func stacks(dump []byte) string {
	var kept []string
	for _, block := range strings.Split(string(dump), "\n\n") {
		for _, f := range frames {
			if strings.Contains(block, f) {
				kept = append(kept, block)
				break
			}
		}
	}
	return strings.Join(kept, "\n\n")
}

// FailIfSurvivors turns the code a finished m.Run() returned into the code its
// TestMain should exit with, asserting the acceptance criterion of issue #2777
// on the way: once every test has finished — and with it every t.Cleanup and
// deferred Close — no goroutine a Manager started may still be running. Before
// the fix instancemanager's dump held ~30k status pumps, ~30k metrics pumps and
// their watchers, and one status dispatcher per manager ever built.
//
// It is a package-level check because the leak is: no single test leaks visibly,
// and the cost (a `-race` run paying for tens of thousands of parked goroutines)
// only shows in the aggregate. It runs only on a green suite, so a failing test
// is never buried under a leak report caused by its own abort.
func FailIfSurvivors(code int) int {
	if code != 0 {
		return code
	}
	if count, stacks := Settle(0, 5*time.Second); count != 0 {
		fmt.Fprintf(os.Stderr,
			"%d manager background goroutine(s) outlived the package's tests (issue #2777):\n%s\n",
			count, stacks)
		return 1
	}
	return code
}
