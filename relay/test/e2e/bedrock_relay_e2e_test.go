//go:build e2e

// This file is the relay-side half of the Bedrock protocol-level e2e (epic
// #1540, issue #1547): it runs the REAL relay/internal/bedrock.Listener -- the
// exact production code the compose deployment runs -- bound to a fixed
// address, with a stub Validator standing in for the API's
// RelayService.ValidateBedrockTunnel. That RPC and the credential it backs are
// minted by the API-side OpenBedrockTunnel dispatch (issue #1544) and are out
// of scope here; this suite's job is the wire path AFTER a credential is
// accepted: relay UDP ingress -> QUIC tunnel -> Worker -> container port,
// mirroring how relay_e2e_test.go covers the Java path's protocol-level
// behavior without a real API driving it end to end.
//
// It is the counterpart to worker/test/e2e/bedrock_e2e_test.go, which drives
// the REAL worker/internal/adapters/bedrocktunnel.Manager and a real Docker
// container against this listener. The two Go modules cannot share a test
// process -- relay/internal/... and worker/internal/... are each importable
// only from within their own module's directory tree -- so
// scripts/run_bedrock_e2e.sh runs them as two coordinating `go test`
// processes: this one serves until MCD_BEDROCK_E2E_STOP_FILE appears (or
// bedrockE2EMaxServe elapses, a safety net if the orchestrator dies), the
// worker-side one drives every assertion and owns the test's pass/fail signal.
//
// Gated the same way as relay_e2e_test.go: the `e2e` build tag, plus
// MCD_BEDROCK_E2E_LISTEN (this test skips when unset, so the ordinary
// `go test -tags e2e ./test/e2e/...` sweep -- e.g. with only
// MCD_RELAY_E2E_GAME_ADDR set for the Java suite -- leaves it inert).
package e2e

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"log/slog"
	"math/big"
	"net"
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/bedrock"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
)

// Shared test fixture. Kept in sync with worker/test/e2e/bedrock_e2e_test.go's
// matching declarations of the same name (bedrockE2EServerID, bedrockE2EToken,
// bedrockE2EDefaultBedrockPort, bedrockE2EPort) -- both files are maintained
// together in the same PR; change one, change both.
const (
	bedrockE2EServerID           = "bedrock-e2e-server"
	bedrockE2EToken              = "bedrock-e2e-token"
	bedrockE2EDefaultBedrockPort = 19140

	// bedrockE2EMaxServe bounds how long this side keeps the listener up if the
	// orchestrator never creates the stop file (e.g. it crashed) -- a safety net,
	// not the normal exit path.
	bedrockE2EMaxServe = 90 * time.Second
)

// bedrockE2EPort returns the public bedrock_port fixture:
// MCD_BEDROCK_E2E_BEDROCK_PORT when set (scripts/run_bedrock_e2e.sh forwards
// it so the harness can run alongside a live bedrock-enabled relay-profile
// deployment already holding the default -- the same posture as
// scripts/run_relay_e2e.sh's port overrides), else the default. The default
// sits inside the compose-published client window (19132-19231/udp).
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

// stubValidator accepts exactly the one (server_id, bedrock_port, token)
// triple the worker-side test dials with, standing in for the API's
// ValidateBedrockTunnel RPC (out of scope here; see the package doc comment).
type stubValidator struct {
	bedrockPort uint32
}

func (v stubValidator) ValidateBedrockTunnel(_ context.Context, serverID string, bedrockPort uint32, token string) (bool, error) {
	valid := serverID == bedrockE2EServerID && bedrockPort == v.bedrockPort && token == bedrockE2EToken
	return valid, nil
}

// noopSessionRecorder stands in for the API session reporter (issue #1904): this
// suite drives the wire path, not session reporting, so promoted flows are
// recorded nowhere.
type noopSessionRecorder struct{}

func (noopSessionRecorder) Start(_, _, _, _, _ string) string { return "" }
func (noopSessionRecorder) End(_ string)                      {}

// TestServeBedrockTunnelForE2E runs the real Bedrock tunnel listener until
// MCD_BEDROCK_E2E_STOP_FILE appears or bedrockE2EMaxServe elapses, whichever is
// first. scripts/run_bedrock_e2e.sh runs this in the background, waits for the
// "BEDROCK-E2E-RELAY-READY" log line, runs the worker-side assertions in the
// foreground, then creates the stop file so this test returns and reports a
// normal pass/fail rather than being killed.
func TestServeBedrockTunnelForE2E(t *testing.T) {
	addr := os.Getenv("MCD_BEDROCK_E2E_LISTEN")
	if addr == "" {
		t.Skip("MCD_BEDROCK_E2E_LISTEN not set; run via scripts/run_bedrock_e2e.sh")
	}
	caFile := os.Getenv("MCD_BEDROCK_E2E_CA_FILE")
	if caFile == "" {
		t.Fatal("MCD_BEDROCK_E2E_CA_FILE must be set alongside MCD_BEDROCK_E2E_LISTEN")
	}
	stopFile := os.Getenv("MCD_BEDROCK_E2E_STOP_FILE")
	if stopFile == "" {
		t.Fatal("MCD_BEDROCK_E2E_STOP_FILE must be set alongside MCD_BEDROCK_E2E_LISTEN")
	}

	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))

	tlsConf, caPEM := selfSignedServerTLS(t, addr)
	if err := os.WriteFile(caFile, caPEM, 0o644); err != nil { //nolint:gosec
		t.Fatalf("write CA file: %v", err)
	}

	preAuthCaps := ipcaps.NewIPCaps(0, 0, 0, nil, logger)
	newIPCaps := func() *ipcaps.IPCaps { return ipcaps.NewIPCaps(0, 0, 0, nil, logger) }

	ln, err := bedrock.NewListener(addr, tlsConf, stubValidator{bedrockPort: bedrockE2EPort(t)}, preAuthCaps, newIPCaps, noopSessionRecorder{}, nil, logger)
	if err != nil {
		t.Fatalf("bedrock.NewListener(%q): %v", addr, err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	serveDone := make(chan struct{})
	go func() {
		defer close(serveDone)
		if err := ln.Serve(ctx); err != nil {
			logger.Warn("Serve returned an error", "error", err)
		}
	}()

	// The orchestrator polls this log line before starting the worker-side test
	// (mirrors scripts/run_relay_e2e.sh polling relay logs for "relay registered
	// with API"). Logged directly via the logger -- not t.Log -- so it reaches
	// the captured file immediately regardless of `go test -v` buffering.
	logger.Info("BEDROCK-E2E-RELAY-READY", "addr", ln.Addr().String())

	deadline := time.Now().Add(bedrockE2EMaxServe)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(stopFile); err == nil {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}

	cancel()
	select {
	case <-serveDone:
	case <-time.After(5 * time.Second):
		t.Fatal("Serve did not return after ctx cancel")
	}
}

// selfSignedServerTLS builds a one-off self-signed TLS config for addr's host
// (mirrors relay/internal/bedrock/quictest_test.go's selfSignedTLS, generalized
// to a configurable IP instead of always 127.0.0.1), returning the certificate's
// PEM encoding so the caller can hand it to the worker-side test as
// bedrocktunnel.Spec.CAPEM.
func selfSignedServerTLS(t *testing.T, addr string) (*tls.Config, []byte) {
	t.Helper()
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		t.Fatalf("split host:port %q: %v", addr, err)
	}
	ip := net.ParseIP(host)
	if ip == nil {
		t.Fatalf("MCD_BEDROCK_E2E_LISTEN host %q must be an IP literal", host)
	}

	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "bedrock-e2e"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		IPAddresses:  []net.IP{ip},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert := tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	return &tls.Config{Certificates: []tls.Certificate{cert}, NextProtos: []string{bedrock.ALPN}, MinVersion: tls.VersionTLS13}, caPEM
}
