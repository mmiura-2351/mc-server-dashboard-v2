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

// The Minecraft startup-complete Done line indicates game and RCON listeners are ready.
var readyMarker = regexp.MustCompile(`Done \([0-9.]+s\)! For help`)

// LogPump never blocks the process: overflow drops oldest lines and later emits a loss marker.
// Logs are streamed, not persisted.
type LogPump struct {
	serverID string
	out      chan LogEvent

	// ready is closed once a captured line matches readyMarker, signalling the server is up and listening.
	// readyOnce guards the one-shot close.
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

// Ready closes at the first startup-complete marker; pair it with a fallback for servers that never log the
// marker.
func (p *LogPump) Ready() <-chan struct{} { return p.ready }

// markReadyIfDone closes the ready channel (once) when line is the startup-
// complete marker.
func (p *LogPump) markReadyIfDone(line string) {
	if readyMarker.MatchString(line) {
		p.readyOnce.Do(func() { close(p.ready) })
	}
}

// WaitReady permits running after readiness or the fallback timeout, but not after observed exit.
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

// Scan truncates oversized lines and continues reading; bufio.Scanner would stop at the first one.
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
		// Strip a trailing \r that a buffer-boundary split left in kept: when CRLF spans the ReadSlice boundary,
		// trimLineEnd only sees the final chunk (\n) and the \r from the previous chunk stays.
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

// On overflow replace the oldest line with a loss marker so the consumer learns about drops without blocking the
// server.
func (p *LogPump) emit(line string, stream LogStream) {
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return
	}
	p.mu.Unlock()

	// Detect the readiness marker before queuing, so a Done line still fires Ready even if backpressure later drops
	// it from the buffer.
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
