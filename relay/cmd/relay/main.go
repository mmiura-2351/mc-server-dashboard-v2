// Command relay is the entry point (the edge / wiring layer) of the game
// ingress relay. It loads configuration, dials the API's RelayService, binds
// the public game listener and the TLS tunnel listener, and runs them until
// SIGINT/SIGTERM triggers a clean shutdown (docs/app/RELAY.md Sections 2–7, 13;
// CONFIGURATION.md Section 1 keeps config reading at the edge).
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/config"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/bedrock"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/game"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/ipcaps"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/relaysvc"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/session"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/tunnel"
)

// tokenTTL is the single-use tunnel token lifetime. The API mints tokens with a
// 10 s TTL (RELAY.md Section 4); the relay's table tracks the same window so a
// late dial-back finds no waiter.
const tokenTTL = 10 * time.Second

// drainTimeout bounds how long the shutdown sequence waits for in-flight
// handle goroutines (active splices) to finish before proceeding. After this
// deadline the reporter shuts down and may miss End events for sessions
// that were still splicing (issue #1051).
const drainTimeout = 30 * time.Second

// configPathEnv names the env var pointing at the TOML config file (optional).
const configPathEnv = "MCD_RELAY_CONFIG"

func main() {
	if err := run(context.Background()); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// run wires the relay and blocks until shutdown. It returns nil on a
// signal-driven clean shutdown and an error on a fatal config/bind failure.
func run(ctx context.Context) error {
	cfg, err := config.Load(os.Getenv(configPathEnv), os.Getenv)
	if err != nil {
		return err
	}

	logger := newLogger(cfg.Log)
	logger.Info("relay configuration loaded", "config", cfg)

	// The tunnel CA the relay advertises to Workers (Register → TunnelDial) for
	// verifying the tunnel certificate (RELAY.md Section 5). Three cases keyed on
	// tunnel.tls.advertised_ca_file: unset → the listener cert PEM (self-signed
	// default); "system" → empty (Workers use system roots); a path → that PEM.
	tunnelCAPEM, err := advertisedTunnelCA(cfg.Tunnel.TLS)
	if err != nil {
		return err
	}

	conn, err := dial(cfg.API, logger)
	if err != nil {
		return err
	}
	defer func() { _ = conn.Close() }()

	apiClient := apiclient.New(conn, cfg.API.Credential)
	reporter := session.NewReporter(apiClient, logger, time.Now)
	svc := relaysvc.New(apiClient, conn, reporter, cfg.Tunnel.PublicEndpoint, tunnelCAPEM, logger)

	tokens := tunnel.NewTokenTable(tokenTTL, time.Now)
	cache := game.NewStatusCache(time.Duration(cfg.Game.StatusCacheSeconds)*time.Second, int(cfg.Game.StatusCacheMaxEntries), time.Now)
	caps := ipcaps.NewIPCaps(cfg.Game.MaxConnsPerIP, cfg.Game.JoinsPerIPPerSecond, 0, time.Now)
	tunnelCaps := ipcaps.NewIPCaps(cfg.Tunnel.MaxConnsPerIP, 0, 0, time.Now)

	tunnelTLS, err := buildTunnelTLS(cfg.Tunnel.TLS)
	if err != nil {
		return err
	}
	tunnelLn, err := tunnel.NewListener(cfg.Tunnel.Listen, tunnelTLS, tokens, tunnelCaps, logger)
	if err != nil {
		return fmt.Errorf("bind tunnel listener %q: %w", cfg.Tunnel.Listen, err)
	}
	gameLn, err := game.NewListener(cfg.Game.Listen, svc, tokens, cache, caps, reporter, logger)
	if err != nil {
		return fmt.Errorf("bind game listener %q: %w", cfg.Game.Listen, err)
	}

	// The Bedrock tunnel listener reuses the TCP tunnel's TLS certificate (same
	// CA the Worker already gets via Register -> OpenBedrockTunnel.tls_ca_pem,
	// docs/app/BEDROCK_TUNNEL.md); only the negotiated ALPN differs, so the two
	// listeners are distinguishable on the wire despite sharing a cert.
	bedrockTLS := tunnelTLS.Clone()
	bedrockTLS.NextProtos = []string{bedrock.ALPN}
	newBedrockIPCaps := func() *ipcaps.IPCaps {
		return ipcaps.NewIPCaps(cfg.Bedrock.MaxFlowsPerIP, cfg.Bedrock.NewFlowsPerIPPerSecond, 0, time.Now)
	}
	bedrockLn, err := bedrock.NewListener(cfg.Bedrock.TunnelListen, bedrockTLS, apiClient, newBedrockIPCaps, logger)
	if err != nil {
		return fmt.Errorf("bind bedrock tunnel listener %q: %w", cfg.Bedrock.TunnelListen, err)
	}

	sigCtx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()

	// Start background eviction for in-memory caches (defense-in-depth
	// against leaked entries — see #1016).
	tokens.StartSweep(sigCtx)
	cache.StartSweep(sigCtx)

	// The reporter and relaysvc get their own context so their shutdown is
	// sequenced after in-flight handle goroutines drain (issue #1051).
	svcCtx, svcStop := context.WithCancel(ctx)
	defer svcStop()

	logger.Info("relay starting", "game_listen", cfg.Game.Listen, "tunnel_listen", cfg.Tunnel.Listen, "bedrock_tunnel_listen", cfg.Bedrock.TunnelListen)

	var wg sync.WaitGroup
	wg.Add(5)
	go func() { defer wg.Done(); svc.Run(svcCtx) }()
	go func() { defer wg.Done(); reporter.Run(svcCtx) }()
	go func() {
		defer wg.Done()
		if err := tunnelLn.Serve(sigCtx); err != nil {
			logger.Error("tunnel listener stopped", "error", err)
			stop()
		}
	}()
	go func() {
		defer wg.Done()
		if err := bedrockLn.Serve(sigCtx); err != nil {
			logger.Error("bedrock tunnel listener stopped", "error", err)
			stop()
		}
	}()
	go func() {
		defer wg.Done()
		if err := gameLn.Serve(sigCtx); err != nil {
			logger.Error("game listener stopped", "error", err)
			stop()
		}

		// The listener is closed; wait for in-flight handle goroutines
		// (including active splices) to finish so their session End events
		// reach the reporter before it shuts down (issue #1051).
		if !gameLn.Drain(drainTimeout) {
			logger.Warn("drain timeout; some sessions may not report End events", "timeout", drainTimeout)
		}

		// All handles drained (or timed out); now stop the reporter so it
		// flushes any remaining End events.
		svcStop()
	}()
	wg.Wait()
	return nil
}

// newLogger builds the structured logger from the log configuration. Secrets are
// masked by logging Config via its slog.LogValuer, never the raw struct.
func newLogger(cfg config.LogConfig) *slog.Logger {
	level := slog.LevelInfo
	_ = level.UnmarshalText([]byte(cfg.Level))

	opts := &slog.HandlerOptions{Level: level}
	var handler slog.Handler
	if cfg.Format == "text" {
		handler = slog.NewTextHandler(os.Stderr, opts)
	} else {
		handler = slog.NewJSONHandler(os.Stderr, opts)
	}
	return slog.New(handler)
}

// dial opens the gRPC client connection to the API. A configured CA file
// verifies the API's TLS; api.tls.insecure=true selects a plaintext dial for
// local/dev with a loud warning. Config validation guarantees exactly one is set
// (mirrors the Worker, RELAY.md Section 13).
func dial(api config.APIConfig, logger *slog.Logger) (*grpc.ClientConn, error) {
	var creds credentials.TransportCredentials
	if api.TLS.CAFile == "" {
		logger.Warn("dialing the API WITHOUT TLS (api.tls.insecure=true); use only for local development")
		creds = insecure.NewCredentials()
	} else {
		caPEM, err := os.ReadFile(api.TLS.CAFile)
		if err != nil {
			return nil, fmt.Errorf("read CA file %q: %w", api.TLS.CAFile, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caPEM) {
			return nil, fmt.Errorf("CA file %q contained no usable certificates", api.TLS.CAFile)
		}
		creds = credentials.NewTLS(&tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS13})
	}

	conn, err := grpc.NewClient(api.GRPCEndpoint, grpc.WithTransportCredentials(creds))
	if err != nil {
		return nil, fmt.Errorf("dial API %q: %w", api.GRPCEndpoint, err)
	}
	return conn, nil
}

// advertisedTunnelCA returns the CA PEM the relay advertises to Workers for
// verifying the tunnel certificate (RELAY.md Section 5), per
// tunnel.tls.advertised_ca_file: unset → the listener cert PEM (self-signed
// default); SystemRootsCA → "" (Workers use system roots); a path → that file.
func advertisedTunnelCA(t config.TunnelTLSConfig) (string, error) {
	switch t.AdvertisedCAFile {
	case "":
		pem, err := os.ReadFile(t.CertFile)
		if err != nil {
			return "", fmt.Errorf("read tunnel cert %q: %w", t.CertFile, err)
		}
		return string(pem), nil
	case config.SystemRootsCA:
		return "", nil
	default:
		pem, err := os.ReadFile(t.AdvertisedCAFile)
		if err != nil {
			return "", fmt.Errorf("read advertised tunnel CA %q: %w", t.AdvertisedCAFile, err)
		}
		return string(pem), nil
	}
}

// buildTunnelTLS loads the tunnel listener's server certificate.
func buildTunnelTLS(t config.TunnelTLSConfig) (*tls.Config, error) {
	cert, err := tls.LoadX509KeyPair(t.CertFile, t.KeyFile)
	if err != nil {
		return nil, fmt.Errorf("load tunnel TLS cert/key: %w", err)
	}
	return &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS13}, nil
}
