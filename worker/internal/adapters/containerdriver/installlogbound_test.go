package containerdriver

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"unicode/utf8"

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
	// The first start prints more, so none of it can hide under what the second
	// one writes over it.
	scriptInstallOutputs(docker,
		"first start, attempt 1, printing more\n", "first start, attempt 2, printing more\n",
		"first start, attempt 3, printing more\n",
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
// much was omitted, then the latest output, which ends with the latest attempt's
// output whole.
func TestForgeInstallLogKeepsTheStartOfTheInstallAndTheLatestAttemptWhole(t *testing.T) {
	shrinkInstallBackoff(t)
	dir := t.TempDir()
	docker := newForgeFakeDocker()
	// 2 MiB each, the most one install prints.
	outputs := []string{
		installOutput("attempt 1", 2<<20), installOutput("attempt 2", 2<<20), installOutput("attempt 3", 2<<20),
	}
	scriptInstallOutputs(docker, outputs...)

	runFailingForgeStart(t, forgeDriver(docker), docker, dir)

	log := readInstallLog(t, dir)
	if len(log) > execution.ForgeInstallLogMaxBytes {
		t.Fatalf("install log is %d bytes, want at most %d", len(log), execution.ForgeInstallLogMaxBytes)
	}
	head, omission, latest := splitAtOmission(t, log)
	if !strings.HasSuffix(latest, outputs[2]) {
		t.Fatal("install log does not end with the latest attempt's whole output")
	}
	if head == "" || !strings.HasPrefix(outputs[0], head) {
		t.Fatalf("install log starts with %.80q, want the start of the first attempt", log)
	}
	printed := strings.Join(outputs, "")
	if !strings.HasSuffix(printed, latest) {
		t.Fatalf("install log after the omission line starts with %.80q, want the end of the output", latest)
	}
	if omitted := len(printed) - len(head) - len(latest); !strings.Contains(omission, strconv.Itoa(omitted)) {
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

// dyingLogFile passes the log's writes through until the log first rewrites
// itself, lets that one call through and fails every call after it: a Worker
// that dies in the middle of a compaction, nothing synced.
type dyingLogFile struct {
	f    *os.File
	size int64
	died bool
}

var errWorkerDied = errors.New("the worker died")

func (d *dyingLogFile) WriteAt(p []byte, off int64) (int, error) {
	if d.died {
		return 0, errWorkerDied
	}
	d.died = off < d.size
	d.size = max(d.size, off+int64(len(p)))
	return d.f.WriteAt(p, off)
}

func (d *dyingLogFile) Truncate(size int64) error {
	if d.died {
		return errWorkerDied
	}
	d.died = true
	d.size = size
	return d.f.Truncate(size)
}

// A compaction cut short leaves a log that is still under its bound and still
// holds the latest output it was given: the rewrite lands before the file is cut
// to its end, never after.
func TestInstallLogCutShortMidCompactionStillHoldsTheLatestOutput(t *testing.T) {
	f, err := os.Create(filepath.Join(t.TempDir(), "forge-install.log"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = f.Close() })
	file := &dyingLogFile{f: f}
	l := &installLog{file: file, logger: slog.New(slog.DiscardHandler)}
	l.beginAttempt()

	var given strings.Builder
	for _, line := range strings.SplitAfter(installOutput("attempt 1", 4<<20), "\n") {
		if file.died {
			break
		}
		l.write([]byte(line))
		given.WriteString(line)
	}
	if !file.died {
		t.Fatal("the log was never compacted")
	}

	data, err := os.ReadFile(f.Name())
	if err != nil {
		t.Fatal(err)
	}
	if len(data) > execution.ForgeInstallLogMaxBytes {
		t.Fatalf("install log is %d bytes, want at most %d", len(data), execution.ForgeInstallLogMaxBytes)
	}
	if latest := given.String()[given.Len()-1<<20:]; !strings.Contains(string(data), latest) {
		t.Fatal("install log cut short does not hold the latest MiB of output it was given")
	}
}

// A log that cannot be opened is refused once for the start's install, not once
// an attempt: the install has one log.
func TestForgeInstallLogIsRefusedOnceForEveryAttemptOfAStart(t *testing.T) {
	shrinkInstallBackoff(t)
	dir, outside := t.TempDir(), t.TempDir()
	if err := os.Symlink(outside, filepath.Join(dir, "logs")); err != nil {
		t.Fatal(err)
	}
	docker := newForgeFakeDocker()
	scriptInstallOutputs(docker, "attempt 1\n", "attempt 2\n", "attempt 3\n")
	logged := &syncBuffer{}
	d := forgeDriver(docker)
	d.logger = slog.New(slog.NewTextHandler(logged, nil))

	runFailingForgeStart(t, d, docker, dir)

	if n := strings.Count(logged.String(), "forge install log not written"); n != 1 {
		t.Fatalf("worker log = %q, want one warning that the install log was not written, got %d", logged.String(), n)
	}
	assertEmptyDir(t, outside)
}

// A write that fails in one attempt drops the log for the rest of the start's
// install: a later attempt writes nothing to it, even once the filesystem has
// room again, and the failure is warned of once.
func TestInstallLogWriteFailureDropsTheLogForTheRestOfTheInstall(t *testing.T) {
	dir := t.TempDir()
	sink := &failingLogFile{limit: len(installLogOutput)}
	docker := newForgeFakeDocker()
	docker.logBodies[forgeInstallID] = string(frame(dockerStreamStdout, installLogOutput)) +
		string(frame(dockerStreamStdout, "attempt 1, the write that fails\n"))
	logged := &syncBuffer{}
	i := &instance{docker: docker, spec: forgeSpec(dir), logger: slog.New(slog.NewTextHandler(logged, nil)),
		installLogSink: func(f *os.File) installLogFile { sink.f = f; return sink }}
	log := i.startInstallLog()
	defer log.close()

	i.captureInstallOutput(context.Background(), forgeInstallID, log)
	sink.limit = 1 << 20
	docker.mu.Lock()
	docker.logBodies[forgeInstallID] = string(frame(dockerStreamStdout, "attempt 2\n"))
	docker.mu.Unlock()
	i.captureInstallOutput(context.Background(), forgeInstallID, log)

	if got := readInstallLog(t, dir); got != installLogOutput {
		t.Fatalf("install log = %q, want only what attempt 1 wrote before the failure", got)
	}
	if n := strings.Count(logged.String(), "forge install log write failed"); n != 1 {
		t.Fatalf("worker log = %q, want one warning of the failed write, got %d", logged.String(), n)
	}
}

// fileInstallLog is an install log of one attempt written straight to a file of
// its own, for a test to hand output to the way demuxLogsTo does, and the path
// of that file.
func fileInstallLog(t *testing.T) (*installLog, string) {
	t.Helper()
	f, err := os.Create(filepath.Join(t.TempDir(), "forge-install.log"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = f.Close() })
	l := &installLog{f: f, file: f, logger: slog.New(slog.DiscardHandler)}
	l.beginAttempt()
	return l, f.Name()
}

// demuxChunk is the most demuxLogsTo hands the install log in one write.
const demuxChunk = 32 << 10

// writeInChunks hands output to l in writes of at most demuxChunk bytes.
func writeInChunks(l *installLog, output string) {
	for len(output) > 0 {
		n := min(demuxChunk, len(output))
		l.write([]byte(output[:n]))
		output = output[n:]
	}
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// One line longer than the room for the latest output does not cost that output,
// even when its newline comes in the write that compacts the log: the kept part
// starts at a line only when one starts close by, and at a character otherwise.
func TestInstallLogKeepsTheLatestOutputOfALineLongerThanItsRoom(t *testing.T) {
	l, path := fileInstallLog(t)
	var printed strings.Builder
	write := func(p string) {
		writeInChunks(l, p)
		printed.WriteString(p)
	}
	write(installOutput("attempt 1", 1<<20))
	// A line of about 3.5 MiB ends the output, its last write the one that takes
	// the log past its bound.
	for line := 0; line < 3<<20 || l.size+demuxChunk <= execution.ForgeInstallLogMaxBytes; line += demuxChunk {
		write(strings.Repeat("x", demuxChunk))
	}
	write(strings.Repeat("x", int(execution.ForgeInstallLogMaxBytes-l.size)) + "\n")

	log := readFile(t, path)
	if len(log) > execution.ForgeInstallLogMaxBytes {
		t.Fatalf("install log is %d bytes, want at most %d", len(log), execution.ForgeInstallLogMaxBytes)
	}
	_, _, latest := splitAtOmission(t, log)
	if !strings.HasSuffix(printed.String(), latest) {
		t.Fatalf("install log after the omission line starts with %.80q, want the end of the output", latest)
	}
	if len(latest) < 1<<20 {
		t.Fatalf("install log keeps %d bytes of the latest output, want at least 1 MiB", len(latest))
	}
}

// Output with no newline at all, in characters of more than one byte, is cut on
// a character: the log stays valid UTF-8, which the WebUI reads it as only when
// all of it is.
func TestInstallLogCutsOutputWithoutNewlinesOnACharacter(t *testing.T) {
	// Each lead puts the cut at a different byte of a three-byte character.
	for lead := range 3 {
		t.Run(fmt.Sprintf("lead %d", lead), func(t *testing.T) {
			l, path := fileInstallLog(t)
			output := strings.Repeat("a", lead) + strings.Repeat("日本語", (13<<18)/9)
			writeInChunks(l, output)

			log := readFile(t, path)
			if len(log) > execution.ForgeInstallLogMaxBytes {
				t.Fatalf("install log is %d bytes, want at most %d", len(log), execution.ForgeInstallLogMaxBytes)
			}
			_, _, latest := splitAtOmission(t, log)
			if !utf8.ValidString(latest) || !strings.HasSuffix(output, latest) {
				t.Fatalf("install log after the omission line starts with %+q, want whole characters from the end of the output",
					latest[:min(len(latest), 8)])
			}
		})
	}
}
