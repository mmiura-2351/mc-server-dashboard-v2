package rcon

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// defaultRCONPort is the Minecraft default RCON port when server.properties does
// not override it.
const defaultRCONPort = "25575"

// OpenFromWorkingDir dials RCON for a server using the rcon.port and
// rcon.password from its working-dir server.properties (the canonical source of
// a server's RCON settings). It errors when RCON is not enabled/configured; the
// graceful-stop path then falls back to signals.
func OpenFromWorkingDir(ctx context.Context, workingDir string) (*Client, error) {
	props, err := readProperties(filepath.Join(workingDir, "server.properties"))
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
	return Dial(ctx, "127.0.0.1:"+port, password)
}

// readProperties parses a Java .properties file into a map. Lines that are blank
// or comments (# or !) are skipped.
func readProperties(path string) (map[string]string, error) {
	f, err := os.Open(path) //nolint:gosec // path is the server's own working dir, not user-controlled.
	if err != nil {
		return nil, fmt.Errorf("rcon: read server.properties: %w", err)
	}
	defer func() { _ = f.Close() }()

	out := map[string]string{}
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "!") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		out[strings.TrimSpace(key)] = strings.TrimSpace(value)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("rcon: scan server.properties: %w", err)
	}
	return out, nil
}
