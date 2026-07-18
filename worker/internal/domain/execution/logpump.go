package execution

import (
	"bufio"
	"fmt"
	"io"
	"regexp"
	"sync"
	"time"
)

// MaxLogLineBytes bounds a single captured log line. A longer line is truncated
// at this length and marked so the API and operators know it was cut, rather
// than streaming an unbounded line onto the control plane (FR-MON-2).
const MaxLogLineBytes = 8 * 1024

// truncationMarker is appended to a line that exceeded MaxLogLineBytes.
const truncationMarker = "…[truncated]"

// readyMarker matches the Minecraft server's startup-complete line, e.g.
// `[Server thread/INFO]: Done (12.345s)! For help, type "help"`. Vanilla, Paper
// and Forge all print this once the server is listening (RCON included), so it
// is the readiness signal a driver waits for before reporting StateRunning
// (issue #345).
var readyMarker = regexp.MustCompile(`Done \([0-9.]+s\)! For help`)

// LogPump captures a server's stdout/stderr line by line into a bounded, lossy
// per-instance buffer (FR-MON-2). Logs are best-effort: under backpressure the
// pump drops the oldest buffered line and, once a slot frees, emits a single
// marker line reporting how many lines were dropped. This mirrors the status
// event posture (issue #96) — log volume is the forcing function, so the
// capture path never blocks the server process. Logs are transient relay-only
// at M1 (REQUIREMENTS.md Section 6.13): the pump streams, it does not persist.
type LogPump struct {
	serverID string
	out      chan LogEvent

	// ready is closed once a captured line matches readyMarker, signalling the
	// server is up and listening (issue #345). readyOnce guards the one-shot close.
	ready     chan struct{}
	readyOnce sync.Once

	mu            sync.Mutex
	dropped       uint64
	markerPending bool
	closed        bool
}

// NewLogPump builds a pump for serverID with an out buffer of bufSize lines.
// A bufSize <= 0 is treated as 1 so the channel is always usable.
func NewLogPump(serverID string, bufSize int) *LogPump {
	if bufSize <= 0 {
		bufSize = 1
	}
	return &LogPump{
		serverID: serverID,
		out:      make(chan LogEvent, bufSize),
		ready:    make(chan struct{}),
	}
}

// Logs is the captured-line stream. It closes once every Scan goroutine has
// finished and Close has been called.
func (p *LogPump) Logs() <-chan LogEvent { return p.out }

// Ready is closed the first time a captured line reports the server finished
// starting (the Minecraft "Done (X.XXXs)! For help" line). A driver selects on
// it to hold StateStarting until the server is actually listening (issue #345).
// It never fires if the marker is never seen; the driver pairs it with a
// bounded fallback timeout.
func (p *LogPump) Ready() <-chan struct{} { return p.ready }

// markReadyIfDone closes the ready channel (once) when line is the startup-
// complete marker.
func (p *LogPump) markReadyIfDone(line string) {
	if readyMarker.MatchString(line) {
		p.readyOnce.Do(func() { close(p.ready) })
	}
}

// WaitReady blocks until the server signals readiness (ready closed), the
// fallback timeout elapses, or the instance exits first (exited closed). It
// reports whether the caller should transition starting→running: true when the
// server became ready or the fallback fired, false when the instance exited
// first (the exit path owns the terminal state). The fallback bounds the wait so
// a server whose log format omits the marker never sticks in starting forever
// (issue #345).
func WaitReady(ready, exited <-chan struct{}, fallback time.Duration) bool {
	timer := time.NewTimer(fallback)
	defer timer.Stop()
	select {
	case <-ready:
		return true
	case <-timer.C:
		return true
	case <-exited:
		return false
	}
}

// Scan reads r line by line and emits each as a LogEvent on the given stream
// until r reaches EOF or errors. It returns when r is exhausted; callers run it
// in a goroutine per stream (stdout, stderr). A line longer than MaxLogLineBytes
// is truncated with a marker and the scan continues — an oversized line never
// stops the stream. It uses a bufio.Reader ReadSlice loop rather than a
// bufio.Scanner so an over-long line is recovered (the Scanner surfaced it as
// ErrTooLong and stopped, losing the rest of the stream).
func (p *LogPump) Scan(r io.Reader, stream LogStream) {
	br := bufio.NewReader(r)
	for {
		// kept holds at most MaxLogLineBytes content bytes; truncated records that
		// the line exceeded the cap so the marker is appended. Excess bytes of an
		// oversized line are read and discarded until the newline arrives.
		var kept []byte
		truncated := false
		for {
			chunk, err := br.ReadSlice('\n')
			content := chunk
			if err == nil {
				content = trimLineEnd(chunk) // drop the trailing \n (and any \r).
			}
			if room := MaxLogLineBytes - len(kept); room > 0 {
				if len(content) > room {
					kept = append(kept, content[:room]...)
					truncated = true
				} else {
					kept = append(kept, content...)
				}
			} else if len(content) > 0 {
				truncated = true
			}

			if err == nil {
				break // ReadSlice stopped at a newline: the line is complete.
			}
			if err == bufio.ErrBufferFull {
				continue // line longer than bufio's buffer; keep reading it.
			}
			// io.EOF or a read error: emit any trailing partial, then stop.
			if len(kept) > 0 || truncated {
				p.emitScanLine(kept, truncated, stream)
			}
			return
		}
		// Strip a trailing \r that a buffer-boundary split left in kept:
		// when CRLF spans the ReadSlice boundary, trimLineEnd only sees the
		// final chunk (\n) and the \r from the previous chunk stays (#2067).
		if n := len(kept); n > 0 && kept[n-1] == '\r' {
			kept = kept[:n-1]
		}
		p.emitScanLine(kept, truncated, stream)
	}
}

// emitScanLine emits one scanned line, appending the truncation marker when the
// line overflowed MaxLogLineBytes.
func (p *LogPump) emitScanLine(kept []byte, truncated bool, stream LogStream) {
	line := string(kept)
	if truncated {
		line += truncationMarker
	}
	p.emit(line, stream)
}

// trimLineEnd drops a trailing \n and any preceding \r from a ReadSlice result.
func trimLineEnd(b []byte) []byte {
	if n := len(b); n > 0 && b[n-1] == '\n' {
		b = b[:n-1]
	}
	if n := len(b); n > 0 && b[n-1] == '\r' {
		b = b[:n-1]
	}
	return b
}

// Emit queues one already-formed line, truncating it to MaxLogLineBytes. It is
// the entry point for callers that demux their own framing (the container
// driver) rather than scanning a raw byte stream. It never blocks: see emit.
func (p *LogPump) Emit(line string, stream LogStream) {
	p.emit(truncate(line), stream)
}

// emit queues a line, dropping the oldest buffered line under backpressure. When
// a drop happens it queues a dropped-count marker in the freed slot (in place of
// the new line) so the consumer always learns about the loss in order, even
// under sustained backpressure. It never blocks the caller (the server
// process's output).
func (p *LogPump) emit(line string, stream LogStream) {
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return
	}
	p.mu.Unlock()

	// Detect the readiness marker before queuing, so a Done line still fires Ready
	// even if backpressure later drops it from the buffer (issue #345).
	p.markReadyIfDone(line)

	ev := LogEvent{ServerID: p.serverID, Line: line, Stream: stream}
	select {
	case p.out <- ev:
		return
	default:
	}

	// Buffer full: evict the oldest line to make room and count the drop.
	select {
	case <-p.out:
	default:
	}
	p.mu.Lock()
	p.dropped++
	n := p.dropped
	pending := p.markerPending
	if !pending {
		p.markerPending = true
	}
	p.mu.Unlock()

	// If no marker is already queued, claim the freed slot for one summarising
	// the drops so far; the new line is itself dropped (counted above). When a
	// marker is already pending, drop the new line silently — the pending marker
	// will report the higher count once the consumer drains it.
	if pending {
		return
	}
	marker := LogEvent{
		ServerID: p.serverID,
		Line:     fmt.Sprintf("[mcsd] dropped %d log line(s) under backpressure", n),
		Stream:   LogStreamStderr,
	}
	select {
	case p.out <- marker:
		p.mu.Lock()
		p.dropped -= n
		p.markerPending = false
		p.mu.Unlock()
	default:
		p.mu.Lock()
		p.markerPending = false
		p.mu.Unlock()
	}
}

// Close marks the pump closed and shuts the out channel so the consumer's range
// ends. It must be called after every Scan goroutine has returned. Further emits
// after Close are dropped.
func (p *LogPump) Close() {
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return
	}
	p.closed = true
	p.mu.Unlock()
	close(p.out)
}

// truncate caps line at MaxLogLineBytes, appending a marker when it was cut.
func truncate(line string) string {
	if len(line) <= MaxLogLineBytes {
		return line
	}
	return line[:MaxLogLineBytes] + truncationMarker
}
