package rcon

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/javaproperties"
)

// defaultRCONPort is the Minecraft default RCON port when server.properties does
// not override it.
const defaultRCONPort = "25575"

// defaultRCONHost is the dial host when the caller passes an empty host: the host
// loopback, preserving the historical bare-metal behavior.
const defaultRCONHost = "127.0.0.1"

// OpenFromWorkingDir dials RCON for a server using the rcon.port and
// rcon.password from its working-dir server.properties (the canonical source of
// a server's RCON settings) at the given host. An empty host dials the loopback
// (127.0.0.1), the historical behavior; the container driver passes the MC
// container's name when its containers run on a user-defined network, so RCON is
// reached over that network instead of the unreachable host loopback (issue
// #218). mcVersion is the server's Minecraft version, which decides the charset
// the file is read in (issue #3116). It errors when RCON is not
// enabled/configured; the graceful-stop path then falls back to signals.
func OpenFromWorkingDir(ctx context.Context, workingDir, host, mcVersion string) (*Client, error) {
	props, err := readProperties(filepath.Join(workingDir, "server.properties"), mcVersion)
	if err != nil {
		return nil, err
	}
	if props["enable-rcon"] != "true" {
		return nil, fmt.Errorf("rcon: not enabled in server.properties")
	}
	password := props["rcon.password"]
	if password == "" {
		return nil, fmt.Errorf("rcon: no rcon.password in server.properties")
	}
	port := props["rcon.port"]
	if port == "" {
		port = defaultRCONPort
	}
	if host == "" {
		host = defaultRCONHost
	}
	return Dial(ctx, net.JoinHostPort(host, port), password)
}

// readProperties reads and parses the server.properties at path with the shared
// Java-compatible reader (internal/javaproperties), in the charset a server of
// mcVersion loads it in, so the credential dialed with is the one the Minecraft
// server itself read: a "rcon.password:secret", an escaped or \uXXXX-spelled
// key, or a backslash continuation is no longer invisible here (issue #2811),
// and a non-ASCII password reaches the server as the text it holds (issue
// #3116). Any read failure is an error, an absent file included -- without a
// password there is nothing to dial, and the caller falls back to signals.
func readProperties(path, mcVersion string) (map[string]string, error) {
	data, err := os.ReadFile(path) //nolint:gosec // path is the server's own working dir, not user-controlled.
	if err != nil {
		return nil, fmt.Errorf("rcon: read server.properties: %w", err)
	}
	if readsUTF8(mcVersion) {
		return javaproperties.ParseUTF8(data), nil
	}
	return javaproperties.Parse(data), nil
}

// readsUTF8 reports whether a Minecraft server of version mcVersion loads its
// server.properties as UTF-8 first, which it does from 1.20 on, the
// year-numbered releases (26.1 and later) included. An empty or unparseable
// version keeps latin-1, the reader the Worker used before it knew the version.
func readsUTF8(mcVersion string) bool {
	parts := strings.Split(mcVersion, ".")
	major, err := strconv.Atoi(parts[0])
	if err != nil {
		return false
	}
	if major != 1 {
		return major > 1
	}
	if len(parts) < 2 {
		return false
	}
	minor, err := strconv.Atoi(parts[1])
	return err == nil && minor >= 20
}
