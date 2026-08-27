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

// TestOpenFromWorkingDirReadsJavaSpellings verifies the credential is read from
// the spellings java.util.Properties.load accepts — a colon separator, a
// whitespace separator, and an escaped key — so the Worker dials with what the
// Minecraft server itself read rather than missing the line entirely (#2811).
func TestOpenFromWorkingDirReadsJavaSpellings(t *testing.T) {
	for _, tc := range []struct {
		name string
		// body renders the server.properties once the fake server's port is known.
		body func(port string) string
	}{
		{"colon separator", func(p string) string {
			return "enable-rcon:true\nrcon.port:" + p + "\nrcon.password:pw\n"
		}},
		{"whitespace separator", func(p string) string {
			return "enable-rcon true\nrcon.port " + p + "\nrcon.password pw\n"
		}},
		{"escaped key", func(p string) string {
			return "enable-rcon=true\nrcon.port=" + p + "\nrcon\\.password=pw\n"
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// A fakeServer accepts exactly one connection, so each case needs its own.
			fs := newFakeServer(t, "pw")
			dir := t.TempDir()
			body := tc.body(listenPort(t, fs.addr()))
			if err := os.WriteFile(filepath.Join(dir, "server.properties"), []byte(body), 0o600); err != nil {
				t.Fatal(err)
			}

			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			client, err := OpenFromWorkingDir(ctx, dir, "")
			if err != nil {
				t.Fatalf("OpenFromWorkingDir = %v, want nil", err)
			}
			_ = client.Close()
		})
	}
}

// TestOpenFromWorkingDirUsesTheLastOccurrence verifies a key respelled and
// appended after an untouched first line wins, as it does for the Minecraft
// server — reading the stale first value would lose RCON control (#2811).
func TestOpenFromWorkingDirUsesTheLastOccurrence(t *testing.T) {
	fs := newFakeServer(t, "pw")
	dir := t.TempDir()
	port := listenPort(t, fs.addr())
	body := "enable-rcon=true\nrcon.port=" + port + "\nrcon.password=stale\nrcon.password:pw\n"
	if err := os.WriteFile(filepath.Join(dir, "server.properties"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	client, err := OpenFromWorkingDir(ctx, dir, "")
	if err != nil {
		t.Fatalf("OpenFromWorkingDir = %v, want nil (the appended password must win)", err)
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
