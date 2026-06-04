package execution

import (
	"strings"
	"testing"
)

// drainLogs reads every event until the channel closes.
func drainLogs(ch <-chan LogEvent) []LogEvent {
	var out []LogEvent
	for ev := range ch {
		out = append(out, ev)
	}
	return out
}

func TestLogPumpScanEmitsLinesPerStream(t *testing.T) {
	p := NewLogPump("s1", 16)
	var wg = make(chan struct{}, 2)
	go func() { p.Scan(strings.NewReader("alpha\nbeta\n"), LogStreamStdout); wg <- struct{}{} }()
	go func() { p.Scan(strings.NewReader("oops\n"), LogStreamStderr); wg <- struct{}{} }()
	<-wg
	<-wg
	p.Close()

	got := drainLogs(p.Logs())
	if len(got) != 3 {
		t.Fatalf("got %d lines, want 3: %+v", len(got), got)
	}
	for _, ev := range got {
		if ev.ServerID != "s1" {
			t.Fatalf("ServerID = %q, want s1", ev.ServerID)
		}
	}
	// stdout lines preserve content and stream.
	if got[0].Line != "alpha" && got[1].Line != "alpha" {
		t.Fatalf("missing alpha line: %+v", got)
	}
}

// A single line far longer than any read buffer is truncated with a marker, AND
// the stream keeps flowing: the line after the giant one is still emitted. The
// old bufio.Scanner path surfaced ErrTooLong on such a line and stopped
// scanning, losing every subsequent line; the ReadSlice reader recovers instead.
func TestLogPumpScanTruncatesOversizedLineAndContinues(t *testing.T) {
	p := NewLogPump("s1", 16)
	huge := strings.Repeat("z", 4*1024*1024) // well past any bufio default buffer
	r := strings.NewReader(huge + "\n" + "after\n")
	p.Scan(r, LogStreamStdout)
	p.Close()

	got := drainLogs(p.Logs())
	if len(got) != 2 {
		t.Fatalf("got %d lines, want 2 (truncated giant + the line after it): lengths=%v", len(got), summarize(got))
	}
	want := strings.Repeat("z", MaxLogLineBytes) + truncationMarker
	if got[0].Line != want {
		t.Fatalf("first line len=%d, want truncated to %d + marker", len(got[0].Line), MaxLogLineBytes)
	}
	if got[1].Line != "after" {
		t.Fatalf("second line = %q, want the line after the oversized one", got[1].Line)
	}
}

// A final line with no trailing newline (the stream ends at EOF mid-line) is
// still emitted: Scan flushes the trailing partial when ReadSlice returns EOF.
func TestLogPumpScanEmitsUnterminatedFinalLine(t *testing.T) {
	p := NewLogPump("s1", 16)
	p.Scan(strings.NewReader("abc"), LogStreamStdout)
	p.Close()

	got := drainLogs(p.Logs())
	if len(got) != 1 || got[0].Line != "abc" {
		t.Fatalf("got %+v, want one line %q", got, "abc")
	}
}

// summarize renders just the line lengths so a failure does not dump megabytes.
func summarize(evs []LogEvent) []int {
	out := make([]int, len(evs))
	for i, e := range evs {
		out[i] = len(e.Line)
	}
	return out
}

func TestLogPumpTruncatesLongLine(t *testing.T) {
	p := NewLogPump("s1", 4)
	long := strings.Repeat("x", MaxLogLineBytes+50)
	p.Emit(long, LogStreamStdout)
	p.Close()

	got := drainLogs(p.Logs())
	if len(got) != 1 {
		t.Fatalf("got %d lines, want 1", len(got))
	}
	want := strings.Repeat("x", MaxLogLineBytes) + truncationMarker
	if got[0].Line != want {
		t.Fatalf("line length = %d, want truncated to %d + marker", len(got[0].Line), MaxLogLineBytes)
	}
}

// Under backpressure the pump drops the oldest line and, once a slot frees,
// emits a dropped-count marker so the consumer learns about the loss. A slow
// consumer (started after a burst that overflows the buffer) must still see the
// marker, and the total delivered must be bounded by the buffer plus marker — it
// must not be all of the produced lines.
func TestLogPumpDropsOldestWithMarker(t *testing.T) {
	const bufSize = 2
	const produced = 50
	p := NewLogPump("s1", bufSize)

	// Produce a burst with no consumer so the buffer overflows and drops mount.
	for i := 0; i < produced; i++ {
		p.Emit("line", LogStreamStdout)
	}
	// Keep producing while a consumer drains so the marker flushes into a freed
	// slot, then close.
	done := make(chan []LogEvent, 1)
	go func() { done <- drainLogs(p.Logs()) }()
	for i := 0; i < produced; i++ {
		p.Emit("line", LogStreamStdout)
	}
	p.Close()
	got := <-done

	var sawMarker bool
	for _, ev := range got {
		if strings.Contains(ev.Line, "dropped") && strings.Contains(ev.Line, "log line") {
			sawMarker = true
			if ev.Stream != LogStreamStderr {
				t.Fatalf("drop marker should be on stderr, got stream %v", ev.Stream)
			}
		}
	}
	if !sawMarker {
		t.Fatalf("expected a dropped-count marker line, got %d events", len(got))
	}
	if len(got) >= 2*produced {
		t.Fatalf("expected lines to be dropped under backpressure, but got %d of %d", len(got), 2*produced)
	}
}

// Emits after Close are dropped and never panic on the closed channel.
func TestLogPumpEmitAfterCloseIsNoOp(t *testing.T) {
	p := NewLogPump("s1", 2)
	p.Close()
	p.Emit("late", LogStreamStdout) // must not panic
	if _, ok := <-p.Logs(); ok {
		t.Fatal("expected no lines after Close")
	}
}
