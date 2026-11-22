package rcon

import (
	"context"
	"errors"
	"net"
	"os"
	"testing"
	"testing/synctest"
	"time"
)

// A peer that accepts the connection and then never answers fails one RCON leg
// at the per-leg ceiling, whatever deadline the caller's context carries (issue
// #4031). The snapshot quiesce runs on the snapshot command's context, about
// 630 s long, and the stop flush on a 90 s one. The ceiling used to apply only
// to a context with no deadline, so a hung peer held a leg for the whole of
// either. These tests run on synctest's fake clock, so the ceiling and the
// deadline are the real values and nothing waits.
const longDeadline = 630 * time.Second

func TestAuthLegFailsAtTheCeilingUnderALongerDeadline(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		conn, peer := net.Pipe()
		go hungPeer(peer, false)
		ctx, cancel := context.WithTimeout(context.Background(), longDeadline)
		defer cancel()

		start := time.Now()
		_, err := NewClient(ctx, conn, "pipe", "secret")
		assertFailedAtTheCeiling(t, time.Since(start), err, ctx.Err())
	})
}

func TestCommandLegFailsAtTheCeilingUnderALongerDeadline(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		conn, peer := net.Pipe()
		go hungPeer(peer, true)
		ctx, cancel := context.WithTimeout(context.Background(), longDeadline)
		defer cancel()
		c, err := NewClient(ctx, conn, "pipe", "secret")
		if err != nil {
			t.Fatalf("NewClient: %v", err)
		}
		defer func() { _ = c.Close() }()

		start := time.Now()
		_, err = c.Execute(ctx, "save-all")
		assertFailedAtTheCeiling(t, time.Since(start), err, ctx.Err())
	})
}

// assertFailedAtTheCeiling checks that a leg against a hung peer failed with its
// own i/o timeout after exactly the per-leg ceiling, while its context was still
// live (ctxErr is the context's Err on return).
func assertFailedAtTheCeiling(t *testing.T, elapsed time.Duration, err, ctxErr error) {
	t.Helper()
	if elapsed != defaultExecuteTimeout {
		t.Fatalf("the leg failed after %v under a %v deadline, want %v (the per-leg ceiling)", elapsed, longDeadline, defaultExecuteTimeout)
	}
	if !errors.Is(err, os.ErrDeadlineExceeded) {
		t.Fatalf("error = %v, want the leg's own i/o timeout", err)
	}
	if ctxErr != nil {
		t.Fatalf("ctx.Err() = %v, want the context still live: the ceiling ends the leg, not the context", ctxErr)
	}
}

// hungPeer serves the server end of an in-memory connection for a peer that
// accepts the connection and then hangs: it answers the AUTH handshake only when
// answerAuth is set and never answers a command. It returns once the client
// closes the connection.
func hungPeer(conn net.Conn, answerAuth bool) {
	defer func() { _ = conn.Close() }()
	for {
		id, typ, _, err := readPacket(conn)
		if err != nil {
			return
		}
		if typ == typeAuth && answerAuth {
			_ = writePacket(conn, id, typeAuthResponse, "")
		}
	}
}
