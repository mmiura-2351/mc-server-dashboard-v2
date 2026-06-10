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
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/clock"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/config"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/containerdriver"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/controlplane"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/datatransfer"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/hostprocess"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/javaruntime"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/rcon"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
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
	// Reclaim any snapshot-*.tar spool a crash mid-snapshot left in the scratch root
	// (issue #787): nothing else GCs them, and each leaks a world-sized file. Run
	// before the held-server scan, which only walks directories and never sees them.
	datatransfer.SweepSnapshotSpools(cfg.Worker.ScratchDir)
	// Advertise the working sets already on the persistent scratch, each tagged
	// with its generation, so the API skips the destructive hydrate on a same-worker
	// restart only when the held generation is fresh enough (issue #763): a hydrate
	// would unpack the last authoritative snapshot over the live, newer working set
	// and roll the world back, while a stale held set must still hydrate.
	heldServers := instancemanager.ScanHeldServers(cfg.Worker.ScratchDir)
	caps := session.Capabilities{
		WorkerID:      cfg.Worker.ID,
		WorkerVersion: version,
		Drivers:       cfg.Worker.Drivers,
		MaxServers:    cfg.Worker.MaxServers,
		HeldServers:   heldServers,
	}
	manager, err := buildInstanceManager(ctx, cfg, logger)
	if err != nil {
		return err
	}
	manager.WithMetrics(sysClock, time.Duration(cfg.Worker.MetricsIntervalSeconds)*time.Second)
	transferClient, err := buildTransferClient(cfg.API)
	if err != nil {
		return err
	}
	manager.WithTransfer(datatransfer.New(transferClient))
	runner := session.NewRunner(dialer, caps, sysClock, logger, session.WithCommandHandler(manager))

	// Cancel the run context on SIGINT/SIGTERM for a clean stream shutdown.
	sigCtx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()

	logger.Info("starting control-plane session", "endpoint", cfg.API.GRPCEndpoint)
	return runner.Run(sigCtx)
}

// buildInstanceManager wires the advertised execution drivers and the instance
// manager that handles lifecycle/console commands (issue #89). RCON for both
// graceful stop and ServerCommand forwarding is opened from the server's
// working-dir server.properties. A driver is constructed only when
// worker.drivers advertises it; the container driver also sweeps leftover
// containers from a previous run before any server is launched.
func buildInstanceManager(ctx context.Context, cfg config.Config, logger *slog.Logger) (*instancemanager.Manager, error) {
	wc := cfg.Worker
	// The host-process driver always dials RCON on the host loopback (empty host).
	openLoopbackControl := func(ctx context.Context, spec execution.InstanceSpec) (execution.ServerControl, error) {
		return rcon.OpenFromWorkingDir(ctx, spec.WorkingDir, "")
	}

	// containerRconHost resolves the RCON dial host for a server. It is empty
	// (loopback) unless a container driver with a configured network is built, in
	// which case it returns the MC container's name so RCON is reached over the
	// docker network rather than the unreachable host loopback (issue #218).
	containerRconHost := func(string) string { return "" }

	drivers := map[string]execution.ExecutionDriver{}
	for _, name := range wc.Drivers {
		switch name {
		case "host-process":
			drivers[name] = hostprocess.New(
				javaruntime.New(wc.Java.Runtimes),
				hostprocess.RealSpawn,
				openLoopbackControl,
				hostprocess.Options{StopTimeout: 30 * time.Second},
			)
		case "container":
			docker, err := containerdriver.NewEngineClient(cfg.Driver.Container.DockerHost)
			if err != nil {
				return nil, err
			}
			// The container driver dials RCON at the host the driver derives from its
			// topology (loopback when no network, the container name when a network is
			// configured); the graceful-stop control func threads that host through.
			openContainerControl := func(ctx context.Context, spec execution.InstanceSpec, rconHost string) (execution.ServerControl, error) {
				return rcon.OpenFromWorkingDir(ctx, spec.WorkingDir, rconHost)
			}
			cd := containerdriver.New(
				docker,
				containerdriver.NewImageSelector(cfg.Driver.Container.Images),
				openContainerControl,
				containerdriver.Options{WorkerID: wc.ID, StopTimeout: 30 * time.Second, GameBindIP: cfg.Driver.Container.GameBindIP, Network: cfg.Driver.Container.Network},
			)
			containerRconHost = cd.RconHost
			// The sweep force-removes every container labelled for this Worker,
			// including ones still running: a graceful restart while servers are up
			// kills those live servers. That is the deliberate M1 stateless-worker
			// posture (no hydration yet; the API sees the resulting state on
			// reconnect/status).
			if err := cd.Sweep(ctx); err != nil {
				// A failed sweep is logged, not fatal: leftover containers block the
				// affected servers' restart but must not stop the Worker from serving.
				logger.Warn("container orphan sweep failed", "error", err)
			}
			drivers[name] = cd
		}
	}

	// ServerCommand forwarding and the pre-snapshot save-all open RCON by server
	// id; the dial host is resolved from the driver that actually runs that server.
	// Only a container-driven server with a configured network is dialed over the
	// network (its container name); every other server — including a host-process
	// server on a worker that also advertises the container driver — keeps the host
	// loopback, so a mixed-driver worker resolves each server correctly (issue #218).
	openControl := func(ctx context.Context, serverID, driver string) (execution.ServerControl, error) {
		host := resolveRconHost(driver, containerRconHost, serverID)
		return rcon.OpenFromWorkingDir(ctx, filepath.Join(wc.ScratchDir, serverID), host)
	}
	return instancemanager.New(drivers, wc.ScratchDir, openControl).WithLogger(logger), nil
}

// resolveRconHost picks the RCON dial host for a server. It is empty (the host
// loopback) for every server except a container-driven one, which is dialed at
// the host the container driver derives from its topology (its container name
// when a network is configured). containerRconHost is the container driver's
// resolver, or the no-container-driver stub that always returns empty. This is
// the mixed-driver gate: a host-process server on a worker that also advertises
// the container driver keeps the loopback (issue #218).
func resolveRconHost(driver string, containerRconHost func(string) string, serverID string) string {
	if driver == "container" {
		return containerRconHost(serverID)
	}
	return ""
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

// buildTransferClient builds the HTTP client for the data plane, mirroring the
// control channel's TLS posture (CONFIGURATION.md Section 6.1): the same CA
// bundle / mTLS pair verifies the API, and api.tls.insecure=true selects a
// plaintext client for local/dev. The control plane already validated that
// exactly one of CA-file / insecure is set.
func buildTransferClient(api config.APIConfig) (*http.Client, error) {
	transport := &http.Transport{}
	if api.TLS.CAFile != "" {
		tlsCfg, err := buildTLSConfig(api.TLS)
		if err != nil {
			return nil, err
		}
		transport.TLSClientConfig = tlsCfg
	}
	return &http.Client{Transport: transport}, nil
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
