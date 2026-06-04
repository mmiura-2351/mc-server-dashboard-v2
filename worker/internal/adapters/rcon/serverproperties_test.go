package rcon

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

// writeRCONProps writes a server.properties enabling RCON on the given port with
// the given password into workingDir.
func writeRCONProps(t *testing.T, workingDir, port, password string) {
	t.Helper()
	body := "enable-rcon=true\nrcon.port=" + port + "\nrcon.password=" + password + "\n"
	if err := os.WriteFile(filepath.Join(workingDir, "server.properties"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

// listenPort splits the port out of a fakeServer's loopback listen address.
func listenPort(t *testing.T, addr string) string {
	t.Helper()
	_, port, err := net.SplitHostPort(addr)
	if err != nil {
		t.Fatalf("split host port %q: %v", addr, err)
	}
	if _, err := strconv.Atoi(port); err != nil {
		t.Fatalf("port %q not numeric: %v", port, err)
	}
	return port
}

// TestOpenFromWorkingDirDefaultsToLoopback verifies an empty host dials loopback,
// preserving the historical bare-metal behavior.
func TestOpenFromWorkingDirDefaultsToLoopback(t *testing.T) {
	fs := newFakeServer(t, "pw")
	dir := t.TempDir()
	writeRCONProps(t, dir, listenPort(t, fs.addr()), "pw")

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	client, err := OpenFromWorkingDir(ctx, dir, "")
	if err != nil {
		t.Fatalf("OpenFromWorkingDir(host=\"\") error = %v", err)
	}
	_ = client.Close()
}

// TestOpenFromWorkingDirUsesHostOverride verifies a non-empty host is used as the
// dial host (the container-name case), with the rcon.port from server.properties.
func TestOpenFromWorkingDirUsesHostOverride(t *testing.T) {
	fs := newFakeServer(t, "pw")
	dir := t.TempDir()
	writeRCONProps(t, dir, listenPort(t, fs.addr()), "pw")

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	// "localhost" resolves to the loopback listener; it differs from the literal
	// 127.0.0.1 default, proving the override host is the one dialed.
	client, err := OpenFromWorkingDir(ctx, dir, "localhost")
	if err != nil {
		t.Fatalf("OpenFromWorkingDir(host=\"localhost\") error = %v", err)
	}
	_ = client.Close()
}
