// Package splice byte-copies two connections in both directions with half-close
// propagation (docs/app/RELAY.md Sections 4 and 5).
//
// Progress-deadline policy: each direction enforces an idle read timeout
// (idleTimeout) and a write-stall timeout (writeStallTimeout). If no bytes
// arrive within idleTimeout the read fails; if a write blocks longer than
// writeStallTimeout it fails. Either failure closes both connections so the
// sibling goroutine unblocks immediately.
package splice

import (
	"errors"
	"io"
	"net"
	"sync"
	"time"
)

// idleTimeout is how long a read may block with no incoming bytes before the
// splice treats the peer as dead.
//
// writeStallTimeout is how long a write may block (peer's receive buffer full,
// peer not reading) before the splice gives up.
//
// Both are package-level vars (not consts) so tests can override them.
var (
	idleTimeout       = 5 * time.Minute
	writeStallTimeout = 1 * time.Minute
)

// halfCloser is the half-close surface of a TCP connection. *net.TCPConn and
// *tls.Conn both implement CloseWrite; the splice uses it so an EOF from one
// peer half-closes the other's write side rather than tearing the whole
// connection down (RELAY.md Section 5).
type halfCloser interface {
	CloseWrite() error
}

// Splice copies a↔b until both directions reach EOF, propagating each
// direction's close as a write half-close on the other side and fully closing
// both connections on return. It blocks until the session ends.
func Splice(a, b net.Conn) {
	var wg sync.WaitGroup
	wg.Add(2)
	go copyHalf(a, b, &wg)
	go copyHalf(b, a, &wg)
	wg.Wait()
	_ = a.Close()
	_ = b.Close()
}

// copyHalf copies src→dst with progress deadlines, then half-closes dst's write
// side so the peer sees a clean EOF on that direction while the other direction
// keeps flowing. On a non-EOF error (timeout or write failure) it closes both
// connections to unblock the sibling goroutine.
func copyHalf(dst, src net.Conn, wg *sync.WaitGroup) {
	defer wg.Done()

	err := copyWithProgressDeadlines(dst, src)

	if err == nil {
		// Clean EOF: half-close dst so the remote peer sees EOF on this direction.
		if hc, ok := dst.(halfCloser); ok {
			_ = hc.CloseWrite()
		} else {
			_ = dst.Close()
		}
		return
	}

	// Non-EOF error (deadline, reset, etc.): tear down both sides so the
	// sibling copyHalf unblocks immediately.
	_ = dst.Close()
	_ = src.Close()
}

// copyWithProgressDeadlines copies src→dst in a loop, refreshing deadlines on
// each successful read/write. Returns nil on clean EOF, or an error on timeout
// or connection failure.
func copyWithProgressDeadlines(dst, src net.Conn) error {
	// Capture timeout values once so a concurrent test override of the
	// package-level vars does not race with this goroutine's loop.
	idle := idleTimeout
	stall := writeStallTimeout

	buf := make([]byte, 32*1024)
	for {
		_ = src.SetReadDeadline(time.Now().Add(idle))
		n, rerr := src.Read(buf)
		if n > 0 {
			_ = dst.SetWriteDeadline(time.Now().Add(stall))
			if _, werr := dst.Write(buf[:n]); werr != nil {
				return werr
			}
		}
		if rerr != nil {
			if isEOF(rerr) {
				return nil
			}
			return rerr
		}
	}
}

// isEOF reports whether err represents a clean end-of-stream.
func isEOF(err error) bool {
	return errors.Is(err, io.EOF)
}
