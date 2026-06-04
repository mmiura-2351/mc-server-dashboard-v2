package containerdriver

import (
	"bytes"
	"encoding/binary"
	"testing"

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
