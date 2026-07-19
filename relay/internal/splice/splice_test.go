package splice

import (
	"io"
	"net"
	"sync"
	"testing"
	"time"
)

// tcpPair returns a connected pair of *net.TCPConn over loopback so the splice's
// CloseWrite half-close path is exercised (net.Pipe has no half-close).
func tcpPair(t *testing.T) (client, server net.Conn) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = ln.Close() }()

	type res struct {
		c   net.Conn
		err error
	}
	ch := make(chan res, 1)
	go func() {
		c, err := ln.Accept()
		ch <- res{c, err}
	}()
	client, err = net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	r := <-ch
	if r.err != nil {
		t.Fatal(r.err)
	}
	return client, r.c
}

// TestSpliceBidirectional verifies bytes flow both ways through the splice.
func TestSpliceBidirectional(t *testing.T) {
	playerOut, playerIn := tcpPair(t) // playerIn is the relay's view of the player
	serverIn, serverOut := tcpPair(t) // serverIn is the relay's view of the server

	go Splice(playerIn, serverIn)

	if _, err := playerOut.Write([]byte("hello")); err != nil {
		t.Fatal(err)
	}
	assertRead(t, serverOut, "hello")

	if _, err := serverOut.Write([]byte("world")); err != nil {
		t.Fatal(err)
	}
	assertRead(t, playerOut, "world")

	_ = playerOut.Close()
	_ = serverOut.Close()
}

// TestSpliceHalfClosePropagation verifies that when the player half-closes its
// write side, the server sees EOF while the reverse direction still delivers a
// buffered payload (half-close, not full teardown).
func TestSpliceHalfClosePropagation(t *testing.T) {
	playerOut, playerIn := tcpPair(t)
	serverIn, serverOut := tcpPair(t)
	go Splice(playerIn, serverIn)

	// Server pre-sends a reply that must survive the player's half-close.
	if _, err := serverOut.Write([]byte("reply")); err != nil {
		t.Fatal(err)
	}

	// Player half-closes its write side.
	if _, err := playerOut.Write([]byte("bye")); err != nil {
		t.Fatal(err)
	}
	_ = playerOut.(*net.TCPConn).CloseWrite()

	// Server reads the payload then sees EOF in that direction.
	assertRead(t, serverOut, "bye")
	_ = serverOut.SetReadDeadline(time.Now().Add(time.Second))
	if _, err := serverOut.Read(make([]byte, 1)); err != io.EOF {
		t.Errorf("server should see EOF after player half-close, got %v", err)
	}

	// Reverse direction still works: the player receives the server's reply.
	assertRead(t, playerOut, "reply")

	_ = playerOut.Close()
	_ = serverOut.Close()
}

func assertRead(t *testing.T, c net.Conn, want string) {
	t.Helper()
	_ = c.SetReadDeadline(time.Now().Add(time.Second))
	buf := make([]byte, len(want))
	if _, err := io.ReadFull(c, buf); err != nil {
		t.Fatalf("read %q: %v", want, err)
	}
	if string(buf) != want {
		t.Fatalf("read = %q, want %q", string(buf), want)
	}
}

// overrideTimeouts sets idleTimeout and writeStallTimeout for the duration of
// the test and restores them on cleanup.
func overrideTimeouts(t *testing.T, idle, writeStall time.Duration) {
	t.Helper()
	origIdle := idleTimeout
	origStall := writeStallTimeout
	idleTimeout = idle
	writeStallTimeout = writeStall
	t.Cleanup(func() {
		idleTimeout = origIdle
		writeStallTimeout = origStall
	})
}

// TestSpliceIdleSilentPeerUnblocks verifies that a TCP-alive but silent peer
// triggers the idle deadline and unblocks the splice (the core fix for #1717).
func TestSpliceIdleSilentPeerUnblocks(t *testing.T) {
	overrideTimeouts(t, 100*time.Millisecond, time.Minute)

	a, b := tcpPair(t)

	done := make(chan struct{})
	go func() {
		Splice(a, b)
		close(done)
	}()

	// Neither side writes anything — a silent peer.
	select {
	case <-done:
		// Splice returned — idle deadline fired.
	case <-time.After(2 * time.Second):
		t.Fatal("Splice did not return within 2s on a silent peer (idle deadline not working)")
	}
}

// TestSpliceWriteStallUnblocks verifies that a peer that reads nothing (causing
// the sender's write buffer to fill and the write deadline to fire) unblocks the
// splice.
func TestSpliceWriteStallUnblocks(t *testing.T) {
	overrideTimeouts(t, time.Minute, 100*time.Millisecond)

	a, b := tcpPair(t)

	done := make(chan struct{})
	go func() {
		Splice(a, b)
		close(done)
	}()

	// Pump data into one side while the peer never reads — fills TCP buffers,
	// then the write deadline fires.
	go func() {
		payload := make([]byte, 64*1024)
		for {
			if _, err := a.Write(payload); err != nil {
				return
			}
		}
	}()

	select {
	case <-done:
		// Splice returned — write-stall deadline fired.
	case <-time.After(5 * time.Second):
		t.Fatal("Splice did not return within 5s on a write-stalled peer")
	}
}

// TestSpliceActivityRefreshesIdleDeadline verifies that ongoing traffic keeps
// the splice alive past the idle timeout window (regression guard against
// overzealous deadlines). Traffic must flow in BOTH directions because the idle
// timeout is per-direction.
func TestSpliceActivityRefreshesIdleDeadline(t *testing.T) {
	overrideTimeouts(t, 300*time.Millisecond, time.Minute)

	playerOut, playerIn := tcpPair(t)
	serverIn, serverOut := tcpPair(t)

	done := make(chan struct{})
	go func() {
		Splice(playerIn, serverIn)
		close(done)
	}()

	// Drain both sides in background readers.
	var playerReceived, serverReceived int
	var mu sync.Mutex

	go func() {
		buf := make([]byte, 64)
		for {
			_ = serverOut.SetReadDeadline(time.Now().Add(time.Second))
			n, err := serverOut.Read(buf)
			if err != nil {
				return
			}
			mu.Lock()
			serverReceived += n
			mu.Unlock()
		}
	}()
	go func() {
		buf := make([]byte, 64)
		for {
			_ = playerOut.SetReadDeadline(time.Now().Add(time.Second))
			n, err := playerOut.Read(buf)
			if err != nil {
				return
			}
			mu.Lock()
			playerReceived += n
			mu.Unlock()
		}
	}()

	// Trickle bytes in both directions every 100ms for 1.5s — well beyond the
	// 300ms idle timeout. The splice must stay alive because activity refreshes
	// the deadline on each direction.
	ticks := 15
	for i := 0; i < ticks; i++ {
		time.Sleep(100 * time.Millisecond)
		if _, err := playerOut.Write([]byte("x")); err != nil {
			t.Fatalf("player write tick %d: %v", i, err)
		}
		if _, err := serverOut.Write([]byte("y")); err != nil {
			t.Fatalf("server write tick %d: %v", i, err)
		}
	}

	// Splice must still be alive after 1.5s of continuous bidirectional activity.
	select {
	case <-done:
		t.Fatal("Splice returned prematurely despite continuous activity")
	default:
	}

	// Allow a brief settling time for the last bytes in the pipeline.
	time.Sleep(50 * time.Millisecond)

	mu.Lock()
	if serverReceived < ticks {
		t.Fatalf("server only received %d/%d trickle bytes", serverReceived, ticks)
	}
	if playerReceived < ticks {
		t.Fatalf("player only received %d/%d trickle bytes", playerReceived, ticks)
	}
	mu.Unlock()

	// Close both sides — splice should return cleanly.
	_ = playerOut.Close()
	_ = serverOut.Close()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Splice did not return after closing both sides")
	}
}
