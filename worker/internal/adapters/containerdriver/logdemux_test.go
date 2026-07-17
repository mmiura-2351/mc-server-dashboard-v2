package containerdriver

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// frame builds one Docker multiplexed stream frame (non-TTY): an 8-byte header
// [streamType,0,0,0, size(uint32 BE)] followed by payload.
func frame(streamType byte, payload string) []byte {
	hdr := make([]byte, dockerStreamHeaderLen)
	hdr[0] = streamType
	binary.BigEndian.PutUint32(hdr[4:], uint32(len(payload)))
	return append(hdr, []byte(payload)...)
}

func drainLogs(pump *execution.LogPump) []execution.LogEvent {
	var out []execution.LogEvent
	for ev := range pump.Logs() {
		out = append(out, ev)
	}
	return out
}

func TestDemuxLogsSplitsStreams(t *testing.T) {
	var buf bytes.Buffer
	buf.Write(frame(dockerStreamStdout, "out line one\n"))
	buf.Write(frame(dockerStreamStderr, "err line\n"))
	buf.Write(frame(dockerStreamStdout, "out line two\n"))

	pump := execution.NewLogPump("s1", 16)
	go func() { demuxLogs(&buf, pump); pump.Close() }()

	got := drainLogs(pump)
	var stdout, stderr []string
	for _, ev := range got {
		if ev.Stream == execution.LogStreamStderr {
			stderr = append(stderr, ev.Line)
		} else {
			stdout = append(stdout, ev.Line)
		}
	}
	if len(stdout) != 2 || stdout[0] != "out line one" || stdout[1] != "out line two" {
		t.Fatalf("stdout = %v", stdout)
	}
	if len(stderr) != 1 || stderr[0] != "err line" {
		t.Fatalf("stderr = %v", stderr)
	}
}

// A line split across two frames is reassembled before being emitted.
func TestDemuxLogsReassemblesAcrossFrames(t *testing.T) {
	var buf bytes.Buffer
	buf.Write(frame(dockerStreamStdout, "partial "))
	buf.Write(frame(dockerStreamStdout, "line\n"))

	pump := execution.NewLogPump("s1", 16)
	go func() { demuxLogs(&buf, pump); pump.Close() }()

	got := drainLogs(pump)
	if len(got) != 1 || got[0].Line != "partial line" {
		t.Fatalf("got %v, want one reassembled line", got)
	}
}

// A trailing \r (Windows-style line ending inside a payload) is trimmed.
func TestDemuxLogsTrimsCarriageReturn(t *testing.T) {
	var buf bytes.Buffer
	buf.Write(frame(dockerStreamStdout, "crlf line\r\n"))

	pump := execution.NewLogPump("s1", 16)
	go func() { demuxLogs(&buf, pump); pump.Close() }()

	got := drainLogs(pump)
	if len(got) != 1 || got[0].Line != "crlf line" {
		t.Fatalf("got %v, want trimmed line", got)
	}
}

// A final payload without a trailing newline is emitted when the stream ends,
// on each stream that carries one: a container dying abruptly flushes its last
// diagnostic newline-less, and that line is the one that explains the crash
// (issue #2023).
func TestDemuxLogsEmitsUnterminatedFinalLine(t *testing.T) {
	var buf bytes.Buffer
	buf.Write(frame(dockerStreamStdout, "terminated\n"))
	buf.Write(frame(dockerStreamStderr, "last words"))
	buf.Write(frame(dockerStreamStdout, "no newline here"))

	pump := execution.NewLogPump("s1", 16)
	go func() { demuxLogs(&buf, pump); pump.Close() }()

	var stdout, stderr []string
	for _, ev := range drainLogs(pump) {
		if ev.Stream == execution.LogStreamStderr {
			stderr = append(stderr, ev.Line)
		} else {
			stdout = append(stdout, ev.Line)
		}
	}
	if len(stdout) != 2 || stdout[1] != "no newline here" {
		t.Fatalf("stdout = %v, want the unterminated final line emitted", stdout)
	}
	if len(stderr) != 1 || stderr[0] != "last words" {
		t.Fatalf("stderr = %v, want the unterminated final line emitted", stderr)
	}
}

// oneHeaderReader returns its header bytes once, then reports EOF. It proves the
// demux rejects an oversized frame from the header alone: if the demux tried to
// read the (multi-GiB) declared payload it would first allocate that buffer; the
// cap must end the stream before any payload read or allocation.
type oneHeaderReader struct {
	header []byte
	off    int
}

func (r *oneHeaderReader) Read(p []byte) (int, error) {
	if r.off >= len(r.header) {
		return 0, io.EOF
	}
	n := copy(p, r.header[r.off:])
	r.off += n
	return n, nil
}

// A corrupt frame whose declared size exceeds the sanity cap ends the stream
// cleanly without allocating the (huge) payload: the demux returns promptly and
// emits nothing.
func TestDemuxLogsRejectsOversizedFrame(t *testing.T) {
	hdr := make([]byte, dockerStreamHeaderLen)
	hdr[0] = dockerStreamStdout
	binary.BigEndian.PutUint32(hdr[4:], maxFrameBytes+1)

	pump := execution.NewLogPump("s1", 16)
	done := make(chan struct{})
	go func() { demuxLogs(&oneHeaderReader{header: hdr}, pump); pump.Close(); close(done) }()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("demux did not return promptly on an oversized frame")
	}
	if got := drainLogs(pump); len(got) != 0 {
		t.Fatalf("got %v, want no emitted lines", got)
	}
}

// A frame whose declared size equals the cap is accepted (boundary): only
// size > maxFrameBytes is rejected. The payload is exactly maxFrameBytes and a
// short newline-terminated line is appended so a complete line follows it; the
// demux must read past the at-cap payload and emit that trailing line, proving
// the frame was not rejected as oversized.
func TestDemuxLogsAcceptsFrameAtCap(t *testing.T) {
	atCap := make([]byte, maxFrameBytes)
	for i := range atCap {
		atCap[i] = 'a'
	}
	atCap[len(atCap)-1] = '\n' // terminate the at-cap line so it is emitted whole.

	var buf bytes.Buffer
	hdr := make([]byte, dockerStreamHeaderLen)
	hdr[0] = dockerStreamStdout
	binary.BigEndian.PutUint32(hdr[4:], maxFrameBytes)
	buf.Write(hdr)
	buf.Write(atCap)
	buf.Write(frame(dockerStreamStdout, "after at-cap\n"))

	pump := execution.NewLogPump("s1", 16)
	go func() { demuxLogs(&buf, pump); pump.Close() }()

	got := drainLogs(pump)
	if len(got) != 2 {
		t.Fatalf("got %d line(s), want 2 (at-cap frame accepted + the line after it)", len(got))
	}
	if got[1].Line != "after at-cap" {
		t.Fatalf("trailing line = %q, want the line after the at-cap frame", got[1].Line)
	}
}

// newlinelessReader streams total bytes of newline-less payload as a sequence of
// Docker frames, generating them lazily so the source itself allocates nothing
// proportional to total. Just before it reports EOF it forces a GC and records
// the live heap, which at that moment still holds the demux's carry buffer.
type newlinelessReader struct {
	total     int
	sent      int
	pending   []byte
	frameSize int
	peakHeap  uint64
}

func (r *newlinelessReader) Read(p []byte) (int, error) {
	if len(r.pending) == 0 {
		if r.sent >= r.total {
			// The carry buffer is still live here: measure before returning EOF, as
			// the demux's deferred flush releases it once demuxLogs returns.
			runtime.GC()
			var ms runtime.MemStats
			runtime.ReadMemStats(&ms)
			r.peakHeap = ms.HeapAlloc
			return 0, io.EOF
		}
		size := min(r.frameSize, r.total-r.sent)
		payload := bytes.Repeat([]byte{'a'}, size)
		r.pending = frame(dockerStreamStdout, string(payload))
		r.sent += size
	}
	n := copy(p, r.pending)
	r.pending = r.pending[n:]
	return n, nil
}

// A stream that never emits a newline must not grow the per-stream carry buffer
// without bound: the cap is consulted while the line accumulates across frames,
// not only once a newline completes it (issue #2029). Without the bound the
// carry retains every byte the container wrote, so the worker's heap tracks the
// container's newline-less output until the stream ends or the worker OOMs.
func TestDemuxLogsBoundsNewlinelessCarry(t *testing.T) {
	const total = 128 << 20 // 128 MiB of newline-less output, streamed lazily.

	runtime.GC()
	var base runtime.MemStats
	runtime.ReadMemStats(&base)

	r := &newlinelessReader{total: total, frameSize: 64 << 10}
	pump := execution.NewLogPump("s1", 16)
	go func() { demuxLogs(r, pump); pump.Close() }()
	drainLogs(pump)

	// The carry holds ~MaxLogLineBytes; the allowance covers the frame buffers and
	// ordinary test-run noise while staying far below the 128 MiB an unbounded
	// carry retains.
	const allowance = 8 << 20
	if growth := int64(r.peakHeap) - int64(base.HeapAlloc); growth > allowance {
		t.Fatalf("live heap grew by %d bytes while accumulating %d bytes of newline-less output; want <= %d (carry is unbounded)",
			growth, total, allowance)
	}
}

// An over-long line spanning frames is truncated at MaxLogLineBytes and marked,
// and the stream resynchronises at the next newline so the line after it is
// emitted intact — mirroring LogPump.Scan, which keeps at most MaxLogLineBytes
// and discards the excess until the newline arrives (issue #2029).
func TestDemuxLogsTruncatesOverLongLineAndResyncs(t *testing.T) {
	long := strings.Repeat("a", execution.MaxLogLineBytes+5000)

	var buf bytes.Buffer
	buf.Write(frame(dockerStreamStdout, long[:3000]))
	buf.Write(frame(dockerStreamStdout, long[3000:]))
	buf.Write(frame(dockerStreamStdout, "\nafter overflow\n"))

	pump := execution.NewLogPump("s1", 16)
	go func() { demuxLogs(&buf, pump); pump.Close() }()

	got := drainLogs(pump)
	if len(got) != 2 {
		t.Fatalf("got %d line(s), want the truncated line plus the line after it", len(got))
	}
	want := long[:execution.MaxLogLineBytes]
	if !strings.HasPrefix(got[0].Line, want) || len(got[0].Line) <= execution.MaxLogLineBytes {
		t.Fatalf("first line = %d bytes, want %d kept bytes plus a truncation marker", len(got[0].Line), execution.MaxLogLineBytes)
	}
	if got[1].Line != "after overflow" {
		t.Fatalf("second line = %q, want the stream to resync at the newline", got[1].Line)
	}
}

// The final unterminated partial is flushed on stream end (issue #2023) even when
// it is itself over-long: it is emitted truncated and marked, as LogPump.Scan
// emits its trailing partial at EOF.
func TestDemuxLogsEmitsOverLongFinalPartial(t *testing.T) {
	long := strings.Repeat("a", execution.MaxLogLineBytes+5000)

	var buf bytes.Buffer
	buf.Write(frame(dockerStreamStdout, long[:3000]))
	buf.Write(frame(dockerStreamStdout, long[3000:])) // no trailing newline.

	pump := execution.NewLogPump("s1", 16)
	go func() { demuxLogs(&buf, pump); pump.Close() }()

	got := drainLogs(pump)
	if len(got) != 1 {
		t.Fatalf("got %d line(s), want the over-long final partial emitted", len(got))
	}
	if !strings.HasPrefix(got[0].Line, long[:execution.MaxLogLineBytes]) || len(got[0].Line) <= execution.MaxLogLineBytes {
		t.Fatalf("final partial = %d bytes, want %d kept bytes plus a truncation marker", len(got[0].Line), execution.MaxLogLineBytes)
	}
}

// The container path (demuxLogs) and the process path (LogPump.Scan) must agree
// on over-long-line handling — #2023 established that the two paths stay
// consistent. Feeding identical content through both must emit identical lines,
// including where the truncation lands and whether the line is marked. The cases
// walk the cap boundary, since that is where the two implementations could
// plausibly disagree (issue #2029).
func TestDemuxLogsMatchesScanOverLongSemantics(t *testing.T) {
	const lineCap = execution.MaxLogLineBytes
	a := func(n int) string { return strings.Repeat("a", n) }

	cases := []struct {
		name    string
		content string
	}{
		{"under cap", "short line\n"},
		{"exactly at cap", a(lineCap) + "\n"},
		{"one over cap", a(lineCap+1) + "\n"},
		{"far over cap then resync", a(lineCap+5000) + "\nnext line\n"},
		{"at cap with crlf", a(lineCap) + "\r\n"},
		{"one over cap with crlf", a(lineCap+1) + "\r\n"},
		// A \r sitting exactly at the cap boundary is interior to the real line, so
		// neither path may treat it as a line ending and drop the truncation mark.
		{"cr at cap boundary", a(lineCap) + "\r" + a(5000) + "\n"},
		{"over-long unterminated final line", a(lineCap + 5000)},
		{"empty", ""},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			scanPump := execution.NewLogPump("s1", 64)
			go func() {
				scanPump.Scan(strings.NewReader(tc.content), execution.LogStreamStdout)
				scanPump.Close()
			}()
			wantLines := linesOf(drainLogs(scanPump))

			// Chunk the same content into frames at a size that is not a divisor of the
			// cap, so line boundaries and the cap boundary fall mid-frame.
			var buf bytes.Buffer
			for rest := tc.content; len(rest) > 0; {
				n := min(1000, len(rest))
				buf.Write(frame(dockerStreamStdout, rest[:n]))
				rest = rest[n:]
			}
			demuxPump := execution.NewLogPump("s1", 64)
			go func() { demuxLogs(&buf, demuxPump); demuxPump.Close() }()
			gotLines := linesOf(drainLogs(demuxPump))

			if !slices.Equal(gotLines, wantLines) {
				t.Fatalf("demuxLogs and LogPump.Scan disagree\n demux: %s\n  scan: %s",
					describeLines(gotLines), describeLines(wantLines))
			}
		})
	}
}

func linesOf(evs []execution.LogEvent) []string {
	out := make([]string, 0, len(evs))
	for _, ev := range evs {
		out = append(out, ev.Line)
	}
	return out
}

// describeLines renders lines as length + suffix, so a failure involving
// multi-KiB lines stays readable.
func describeLines(lines []string) string {
	parts := make([]string, 0, len(lines))
	for _, l := range lines {
		suffix := l
		if len(suffix) > 24 {
			suffix = "..." + l[len(l)-24:]
		}
		parts = append(parts, fmt.Sprintf("(%d bytes, ending %q)", len(l), suffix))
	}
	return "[" + strings.Join(parts, " ") + "]"
}

// An out-of-range stream-type byte is labelled stderr (not silently stdout), so
// the output is preserved and visibly attributed to the error stream.
func TestDemuxLogsUnknownStreamTypeGoesToStderr(t *testing.T) {
	var buf bytes.Buffer
	buf.Write(frame(7, "mystery line\n")) // 7 is neither stdout(1) nor stderr(2)

	pump := execution.NewLogPump("s1", 16)
	go func() { demuxLogs(&buf, pump); pump.Close() }()

	got := drainLogs(pump)
	if len(got) != 1 || got[0].Line != "mystery line" {
		t.Fatalf("got %v, want one line", got)
	}
	if got[0].Stream != execution.LogStreamStderr {
		t.Fatalf("stream = %v, want stderr for an unknown stream-type byte", got[0].Stream)
	}
}
