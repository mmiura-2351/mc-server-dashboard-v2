//go:build e2e

// Exercise real Worker QUIC tunnels, relay routing, and a Docker RakNet ping/pong stub.
// This checks datagram forwarding and flow isolation, not full Geyser login.
package e2e

import (
	"context"
	"encoding/binary"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/bedrocktunnel"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/containerdriver"
)

// Keep these fixture IDs, tokens, ports, and overrides aligned with relay/test/e2e/bedrock_relay_e2e_test.go.
const (
	bedrockE2EServerID           = "bedrock-e2e-server"
	bedrockE2EToken              = "bedrock-e2e-token"
	bedrockE2EDefaultBedrockPort = 19140
)

// Allow an overridden public UDP port so E2E can run beside a live deployment; the default lies in the published
// range.
func bedrockE2EPort(t *testing.T) uint32 {
	t.Helper()
	v := os.Getenv("MCD_BEDROCK_E2E_BEDROCK_PORT")
	if v == "" {
		return bedrockE2EDefaultBedrockPort
	}
	port, err := strconv.ParseUint(v, 10, 16)
	if err != nil || port == 0 {
		t.Fatalf("MCD_BEDROCK_E2E_BEDROCK_PORT %q is not a valid port", v)
	}
	return uint32(port)
}

// --- minimal RakNet client (mirrors worker/test/e2e/stub-geyser/main.go's
// server-side framing) ---

const (
	raknetIDUnconnectedPing = 0x01
	raknetIDUnconnectedPong = 0x1c
)

var raknetMagic = [16]byte{0x00, 0xff, 0xff, 0x00, 0xfe, 0xfe, 0xfe, 0xfe, 0xfd, 0xfd, 0xfd, 0xfd, 0x12, 0x34, 0x56, 0x78}

func buildUnconnectedPing(pingTime, clientGUID int64) []byte {
	buf := make([]byte, 1+8+16+8)
	buf[0] = raknetIDUnconnectedPing
	binary.BigEndian.PutUint64(buf[1:9], uint64(pingTime))
	copy(buf[9:25], raknetMagic[:])
	binary.BigEndian.PutUint64(buf[25:33], uint64(clientGUID))
	return buf
}

// parseUnconnectedPong extracts the echoed ping-time field from a well-formed
// Unconnected Pong, or returns an error.
func parseUnconnectedPong(data []byte) (pingTime int64, err error) {
	const minLen = 1 + 8 + 8 + 16 + 2
	if len(data) < minLen {
		return 0, fmt.Errorf("short pong: %d bytes, want at least %d", len(data), minLen)
	}
	if data[0] != raknetIDUnconnectedPong {
		return 0, fmt.Errorf("message id 0x%02x, want 0x%02x", data[0], raknetIDUnconnectedPong)
	}
	return int64(binary.BigEndian.Uint64(data[1:9])), nil
}

// pingRelay sends one Unconnected Ping to the relay's public Bedrock port and
// returns the pong's echoed ping-time field.
func pingRelay(relayPublicAddr string, pingTime int64) (int64, error) {
	conn, err := net.Dial("udp", relayPublicAddr)
	if err != nil {
		return 0, err
	}
	defer func() { _ = conn.Close() }()
	if err := conn.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
		return 0, err
	}
	if _, err := conn.Write(buildUnconnectedPing(pingTime, pingTime)); err != nil {
		return 0, err
	}
	buf := make([]byte, 2048)
	n, err := conn.Read(buf)
	if err != nil {
		return 0, err
	}
	return parseUnconnectedPong(buf[:n])
}

// pollUntil retries cond every 200ms until it returns true or timeout elapses,
// failing the test on timeout.
func pollUntil(t *testing.T, timeout time.Duration, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	if !cond() {
		t.Fatalf("%s: condition not met within %s", what, timeout)
	}
}

// The host test lacks container-network DNS, so inspect the IP directly; production dials container names.
func containerIPAddress(t *testing.T, ctx context.Context, containerName, network string) string {
	t.Helper()
	// `index` (not dotted field access) because a docker-network name may
	// contain '-', which Go's text/template parser rejects mid-identifier.
	format := fmt.Sprintf(`{{(index .NetworkSettings.Networks %q).IPAddress}}`, network)
	out, err := exec.CommandContext(ctx, "docker", "inspect", "-f", format, containerName).CombinedOutput()
	if err != nil {
		t.Fatalf("docker inspect %s: %v: %s", containerName, err, out)
	}
	ip := strings.TrimSpace(string(out))
	if ip == "" {
		t.Fatalf("docker inspect %s: empty IP on network %s", containerName, network)
	}
	return ip
}

// TestBedrockTunnelEndToEnd is documented in this file's package doc comment.
func TestBedrockTunnelEndToEnd(t *testing.T) {
	if os.Getenv("MCD_E2E_DOCKER") == "" {
		t.Skip("MCD_E2E_DOCKER not set; skipping Bedrock e2e (needs a Docker daemon)")
	}
	relayAddr := env(t, "MCD_BEDROCK_E2E_RELAY_ADDR")
	caFile := env(t, "MCD_BEDROCK_E2E_CA_FILE")
	image := env(t, "MCD_E2E_STUB_GEYSER_IMAGE")

	caPEM, err := os.ReadFile(caFile)
	if err != nil {
		t.Fatalf("read CA file %s: %v", caFile, err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	// A dedicated user-defined network, matching how compose.yaml pins the real
	// deployment's `mcsd` network for container-name DNS
	// (docs/dev/DEPLOYMENT.md Section 7): the default bridge has none.
	networkName := "mcsd-bedrock-e2e-" + newServerID(t)
	if out, err := exec.CommandContext(ctx, "docker", "network", "create", networkName).CombinedOutput(); err != nil {
		t.Fatalf("docker network create %s: %v: %s", networkName, err, out)
	}
	t.Cleanup(func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		_ = exec.CommandContext(cleanupCtx, "docker", "network", "rm", networkName).Run()
	})

	docker, err := containerdriver.NewEngineClient("")
	if err != nil {
		t.Fatalf("docker engine client: %v", err)
	}

	// Created directly via EngineClient, bypassing containerdriver.Driver: the
	// fake Geyser is not an MC server (no java shim, no working-dir bind, no
	// RCON), so the Driver's MC-specific plumbing does not apply -- only its
	// network-attach mechanics, exercised the same way the Driver's own Create
	// uses them (dockerclient.go's CreateSpec.Network).
	containerName := "bedrock-e2e-geyser-" + newServerID(t)
	id, err := docker.Create(ctx, containerdriver.CreateSpec{
		Name:    containerName,
		Image:   image,
		Network: networkName,
		Labels:  map[string]string{"mcsd.bedrock-e2e": "1"},
	})
	if err != nil {
		t.Fatalf("docker create: %v", err)
	}
	t.Cleanup(func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		_ = docker.Remove(cleanupCtx, id)
	})
	if err := docker.Start(ctx, id); err != nil {
		t.Fatalf("docker start: %v", err)
	}

	containerIP := containerIPAddress(t, ctx, containerName, networkName)

	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	mgrCtx, mgrCancel := context.WithCancel(context.Background())
	defer mgrCancel()
	// gameHost returns the discovered container IP regardless of the requested
	// server id: this harness opens exactly one tunnel.
	m := bedrocktunnel.New(mgrCtx, "0.0.0.0", func(string) string { return containerIP }, logger)

	bedrockPort := bedrockE2EPort(t)
	spec := bedrocktunnel.Spec{
		ServerID:      bedrockE2EServerID,
		RelayEndpoint: relayAddr,
		BedrockPort:   bedrockPort,
		Token:         bedrockE2EToken,
		CAPEM:         string(caPEM),
	}
	if err := m.Open(spec); err != nil {
		t.Fatalf("Open: %v", err)
	}

	relayPublicAddr := fmt.Sprintf("127.0.0.1:%d", bedrockPort)

	// 1. A scripted client's datagram reaches the container and the reply
	// returns through the same flow. The tunnel dial/handshake/bind chain is
	// asynchronous (Manager.Open only registers it), so this also doubles as
	// the "tunnel is up" readiness poll.
	var firstReply int64
	pollUntil(t, 30*time.Second, "initial ping round trip", func() bool {
		got, err := pingRelay(relayPublicAddr, 111)
		if err != nil {
			return false
		}
		firstReply = got
		return true
	})
	if firstReply != 111 {
		t.Fatalf("pong ping-time = %d, want 111 (echoed from the ping)", firstReply)
	}

	// 2. Two concurrent clients demux correctly: each sends a distinct ping
	// time and must get back exactly its own value, never the other's.
	var wg sync.WaitGroup
	results := make([]int64, 2)
	errs := make([]error, 2)
	pingTimes := []int64{222, 333}
	for i := range 2 {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			results[i], errs[i] = pingRelay(relayPublicAddr, pingTimes[i])
		}(i)
	}
	wg.Wait()
	for i := range 2 {
		if errs[i] != nil {
			t.Fatalf("concurrent client %d: %v", i, errs[i])
		}
		if results[i] != pingTimes[i] {
			t.Fatalf("concurrent client %d: pong ping-time = %d, want %d (its own value, not the other client's)", i, results[i], pingTimes[i])
		}
	}

	// Close the tunnel directly to test UDP unbinding; command-to-tunnel wiring is covered by Manager unit tests.
	m.Close(bedrockE2EServerID)
	pollUntil(t, 10*time.Second, "relay port unbind", func() bool {
		l, err := net.ListenPacket("udp", relayPublicAddr)
		if err != nil {
			return false
		}
		_ = l.Close()
		return true
	})
}
