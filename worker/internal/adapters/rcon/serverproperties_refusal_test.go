package rcon

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/serverproperties"
)

// These tests pin what OpenFromWorkingDir does with a server.properties the
// server planted (issue #3335). The credential it dials with is sent to the
// server's own container, so a server.properties linked to another server's file
// used to hand that server's RCON password to whoever planted the link.

// rconListener accepts RCON connections and records whether anything dialed it.
type rconListener struct {
	ln     net.Listener
	dialed chan struct{}
}

// newRCONListener listens on loopback and answers every auth as accepted, so a
// client that reaches it with any password at all succeeds.
func newRCONListener(t *testing.T) *rconListener {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	l := &rconListener{ln: ln, dialed: make(chan struct{}, 1)}
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		l.dialed <- struct{}{}
		id, _, _, _, err := readPacketMC(conn, false)
		if err != nil {
			return
		}
		_ = writePacket(conn, id, typeResponseValue, "")
		_ = writePacket(conn, id, typeAuthResponse, "")
	}()
	return l
}

// assertRefusedWithoutDialing fails unless the open failed, quickly, with an error
// naming the file and carrying none of secret, and nothing connected to l.
func assertRefusedWithoutDialing(t *testing.T, l *rconListener, workingDir, secret string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	type result struct {
		client *Client
		err    error
	}
	done := make(chan result, 1)
	go func() {
		client, err := OpenFromWorkingDir(ctx, workingDir, "", "")
		done <- result{client, err}
	}()
	var r result
	select {
	case r = <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("OpenFromWorkingDir blocked")
	}
	if r.err == nil {
		_ = r.client.Close()
		t.Fatal("OpenFromWorkingDir = nil error; want the planted server.properties refused")
	}
	if !strings.Contains(r.err.Error(), "server.properties") {
		t.Errorf("error = %v, want it to name server.properties", r.err)
	}
	if secret != "" && strings.Contains(r.err.Error(), secret) {
		t.Errorf("error leaks the file's content: %v", r.err)
	}
	select {
	case <-l.dialed:
		t.Error("RCON was dialed; a refused server.properties must not lead to any authentication")
	case <-time.After(100 * time.Millisecond):
	}
}

func TestOpenFromWorkingDirRefusesALinkToAnotherServersProperties(t *testing.T) {
	l := newRCONListener(t)
	const otherPassword = "the-other-servers-password"
	other := t.TempDir()
	writeRCONProps(t, other, listenPort(t, l.ln.Addr().String()), otherPassword)

	dir := t.TempDir()
	if err := os.Symlink(filepath.Join(other, "server.properties"), filepath.Join(dir, "server.properties")); err != nil {
		t.Fatal(err)
	}
	assertRefusedWithoutDialing(t, l, dir, otherPassword)
}

func TestOpenFromWorkingDirRefusesAFIFOWithoutBlocking(t *testing.T) {
	l := newRCONListener(t)
	dir := t.TempDir()
	if err := unix.Mkfifo(filepath.Join(dir, "server.properties"), 0o600); err != nil {
		t.Fatal(err)
	}
	assertRefusedWithoutDialing(t, l, dir, "")
}

// An oversized file is refused whole: the credentials in the part that would fit
// under the cap are not used.
func TestOpenFromWorkingDirRefusesAnOversizedFile(t *testing.T) {
	l := newRCONListener(t)
	dir := t.TempDir()
	body := "enable-rcon=true\nrcon.port=" + listenPort(t, l.ln.Addr().String()) + "\nrcon.password=pw\n#" +
		strings.Repeat("x", serverproperties.MaxSize)
	if err := os.WriteFile(filepath.Join(dir, "server.properties"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	assertRefusedWithoutDialing(t, l, dir, "")
}

// A refusal does not fall back to the credentials a previous, successful open
// read from the same working set.
func TestOpenFromWorkingDirDoesNotReuseAnEarlierPasswordAfterARefusal(t *testing.T) {
	first := newFakeServer(t, "pw")
	dir := t.TempDir()
	port := listenPort(t, first.addr())
	writeRCONProps(t, dir, port, "pw")
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	client, err := OpenFromWorkingDir(ctx, dir, "", "")
	if err != nil {
		t.Fatalf("OpenFromWorkingDir on a regular file: %v", err)
	}
	_ = client.Close()

	l := newRCONListener(t)
	other := t.TempDir()
	writeRCONProps(t, other, listenPort(t, l.ln.Addr().String()), "pw")
	path := filepath.Join(dir, "server.properties")
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(other, "server.properties"), path); err != nil {
		t.Fatal(err)
	}
	assertRefusedWithoutDialing(t, l, dir, "")
}
