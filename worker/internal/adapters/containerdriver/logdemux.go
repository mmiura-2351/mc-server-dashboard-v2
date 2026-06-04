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

// demuxLogs reads Docker's multiplexed log stream from r, splits each frame's
// payload into lines, and emits them into pump tagged with the frame's stream
// (FR-MON-2). It returns when r is exhausted or errors (e.g. the follow is
// closed on container exit). Lines are reassembled across frame boundaries: a
// frame may end mid-line, so a trailing partial is held until the newline
// arrives in a later frame.
func demuxLogs(r io.Reader, pump *execution.LogPump) {
	br := bufio.NewReader(r)
	header := make([]byte, dockerStreamHeaderLen)
	// Carry a partial (newline-less) line per stream across frames.
	var partial [3]strings.Builder // index by stream type (1=stdout, 2=stderr)

	for {
		if _, err := io.ReadFull(br, header); err != nil {
			return
		}
		streamType := header[0]
		size := binary.BigEndian.Uint32(header[4:])
		if size == 0 {
			continue
		}

		payload := make([]byte, size)
		if _, err := io.ReadFull(br, payload); err != nil {
			return
		}

		stream := execution.LogStreamStdout
		buf := &partial[dockerStreamStdout]
		if streamType == dockerStreamStderr {
			stream = execution.LogStreamStderr
			buf = &partial[dockerStreamStderr]
		}
		emitFramePayload(pump, stream, buf, string(payload))
	}
}

// emitFramePayload appends payload to the stream's partial buffer and emits every
// complete (newline-terminated) line, keeping any trailing partial for the next
// frame.
func emitFramePayload(pump *execution.LogPump, stream execution.LogStream, buf *strings.Builder, payload string) {
	buf.WriteString(payload)
	text := buf.String()
	for {
		idx := strings.IndexByte(text, '\n')
		if idx < 0 {
			break
		}
		line := strings.TrimSuffix(text[:idx], "\r")
		pump.Emit(line, stream)
		text = text[idx+1:]
	}
	buf.Reset()
	buf.WriteString(text)
}
