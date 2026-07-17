package containerdriver

import (
	"bytes"
	"encoding/binary"
	"io"
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
