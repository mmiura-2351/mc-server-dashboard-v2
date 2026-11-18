package containerdriver

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// The install log is bounded (issue #3282): a start empties it, it keeps every
// attempt of that start's install, and it stays under
// execution.ForgeInstallLogMaxBytes by keeping the start of the install and its
// latest output, with a line saying what was dropped between them.

// scriptInstallOutputs makes the install containers docker creates print outputs
// in turn, one per attempt across every start. Retries reuse the installer's
// name, so the output is swapped as each one is created.
func scriptInstallOutputs(docker *forgeFakeDocker, outputs ...string) {
	next := 0
	docker.onCreateHook = func(spec CreateSpec) {
		if spec.Name != forgeInstallID {
			return
		}
		docker.mu.Lock()
		defer docker.mu.Unlock()
		if next < len(outputs) {
			docker.logBodies[forgeInstallID] = string(frame(dockerStreamStdout, outputs[next]))
			next++
		}
	}
}

// runFailingForgeStart runs one start whose every install attempt exits 1, as the
// daemon reports it (the status code, no error), until the start has crashed.
func runFailingForgeStart(t *testing.T, d *Driver, docker *forgeFakeDocker, dir string) {
	t.Helper()
	gate := make(chan waitResult, maxInstallRetries+1)
	for range maxInstallRetries + 1 {
		gate <- waitResult{code: 1}
	}
	docker.mu.Lock()
	docker.waitGates[forgeInstallID] = gate
	docker.mu.Unlock()

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateCrashed)
	drainClosed(inst.Events())
}

// installOutput is at least n bytes of installer output, in whole lines that
// name what printed them.
func installOutput(name string, n int) string {
	var b strings.Builder
	for i := 0; b.Len() < n; i++ {
		fmt.Fprintf(&b, "%s: downloaded library %07d\n", name, i)
	}
	return b.String()
}

// The log holds the latest start's install only, and every attempt of it: a
// start empties it, a retry does not.
func TestForgeInstallLogHoldsEveryAttemptOfTheLatestStartOnly(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	scriptInstallOutputs(docker,
		"first start, attempt 1\n", "first start, attempt 2\n", "first start, attempt 3\n",
		"second start, attempt 1\n", "second start, attempt 2\n", "second start, attempt 3\n")
	d := forgeDriver(docker)

	runFailingForgeStart(t, d, docker, dir)
	runFailingForgeStart(t, d, docker, dir)

	want := "second start, attempt 1\nsecond start, attempt 2\nsecond start, attempt 3\n"
	if got := readInstallLog(t, dir); got != want {
		t.Fatalf("install log = %q, want %q", got, want)
	}
}

// A server that keeps failing its install, start after start, never grows its log
// past the bound, though each start alone prints more than that.
func TestForgeInstallLogStaysBoundedAcrossRepeatedFailingStarts(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	const starts = 2
	var outputs []string
	for start := range starts {
		for attempt := range maxInstallRetries + 1 {
			outputs = append(outputs, installOutput(fmt.Sprintf("start %d attempt %d", start+1, attempt+1), 5<<18))
		}
	}
	scriptInstallOutputs(docker, outputs...)
	d := forgeDriver(docker)

	for start := range starts {
		runFailingForgeStart(t, d, docker, dir)
		if size := len(readInstallLog(t, dir)); size > execution.ForgeInstallLogMaxBytes {
			t.Fatalf("after start %d the install log is %d bytes, want at most %d",
				start+1, size, execution.ForgeInstallLogMaxBytes)
		}
	}
}

// Past the bound the log keeps the start of the install, then one line saying how
// much was dropped, then the latest attempt's output whole.
func TestForgeInstallLogKeepsTheStartOfTheInstallAndTheLatestAttemptWhole(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	outputs := []string{
		installOutput("attempt 1", 5<<18), installOutput("attempt 2", 5<<18), installOutput("attempt 3", 5<<18),
	}
	scriptInstallOutputs(docker, outputs...)

	runFailingForgeStart(t, forgeDriver(docker), docker, dir)

	log := readInstallLog(t, dir)
	if len(log) > execution.ForgeInstallLogMaxBytes {
		t.Fatalf("install log is %d bytes, want at most %d", len(log), execution.ForgeInstallLogMaxBytes)
	}
	head, omission, latest := splitAtOmission(t, log)
	if latest != outputs[2] {
		t.Fatalf("install log after the omission line is %d bytes, want the latest attempt's whole output (%d bytes)",
			len(latest), len(outputs[2]))
	}
	if head == "" || !strings.HasPrefix(outputs[0], head) {
		t.Fatalf("install log starts with %.80q, want the start of the first attempt", log)
	}
	if omitted := len(outputs[0]) + len(outputs[1]) - len(head); !strings.Contains(omission, strconv.Itoa(omitted)) {
		t.Fatalf("omission line = %q, want it to say %d bytes were omitted", omission, omitted)
	}
}

// An attempt that alone prints more than the bound keeps its latest output: the
// end of the stream, the failure the installer died on, is never what is
// dropped, and it starts at a whole line.
func TestForgeInstallLogKeepsTheEndOfAnAttemptLargerThanTheBound(t *testing.T) {
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	// The out-of-memory error is reported at once, so this is the only attempt.
	output := installOutput("attempt 1", 4<<20) + "java.lang.OutOfMemoryError: Java heap space\n"
	scriptInstallOutputs(docker, output)

	runFailingForgeStart(t, forgeDriver(docker), docker, dir)

	log := readInstallLog(t, dir)
	if len(log) > execution.ForgeInstallLogMaxBytes {
		t.Fatalf("install log is %d bytes, want at most %d", len(log), execution.ForgeInstallLogMaxBytes)
	}
	head, _, tail := splitAtOmission(t, log)
	if head == "" || !strings.HasPrefix(output, head) {
		t.Fatalf("install log starts with %.80q, want the start of the output", log)
	}
	if !strings.HasSuffix(output, tail) || output[len(output)-len(tail)-1] != '\n' {
		t.Fatalf("install log after the omission line starts with %.80q, want whole lines from the end of the output", tail)
	}
	if len(tail) < 1<<20 {
		t.Fatalf("install log keeps %d bytes of the latest output, want at least 1 MiB", len(tail))
	}
}

// splitAtOmission splits an install log at the one line saying what was omitted:
// what came before it, the line itself, and what follows it.
func splitAtOmission(t *testing.T, log string) (before, omission, after string) {
	t.Helper()
	lines := strings.SplitAfter(log, "\n")
	at := -1
	for i, line := range lines {
		if strings.Contains(line, "omitted") {
			if at >= 0 {
				t.Fatalf("install log has omission lines %q and %q, want one", lines[at], line)
			}
			at = i
		}
	}
	if at < 0 {
		t.Fatal("install log has no line saying what was omitted")
	}
	return strings.Join(lines[:at], ""), lines[at], strings.Join(lines[at+1:], "")
}
