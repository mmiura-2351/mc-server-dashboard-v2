// Command worker is the entry point (the edge / wiring layer) of the Worker
// execution agent. It loads configuration, constructs the gRPC control-plane
// client, and runs the session Runner until SIGINT/SIGTERM triggers a clean
// shutdown (CONTROL_PLANE.md Section 4; CONFIGURATION.md Section 1 keeps config
// reading at the edge).
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/clock"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/config"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/controlplane"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// version is the Worker build string advertised at registration. It is a
// placeholder until release tooling injects a real value.
const version = "0.0.0-dev"

// configPathEnv names the env var pointing at the TOML config file (optional).
const configPathEnv = "MCD_WORKER_CONFIG"

func main() {
	if err := run(context.Background()); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// run wires the Worker and blocks until the session ends. It returns nil on a
// signal-driven clean shutdown and an error on a fatal config/registration
// failure.
func run(ctx context.Context) error {
	cfg, err := config.Load(os.Getenv(configPathEnv), os.Getenv)
	if err != nil {
		return err
	}

	logger := newLogger(cfg.Log)
	logger.Info("worker configuration loaded", "config", cfg)

	conn, err := dial(cfg.API, logger)
	if err != nil {
		return err
	}
	defer func() { _ = conn.Close() }()

	sysClock := clock.System{}
	dialer := controlplane.NewDialer(conn, cfg.API.Credential, sysClock)
	caps := session.Capabilities{
		WorkerID:      cfg.Worker.ID,
		WorkerVersion: version,
		Drivers:       cfg.Worker.Drivers,
		MaxServers:    cfg.Worker.MaxServers,
	}
	runner := session.NewRunner(dialer, caps, sysClock, logger)

	// Cancel the run context on SIGINT/SIGTERM for a clean stream shutdown.
	sigCtx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()

	logger.Info("starting control-plane session", "endpoint", cfg.API.GRPCEndpoint)
	return runner.Run(sigCtx)
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

// dial opens the gRPC client connection to the API control plane. A configured
// CA file verifies the API's TLS (with optional mTLS when a client cert/key pair
// is set); api.tls.insecure=true selects a plaintext dial for local/dev with a
// loud warning. Config validation guarantees exactly one of the two is set
// (CONFIGURATION.md Section 6.1).
func dial(api config.APIConfig, logger *slog.Logger) (*grpc.ClientConn, error) {
	var creds credentials.TransportCredentials
	if api.TLS.CAFile == "" {
		logger.Warn("dialing the API control plane WITHOUT TLS (api.tls.insecure=true); use only for local development")
		creds = insecure.NewCredentials()
	} else {
		tlsCfg, err := buildTLSConfig(api.TLS)
		if err != nil {
			return nil, err
		}
		creds = credentials.NewTLS(tlsCfg)
	}

	conn, err := grpc.NewClient(api.GRPCEndpoint, grpc.WithTransportCredentials(creds))
	if err != nil {
		return nil, fmt.Errorf("dial API %q: %w", api.GRPCEndpoint, err)
	}
	return conn, nil
}

// buildTLSConfig assembles the control-channel TLS config from the CA bundle and
// an optional mTLS client certificate.
func buildTLSConfig(tlsCfg config.TLSConfig) (*tls.Config, error) {
	caPEM, err := os.ReadFile(tlsCfg.CAFile)
	if err != nil {
		return nil, fmt.Errorf("read CA file %q: %w", tlsCfg.CAFile, err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("CA file %q contained no usable certificates", tlsCfg.CAFile)
	}

	out := &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}

	if tlsCfg.ClientCertFile != "" && tlsCfg.ClientKeyFile != "" {
		cert, err := tls.LoadX509KeyPair(tlsCfg.ClientCertFile, tlsCfg.ClientKeyFile)
		if err != nil {
			return nil, fmt.Errorf("load mTLS client cert/key: %w", err)
		}
		out.Certificates = []tls.Certificate{cert}
	}

	return out, nil
}
