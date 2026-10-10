// Package goroutineleak shares Manager goroutine leak checks between unit and E2E test binaries.
package goroutineleak

import (
	"bytes"
	"fmt"
	"os"
	"runtime"
	"strings"
	"time"
)

// Match method frames exactly: the trailing parenthesis excludes nested watcher functions.
// Convergers are checked separately by the orphan tests.
var frames = []string{
	"instancemanager.(*Manager).pump(",
	"instancemanager.(*Manager).metricsPump(",
	"instancemanager.(*Manager).metricsPump.func1(",
	"instancemanager.(*Manager).logPump(",
	"instancemanager.(*Manager).statusDispatcher(",
	"instancemanager.(*Manager).reclaimDeletedScratches(",
	"instancemanager.(*Manager).restoreSaveOnWhileClosing(",
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

// Settle polls for goroutine frames to disappear after WaitGroup completion.
// A joined goroutine may briefly retain its frame; a persistent leak still exhausts the budget.
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

// FailIfSurvivors checks that Manager goroutines are gone after all test cleanup.
// Run only after a passing suite so failed-test cleanup does not obscure the original failure.
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
