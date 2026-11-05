package rcon

import (
	"context"
	"testing"
	"time"
)

// Redial opens a fresh connection with the address and password the client was
// dialed with (issue #3511), so a caller can reconnect without reading
// server.properties again.
func TestRedialReusesTheDialedCredentials(t *testing.T) {
	fs := newFakeServer(t, "secret")
	fs.reply["save-on"] = "Automatic saving is now enabled"

	ctx := context.Background()
	c, err := Dial(ctx, fs.addr(), "secret")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	_ = c.Close()
	// The fake serves one connection; accept the redial's as well.
	go fs.serve()

	fresh, err := c.Redial(ctx)
	if err != nil {
		t.Fatalf("Redial: %v", err)
	}
	defer func() { _ = fresh.Close() }()
	if out, err := fresh.Execute(ctx, "save-on"); err != nil || out != "Automatic saving is now enabled" {
		t.Fatalf("Execute on the redialed client = %q, %v", out, err)
	}
	select {
	case got := <-fs.got:
		if got != "save-on" {
			t.Fatalf("server received %q, want save-on", got)
		}
	case <-time.After(time.Second):
		t.Fatal("server never received the redialed command")
	}
}
