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

// OpenFromWorkingDir requires enabled RCON and credentials from server.properties.
// host selects loopback or container DNS; mcVersion selects the password charset.
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

// readProperties uses the Minecraft version's charset and Java properties grammar.
// All read failures, including a missing file, prevent RCON setup.
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

// Minecraft 1.20+ and year-numbered releases read UTF-8 first; unknown versions keep latin-1.
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
