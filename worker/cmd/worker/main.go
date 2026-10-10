// Command worker wires configuration, transports, and drivers into the Worker session.
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
	"google.golang.org/grpc/keepalive"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/bedrocktunnel"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/clock"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/config"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/containerdriver"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/controlplane"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/datatransfer"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/hostresources"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/rcon"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/tunnel"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// version is advertised at registration and set with -ldflags "-X main.version=<tag>".
var version = "0.0.0-dev"

const configPathEnv = "MCD_WORKER_CONFIG"

func main() {
	if err := run(context.Background()); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// run blocks until shutdown or a fatal configuration or registration error.
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
	// Reclaim snapshot spools left by a crash; directory scans do not find them.
	datatransfer.SweepSnapshotSpools(cfg.Worker.ScratchDir)
	// Bind tunnel teardown to signal cancellation so shutdown closes live splices.
	sigCtx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()

	// Sweep orphan containers before fsck: scanning a live writer can falsely report corruption.
	manager, quiesced, err := buildInstanceManager(sigCtx, cfg, logger)
	if err != nil {
		return err
	}
	// Join manager-owned goroutines after the session ends; pending telemetry is dropped during shutdown.
	defer manager.Close()
	// Reclaim interrupted sweeps and hydrates only after orphan writers are confirmed stopped.
	// A failed sweep leaves the recovery trees for a later boot.
	if quiesced {
		instancemanager.ReclaimInterruptedDisplacedSweeps(cfg.Worker.ScratchDir)
		instancemanager.ReclaimHydrateLeftovers(cfg.Worker.ScratchDir)
	} else {
		logger.Warn("skipping the boot scratch reclaims: the container orphan sweep did not "+
			"establish that no container is still writing, so a .sweeping-<id>-* or "+
			"a .hydrate-<id>-* tree here may be a running orphan's live world; the next boot "+
			"whose sweep succeeds reclaims them (issues #2799/#3167)",
			"scratch_dir", cfg.Worker.ScratchDir)
	}
	// Scan held generations, checking regions only when orphan writers are stopped.
	// Persist torn verdicts in markers because registration re-reads them through HeldServers.
	heldServers := instancemanager.ScanHeldServers(cfg.Worker.ScratchDir, quiesced, logger)
	// Warn about displaced recovery copies with no held server; these require manual recovery or removal.
	instancemanager.WarnOrphanDisplacedTrees(cfg.Worker.ScratchDir, heldServers, logger)
	cpuCores := hostresources.CPUCores()
	memoryBytes := hostresources.MemoryBytes()
	logger.Info("detected host resources", "cpu_cores", cpuCores, "memory_bytes", memoryBytes)
	caps := session.Capabilities{
		WorkerID:      cfg.Worker.ID,
		WorkerVersion: version,
		Drivers:       cfg.Worker.Drivers,
		MaxServers:    cfg.Worker.MaxServers,
		HeldServers:   heldServers,
		Resources: session.HostResources{
			CPUCores:    cpuCores,
			MemoryBytes: memoryBytes,
		},
	}
	manager.WithMetrics(sysClock, time.Duration(cfg.Worker.MetricsIntervalSeconds)*time.Second)
	transferClient, err := buildTransferClient(cfg.API)
	if err != nil {
		return err
	}
	manager.WithTransfer(datatransfer.New(transferClient).WithLogger(logger))
	runner := session.NewRunner(dialer, caps, sysClock, logger, session.WithCommandHandler(manager))

	logger.Info("starting control-plane session", "endpoint", cfg.API.GRPCEndpoint)
	return runner.Run(sigCtx)
}

// buildInstanceManager sweeps orphan containers and wires the configured drivers.
// Its second result permits destructive boot cleanup and fsck only when the sweep succeeded.
func buildInstanceManager(ctx context.Context, cfg config.Config, logger *slog.Logger) (*instancemanager.Manager, bool, error) {
	wc := cfg.Worker
	quiesced := true

	// Use container DNS for RCON on a configured network; otherwise use host loopback.
	containerRconHost := func(string) string { return "" }
	// Resolve game traffic over the same container network as RCON.
	containerGameHost := func(string) string { return "" }

	drivers := map[string]execution.ExecutionDriver{}
	for _, name := range wc.Drivers {
		switch name {
		case "container":
			docker, err := containerdriver.NewEngineClient(cfg.Driver.Container.DockerHost)
			if err != nil {
				return nil, false, err
			}
			openContainerControl := func(ctx context.Context, spec execution.InstanceSpec, rconHost string) (execution.ServerControl, error) {
				return rcon.OpenFromWorkingDir(ctx, spec.WorkingDir, rconHost, spec.MinecraftVersion)
			}
			cd := containerdriver.New(
				docker,
				containerdriver.NewImageSelector(cfg.Driver.Container.Images),
				openContainerControl,
				containerdriver.Options{WorkerID: wc.ID, StopTimeout: 30 * time.Second, GameBindIP: cfg.Driver.Container.GameBindIP, Network: cfg.Driver.Container.Network, ScratchDir: wc.ScratchDir, RunAsUID: cfg.Driver.Container.User.UID, RunAsGID: cfg.Driver.Container.User.GID, Logger: logger},
			)
			containerRconHost = cd.RconHost
			containerGameHost = cd.GameHost
			// Force-remove this Worker's leftover containers; startup does not adopt running instances.
			if err := cd.Sweep(ctx); err != nil {
				// A failed sweep leaves orphan writers possible; keep serving, but skip destructive boot cleanup and fsck.
				logger.Warn("container orphan sweep failed", "error", err)
				quiesced = false
			}
			drivers[name] = cd
		}
	}

	// Resolve RCON from the server's driver, including commands and pre-snapshot saves.
	openControl := func(ctx context.Context, serverID, driver, mcVersion string) (execution.ServerControl, error) {
		host := resolveRconHost(driver, containerRconHost, serverID)
		return rcon.OpenFromWorkingDir(ctx, filepath.Join(wc.ScratchDir, serverID), host, mcVersion)
	}
	// Signal cancellation tears down live TCP splices; container DNS avoids dialing the Worker's loopback.
	tunnelDialer := tunnel.New(ctx, cfg.Driver.Container.GameBindIP, containerGameHost, logger)
	// Share the shutdown context and container address resolver with the TCP tunnel dialer.
	bedrockTunnel := bedrocktunnel.New(ctx, cfg.Driver.Container.GameBindIP, containerGameHost, logger)
	return instancemanager.New(drivers, wc.ScratchDir, openControl).
		WithLogger(logger).
		WithWorkerID(wc.ID).
		WithTunnelDialer(tunnelDialerAdapter{tunnelDialer}).
		WithBedrockTunneler(bedrockTunnelerAdapter{bedrockTunnel}), quiesced, nil
}

// tunnelDialerAdapter translates specs at the wiring edge to keep the layers independent.
type tunnelDialerAdapter struct{ d *tunnel.Dialer }

func (a tunnelDialerAdapter) Dial(ctx context.Context, spec instancemanager.TunnelSpec) error {
	return a.d.Dial(ctx, tunnel.Spec{
		ServerID:   spec.ServerID,
		WorkingDir: spec.WorkingDir,
		Endpoint:   spec.Endpoint,
		Token:      spec.Token,
		CAPEM:      spec.CAPEM,
	})
}

// bedrockTunnelerAdapter translates specs at the wiring edge to keep the layers independent.
type bedrockTunnelerAdapter struct{ m *bedrocktunnel.Manager }

func (a bedrockTunnelerAdapter) Open(spec instancemanager.BedrockTunnelSpec) error {
	return a.m.Open(bedrocktunnel.Spec{
		ServerID:      spec.ServerID,
		RelayEndpoint: spec.RelayEndpoint,
		BedrockPort:   spec.BedrockPort,
		Token:         spec.Token,
		CAPEM:         spec.CAPEM,
	})
}

func (a bedrockTunnelerAdapter) Close(serverID string) { a.m.Close(serverID) }

// resolveRconHost uses container DNS only for a container driver with a configured network.
func resolveRconHost(driver string, containerRconHost func(string) string, serverID string) string {
	if driver == "container" {
		return containerRconHost(serverID)
	}
	return ""
}

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

// controlPlaneKeepalive detects silent path loss and triggers reconnect within about 30 seconds.
// The API must permit this ping cadence and pings without active streams.
var controlPlaneKeepalive = keepalive.ClientParameters{
	Time:                20 * time.Second,
	Timeout:             10 * time.Second,
	PermitWithoutStream: true,
}

// dial uses the configured CA and optional mTLS pair, or explicitly configured plaintext.
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

	conn, err := grpc.NewClient(api.GRPCEndpoint,
		grpc.WithTransportCredentials(creds),
		grpc.WithKeepaliveParams(controlPlaneKeepalive),
	)
	if err != nil {
		return nil, fmt.Errorf("dial API %q: %w", api.GRPCEndpoint, err)
	}
	return conn, nil
}

// buildTransferClient uses the same CA, mTLS pair, and plaintext policy as the control plane.
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

func buildTLSConfig(tlsCfg config.TLSConfig) (*tls.Config, error) {
	caPEM, err := os.ReadFile(tlsCfg.CAFile)
	if err != nil {
		return nil, fmt.Errorf("read CA file %q: %w", tlsCfg.CAFile, err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("CA file %q contained no usable certificates", tlsCfg.CAFile)
	}

	out := &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS13}

	if tlsCfg.ClientCertFile != "" && tlsCfg.ClientKeyFile != "" {
		cert, err := tls.LoadX509KeyPair(tlsCfg.ClientCertFile, tlsCfg.ClientKeyFile)
		if err != nil {
			return nil, fmt.Errorf("load mTLS client cert/key: %w", err)
		}
		out.Certificates = []tls.Certificate{cert}
	}

	return out, nil
}
