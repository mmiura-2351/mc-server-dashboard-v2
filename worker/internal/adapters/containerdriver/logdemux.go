package containerdriver

import (
	"bufio"
	"encoding/binary"
	"io"
	"strings"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// dockerStreamHeaderLen is the size of Docker's multiplexed stream frame header
// (non-TTY logs): [STREAM_TYPE, 0, 0, 0, SIZE(uint32 big-endian)].
const dockerStreamHeaderLen = 8

// stream-type bytes in the frame header.
const (
	dockerStreamStdout = 1
	dockerStreamStderr = 2
)

// maxFrameBytes caps a single frame's declared payload size. A corrupt header
// can claim up to ~4 GiB (uint32); allocating that blindly would let one bad
// frame exhaust memory. A frame larger than this is treated as corruption and
// ends the stream cleanly rather than allocating the buffer.
const maxFrameBytes = 16 * 1024 * 1024

// demuxLogs reads Docker's multiplexed log stream from r, splits each frame's
// payload into lines, and emits them into pump tagged with the frame's stream
// (FR-MON-2). It returns when r is exhausted or errors (e.g. the follow is
// closed on container exit). Lines are reassembled across frame boundaries: a
// frame may end mid-line, so a trailing partial is held until the newline
// arrives in a later frame. Whatever partial is still held when the stream ends
// is emitted, mirroring LogPump.Scan's EOF handling (issue #2023).
func demuxLogs(r io.Reader, pump *execution.LogPump) {
	br := bufio.NewReader(r)
	header := make([]byte, dockerStreamHeaderLen)
	// Carry a partial (newline-less) line per stream across frames.
	var partial [3]strings.Builder // index by stream type (1=stdout, 2=stderr)

	// The stream can end mid-line on every exit path below (header EOF, oversized
	// frame, payload read error). A container that dies abruptly flushes its last
	// diagnostic without a trailing newline, so emit it rather than drop it.
	defer func() {
		if line := partial[dockerStreamStdout].String(); line != "" {
			pump.Emit(line, execution.LogStreamStdout)
		}
		if line := partial[dockerStreamStderr].String(); line != "" {
			pump.Emit(line, execution.LogStreamStderr)
		}
	}()

	for {
		if _, err := io.ReadFull(br, header); err != nil {
			return
		}
		streamType := header[0]
		size := binary.BigEndian.Uint32(header[4:])
		if size == 0 {
			continue
		}
		if size > maxFrameBytes {
			// Corrupt/oversized frame: refuse to allocate it and end the stream.
			return
		}

		payload := make([]byte, size)
		if _, err := io.ReadFull(br, payload); err != nil {
			return
		}

		// Map the stream-type byte to a stream and its carry buffer. Anything but
		// stdout(1) is treated as stderr: an out-of-range byte (corrupt frame) is
		// attributed to the error stream rather than silently mislabelled as stdout,
		// so the output is preserved and visibly flagged.
		stream := execution.LogStreamStdout
		buf := &partial[dockerStreamStdout]
		if streamType != dockerStreamStdout {
			stream = execution.LogStreamStderr
			buf = &partial[dockerStreamStderr]
		}
		emitFramePayload(pump, stream, buf, string(payload))
	}
}

// demuxLogsTo reads Docker's multiplexed log stream from r and writes the frame
// payloads (both stdout and stderr, interleaved in arrival order) to w, stripping
// the 8-byte frame headers so the result is plain text (issue #305). It is used to
// persist the Forge install container's output to a working-dir log file an
// operator can read; it returns when r is exhausted or a frame is corrupt.
func demuxLogsTo(r io.Reader, w io.Writer) {
	br := bufio.NewReader(r)
	header := make([]byte, dockerStreamHeaderLen)
	for {
		if _, err := io.ReadFull(br, header); err != nil {
			return
		}
		size := binary.BigEndian.Uint32(header[4:])
		if size == 0 {
			continue
		}
		if size > maxFrameBytes {
			return
		}
		if _, err := io.CopyN(w, br, int64(size)); err != nil {
			return
		}
	}
}

// maxCarryBytes bounds the per-stream partial line held across frames. Without
// it a newline-less stream (a \r-rewriting progress bar, one enormous stack dump,
// a binary stream misrouted to stdout) grows the carry with every frame until the
// stream ends or the worker OOMs: the MaxLogLineBytes cap is only consulted once
// a newline completes a line (issue #2029).
//
// It sits two bytes above MaxLogLineBytes so LogPump.Emit's own truncation still
// sees the overflow: one byte puts a capped carry past the cap (Emit marks a line
// only when it exceeds MaxLogLineBytes), and a second absorbs a trailing \r that
// the line-end trim may strip at the cap boundary.
const maxCarryBytes = execution.MaxLogLineBytes + 2

// emitFramePayload appends payload to the stream's partial buffer and emits every
// complete (newline-terminated) line, keeping any trailing partial for the next
// frame. The carry is capped at maxCarryBytes: content past the cap is discarded
// until the next newline resynchronizes the stream, and Emit truncates the kept
// content to MaxLogLineBytes and marks it. This mirrors LogPump.Scan, which keeps
// at most MaxLogLineBytes of an over-long line, discards the excess until the
// newline arrives, and emits the line marked as truncated (issue #2029).
func emitFramePayload(pump *execution.LogPump, stream execution.LogStream, buf *strings.Builder, payload string) {
	for {
		idx := strings.IndexByte(payload, '\n')
		if idx < 0 {
			appendCapped(buf, payload)
			return
		}
		appendCapped(buf, payload[:idx])
		pump.Emit(strings.TrimSuffix(buf.String(), "\r"), stream)
		buf.Reset()
		payload = payload[idx+1:]
	}
}

// appendCapped appends as much of s to buf as maxCarryBytes allows, dropping the
// rest. The dropped bytes are what makes an over-long line truncated; Emit adds
// the marker when the kept content is handed over.
func appendCapped(buf *strings.Builder, s string) {
	room := maxCarryBytes - buf.Len()
	if room <= 0 {
		return
	}
	if len(s) > room {
		s = s[:room]
	}
	buf.WriteString(s)
}
