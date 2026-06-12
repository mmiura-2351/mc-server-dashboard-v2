package splice

import (
	"io"
	"net"
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
