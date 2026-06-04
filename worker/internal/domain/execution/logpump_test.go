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
