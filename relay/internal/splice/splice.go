// Package splice byte-copies two connections in both directions with half-close
// propagation (docs/app/RELAY.md Sections 4 and 5). The relay applies no idle
// timeout: Minecraft's keep-alives and the server's dead-client kick handle a
// dead peer; the relay only propagates a close from one side to the other.
package splice

import (
	"io"
	"net"
	"sync"
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

// copyHalf copies src→dst, then half-closes dst's write side so the peer sees a
// clean EOF on that direction while the other direction keeps flowing.
func copyHalf(dst, src net.Conn, wg *sync.WaitGroup) {
	defer wg.Done()
	_, _ = io.Copy(dst, src)
	if hc, ok := dst.(halfCloser); ok {
		_ = hc.CloseWrite()
	} else {
		_ = dst.Close()
	}
}
