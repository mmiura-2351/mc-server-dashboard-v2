// Command relay is the entry point (the edge / wiring layer) of the game
// ingress relay. It loads configuration, dials the API's RelayService, binds
// the public game listener and the TLS tunnel listener, and runs them until
// SIGINT/SIGTERM triggers a clean shutdown (docs/app/RELAY.md Sections 2–7, 12;
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
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/game"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/relaysvc"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/session"
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/tunnel"
)

// tokenTTL is the single-use tunnel token lifetime. The API mints tokens with a
// 10 s TTL (RELAY.md Section 4); the relay's table tracks the same window so a
// late dial-back finds no waiter.
const tokenTTL = 10 * time.Second

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

	// The tunnel CA the relay advertises to Workers (Register → TunnelDial) is the
	// listener's certificate PEM: with self-signed material the cert file is the
	// chain the Worker verifies against (RELAY.md Section 5).
	tunnelCAPEM, err := os.ReadFile(cfg.Tunnel.TLS.CertFile)
	if err != nil {
		return fmt.Errorf("read tunnel cert %q: %w", cfg.Tunnel.TLS.CertFile, err)
	}

	conn, err := dial(cfg.API, logger)
	if err != nil {
		return err
	}
	defer func() { _ = conn.Close() }()

	apiClient := apiclient.New(conn, cfg.API.Credential)
	reporter := session.NewReporter(apiClient, logger, time.Now)
	svc := relaysvc.New(apiClient, reporter, cfg.Tunnel.PublicEndpoint, string(tunnelCAPEM), logger)

	tokens := tunnel.NewTokenTable(tokenTTL, time.Now)
	cache := game.NewStatusCache(time.Duration(cfg.Game.StatusCacheSeconds)*time.Second, time.Now)
	caps := game.NewIPCaps(cfg.Game.MaxConnsPerIP, cfg.Game.JoinsPerIPPerSecond, time.Now)

	tunnelTLS, err := buildTunnelTLS(cfg.Tunnel.TLS)
	if err != nil {
		return err
	}
	tunnelLn, err := tunnel.NewListener(cfg.Tunnel.Listen, tunnelTLS, tokens, logger)
	if err != nil {
		return fmt.Errorf("bind tunnel listener %q: %w", cfg.Tunnel.Listen, err)
	}
	gameLn, err := game.NewListener(cfg.Game.Listen, svc, tokens, cache, caps, reporter, logger)
	if err != nil {
		return fmt.Errorf("bind game listener %q: %w", cfg.Game.Listen, err)
	}

	sigCtx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()

	logger.Info("relay starting", "game_listen", cfg.Game.Listen, "tunnel_listen", cfg.Tunnel.Listen)

	var wg sync.WaitGroup
	wg.Add(4)
	go func() { defer wg.Done(); svc.Run(sigCtx) }()
	go func() { defer wg.Done(); reporter.Run(sigCtx) }()
	go func() {
		defer wg.Done()
		if err := tunnelLn.Serve(sigCtx); err != nil {
			logger.Error("tunnel listener stopped", "error", err)
			stop()
		}
	}()
	go func() {
		defer wg.Done()
		if err := gameLn.Serve(sigCtx); err != nil {
			logger.Error("game listener stopped", "error", err)
			stop()
		}
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
// (mirrors the Worker, RELAY.md Section 12).
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
		creds = credentials.NewTLS(&tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12})
	}

	conn, err := grpc.NewClient(api.GRPCEndpoint, grpc.WithTransportCredentials(creds))
	if err != nil {
		return nil, fmt.Errorf("dial API %q: %w", api.GRPCEndpoint, err)
	}
	return conn, nil
}

// buildTunnelTLS loads the tunnel listener's server certificate.
func buildTunnelTLS(t config.TunnelTLSConfig) (*tls.Config, error) {
	cert, err := tls.LoadX509KeyPair(t.CertFile, t.KeyFile)
	if err != nil {
		return nil, fmt.Errorf("load tunnel TLS cert/key: %w", err)
	}
	return &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12}, nil
}
