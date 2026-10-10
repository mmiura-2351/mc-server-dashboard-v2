// Package containerdriver runs Minecraft in Docker through an injectable Engine API.
// It enforces hard memory limits and relative CPU shares; zero values leave the configured defaults.
package containerdriver

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// defaultGamePort is the Minecraft server port when server.properties does not
// override server-port; defaultRCONPort mirrors the RCON adapter's default.
const (
	defaultGamePort = "25565"
	defaultRCONPort = "25575"
)

// defaultStopTimeout bounds the `docker stop` SIGTERM grace period before the
// daemon escalates to SIGKILL.
const defaultStopTimeout = 30 * time.Second

// defaultFlushTimeout covers RCON and settling before escalation starts; it is additive to stopDeadline.
const defaultFlushTimeout = 90 * time.Second

// Bound sweep calls against an unresponsive daemon; stop gets its full grace plus this margin.
const defaultSweepCallMargin = 10 * time.Second

// Fall back to running after this bound if the startup-complete log marker never appears.
const defaultReadinessTimeout = 5 * time.Minute

// defaultConflictPollInterval and defaultConflictDeadline bound the wait-for-name-free loop createContainer runs
// on a create name conflict: it polls every interval until the deadline for the deterministic name to free as
// the async exit-watcher finishes the previous container's teardown.
const (
	defaultConflictPollInterval = 250 * time.Millisecond
	defaultConflictDeadline     = 10 * time.Second
)

// Re-inspect after Wait transport errors; unreachable does not prove exit.
// Bound probes and preserve the last observed state when the daemon remains unavailable.
var (
	waitTransportProbeInterval = 250 * time.Millisecond
	waitTransportProbeDeadline = 30 * time.Second
)

// maxInstallRetries is the number of additional attempts after the first Forge
// install failure (3 total attempts). installRetryBackoff is the delay before
// each retry; a var so tests can shrink it.
const maxInstallRetries = 2

var installRetryBackoff = []time.Duration{5 * time.Second, 15 * time.Second}

// Give lazy image pulls their own bounded budget; downloads may outlast the create request.
var defaultImagePullTimeout = 10 * time.Minute

// defaultGameBindIP is the host interface the game port is published on when
// Options.GameBindIP is unset: loopback, preserving the historical behavior.
// rconBindIP is fixed: RCON is a control channel and must not be exposed.
const (
	defaultGameBindIP = "127.0.0.1"
	rconBindIP        = "127.0.0.1"
)

// controlFunc opens RCON through loopback or container DNS; failure permits docker-stop fallback.
type controlFunc func(ctx context.Context, spec execution.InstanceSpec, rconHost string) (execution.ServerControl, error)

// Options tunes the driver.
type Options struct {
	// WorkerID labels every container so a startup sweep can find and remove this
	// Worker's orphaned containers (crash-orphan recovery).
	WorkerID string
	// StopTimeout bounds the `docker stop` grace period. Zero uses
	// defaultStopTimeout.
	StopTimeout time.Duration
	// FlushTimeout bounds the pre-stop flush the caller supplies to Stop. Zero uses
	// defaultFlushTimeout; tests set a short value to keep the suite fast.
	FlushTimeout time.Duration
	// GameBindIP is the host interface the game port is published on. Empty uses
	// defaultGameBindIP (loopback), preserving the historical behavior.
	GameBindIP string
	// Network enables container DNS and direct RCON, replacing loopback host publication.
	Network string
	// ConflictPollInterval and ConflictDeadline tune the wait-for-name-free loop createContainer runs on a create
	// name conflict. Zero uses the production defaults; tests set short values to keep the suite fast.
	ConflictPollInterval time.Duration
	ConflictDeadline     time.Duration
	// ReadinessTimeout bounds how long the driver holds StateStarting waiting for the server's startup-complete log
	// marker before falling back to running. Zero uses defaultReadinessTimeout.
	ReadinessTimeout time.Duration
	// SweepCallMargin is the slack added to each startup-Sweep daemon call's deadline so a wedged daemon cannot
	// hang worker startup. Zero uses defaultSweepCallMargin; tests set a short value to keep the suite fast.
	SweepCallMargin time.Duration
	// ScratchDir enables best-effort save-on for running orphans before startup sweep stops them.
	ScratchDir string
	// RunAsUID/RunAsGID select non-root container ownership; zero uses the Worker IDs or the root-Worker defaults.
	RunAsUID int
	RunAsGID int
	// Logger records the lazy base-image pull (image name, duration) at INFO. Nil uses a discard logger.
	Logger *slog.Logger
}

// Driver is the container ExecutionDriver.
type Driver struct {
	docker      dockerAPI
	images      *ImageSelector
	openControl controlFunc
	workerID    string
	stopTimeout time.Duration
	// flushTimeout bounds the pre-stop flush.
	flushTimeout time.Duration
	gameBindIP   string
	network      string
	scratchDir   string
	// runAsUID and runAsGID are the uid:gid server containers run as, and chownAt the call that hands a working-set
	// entry to them (chownAtNoFollow; a test seam, since only root can give a file away).
	runAsUID int
	runAsGID int
	chownAt  func(dirFd int, name string, uid, gid int) error
	// conflictPoll and conflictDeadline bound the wait-for-name-free loop.
	conflictPoll     time.Duration
	conflictDeadline time.Duration
	// readinessTimeout bounds the hold-on-starting wait.
	readinessTimeout time.Duration
	// sweepCallMargin bounds each startup-Sweep daemon call.
	sweepCallMargin time.Duration
	// imagePullTimeout bounds a lazy base-image pull.
	imagePullTimeout time.Duration
	// logger records the lazy base-image pull at INFO.
	logger *slog.Logger
}

// New builds a container Driver. docker is the Engine seam; images resolves a
// base image from the Minecraft version; openControl opens RCON for graceful
// stop.
func New(docker dockerAPI, images *ImageSelector, openControl controlFunc, opts Options) *Driver {
	timeout := opts.StopTimeout
	if timeout <= 0 {
		timeout = defaultStopTimeout
	}
	flushTimeout := opts.FlushTimeout
	if flushTimeout <= 0 {
		flushTimeout = defaultFlushTimeout
	}
	gameBindIP := opts.GameBindIP
	if gameBindIP == "" {
		gameBindIP = defaultGameBindIP
	}
	conflictPoll := opts.ConflictPollInterval
	if conflictPoll <= 0 {
		conflictPoll = defaultConflictPollInterval
	}
	conflictDeadline := opts.ConflictDeadline
	if conflictDeadline <= 0 {
		conflictDeadline = defaultConflictDeadline
	}
	readinessTimeout := opts.ReadinessTimeout
	if readinessTimeout <= 0 {
		readinessTimeout = defaultReadinessTimeout
	}
	sweepCallMargin := opts.SweepCallMargin
	if sweepCallMargin <= 0 {
		sweepCallMargin = defaultSweepCallMargin
	}
	logger := opts.Logger
	if logger == nil {
		logger = slog.New(slog.DiscardHandler)
	}
	runAsUID, runAsGID := resolveRunAs(opts.RunAsUID, opts.RunAsGID, os.Getuid(), os.Getgid())
	return &Driver{
		docker:           docker,
		images:           images,
		openControl:      openControl,
		workerID:         opts.WorkerID,
		stopTimeout:      timeout,
		flushTimeout:     flushTimeout,
		gameBindIP:       gameBindIP,
		network:          opts.Network,
		scratchDir:       opts.ScratchDir,
		runAsUID:         runAsUID,
		runAsGID:         runAsGID,
		chownAt:          chownAtNoFollow,
		conflictPoll:     conflictPoll,
		conflictDeadline: conflictDeadline,
		readinessTimeout: readinessTimeout,
		sweepCallMargin:  sweepCallMargin,
		imagePullTimeout: defaultImagePullTimeout,
		logger:           logger,
	}
}

// RconHost returns container DNS on a configured network, otherwise empty for loopback fallback.
func (d *Driver) RconHost(serverID string) string {
	return d.networkHost(serverID)
}

// GameHost returns container DNS on a configured network; Worker loopback cannot reach host-published game
// ports.
func (d *Driver) GameHost(serverID string) string {
	return d.networkHost(serverID)
}

// networkHost is the single decision RconHost and GameHost share: empty with no network (caller dials the host
// loopback), the container name over the user-defined network otherwise, so the RCON and tunnel dial hosts can
// never drift.
func (d *Driver) networkHost(serverID string) string {
	if d.network == "" {
		return ""
	}
	return containerName(serverID)
}

// Start launches directly or supervises Forge installation before launching the same instance.
// Success means a container started, not that installation or Minecraft startup has completed.
func (d *Driver) Start(ctx context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	image, err := d.images.Select(spec.MinecraftVersion)
	if err != nil {
		return nil, fmt.Errorf("containerdriver: select image: %w", err)
	}

	plan, err := execution.BuildLaunchPlan(spec, spec.WorkingDir, containerPathResolver(spec.WorkingDir))
	if err != nil {
		return nil, fmt.Errorf("containerdriver: plan launch: %w", err)
	}

	inst := &instance{
		spec:             spec,
		docker:           d.docker,
		image:            image,
		network:          d.network,
		gameBindIP:       d.gameBindIP,
		labels:           d.labels(spec.ServerID, spec.MinecraftVersion),
		createFn:         d.createServerContainer,
		openControl:      d.openControl,
		rconHost:         d.RconHost(spec.ServerID),
		stopTimeout:      d.stopTimeout,
		flushTimeout:     d.flushTimeout,
		readinessTimeout: d.readinessTimeout,
		logger:           d.logger,
		events:           make(chan execution.StatusEvent, 8),
		exited:           make(chan struct{}),
		state:            execution.StateStarting,
		logPump:          execution.NewLogPump(spec.ServerID, logBufferLines),
	}
	inst.emit(execution.StateStarting, "")

	if plan.NeedsInstall {
		// Remove stale Forge install artifacts (args files, legacy jars) so the re-install starts from a clean slate
		// and never hits ambiguous-match errors. A cleanup failure is logged but does not block the install.
		if cleanErr := execution.CleanForgeInstallArtifacts(spec.WorkingDir); cleanErr != nil {
			d.logger.Warn("failed to clean stale Forge artifacts before install",
				"server_id", spec.ServerID, "err", cleanErr)
		}
		id, err := d.runInstallContainer(ctx, spec, image, plan)
		if err != nil {
			return nil, fmt.Errorf("containerdriver: start install container: %w", classifyStartError(err))
		}
		inst.setContainerID(id)
		go inst.superviseInstall(id)
		return inst, nil
	}

	id, err := d.launchContainer(ctx, spec, image, plan.LaunchArgs)
	if err != nil {
		return nil, fmt.Errorf("containerdriver: start container: %w", classifyStartError(err))
	}
	inst.beginLaunch(id)
	return inst, nil
}

// containerPathResolver maps working-set-relative paths onto in-container paths
// under /data and checks existence against the host working dir (the bind
// source), for execution.BuildLaunchPlan.
func containerPathResolver(workingDir string) execution.PathResolver {
	return execution.PathResolver{
		Resolve: func(rel string) string { return containerWorkDir + "/" + rel },
		Exists: func(rel string) bool {
			_, err := os.Stat(filepath.Join(workingDir, filepath.FromSlash(rel)))
			return err == nil
		},
	}
}

// launchContainer creates and starts the server launch container, returning its id. It publishes the game (and,
// off-network, RCON) ports and runs the launch argv in exec form. It heals the deterministic-name conflict via
// createContainer (the wait-for-name-free loop).
func (d *Driver) launchContainer(ctx context.Context, spec execution.InstanceSpec, image string, launchArgs []string) (string, error) {
	gamePort, rconPort, err := ports(spec.WorkingDir)
	if err != nil {
		return "", err
	}
	// The game port binds to the configured host interface (driver.container.
	// game_bind_ip) so players can reach the server.
	portMappings := []PortMapping{
		{ContainerPort: gamePort, HostIP: d.gameBindIP, HostPort: gamePort},
	}
	// Keep RCON inside the Docker network when configured, otherwise publish only on host loopback.
	if d.network == "" {
		portMappings = append(portMappings,
			PortMapping{ContainerPort: rconPort, HostIP: rconBindIP, HostPort: rconPort})
	}
	create := CreateSpec{
		Name:             containerName(spec.ServerID),
		Image:            image,
		Cmd:              containerCmd(launchArgs),
		WorkingDir:       containerWorkDir,
		Binds:            []string{spec.WorkingDir + ":" + containerWorkDir},
		Ports:            portMappings,
		Network:          d.network,
		Labels:           d.labels(spec.ServerID, spec.MinecraftVersion),
		MemoryLimitBytes: memoryLimitBytes(spec.MemoryLimitMB),
		CPUShares:        cpuShares(spec.CPUMillis),
	}

	id, err := d.createServerContainer(ctx, spec, create)
	if err != nil {
		return "", err
	}
	if err := d.docker.Start(ctx, id); err != nil {
		d.removeCreated(ctx, id)
		return "", err
	}
	return id, nil
}

// Bound detached failed-start cleanup so an unresponsive daemon cannot stall it indefinitely.
const startCleanupRemoveTimeout = 10 * time.Second

// Remove failed-start containers on a detached, bounded context; the cancelled request cannot perform its own
// cleanup.
func (d *Driver) removeCreated(ctx context.Context, id string) {
	removeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), startCleanupRemoveTimeout)
	defer cancel()
	_ = d.docker.Remove(removeCtx, id)
}

// runInstallContainer uses a distinct name and no published ports for supervised Forge installation.
// It currently uses the default bridge rather than the configured server network.
func (d *Driver) runInstallContainer(ctx context.Context, spec execution.InstanceSpec, image string, plan execution.LaunchPlan) (string, error) {
	create := CreateSpec{
		Name:             installContainerName(spec.ServerID),
		Image:            image,
		Cmd:              containerCmd(plan.InstallArgs),
		WorkingDir:       containerWorkDir,
		Binds:            []string{spec.WorkingDir + ":" + containerWorkDir},
		Labels:           d.labels(spec.ServerID, spec.MinecraftVersion),
		MemoryLimitBytes: memoryLimitBytes(spec.MemoryLimitMB),
		CPUShares:        cpuShares(spec.CPUMillis),
	}
	// Keep conflict recovery for stale install names, even though install and launch names differ.
	id, err := d.createServerContainer(ctx, spec, create)
	if err != nil {
		return "", err
	}
	if err := d.docker.Start(ctx, id); err != nil {
		// Best-effort cleanup, detached from the command context.
		d.removeCreated(ctx, id)
		return "", err
	}
	return id, nil
}

// Root Workers default to a fixed non-root uid:gid distinct from the API and relay user.
// Server-controlled working sets must not share ownership with authoritative storage.
const (
	DefaultRunAsUID = 25565
	DefaultRunAsGID = 25565
)

// resolveRunAs settles the uid:gid the driver runs containers as. No container
// it creates runs as root, whatever its caller left unset: an unset user (uid 0)
// is the Worker's own (ownUID:ownGID), and the fixed unprivileged default when
// the Worker itself is root.
func resolveRunAs(uid, gid, ownUID, ownGID int) (int, int) {
	if uid != 0 {
		return uid, gid
	}
	if ownUID != 0 {
		return ownUID, ownGID
	}
	return DefaultRunAsUID, DefaultRunAsGID
}

// droppedCapabilities are the capabilities removed from every container the driver creates. NET_RAW is what
// opens raw and packet sockets, the primitive behind ARP spoofing and sniffing on the shared servers bridge; a
// Minecraft server needs neither.
var droppedCapabilities = []string{"NET_RAW"}

// Hand ownership to the non-root user before every install or launch create and drop NET_RAW.
// Hydrate and Worker log writes can introduce new Worker-owned files between creates.
func (d *Driver) createServerContainer(ctx context.Context, spec execution.InstanceSpec, create CreateSpec) (string, error) {
	begin := time.Now()
	if err := d.awaitQuiescent(ctx, spec.ServerID); err != nil {
		return "", fmt.Errorf("containerdriver: not handing over the working set: %w", err)
	}
	walk := time.Now()
	stats, err := d.handOverWorkingSet(ctx, spec.WorkingDir)
	if err != nil {
		return "", fmt.Errorf("containerdriver: hand working set to uid:gid %d:%d: %w", d.runAsUID, d.runAsGID, err)
	}
	d.logger.Info("working set handed to the run-as user",
		"server_id", spec.ServerID, "container", create.Name,
		"entries", stats.entries, "changed", stats.changed,
		"quiescence_wait", walk.Sub(begin), "duration", time.Since(walk))
	create.User = fmt.Sprintf("%d:%d", d.runAsUID, d.runAsGID)
	create.CapDrop = droppedCapabilities
	return d.createContainer(ctx, create)
}

// Daemon error classes require substring matching because the Engine API returns free-text errors.
// Unknown wording stays unclassified; raw diagnostics remain in Worker logs.
func classifyStartError(err error) error {
	if err == nil {
		return nil
	}
	if strings.Contains(err.Error(), "port is already allocated") {
		return fmt.Errorf("%w: %v", execution.ErrPortConflict, err)
	}
	if isImageMissing(err) {
		return fmt.Errorf("%w: %v", execution.ErrImageMissing, err)
	}
	return err
}

// Use the same free-text image-miss classifier for lazy pulls and sanitized start errors.
func isImageMissing(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "No such image") || strings.Contains(msg, "pull access denied")
}

// On image miss, pull once and retry create; keep ErrImageMissing classification if the pull fails.
func (d *Driver) createPullingOnMiss(ctx context.Context, create CreateSpec) (string, error) {
	id, err := d.docker.Create(ctx, create)
	if !isImageMissing(err) {
		return id, err
	}
	if pullErr := d.pullImage(ctx, create.Image); pullErr != nil {
		return "", fmt.Errorf("%w (pull failed: %v)", err, pullErr)
	}
	return d.docker.Create(ctx, create)
}

// Pull on a detached, bounded context so a slow first download can populate the cache after the start request
// expires.
func (d *Driver) pullImage(ctx context.Context, image string) error {
	pullCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), d.imagePullTimeout)
	defer cancel()
	d.logger.Info("pulling base image", "image", image)
	start := time.Now()
	if err := d.docker.ImagePull(pullCtx, image); err != nil {
		return err
	}
	d.logger.Info("pulled base image", "image", image, "duration", time.Since(start))
	return nil
}

// Poll name conflicts until stale owned containers are removed; never remove a running or foreign container.
// Transient inspect/removal errors retry within the bound; expiry returns the original conflict with
// diagnostics.
func (d *Driver) createContainer(ctx context.Context, create CreateSpec) (string, error) {
	id, err := d.createPullingOnMiss(ctx, create)
	if !errors.Is(err, errNameConflict) {
		return id, err
	}
	conflict := err

	timer := time.NewTimer(d.conflictDeadline)
	defer timer.Stop()

	lastReason := "name still in use"
	for {
		info, inspectErr := d.docker.Inspect(ctx, create.Name)
		switch {
		case errors.Is(inspectErr, errNotFound):
			// The name is free; retry the create. This calls Create directly, not
			// createPullingOnMiss: reaching this loop at all means the first create
			// got past image resolution to the name-conflict check, so the base image
			// is already on the host and a pull-on-miss wrapper would never fire.
			id, createErr := d.docker.Create(ctx, create)
			if createErr == nil {
				return id, nil
			}
			if !errors.Is(createErr, errNameConflict) {
				return "", createErr
			}
			lastReason = "create still conflicts after the name freed"
		case inspectErr != nil:
			lastReason = fmt.Sprintf("inspect failed: %v", inspectErr)
		case info.Labels[labelWorkerID] != d.workerID:
			return "", fmt.Errorf("declined conflict resolution: conflicting container not owned by this worker: %w", conflict)
		case info.Running:
			return "", fmt.Errorf("declined conflict resolution: conflicting container is running: %w", conflict)
		default:
			if removeErr := d.docker.Remove(ctx, info.ID); removeErr != nil && !errors.Is(removeErr, errRemovalInProgress) {
				lastReason = fmt.Sprintf("remove failed: %v", removeErr)
			}
		}

		// Wait one poll interval for the name to free, honoring the deadline and
		// ctx cancellation.
		poll := time.NewTimer(d.conflictPoll)
		select {
		case <-ctx.Done():
			poll.Stop()
			return "", fmt.Errorf("conflict resolution cancelled (%s): %w", lastReason, ctx.Err())
		case <-timer.C:
			poll.Stop()
			return "", fmt.Errorf("conflict resolution timed out (%s): %w", lastReason, conflict)
		case <-poll.C:
		}
	}
}

// logBufferLines bounds the per-instance captured-log buffer; matches the now-removed host-process driver's
// posture (drop-oldest + dropped-count marker).
const logBufferLines = 256

// containerStateRunning is the Engine's container-state string for a running container (the State field
// /containers/json reports). The sweep gracefully stops a running orphan before removing it.
const containerStateRunning = "running"

// Sweep stops running containers owned by this Worker before removing them, even if stop fails.
// Bound each daemon call and return joined errors; the caller decides whether startup continues.
func (d *Driver) Sweep(ctx context.Context) error {
	listCtx, cancel := context.WithTimeout(ctx, d.sweepCallMargin)
	containers, err := d.docker.List(listCtx, labelWorkerID, d.workerID)
	cancel()
	if err != nil {
		return fmt.Errorf("containerdriver: list orphans: %w", err)
	}
	var errs []error
	for _, c := range containers {
		if c.State == containerStateRunning {
			// Best-effort save-on repairs interrupted snapshot brackets before graceful orphan stop.
			d.sweepSaveOn(ctx, c.Name, c.Labels[labelMCVersion])

			// Daemon-internal stop escalation is not directly observable here; unlike instance.Stop, this path has no
			// explicit kill event.
			stopCtx, cancel := context.WithTimeout(ctx, d.stopTimeout+d.sweepCallMargin)
			err := d.docker.Stop(stopCtx, c.ID, d.stopTimeout)
			cancel()
			if err != nil {
				errs = append(errs, fmt.Errorf("stop %s (%s): %w", c.Name, c.ID, err))
			}
		}
		removeCtx, cancel := context.WithTimeout(ctx, d.sweepCallMargin)
		err := d.docker.Remove(removeCtx, c.ID)
		cancel()
		if err != nil {
			errs = append(errs, fmt.Errorf("remove %s (%s): %w", c.Name, c.ID, err))
		}
	}
	return errors.Join(errs...)
}

// Best-effort save-on before stopping an orphan repairs a bracket interrupted by Worker failure.
// Use the container's Minecraft-version label for password decoding; skip install containers.
func (d *Driver) sweepSaveOn(ctx context.Context, containerName, mcVersion string) {
	if d.openControl == nil || d.scratchDir == "" {
		return
	}
	serverID := d.serverIDFromName(containerName)
	if serverID == "" {
		return
	}
	// Skip install containers — they are not MC servers.
	if strings.HasSuffix(serverID, "-install") {
		return
	}
	rconHost := d.networkHost(serverID)
	spec := execution.InstanceSpec{
		ServerID:         serverID,
		WorkingDir:       filepath.Join(d.scratchDir, serverID),
		MinecraftVersion: mcVersion,
	}
	saveOnCtx, cancel := context.WithTimeout(ctx, d.sweepCallMargin)
	defer cancel()
	ctrl, err := d.openControl(saveOnCtx, spec, rconHost)
	if err != nil {
		d.logger.Warn("sweep save-on: open rcon failed; stopping without restore",
			"server_id", serverID, "error", err)
		return
	}
	defer func() { _ = ctrl.Close() }()
	if _, err := ctrl.Execute(saveOnCtx, "save-on"); err != nil {
		d.logger.Warn("sweep save-on: save-on failed; stopping without restore",
			"server_id", serverID, "error", err)
	}
}

// serverIDFromName extracts the server ID from a Docker container name. Docker
// List returns names prefixed with "/" (e.g. "/mcsd-abc123"). Returns empty when
// the name does not match the expected prefix.
func (d *Driver) serverIDFromName(name string) string {
	// Strip the leading "/" Docker prefixes.
	name = strings.TrimPrefix(name, "/")
	if !strings.HasPrefix(name, containerNamePrefix) {
		return ""
	}
	return strings.TrimPrefix(name, containerNamePrefix)
}

// labels are attached to every container: a worker-id label scopes the orphan sweep, a server-id label
// identifies the server, and an mc-version label records the Minecraft version, which the sweep needs after a
// crash took the StartServer command that carried it.
func (d *Driver) labels(serverID, mcVersion string) map[string]string {
	return map[string]string{
		labelWorkerID:  d.workerID,
		labelServerID:  serverID,
		labelMCVersion: mcVersion,
	}
}

// containerWorkDir is the in-container path the working dir is bind-mounted to
// and the server's working directory.
const containerWorkDir = "/data"

// Set user.home to /tmp because numeric users may lack passwd entries and older JVMs otherwise use "?".
// This keeps temporary library caches out of the snapshotted working set.
func containerCmd(args []string) []string {
	return append([]string{"java", "-Duser.home=" + containerHomeDir}, args...)
}

// containerHomeDir is the user.home every server JVM is launched with.
const containerHomeDir = "/tmp"

// memoryLimitBytes converts the per-server memory ceiling from mebibytes (the InstanceSpec unit) to bytes for
// the Docker host-config Memory field. A zero ceiling stays zero, no constraint.
func memoryLimitBytes(limitMB uint32) int64 {
	return int64(limitMB) * 1024 * 1024
}

// cpuShares maps millicores to relative weight at 1024 shares per core; zero uses gameServerCPUShares.
// No hard CPU quota is imposed.
func cpuShares(cpuMillis uint32) int64 {
	if cpuMillis == 0 {
		return gameServerCPUShares
	}
	return (int64(cpuMillis)*1024 + 500) / 1000
}

// instance is one running container. Across a Forge install+launch it owns two containers in succession (the
// install container, then the launch container); containerID is the current one, guarded by mu.
type instance struct {
	spec        execution.InstanceSpec
	docker      dockerAPI
	containerID string
	// image/network/gameBindIP/labels/createFn carry what superviseInstall needs to create the launch container
	// after the install container exits.
	image       string
	network     string
	gameBindIP  string
	labels      map[string]string
	createFn    func(ctx context.Context, spec execution.InstanceSpec, create CreateSpec) (string, error)
	openControl controlFunc
	// rconHost is the host the graceful-stop RCON connection dials: empty for the
	// host loopback, the container name when a user-defined network is configured.
	rconHost    string
	stopTimeout time.Duration
	// flushTimeout bounds the pre-stop flush preFallback.
	flushTimeout time.Duration
	// readinessTimeout bounds the hold-on-starting wait before falling back to running.
	readinessTimeout time.Duration
	// logger records the graceful-stop -> kill escalation at WARN, so a stop that timed out (leaving the world's
	// regions unpadded for the stop-leg snapshot) is diagnosable. Never nil: Start copies the Driver's logger,
	// which defaults to a discard handler.
	logger *slog.Logger

	events chan execution.StatusEvent
	// exited is closed by supervise once the container has reached a terminal
	// state; waitExit selects on it.
	exited chan struct{}

	// logPump captures the demuxed container log stream; logWG tracks the capture
	// goroutine and logCancel ends its follow on container exit.
	logPump   *execution.LogPump
	logWG     sync.WaitGroup
	logCancel context.CancelFunc

	// Test hook after launch creation and before the stop-check/start critical section; nil in production.
	beforeLaunch func()

	// beforeRetryStart is a test-only hook fired inside superviseInstall after the retry install container is
	// created but before the latch-check-and-start critical section, so a test can drive a Stop into the exact
	// retry-setup window. Nil in production.
	beforeRetryStart func()

	// Test hook under i.mu between running-state assignment and event publication; nil in production.
	beforeReadyPublish func()

	// beforeSurvivedReset is a test-only hook fired inside Stop after the post-kill confirm wait times out but
	// before re-acquiring the lock to reset the latch, so a test can drive the container exit (and supervise) into
	// the exact window the survived-kill restore must not stomp. Nil in production.
	beforeSurvivedReset func()

	// Use a separate metrics lock so slow RCON sampling cannot block status or stop.
	// Console and graceful-stop connections are independent.
	metricsMu sync.Mutex
	// Serialize metrics RCON access; discard a poisoned connection so the next sample redials.
	metricsControl execution.ServerControl
	// metricsClosed latches once the instance terminates and closeMetricsControl has
	// run, so a late in-flight sample does not redial a connection nobody would
	// close.
	metricsClosed bool

	mu       sync.Mutex
	state    execution.ServerState
	stopping bool
	// stopRequested survives failed-stop latch resets so a later exit is still classified as requested.
	stopRequested bool
	// exitObserved is set by supervise under the lock the moment it observes the container exit, before recording
	// the terminal state. The survived-kill restore checks it under the same lock and skips the reset when set, so
	// it cannot stomp a terminal state supervise reached during the post-kill wait window.
	exitObserved bool
	closed       bool
	// Latch the first terminal event so racing readiness and stopping events cannot report a dead container as
	// live.
	terminalLatched bool
}

// setContainerID records the instance's current container under the lock (the
// install container during the install phase, the launch container after).
func (i *instance) setContainerID(id string) {
	i.mu.Lock()
	i.containerID = id
	i.mu.Unlock()
}

// currentContainerID returns the instance's current container id under the lock.
func (i *instance) currentContainerID() string {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.containerID
}

// beginLaunch wires the launch container's log capture, marks the instance running, and starts the exit
// supervisor. It is the shared tail of a direct launch and a post-install launch, so a Forge install+launch
// reaches running through the same path as a plain start.
func (i *instance) beginLaunch(id string) {
	i.setContainerID(id)
	i.beginLaunchTail(id)
}

// Begin log capture and exit supervision only after launch publication under the stop latch lock.
func (i *instance) beginLaunchTail(id string) {
	// Follow the container's multiplexed log stream into the per-instance pump.
	// The follow is bound to logCtx so supervise can end it on container exit;
	// supervise then waits on logWG before closing the pump (FR-MON-2).
	logCtx, logCancel := context.WithCancel(context.Background())
	i.mu.Lock()
	i.logCancel = logCancel
	i.mu.Unlock()
	i.logWG.Add(1)
	go i.captureLogs(logCtx, id)

	go i.supervise()
	go i.awaitReady()
}

// Only transition starting to running after readiness or fallback; never overwrite stopping or a terminal state.
func (i *instance) awaitReady() {
	if !execution.WaitReady(i.logPump.Ready(), i.exited, i.readinessTimeout) {
		return // the container exited first; supervise owns the terminal state.
	}
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.state != execution.StateStarting {
		return
	}
	i.state = execution.StateRunning
	if i.beforeReadyPublish != nil {
		i.beforeReadyPublish()
	}
	i.emitLocked(execution.StateRunning, "", execution.CrashReasonUnspecified)
}

// Supervise installation and publish the launch as the same instance, keeping the install container until
// handoff.
// Serialize stop recheck, Start, and container-ID publication so Stop cannot miss a retry or launch.
func (i *instance) superviseInstall(installID string) {
	// Retry loop: on a non-zero install exit, clean artifacts and re-run the install container up to
	// maxInstallRetries additional times before giving up. Transport-error re-attach is per-attempt.
	var (
		exitCode int64
		waitErr  error
	)
	for attempt := 0; ; attempt++ {
		scan := i.captureInstallOutput(installID)

		// A Wait transport error is not an install exit; recover the stored exit record by Inspect.
		// If the container is gone, the launch artifact check decides whether installation succeeded.
		var recovered *ContainerInfo
		for {
			exitCode, waitErr = i.docker.Wait(context.Background(), installID)
			if waitErr == nil {
				break
			}
			if !isTransportError(waitErr) {
				break
			}
			if exited, info, found := i.exitAfterTransportError(installID); exited {
				exitCode, waitErr = 0, nil
				if found {
					exitCode, recovered = info.ExitCode, &info
				}
				break
			}
			time.Sleep(waitTransportProbeInterval)
		}

		i.mu.Lock()
		// Mark exit before publishing terminal state so a failed-stop reset cannot overwrite it.
		// Use sticky stop intent even when the transient stopping latch was reset.
		i.exitObserved = true
		stopping := i.stopRequested
		i.mu.Unlock()

		if stopping {
			_ = i.docker.Remove(context.Background(), installID)
			i.finishTerminal(execution.StateStopped, "")
			return
		}

		// A Wait that returned cleanly reports the installer's own exit code, and only exit 0 is a successful install:
		// the daemon answers a non-zero exit with a nil error too.
		if waitErr == nil && exitCode == 0 {
			break // install succeeded, proceed to re-plan
		}

		// Report explained install failures immediately; the same memory limit or Java runtime will fail again.
		oomKilled := false
		if recovered != nil {
			oomKilled = recovered.OOMKilled
		} else {
			oomKilled = i.oomKilled(installID)
		}
		reason, detail, explained := i.explainInstallFailure(scan, oomKilled)
		_ = i.docker.Remove(context.Background(), installID)
		if explained {
			i.finishInstallCrash(reason, detail)
			return
		}

		// Anything else may be transient. Can we retry?
		if attempt >= maxInstallRetries {
			failure := fmt.Sprintf("installer exited with code %d", exitCode)
			if waitErr != nil {
				failure = waitErr.Error()
			}
			i.finishInstallCrash(execution.CrashReasonForgeInstallFailed,
				fmt.Sprintf("forge install failed after %d attempts: %s", attempt+1, failure))
			return
		}

		// Backoff before retry, polling for Stop so a concurrent Stop is observed promptly rather than sleeping the
		// full backoff.
		if i.installBackoffOrStopping(installRetryBackoff[attempt]) {
			i.finishTerminal(execution.StateStopped, "")
			return
		}

		// Re-check stopRequested after backoff: a Stop may have arrived in the window between Remove and the backoff
		// start that was too late for the backoff polling to observe. Use the sticky stopRequested, consistent with
		// installBackoffOrStopping.
		i.mu.Lock()
		stopping = i.stopRequested
		i.mu.Unlock()
		if stopping {
			i.finishTerminal(execution.StateStopped, "")
			return
		}

		// Create retry containers outside the lock, then serialize stop recheck, Start, ID publication, and exit
		// reset.
		_ = execution.CleanForgeInstallArtifacts(i.spec.WorkingDir)
		newID, err := i.createInstallRetryContainer()
		if err != nil {
			i.finishInstallCrash(execution.CrashReasonForgeInstallFailed, "forge install retry failed: "+err.Error())
			return
		}
		if i.beforeRetryStart != nil {
			i.beforeRetryStart()
		}
		i.mu.Lock()
		if i.stopRequested {
			i.mu.Unlock()
			_ = i.docker.Remove(context.Background(), newID)
			i.finishTerminal(execution.StateStopped, "")
			return
		}
		if err := i.docker.Start(context.Background(), newID); err != nil {
			i.mu.Unlock()
			_ = i.docker.Remove(context.Background(), newID)
			i.finishInstallCrash(execution.CrashReasonForgeInstallFailed, "forge install retry failed: "+err.Error())
			return
		}
		i.containerID = newID
		i.exitObserved = false
		i.mu.Unlock()
		installID = newID
	}

	// The install exited cleanly. A Stop that arrived after the wait returned but before the re-plan still wins,
	// report stopped and clean up. Read the sticky stopRequested, consistent with the in-loop check.
	i.mu.Lock()
	stopping := i.stopRequested
	i.mu.Unlock()
	if stopping {
		_ = i.docker.Remove(context.Background(), installID)
		i.finishTerminal(execution.StateStopped, "")
		return
	}

	plan, err := execution.BuildLaunchPlan(i.spec, i.spec.WorkingDir, containerPathResolver(i.spec.WorkingDir))
	if err != nil {
		_ = i.docker.Remove(context.Background(), installID)
		i.finishInstallCrash(execution.CrashReasonForgeInstallFailed, "forge re-plan after install failed: "+err.Error())
		return
	}
	if plan.NeedsInstall {
		// No args file: try legacy Forge jar fallback (MC <=1.16.x installers
		// produce forge-<version>.jar instead of unix_args.txt).
		rel, found, legacyErr := execution.ResolveLegacyForgeJar(i.spec.WorkingDir)
		if legacyErr != nil || !found {
			_ = i.docker.Remove(context.Background(), installID)
			detail := "forge install produced no args file"
			if legacyErr != nil {
				detail = "forge legacy jar resolve failed: " + legacyErr.Error()
			}
			i.finishInstallCrash(execution.CrashReasonForgeInstallFailed, detail)
			return
		}
		jarPath := containerWorkDir + "/" + rel
		plan = execution.LaunchPlan{LaunchArgs: execution.JarLaunchArgs(i.spec, jarPath)}
	}

	// Create outside the lock so name-conflict polling cannot block Stop.
	// Keep the install container as Stop's target until the launch decision.
	id, err := i.createLaunchContainer(plan.LaunchArgs)
	if err != nil {
		_ = i.docker.Remove(context.Background(), installID)
		i.finishTerminal(execution.StateCrashed, "forge launch after install failed: "+err.Error())
		return
	}

	if i.beforeLaunch != nil {
		i.beforeLaunch()
	}

	// Serialize stop recheck, Start, and publication so Stop cannot target a published-but-unstarted launch.
	i.mu.Lock()
	if i.stopping {
		// If Stop won before launch, remove the unstarted launch and exited install containers, then release exit
		// waiters.
		i.mu.Unlock()
		_ = i.docker.Remove(context.Background(), id)
		_ = i.docker.Remove(context.Background(), installID)
		i.finishTerminal(execution.StateStopped, "")
		return
	}
	if err := i.docker.Start(context.Background(), id); err != nil {
		i.mu.Unlock()
		_ = i.docker.Remove(context.Background(), id)
		_ = i.docker.Remove(context.Background(), installID)
		i.finishTerminal(execution.StateCrashed, "forge launch after install failed: "+err.Error())
		return
	}
	i.containerID = id
	i.mu.Unlock()

	// The launch is now the current container; reap the exited install container.
	_ = i.docker.Remove(context.Background(), installID)

	i.beginLaunchTail(id)
}

// createLaunchContainer creates (but does not start) the launch container after a successful install, reusing
// the driver's create helper through the captured fields. Starting is deferred to the latch-guarded critical
// section so a Stop can abort the launch before it starts.
func (i *instance) createLaunchContainer(launchArgs []string) (string, error) {
	gamePort, rconPort, err := ports(i.spec.WorkingDir)
	if err != nil {
		return "", err
	}
	portMappings := []PortMapping{
		{ContainerPort: gamePort, HostIP: i.gameBindIP, HostPort: gamePort},
	}
	if i.network == "" {
		portMappings = append(portMappings,
			PortMapping{ContainerPort: rconPort, HostIP: rconBindIP, HostPort: rconPort})
	}
	create := CreateSpec{
		Name:             containerName(i.spec.ServerID),
		Image:            i.image,
		Cmd:              containerCmd(launchArgs),
		WorkingDir:       containerWorkDir,
		Binds:            []string{i.spec.WorkingDir + ":" + containerWorkDir},
		Ports:            portMappings,
		Network:          i.network,
		Labels:           i.labels,
		MemoryLimitBytes: memoryLimitBytes(i.spec.MemoryLimitMB),
		CPUShares:        cpuShares(i.spec.CPUMillis),
	}
	return i.createFn(context.Background(), i.spec, create)
}

// Check sticky stopRequested between backoff ticks; a failed kill may already have reset stopping.
func (i *instance) installBackoffOrStopping(d time.Duration) bool {
	const tick = 50 * time.Millisecond
	remaining := d
	for remaining > 0 {
		sleep := tick
		if sleep > remaining {
			sleep = remaining
		}
		time.Sleep(sleep)
		remaining -= sleep
		i.mu.Lock()
		stopping := i.stopRequested
		i.mu.Unlock()
		if stopping {
			return true
		}
	}
	return false
}

// Create retries without starting them so the latch-guarded handoff can abort a racing Stop.
func (i *instance) createInstallRetryContainer() (string, error) {
	plan, err := execution.BuildLaunchPlan(i.spec, i.spec.WorkingDir, containerPathResolver(i.spec.WorkingDir))
	if err != nil {
		return "", err
	}
	create := CreateSpec{
		Name:             installContainerName(i.spec.ServerID),
		Image:            i.image,
		Cmd:              containerCmd(plan.InstallArgs),
		WorkingDir:       containerWorkDir,
		Binds:            []string{i.spec.WorkingDir + ":" + containerWorkDir},
		Labels:           i.labels,
		MemoryLimitBytes: memoryLimitBytes(i.spec.MemoryLimitMB),
		CPUShares:        cpuShares(i.spec.CPUMillis),
	}
	return i.createFn(context.Background(), i.spec, create)
}

// Append install output to the working-set log without failing installation on logging errors.
// Classify only this attempt's stream because the file includes earlier attempts.
func (i *instance) captureInstallOutput(installID string) *installOutputScan {
	scan := &installOutputScan{}
	rc, err := i.docker.Logs(context.Background(), installID)
	if err != nil {
		return scan
	}
	defer func() { _ = rc.Close() }()

	logPath := filepath.Join(i.spec.WorkingDir, filepath.FromSlash(execution.ForgeInstallLogRelpath))
	if err := os.MkdirAll(filepath.Dir(logPath), 0o750); err != nil {
		return scan
	}
	f, err := os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o640) //nolint:gosec // logPath is the server's own working dir, not user-controlled.
	if err != nil {
		return scan
	}
	defer func() { _ = f.Close() }()
	demuxLogsTo(rc, io.MultiWriter(scan, f))
	return scan
}

// Match JVM dotted exception names; installer class listings use slashes and must not trigger these diagnoses.
var (
	installOutOfMemoryMarker      = []byte("java.lang.OutOfMemoryError")
	installJavaIncompatibleMarker = []byte("java.lang.UnsupportedClassVersionError")
)

// installOutputScan is the io.Writer one install attempt's output is teed
// through: it records whether either failure marker appeared, holding back only
// enough of the stream to find a marker split across two writes.
type installOutputScan struct {
	outOfMemory      bool
	javaIncompatible bool
	tail             []byte
}

func (s *installOutputScan) Write(p []byte) (int, error) {
	buf := append(s.tail, p...)
	s.outOfMemory = s.outOfMemory || bytes.Contains(buf, installOutOfMemoryMarker)
	s.javaIncompatible = s.javaIncompatible || bytes.Contains(buf, installJavaIncompatibleMarker)
	// One byte short of the longer marker: a marker cut by the write boundary is
	// whole in the next buf, and nothing already searched is kept beyond that.
	if keep := len(installJavaIncompatibleMarker) - 1; len(buf) > keep {
		buf = buf[len(buf)-keep:]
	}
	s.tail = append(s.tail[:0], buf...)
	return len(p), nil
}

// Classify memory failure from the daemon OOM flag or JVM output; exit 137 alone also means other SIGKILL
// causes.
// Unexplained failures remain generic and retryable.
func (i *instance) explainInstallFailure(scan *installOutputScan, oomKilled bool) (reason execution.CrashReason, detail string, ok bool) {
	if scan.outOfMemory || oomKilled {
		detail = "forge install ran out of memory: no memory limit is set for this server, " +
			"so the host or the JVM's default heap was exhausted"
		if i.spec.MemoryLimitMB > 0 {
			detail = fmt.Sprintf("forge install ran out of memory: the installer did not fit in the server's "+
				"%d MiB memory limit; raise the limit and start the server again", i.spec.MemoryLimitMB)
		}
		return execution.CrashReasonForgeInstallOutOfMemory, detail, true
	}
	if scan.javaIncompatible {
		return execution.CrashReasonForgeInstallJavaIncompatible, fmt.Sprintf(
			"forge install failed: the installer cannot run on the Java runtime selected for Minecraft %s "+
				"(image %s): java.lang.UnsupportedClassVersionError, the runtime is older than this Forge version needs",
			i.spec.MinecraftVersion, i.image), true
	}
	return execution.CrashReasonUnspecified, "", false
}

// oomKilled reports whether the daemon recorded an OOM kill for the exited
// container. It is best-effort and bounded like the other post-exit probes: an
// unreachable daemon or an already-gone container is "not known to be", never a
// guess.
func (i *instance) oomKilled(id string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), waitTransportProbeDeadline)
	defer cancel()
	info, err := i.docker.Inspect(ctx, id)
	return err == nil && info.OOMKilled
}

// finishTerminal records a terminal state reached during the install phase (no launch container started), emits
// it, and closes the event/exited channels so the manager's pump and any in-flight Stop wait observe the end.
func (i *instance) finishTerminal(state execution.ServerState, detail string) {
	i.finish(state, detail, execution.CrashReasonUnspecified)
}

// finishInstallCrash is finishTerminal for a crash of the install itself, tagged with why so a client can tell
// it from a runtime crash.
func (i *instance) finishInstallCrash(reason execution.CrashReason, detail string) {
	i.finish(execution.StateCrashed, detail, reason)
}

func (i *instance) finish(state execution.ServerState, detail string, reason execution.CrashReason) {
	i.set(state)
	i.mu.Lock()
	i.emitLocked(state, detail, reason)
	i.mu.Unlock()
	close(i.exited)
	i.logPump.Close()
	i.mu.Lock()
	i.closed = true
	close(i.events)
	i.mu.Unlock()
	// Release the cached metrics RCON connection so it never outlives the instance. Closing i.events above stops
	// the metrics pump, so this cannot contend with a sample for long.
	i.closeMetricsControl()
}

func (i *instance) Status() execution.ServerState {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.state
}

// ProbeAlive performs one live Inspect; cached state cannot establish liveness and daemon errors remain unknown.
func (i *instance) ProbeAlive(ctx context.Context) (bool, error) {
	return i.inspectAlive(ctx, i.currentContainerID())
}

func (i *instance) Events() <-chan execution.StatusEvent { return i.events }

// Logs streams the container's captured console output (execution.LogSource).
func (i *instance) Logs() <-chan execution.LogEvent { return i.logPump.Logs() }

// captureLogs opens the container's following log stream and demuxes it into the
// pump until the stream ends (container exit) or logCtx is cancelled. A failure
// to open the stream is non-fatal: logs are best-effort relay (FR-MON-2), so the
// goroutine simply exits and the server runs without log capture.
func (i *instance) captureLogs(ctx context.Context, id string) {
	defer i.logWG.Done()
	rc, err := i.docker.Logs(ctx, id)
	if err != nil {
		return
	}
	defer func() { _ = rc.Close() }()
	demuxLogs(rc, i.logPump)
}

// Sample reads a one-shot resource sample from the Engine stats endpoint (execution.StatsSource, FR-MON-3). An
// error (daemon unreachable, container gone) makes the manager fall back to an up-only sample. Player count is
// queried best-effort via RCON "list"; a failure leaves it zero.
func (i *instance) Sample(ctx context.Context) (execution.MetricsSample, error) {
	stats, err := i.docker.Stats(ctx, i.currentContainerID())
	if err != nil {
		return execution.MetricsSample{}, err
	}
	return execution.MetricsSample{
		ServerID:    i.spec.ServerID,
		CPUMillis:   stats.CPUMillis,
		MemoryBytes: stats.MemoryBytes,
		PlayerCount: i.queryPlayerCount(ctx),
	}, nil
}

// Reuse RCON across metrics samples to avoid connection lifecycle log spam.
// On failure return zero players and discard poisoned connections; serialize sampling and terminal close.
func (i *instance) queryPlayerCount(ctx context.Context) uint32 {
	i.metricsMu.Lock()
	defer i.metricsMu.Unlock()

	// The instance has terminated and its connection was closed; do not redial —
	// nothing would close a fresh connection.
	if i.metricsClosed {
		return 0
	}

	if i.metricsControl == nil {
		ctrl, err := i.openControl(ctx, i.spec, i.rconHost)
		if err != nil {
			return 0
		}
		i.metricsControl = ctrl
	}

	reply, err := i.metricsControl.Execute(ctx, "list")
	if err != nil {
		// The rcon client poisoned this connection; discard it so the next sample
		// redials rather than reading off a broken stream.
		_ = i.metricsControl.Close()
		i.metricsControl = nil
		return 0
	}
	return parsePlayerCount(reply)
}

// closeMetricsControl closes the cached metrics RCON connection (if any) and latches metricsClosed so a late
// in-flight sample does not redial a connection nobody would close. Called from the terminal paths so the
// connection never outlives the instance.
func (i *instance) closeMetricsControl() {
	i.metricsMu.Lock()
	defer i.metricsMu.Unlock()
	i.metricsClosed = true
	if i.metricsControl != nil {
		_ = i.metricsControl.Close()
		i.metricsControl = nil
	}
}

// parsePlayerCount extracts the online count from a Minecraft "list" response.
// The vanilla format is "There are N of a max of M players online: …". It
// returns 0 when the format is unrecognised.
func parsePlayerCount(reply string) uint32 {
	// "There are 3 of a max of 20 players online: ..."
	const prefix = "There are "
	idx := strings.Index(reply, prefix)
	if idx < 0 {
		return 0
	}
	rest := reply[idx+len(prefix):]
	end := strings.IndexByte(rest, ' ')
	if end <= 0 {
		return 0
	}
	n, err := strconv.ParseUint(rest[:end], 10, 32)
	if err != nil {
		return 0
	}
	return uint32(n)
}

// Stop ends the instance. A graceful stop tries RCON "stop", then `docker stop`
// (which SIGTERMs then SIGKILLs after stopTimeout inside the daemon), then a
// direct `docker kill`; a forced stop skips the RCON step. Any terminal state
// makes Stop a prompt no-op success.
func (i *instance) Stop(ctx context.Context, graceful bool, preFallback ...func(context.Context) bool) error {
	i.mu.Lock()
	if i.stopping || isTerminal(i.state) {
		i.mu.Unlock()
		return nil
	}
	i.stopping = true
	// Record the stop intent stickily so supervise reports the eventual exit as stopped even if the survived-kill
	// failure path later clears stopping.
	i.stopRequested = true
	// Capture the prior state so a failed stop of a booting server restores starting rather than reporting running.
	prior := i.state
	i.state = execution.StateStopping
	// Capture the current container under the same lock that latches stopping, so the install→launch handoff (which
	// only proceeds when stopping is unset) cannot race this read: Stop acts on whichever container is current, and
	// a concurrent install supervisor sees stopping set and launches nothing.
	id := i.containerID
	i.mu.Unlock()
	i.emit(execution.StateStopping, "")

	// Flush on a detached budget before escalation; its duration must not consume the shutdown grace.
	// A successful flush skips RCON stop and SIGTERM: their shutdown saves could overwrite flushed data if
	// interrupted.
	flushed := false
	if graceful && len(preFallback) > 0 && preFallback[0] != nil {
		flushCtx, flushCancel := context.WithTimeout(context.WithoutCancel(ctx), i.flushTimeout)
		flushed = preFallback[0](flushCtx)
		flushCancel()
	}

	// Detach escalation from the session so reconnect cannot interrupt a stop already in progress.
	// Start its deadline after the separately bounded flush.
	stopCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), i.stopDeadline())
	defer cancel()

	if !flushed {
		if graceful && i.tryRCONStop(stopCtx) && i.waitExit(stopCtx, i.stopTimeout) {
			return nil
		}

		if err := i.docker.Stop(stopCtx, id, i.stopTimeout); err == nil && i.waitExit(stopCtx, i.stopTimeout) {
			return nil
		}
	}

	// Distinguish a successful flush, a timed-out graceful shutdown, and an intentional forced kill in diagnostics.
	if flushed {
		i.logger.Info("flush succeeded; terminating container",
			"server_id", i.spec.ServerID)
	} else if graceful {
		i.logger.Warn("graceful stop timed out; escalating to kill",
			"server_id", i.spec.ServerID, "timeout", i.stopTimeout)
	} else {
		i.logger.Warn("forced stop; killing container directly",
			"server_id", i.spec.ServerID, "timeout", i.stopTimeout)
	}

	// Use a detached context for the kill call so it is never starved by
	// the stopDeadline — a kill MUST reach the daemon even when the prior
	// RCON + docker-stop phases consumed the entire budget (observed with
	// MC 26.1.2 shutdown-save hangs under relay/tunnel connections).
	killCtx, killCancel := context.WithTimeout(context.WithoutCancel(stopCtx), 30*time.Second)
	defer killCancel()
	if err := i.docker.Kill(killCtx, id); err != nil {
		// Reset a failed kill for retry unless supervision already observed exit.
		// Keep stopRequested sticky so a later exit is stopped, not a crash.
		i.mu.Lock()
		if i.exitObserved {
			i.mu.Unlock()
			return nil
		}
		i.stopping = false
		i.state = prior
		i.mu.Unlock()
		return fmt.Errorf("containerdriver: kill: %w", err)
	}
	// Confirm termination after kill before reporting success; the API must keep assignment if the container
	// survives.
	// Ignore caller cancellation during this confirmation because the kill is already issued.
	if !i.waitExitDone(i.stopTimeout) {
		if i.beforeSurvivedReset != nil {
			i.beforeSurvivedReset()
		}
		// Restore pre-stop state for retry only if supervision has not observed exit.
		// Keep stopRequested sticky so a late exit remains an intentional stop.
		i.mu.Lock()
		if i.exitObserved {
			i.mu.Unlock()
			return nil
		}
		i.stopping = false
		i.state = prior
		i.mu.Unlock()
		return fmt.Errorf("containerdriver: container survived docker kill after %s", i.stopTimeout)
	}
	return nil
}

// stopDeadlineGrace pads the detached stop-escalation deadline beyond the
// stopTimeout-bounded steps, leaving headroom for the RCON open/"stop" and the
// docker Kill call so a healthy stop never trips the bound; it only caps a hung
// daemon call.
const stopDeadlineGrace = 10 * time.Second

// stopDeadline covers RCON wait, daemon grace, post-stop wait, and kill margin.
// The pre-stop flush has its own preceding budget.
func (i *instance) stopDeadline() time.Duration {
	return 3*i.stopTimeout + stopDeadlineGrace
}

// waitExitDone reports whether the container reached a terminal state within d, observing only the exit and the
// timeout (not caller-context cancellation). It confirms a kill terminated the container.
func (i *instance) waitExitDone(d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-i.exited:
		return true
	case <-timer.C:
		return false
	}
}

// isTerminal reports whether s is a state the container can no longer leave.
func isTerminal(s execution.ServerState) bool {
	return s == execution.StateStopped || s == execution.StateCrashed
}

// rconPhaseCap bounds the RCON open+"stop" exchange to its own slice of the stop
// budget. It mirrors rcon's defaultExecuteTimeout: a healthy exchange is
// sub-second, so this ceiling never trips a real stop; it only caps a hung peer.
// A var (not a const) so tests can shrink it.
var rconPhaseCap = 30 * time.Second

// Cap RCON at min(rconPhaseCap, remaining/3) so the later signal and exit waits retain shutdown grace.
func rconStopDeadline(ctx context.Context) time.Time {
	phase := rconPhaseCap
	if deadline, ok := ctx.Deadline(); ok {
		if third := time.Until(deadline) / 3; third < phase {
			phase = third
		}
	}
	return time.Now().Add(phase)
}

// tryRCONStop opens RCON and sends "stop", reporting whether the in-band stop was issued successfully. A failure
// returns false so Stop falls back to `docker stop`. The exchange runs under a phase deadline so a hung peer
// cannot consume the whole stop budget.
func (i *instance) tryRCONStop(ctx context.Context) bool {
	ctx, cancel := context.WithDeadline(ctx, rconStopDeadline(ctx))
	defer cancel()
	ctrl, err := i.openControl(ctx, i.spec, i.rconHost)
	if err != nil {
		return false
	}
	defer func() { _ = ctrl.Close() }()
	if _, err := ctrl.Execute(ctx, "stop"); err != nil {
		return false
	}
	return true
}

// waitExit reports whether the container reached a terminal state within d. The
// supervisor goroutine observes the actual exit and closes i.exited; the wait is
// released by that close regardless of the recorded terminal state. It returns
// false if the stop timeout elapses or ctx is cancelled first.
func (i *instance) waitExit(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-i.exited:
		return true
	case <-ctx.Done():
		return false
	case <-timer.C:
		return false
	}
}

// supervise emits stopped for requested stops and crashed for other confirmed exits.
// Wait transport errors trigger bounded re-inspection and reattachment, never an assumed crash.
func (i *instance) supervise() {
	id := i.currentContainerID()

	var waitErr error
	for {
		_, waitErr = i.docker.Wait(context.Background(), id)
		if waitErr == nil || !isTransportError(waitErr) || i.exitedAfterTransportError(id) {
			break
		}
		// Throttle re-attach so back-to-back transport errors do not hot-spin against the daemon socket.
		time.Sleep(waitTransportProbeInterval)
	}

	i.mu.Lock()
	// Mark exit before terminal publication and use sticky stop intent so late failed-stop resets cannot invent a
	// crash.
	i.exitObserved = true
	stopping := i.stopRequested
	i.mu.Unlock()

	if stopping {
		i.set(execution.StateStopped)
		i.emit(execution.StateStopped, "")
	} else {
		detail := "container exited unexpectedly"
		if waitErr != nil {
			detail = waitErr.Error()
		}
		i.set(execution.StateCrashed)
		i.emit(execution.StateCrashed, detail)
	}
	// Release any in-flight waitExit now the terminal state is set.
	close(i.exited)

	// End the log follow and wait for the capture goroutine before closing the
	// pump so Logs() consumers finish cleanly (no goroutine leak).
	i.logCancel()
	i.logWG.Wait()
	i.logPump.Close()

	// Remove the exited container so a later start can reuse the deterministic name.
	_ = i.docker.Remove(context.Background(), id)

	i.mu.Lock()
	i.closed = true
	close(i.events)
	i.mu.Unlock()

	// Release cached metrics RCON after container removal; event closure cancels sampling so terminal cleanup can
	// acquire its lock.
	i.closeMetricsControl()
}

// Only statusError is an Engine HTTP response; other call errors require re-observing the container before
// declaring exit.
func isTransportError(err error) bool {
	if err == nil {
		return false
	}
	var status statusError
	return !errors.As(err, &status)
}

// inspectAlive shares one liveness rule with ProbeAlive and supervision: 404 means dead, other errors mean
// unknown.
func (i *instance) inspectAlive(ctx context.Context, id string) (bool, error) {
	info, _, err := i.inspectState(ctx, id)
	return info.Running, err
}

// inspectState is the Inspect behind inspectAlive, keeping what the daemon said:
// found is false, with a zero info, for a container it does not know (a 404).
func (i *instance) inspectState(ctx context.Context, id string) (info ContainerInfo, found bool, err error) {
	info, err = i.docker.Inspect(ctx, id)
	switch {
	case errors.Is(err, errNotFound):
		return ContainerInfo{}, false, nil
	case err != nil:
		return ContainerInfo{}, false, err
	default:
		return info, true, nil
	}
}

// Return true only for confirmed exit; alive or unreachable reattaches Wait without changing status.
// Each Inspect is bounded by the overall probe deadline.
func (i *instance) exitedAfterTransportError(id string) bool {
	exited, _, _ := i.exitAfterTransportError(id)
	return exited
}

// exitAfterTransportError is exitedAfterTransportError keeping the inspection that confirmed the exit: found is
// true, with the exited container's info, when the container still exists, and false when it is already gone.
func (i *instance) exitAfterTransportError(id string) (exited bool, info ContainerInfo, found bool) {
	deadline := time.Now().Add(waitTransportProbeDeadline)
	for {
		ctx, cancel := context.WithDeadline(context.Background(), deadline)
		info, found, err := i.inspectState(ctx, id)
		cancel()
		if err == nil {
			return !info.Running, info, found
		}
		// Daemon still unreachable: retry until the deadline, then re-attach a
		// waiter rather than guess a terminal.
		if time.Now().After(deadline) {
			return false, ContainerInfo{}, false
		}
		time.Sleep(waitTransportProbeInterval)
	}
}

func (i *instance) set(s execution.ServerState) {
	i.mu.Lock()
	i.state = s
	i.mu.Unlock()
}

// emit coalesces without blocking; once terminal, ignore later non-terminal events from readiness or Stop races.
func (i *instance) emit(state execution.ServerState, detail string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.emitLocked(state, detail, execution.CrashReasonUnspecified)
}

// emitLocked is the lock-free core of emit. Caller must hold i.mu. reason classifies a crashed state the driver
// can explain.
func (i *instance) emitLocked(state execution.ServerState, detail string, reason execution.CrashReason) {
	if i.closed {
		return
	}
	if i.terminalLatched && !isTerminal(state) {
		return
	}
	if isTerminal(state) {
		i.terminalLatched = true
	}
	ev := execution.StatusEvent{ServerID: i.spec.ServerID, State: state, Detail: detail, CrashReason: reason}
	for {
		select {
		case i.events <- ev:
			return
		default:
		}
		// Buffer full: drop the oldest buffered event and retry so the latest
		// state wins. The retry can race a concurrent reader that just freed a
		// slot, in which case the drain misses and we loop back to the send.
		select {
		case <-i.events:
		default:
		}
	}
}

// ensure the driver satisfies the ExecutionDriver Port.
var _ execution.ExecutionDriver = (*Driver)(nil)

// instance implements the optional log/metrics capabilities the instance manager
// type-asserts (FR-MON-2, FR-MON-3).
var (
	_ execution.LogSource   = (*instance)(nil)
	_ execution.StatsSource = (*instance)(nil)
)

// ports must agree with tunnel.gamePort; only an absent file or key uses defaults.
// Fail unreadable files rather than accidentally publishing the relay's default port.
func ports(workingDir string) (game, rcon string, err error) {
	props, err := readProperties(filepath.Join(workingDir, "server.properties"))
	if err != nil {
		return "", "", err
	}
	game = props["server-port"]
	if game == "" {
		game = defaultGamePort
	}
	rcon = props["rcon.port"]
	if rcon == "" {
		rcon = defaultRCONPort
	}
	return game, rcon, nil
}
