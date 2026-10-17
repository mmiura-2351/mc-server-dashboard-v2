package rcon

import (
	"context"
	"fmt"
	"net"
	"strconv"
	"strings"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/serverproperties"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/javaproperties"
)

// defaultRCONPort is the Minecraft default RCON port when server.properties does
// not override it.
const defaultRCONPort = 25575

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
// the file is read in (issue #3116). An rcon.port that is missing or not an
// integer dials the default port, since the server itself listens there then; an
// integer outside 1-65535 is an error. It errors when RCON is not
// enabled/configured; the graceful-stop path then falls back to signals. No error
// carries a value read from the file, because the container driver logs them
// (issue #3345).
func OpenFromWorkingDir(ctx context.Context, workingDir, host, mcVersion string) (*Client, error) {
	props, err := readProperties(workingDir, mcVersion)
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
	// Minecraft reads rcon.port with Integer.parseInt and takes the default when
	// that throws, so a value that is not a 32-bit integer -- absent, or a
	// backslash-continued line that folded the next line (a password, say) into it
	// -- means the server listens on the default port (issue #3345). The address
	// is always built from the number, never from the text read: Dial's errors
	// repeat the address and the caller logs them, and for the same reason an
	// out-of-range port is refused without naming it.
	port, err := strconv.ParseInt(props["rcon.port"], 10, 32)
	if err != nil {
		port = defaultRCONPort
	}
	if port < 1 || port > 65535 {
		return nil, fmt.Errorf("rcon: rcon.port in server.properties is not a port number (1-65535)")
	}
	if host == "" {
		host = defaultRCONHost
	}
	return Dial(ctx, net.JoinHostPort(host, strconv.FormatInt(port, 10)), password)
}

// readProperties reads and parses the server.properties of workingDir with the
// shared Java-compatible reader (internal/javaproperties), in the charset a server
// of mcVersion loads it in, so the credential dialed with is the one the Minecraft
// server itself read: a "rcon.password:secret", an escaped or \uXXXX-spelled
// key, or a backslash continuation is no longer invisible here (issue #2811),
// and a non-ASCII password reaches the server as the text it holds (issue
// #3116).
//
// The bytes come from the shared no-follow reader (adapters/serverproperties),
// because the password read here is then sent to the server's own container: a
// server.properties the server replaced with a link to another server's file
// would otherwise disclose that server's password (issue #3335). Any read
// failure is an error -- an absent file, and a file the reader refuses (a
// symlink, a FIFO or other non-regular file, an oversized one). There is no
// default and no remembered password to fall back on: without one read from this
// file, now, there is nothing to dial, and the caller falls back to signals.
func readProperties(workingDir, mcVersion string) (map[string]string, error) {
	data, err := serverproperties.Read(workingDir)
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
