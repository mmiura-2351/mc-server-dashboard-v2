// Package containerdriver implements the execution.ExecutionDriver Port by
// running a server inside a Docker container (FR-EXE-2, FR-EXE-4). The Docker
// Engine interaction sits behind the narrow dockerAPI seam so unit tests run
// against a fake and no Docker daemon is needed in CI; the real client is a
// hand-rolled HTTP-over-unix-socket adapter (dockerclient.go), keeping the
// dependency tree empty as the RCON client did (docs/dev/DEPENDENCIES.md).
//
// Lifecycle: a successful create+start
// enters StateStarting and is held there until the server logs its startup-
// complete "Done (X.XXXs)! For help" line (by which point RCON is listening),
// then transitions to running; a bounded fallback timeout reports running anyway
// if the marker never appears (issue #345). The container exiting while no Stop
// is in flight is a crash (StateCrashed, FR-SRV-4); an exit during a Stop is a
// clean StateStopped.
//
// Stop semantics (ARCHITECTURE.md Section 5.2): a graceful stop prefers the
// in-band RCON "stop" command (reusing the ServerControl seam), then falls back
// to `docker stop` (SIGTERM with a timeout, escalating to SIGKILL inside the
// daemon), then a direct `docker kill`. A forced stop skips the RCON step.
//
// Memory is enforced as a hard container limit: the create path sets the Docker
// host-config Memory field from InstanceSpec.MemoryLimitMB (MiB→bytes) so the
// kernel OOM-kills a runaway server at the container boundary rather than letting
// it starve the host (issue #707). An unset limit (0) sets no constraint,
// preserving the prior behavior. CPU is a per-server SOFT relative share: the
// create path sets the host-config CpuShares weight proportional to
// InstanceSpec.CPUMillis (1024 shares = 1 core), so a larger allocation wins more
// CPU under contention without any hard quota (NanoCpus is never set) that would
// throttle MC tick latency (issue #724); an unset allocation (0) keeps the fixed
// default weight (issue #518). Disk quotas remain deferred.
package containerdriver

import (
	"context"
	"errors"
	"fmt"
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

// defaultSweepCallMargin is the slack added on top of each Sweep daemon call's
// expected duration to bound it against a wedged daemon (issue #338). The startup
// Sweep runs with context.Background() (cmd/worker), so without a per-call
// deadline a wedged Docker daemon would block worker startup indefinitely. A
// healthy daemon answers each call well inside the deadline, so the bound never
// fires on the success path; it only caps the wedged case. The graceful-stop call
// gets StopTimeout + this margin (the daemon needs the full grace before
// escalating to SIGKILL); the list/remove calls get this margin alone.
const defaultSweepCallMargin = 10 * time.Second

// defaultReadinessTimeout bounds how long the driver holds StateStarting waiting
// for the server's startup-complete log marker before falling back to running
// (issue #345). It is generous enough for a modded server's boot (tens of
// seconds) while never leaving a server stuck in starting when its log format
// omits the marker.
const defaultReadinessTimeout = 5 * time.Minute

// defaultConflictPollInterval and defaultConflictDeadline bound the
// wait-for-name-free loop createContainer runs on a create name conflict: it
// polls every interval until the deadline for the deterministic name to free as
// the async exit-watcher finishes the previous container's teardown (issue #233).
const (
	defaultConflictPollInterval = 250 * time.Millisecond
	defaultConflictDeadline     = 10 * time.Second
)

// waitTransportProbeInterval and waitTransportProbeDeadline bound the re-inspect
// loop supervise runs when docker.Wait returns a TRANSPORT error (a daemon
// restart/blip) rather than a confirmed exit. A transport error does not mean the
// container died, so supervise re-inspects the container to learn its real state
// before deciding a terminal: it polls every interval until the deadline,
// re-attaching a waiter if the container is still running, emitting the terminal
// if it is gone, and emitting nothing if the daemon stays unreachable past the
// deadline so the manager's last authoritative state stands (issue #865). The
// bound mirrors the wedged-daemon posture of the conflict loop (issue #233) and
// the startup Sweep (issue #338): a healthy daemon answers the first probe, so
// this only caps the wedged case. Vars (not consts) so tests can shrink them.
var (
	waitTransportProbeInterval = 250 * time.Millisecond
	waitTransportProbeDeadline = 30 * time.Second
)

// maxInstallRetries is the number of additional attempts after the first Forge
// install failure (3 total attempts). installRetryBackoff is the delay before
// each retry; a var so tests can shrink it.
const maxInstallRetries = 2

var installRetryBackoff = []time.Duration{5 * time.Second, 15 * time.Second}

// defaultImagePullTimeout bounds a lazy base-image pull (issue #904). A pull is
// hundreds of MB and the EngineClient has no http.Client timeout, so the create
// path gives the pull its own generous deadline rather than the create call's
// short budget; a slow first pull of a large tier completes within it on a
// healthy host, and the bound only caps a wedged/never-finishing pull. A var (not
// a const) so a future test can shrink it; none does today.
var defaultImagePullTimeout = 10 * time.Minute

// defaultGameBindIP is the host interface the game port is published on when
// Options.GameBindIP is unset: loopback, preserving the historical behavior.
// rconBindIP is fixed: RCON is a control channel and must not be exposed.
const (
	defaultGameBindIP = "127.0.0.1"
	rconBindIP        = "127.0.0.1"
)

// controlFunc opens an execution.ServerControl (RCON) for a server, used for the
// graceful-stop "stop" command. rconHost is the host to dial RCON at — empty for
// the host loopback (no network), or the container name when a user-defined
// network is configured (issue #218). It returns an error when RCON is
// unavailable; the driver then falls back to `docker stop`.
type controlFunc func(ctx context.Context, spec execution.InstanceSpec, rconHost string) (execution.ServerControl, error)

// Options tunes the driver.
type Options struct {
	// WorkerID labels every container so a startup sweep can find and remove this
	// Worker's orphaned containers (crash-orphan recovery).
	WorkerID string
	// StopTimeout bounds the `docker stop` grace period. Zero uses
	// defaultStopTimeout.
	StopTimeout time.Duration
	// GameBindIP is the host interface the game port is published on. Empty uses
	// defaultGameBindIP (loopback), preserving the historical behavior.
	GameBindIP string
	// Network is the user-defined Docker network MC containers attach to. Empty
	// (the default) keeps the historical behavior: containers run on the default
	// bridge and RCON is published to the host loopback. When set, the driver
	// attaches each container to this network, drops the RCON host publication, and
	// dials RCON at the container name over the network (issue #218).
	Network string
	// ConflictPollInterval and ConflictDeadline tune the wait-for-name-free loop
	// createContainer runs on a create name conflict (issue #233). Zero uses the
	// production defaults; tests set short values to keep the suite fast.
	ConflictPollInterval time.Duration
	ConflictDeadline     time.Duration
	// ReadinessTimeout bounds how long the driver holds StateStarting waiting for
	// the server's startup-complete log marker before falling back to running
	// (issue #345). Zero uses defaultReadinessTimeout.
	ReadinessTimeout time.Duration
	// SweepCallMargin is the slack added to each startup-Sweep daemon call's
	// deadline so a wedged daemon cannot hang worker startup (issue #338). Zero uses
	// defaultSweepCallMargin; tests set a short value to keep the suite fast.
	SweepCallMargin time.Duration
	// Logger records the lazy base-image pull (image name, duration) at INFO (issue
	// #904). Nil uses a discard logger.
	Logger *slog.Logger
}

// Driver is the container ExecutionDriver.
type Driver struct {
	docker      dockerAPI
	images      *ImageSelector
	openControl controlFunc
	workerID    string
	stopTimeout time.Duration
	gameBindIP  string
	network     string
	// conflictPoll and conflictDeadline bound the wait-for-name-free loop (#233).
	conflictPoll     time.Duration
	conflictDeadline time.Duration
	// readinessTimeout bounds the hold-on-starting wait (issue #345).
	readinessTimeout time.Duration
	// sweepCallMargin bounds each startup-Sweep daemon call (issue #338).
	sweepCallMargin time.Duration
	// imagePullTimeout bounds a lazy base-image pull (issue #904).
	imagePullTimeout time.Duration
	// logger records the lazy base-image pull at INFO (issue #904).
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
	return &Driver{
		docker:           docker,
		images:           images,
		openControl:      openControl,
		workerID:         opts.WorkerID,
		stopTimeout:      timeout,
		gameBindIP:       gameBindIP,
		network:          opts.Network,
		conflictPoll:     conflictPoll,
		conflictDeadline: conflictDeadline,
		readinessTimeout: readinessTimeout,
		sweepCallMargin:  sweepCallMargin,
		imagePullTimeout: defaultImagePullTimeout,
		logger:           logger,
	}
}

// RconHost returns the host that RCON for serverID is dialed at. It is empty when no
// network is configured (the caller falls back to the host loopback), and the
// container name when a user-defined network is configured: the network's
// container-name DNS resolves it, so RCON is reached over the network rather than
// the unreachable host loopback (issue #218).
func (d *Driver) RconHost(serverID string) string {
	return d.networkHost(serverID)
}

// GameHost returns the host the relay tunnel dials serverID's game port at. Like
// RconHost it is empty when no network is configured (the tunnel falls back to the
// published-port loopback) and the container name when a user-defined network is
// configured: the worker process is itself a container on that network, so the
// server's game port is reachable at the container name over the network, not at
// the worker's own loopback where the host publication does not exist (issue #979).
func (d *Driver) GameHost(serverID string) string {
	return d.networkHost(serverID)
}

// networkHost is the single decision RconHost and GameHost share: empty with no
// network (caller dials the host loopback), the container name over the
// user-defined network otherwise, so the RCON and tunnel dial hosts can never
// drift (issues #218, #979).
func (d *Driver) networkHost(serverID string) string {
	if d.network == "" {
		return ""
	}
	return containerName(serverID)
}

// Start resolves the base image and the launch plan, then either launches the
// server container directly or — for a Forge args-file launch whose working set
// is not yet installed — runs a supervised install container first and returns
// immediately; a supervisor goroutine runs the install to completion, then
// creates+starts the launch container as the SAME instance and removes the exited
// install container once the launch's fate is decided (issue #305). The install
// container carries a distinct name (mcsd-<id>-install) so it never collides with
// the deterministic launch name, so the #233 wait-for-name-free loop is not
// entered against the still-present install container. It emits starting then
// running (or crashed if the install fails); a successful return means the install
// or launch container was started.
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
		labels:           d.labels(spec.ServerID),
		createFn:         d.createContainer,
		openControl:      d.openControl,
		rconHost:         d.RconHost(spec.ServerID),
		stopTimeout:      d.stopTimeout,
		readinessTimeout: d.readinessTimeout,
		logger:           d.logger,
		events:           make(chan execution.StatusEvent, 8),
		exited:           make(chan struct{}),
		state:            execution.StateStarting,
		logPump:          execution.NewLogPump(spec.ServerID, logBufferLines),
	}
	inst.emit(execution.StateStarting, "")

	if plan.NeedsInstall {
		// Remove stale Forge install artifacts (args files, legacy jars) so the
		// re-install starts from a clean slate and never hits ambiguous-match
		// errors (issue #1127). A cleanup failure is logged but does not block
		// the install.
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

// launchContainer creates and starts the server launch container, returning its
// id. It publishes the game (and, off-network, RCON) ports and runs the launch
// argv in exec form. It heals the deterministic-name conflict via createContainer
// (the #233 wait-for-name-free loop).
func (d *Driver) launchContainer(ctx context.Context, spec execution.InstanceSpec, image string, launchArgs []string) (string, error) {
	gamePort, rconPort := ports(spec.WorkingDir)
	// The game port binds to the configured host interface (driver.container.
	// game_bind_ip) so players can reach the server.
	portMappings := []PortMapping{
		{ContainerPort: gamePort, HostIP: d.gameBindIP, HostPort: gamePort},
	}
	// RCON publication depends on the topology. With no network configured (bare-
	// metal parity) RCON is published on the host loopback and
	// dialed there. With a user-defined network configured, the host RCON
	// publication is DROPPED — RCON never leaves the docker network — and the
	// driver dials RCON at the container name over that network instead (issue
	// #218). It is a control channel that must never be exposed beyond loopback /
	// the docker network.
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
		Labels:           d.labels(spec.ServerID),
		MemoryLimitBytes: memoryLimitBytes(spec.MemoryLimitMB),
		CPUShares:        cpuShares(spec.CPUMillis),
	}

	id, err := d.createContainer(ctx, create)
	if err != nil {
		return "", err
	}
	if err := d.docker.Start(ctx, id); err != nil {
		// Best-effort cleanup of the created-but-unstarted container.
		d.removeCreated(ctx, id)
		return "", err
	}
	return id, nil
}

// startCleanupRemoveTimeout bounds removeCreated's best-effort Remove of a
// created-but-unstarted container after a failed Start. A healthy daemon
// answers a remove well inside it (the startup Sweep's remove calls carry the
// same 10s posture, issue #338); the bound only caps a wedged daemon.
const startCleanupRemoveTimeout = 10 * time.Second

// removeCreated is the best-effort cleanup of a created-but-unstarted container
// whose Start failed. It runs detached from the command context
// (context.WithoutCancel, bounded by startCleanupRemoveTimeout): a failed Start
// is often *caused by* that context being cancelled (a dropped session stream),
// and a Remove on the same context fails instantly, leaking a Created container
// that pins the deterministic name until the #233 wait-for-name-free loop or a
// worker-restart Sweep reaps it (issue #1715). The detachment mirrors the
// driver's other cleanup calls (pullImage, Stop's escalation).
func (d *Driver) removeCreated(ctx context.Context, id string) {
	removeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), startCleanupRemoveTimeout)
	defer cancel()
	_ = d.docker.Remove(removeCtx, id)
}

// runInstallContainer creates and starts the supervised Forge install container,
// returning its id. It runs `java -jar <jar> --installServer` (exec form) against
// the same image and bind-mounted working dir as the launch, under a distinct
// name (mcsd-<id>-install) so it never collides with the launch name. It publishes
// no ports (the installer needs none) and attaches no network, keeping the install
// step independent of the launch topology.
func (d *Driver) runInstallContainer(ctx context.Context, spec execution.InstanceSpec, image string, plan execution.LaunchPlan) (string, error) {
	create := CreateSpec{
		Name:             installContainerName(spec.ServerID),
		Image:            image,
		Cmd:              containerCmd(plan.InstallArgs),
		WorkingDir:       containerWorkDir,
		Binds:            []string{spec.WorkingDir + ":" + containerWorkDir},
		Labels:           d.labels(spec.ServerID),
		MemoryLimitBytes: memoryLimitBytes(spec.MemoryLimitMB),
		CPUShares:        cpuShares(spec.CPUMillis),
	}
	// createContainer carries the #233 wait-for-name-free loop, but the distinct
	// install name almost never hits it: a leftover install container from a crash
	// is reaped by the startup Sweep (which the worker-id label scopes), and the
	// loop self-heals the rare case where a prior install container under the same
	// name has not finished tearing down yet. Do not "simplify" this to a bare
	// docker.Create — that would lose the stale-install-container self-healing.
	id, err := d.createContainer(ctx, create)
	if err != nil {
		return "", err
	}
	if err := d.docker.Start(ctx, id); err != nil {
		// Best-effort cleanup, detached from the command context (issue #1715).
		d.removeCreated(ctx, id)
		return "", err
	}
	return id, nil
}

// classifyStartError wraps a create/start failure with a sanitized execution
// sentinel (execution.ErrPortConflict / execution.ErrImageMissing) when the
// Docker daemon's message matches a known operational class, so the instance
// manager can surface a friendlier failure code than the generic internal one
// (issue #225). Any other error is returned unchanged (the default stays
// internal).
//
// FRAGILITY: the Docker Engine API has no machine-readable error class for these
// — it returns a 500/404 with a free-text daemon message — so detection is
// substring matching on that prose. A daemon-message wording change across Docker
// versions would silently drop a server back to the unclassified "internal" code
// (never a misclassification: an unmatched message simply falls through). The raw
// daemon text continues to ride the wrapped error into the Worker logs regardless,
// so a field diagnosis never depends on the classification succeeding.
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

// isImageMissing reports whether a create error is the daemon saying the base
// image is not present (or cannot be pulled). The detection is substring matching
// on the free-text daemon message (see classifyStartError's FRAGILITY note); the
// create path uses it to decide whether to attempt a lazy pull (issue #904) and
// classifyStartError uses it to sanitize the failure.
func isImageMissing(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "No such image") || strings.Contains(msg, "pull access denied")
}

// createPullingOnMiss creates the container, and on an image-missing failure
// pulls the base image once and retries the create (issue #904). The container
// driver historically never pulled, so a fresh host's first start failed with
// image_missing even though DEPLOYMENT.md promised the worker would pull; this
// lazy pull-on-miss makes that promise true without the disk/bandwidth cost of
// eager startup pulls. A pull failure (offline host, denied/unknown image) is
// folded back into the original image-missing create error so the friendly
// ErrImageMissing classification (issue #225) is preserved and the operator never
// sees a raw 404 — the pull error rides along for diagnostics. The retry runs
// Create directly (not back through this helper) so a second image-missing
// genuinely fails rather than looping.
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

// pullImage pulls the base image under its own generous deadline (issue #904),
// logging the image and duration at INFO. The pull is hundreds of MB and the
// EngineClient has no http.Client timeout, so the create call's short context
// would cut it off; it runs on a detached context bounded by imagePullTimeout
// instead, so a slow first pull of a large tier is not aborted while a wedged
// pull is still capped. The pull persists on the host regardless of the caller's
// outcome, so even if a first pull outlasts the caller's start budget (the retry
// Create then fails on the cancelled ctx), the image is now cached and the
// reconciler's next start converges.
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

// createContainer creates the container, healing the create name conflict a
// back-to-back restart hits while the exit-watcher's async removal of the exited
// container has not yet freed the deterministic name. Three successive one-shot
// fixes each lost to a new interleaving of this race (#226 name conflict, #229
// inspect-404, #233 remove-already-in-progress), so on a 409 name conflict the
// driver runs a bounded wait-for-name-free loop instead of a single special case
// (issue #233): it polls (every conflictPoll, until conflictDeadline, honoring
// ctx) for the name to free.
//
// Each iteration inspects the conflicting name:
//   - 404: the name is free, so retry the create. Success returns; a fresh 409
//     means the name flickered while the daemon finishes teardown, so keep
//     polling; any other create error is returned.
//   - this Worker's label and not running: a stale leftover, so issue a remove.
//     A DELETE 409 ("removal in progress") is the watcher already removing it —
//     progress, keep polling. Any other remove error: the watcher may still win,
//     keep polling until the deadline.
//   - foreign label or running: fail immediately, never removing a container we
//     do not own or a live server (the conservative posture is unchanged).
//   - any other inspect error: transient, keep polling until the deadline.
//
// On deadline expiry the original conflict is returned wrapped with the last
// decline reason, so a field diagnosis does not require code reading (keeping
// #231's observability).
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

// logBufferLines bounds the per-instance captured-log buffer; matches the
// now-removed host-process driver's posture (drop-oldest + dropped-count marker, issue #96).
const logBufferLines = 256

// containerStateRunning is the Engine's container-state string for a running
// container (the State field /containers/json reports). The sweep gracefully
// stops a running orphan before removing it (issue #336).
const containerStateRunning = "running"

// Sweep removes leftover containers labelled for this Worker, recovering from a
// crash that left a server's container running or stopped. It is called once at
// startup before any server is launched; the deterministic name plus the
// worker-id label scope it to this Worker's own containers so it never touches
// unrelated ones.
//
// A RUNNING orphan is stopped gracefully first — `docker stop` with the driver's
// StopTimeout grace, SIGTERM then SIGKILL inside the daemon — so the MC server's
// shutdown hook saves the world before the container goes away; a plain
// force-remove would SIGKILL it and lose unsaved data (issue #336). A stop
// failure does not leak the container: the remove runs regardless, and the stop
// error is surfaced in the joined result. Exited/created orphans are
// force-removed directly (no graceful stop). Removal/stop errors for individual
// containers are returned joined so the caller can log them, but a partial sweep
// does not block startup.
//
// Each daemon call runs under a per-call deadline derived from ctx so a wedged
// daemon cannot block worker startup indefinitely (issue #338): the startup Sweep
// is invoked with context.Background() and the EngineClient has no http.Client
// timeout, so without these bounds a hung daemon would hang startup forever. The
// graceful stop gets StopTimeout + a margin (the daemon needs the full grace to
// escalate to SIGKILL); list/remove get the margin alone. A healthy daemon
// answers each call well inside its deadline, so the bound never fires on the
// success path.
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
			// This bare docker.Stop is the orphan-sweep stop leg observed in the #927
			// incident (a redeploy sweep stopped a running 26.x server, then the stop-leg
			// snapshot found unpadded regions). The daemon-internal SIGTERM→SIGKILL
			// escalation here is NOT directly observable — unlike the supervised stop in
			// instance.Stop, which emits a WARN on its explicit kill fallback (see
			// Instance.Stop). An elapsed-time/exit-code(137) escalation heuristic at this
			// site is deferred (#927).
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

// labels are attached to every container: a worker-id label scopes the orphan
// sweep, a server-id label identifies the server.
func (d *Driver) labels(serverID string) map[string]string {
	return map[string]string{
		labelWorkerID: d.workerID,
		labelServerID: serverID,
	}
}

// containerWorkDir is the in-container path the working dir is bind-mounted to
// and the server's working directory.
const containerWorkDir = "/data"

// containerCmd builds the in-container command (exec form) from a launch argv:
// the base image provides the `java` binary, so the command is `java` followed by
// the resolved JVM arguments. The argv is built by execution.BuildLaunchPlan
// against the in-container path resolver, so paths are already /data-relative.
func containerCmd(args []string) []string {
	return append([]string{"java"}, args...)
}

// memoryLimitBytes converts the per-server memory ceiling from mebibytes (the
// InstanceSpec unit, issue #706) to bytes for the Docker host-config Memory
// field (issue #707). A zero ceiling stays zero — no constraint.
func memoryLimitBytes(limitMB uint32) int64 {
	return int64(limitMB) * 1024 * 1024
}

// cpuShares converts the per-server CPU allocation from millicores (the
// InstanceSpec unit, issue #723) to a Docker relative CPU weight (issue #724),
// at Docker's baseline of 1024 shares = 1 core: round(millis * 1024 / 1000) (so
// 2000m → 2048). An unset allocation (0) keeps the fixed default weight
// (gameServerCPUShares), preserving the pre-#724 behavior for existing servers.
// The result is a SOFT share that only arbitrates contention; no hard CPU quota
// (NanoCpus) is set.
func cpuShares(cpuMillis uint32) int64 {
	if cpuMillis == 0 {
		return gameServerCPUShares
	}
	return (int64(cpuMillis)*1024 + 500) / 1000
}

// instance is one running container. Across a Forge install+launch it owns two
// containers in succession (the install container, then the launch container);
// containerID is the current one, guarded by mu (issue #305).
type instance struct {
	spec        execution.InstanceSpec
	docker      dockerAPI
	containerID string
	// image/network/gameBindIP/labels/createFn carry what superviseInstall needs to
	// create the launch container after the install container exits (issue #305).
	image       string
	network     string
	gameBindIP  string
	labels      map[string]string
	createFn    func(ctx context.Context, create CreateSpec) (string, error)
	openControl controlFunc
	// rconHost is the host the graceful-stop RCON connection dials: empty for the
	// host loopback, the container name when a user-defined network is configured.
	rconHost    string
	stopTimeout time.Duration
	// readinessTimeout bounds the hold-on-starting wait before falling back to
	// running (issue #345).
	readinessTimeout time.Duration
	// logger records the graceful-stop -> kill escalation at WARN (issue #927), so a
	// stop that timed out (leaving the world's regions unpadded for the stop-leg
	// snapshot) is diagnosable. Never nil: Start copies the Driver's logger, which
	// defaults to a discard handler.
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

	// beforeLaunch is a test-only hook fired inside superviseInstall after the
	// launch container is created but immediately before the latch-check-and-start
	// critical section, so a test can drive a Stop into the exact
	// install-exit→launch window the section must close (issue #306). Nil in
	// production.
	beforeLaunch func()

	// beforeRetryStart is a test-only hook fired inside superviseInstall after the
	// retry install container is created but before the latch-check-and-start
	// critical section, so a test can drive a Stop into the exact retry-setup
	// window (issue #1987). Nil in production.
	beforeRetryStart func()

	// beforeReadyPublish is a test-only hook fired inside awaitReady after
	// WaitReady returns success but before taking the lock to publish running,
	// so a test can drive a Stop into the exact ready-publish window the guard
	// must close (issue #2022). Nil in production.
	beforeReadyPublish func()

	// beforeSurvivedReset is a test-only hook fired inside Stop after the post-kill
	// confirm wait times out but before re-acquiring the lock to reset the latch, so
	// a test can drive the container exit (and supervise) into the exact window the
	// survived-kill restore must not stomp (issue #392). Nil in production.
	beforeSurvivedReset func()

	// metricsMu guards the cached metrics RCON connection reused across player-count
	// samples (issue #1622). It is a dedicated lock — never i.mu — held only for the
	// duration of a sample's dial+"list" or the terminal close, so a slow RCON round
	// trip cannot stall the state machine (Status/Stop/supervise). The connection is
	// kept separate from the console-command and graceful-stop RCON paths (each of
	// which dials its own transient connection), so no cross-path locking arises.
	metricsMu sync.Mutex
	// metricsControl is the persistent RCON connection player-count sampling reuses;
	// nil before the first dial and after a poisoned connection is discarded. The
	// rcon client is not safe for concurrent use and poisons itself on any Execute
	// error, so access is serialized by metricsMu and a broken connection is
	// discarded to force a redial on the next sample.
	metricsControl execution.ServerControl
	// metricsClosed latches once the instance terminates and closeMetricsControl has
	// run, so a late in-flight sample does not redial a connection nobody would
	// close.
	metricsClosed bool

	mu       sync.Mutex
	state    execution.ServerState
	stopping bool
	// stopRequested is a sticky record that a Stop was ever requested. Unlike
	// stopping (which the survived-kill failure path resets so a retry re-runs the
	// escalation, issue #253), it is never cleared, so supervise reports the
	// eventual exit as stopped — not a spurious crash — even when the container
	// survived the kill window and then died after the latch reset (issue #257).
	stopRequested bool
	// exitObserved is set by supervise under the lock the moment it observes the
	// container exit, before recording the terminal state. The survived-kill restore
	// checks it under the same lock and skips the reset when set, so it cannot stomp
	// a terminal state supervise reached during the post-kill wait window (#392).
	exitObserved bool
	closed       bool
	// terminalLatched is set by emit the first time a terminal state is published.
	// Once latched, emit drops any later non-terminal event: awaitReady's running
	// and Stop's stopping can race past supervise's stopped|crashed, and a
	// latest-wins consumer would otherwise transiently report a dead container as
	// running/stopping. The latch is read and set under i.mu, which emit already
	// holds, so the terminal emit and these post-terminal emits are serialized
	// (issue #835).
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

// beginLaunch wires the launch container's log capture, marks the instance
// running, and starts the exit supervisor. It is the shared tail of a direct
// launch and a post-install launch, so a Forge install+launch reaches running
// through the same path as a plain start (issue #305).
func (i *instance) beginLaunch(id string) {
	i.setContainerID(id)
	i.beginLaunchTail(id)
}

// beginLaunchTail wires log capture, starts the exit supervisor, and holds
// StateStarting until the server reports readiness (the startup-complete log
// marker) before transitioning to running (issue #345). superviseInstall calls
// it after publishing and starting the launch under the latch lock, so the
// publish and the stopping re-check stay one critical section (issue #306).
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

// awaitReady holds StateStarting until the server's startup-complete log marker
// appears (RCON is listening by then), the readiness fallback elapses, or the
// container exits first; only the first two transition to running (issue #345).
// The transition is gated under the lock on the instance still being in
// StateStarting, so a container that crashed while booting (supervise set
// crashed) or a Stop that latched stopping is never overwritten with running.
func (i *instance) awaitReady() {
	if !execution.WaitReady(i.logPump.Ready(), i.exited, i.readinessTimeout) {
		return // the container exited first; supervise owns the terminal state.
	}
	if i.beforeReadyPublish != nil {
		i.beforeReadyPublish()
	}
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.state != execution.StateStarting {
		return
	}
	i.state = execution.StateRunning
	i.emitLocked(execution.StateRunning, "")
}

// superviseInstall waits for the supervised install container to exit, captures
// its output to logs/forge-install.log, then creates+starts the launch container
// as the SAME instance (issue #305). On a non-zero install exit the instance goes
// crashed and no launch container is created; a Stop that terminated the install
// container reports stopped. The install container is removed once its fate is
// decided (in every terminal branch, and after the launch is published): keeping
// it as the current container until then gives a concurrent Stop a valid target
// through the install-exit→launch handoff window (issue #306). Its distinct name
// (mcsd-<id>-install) means the launch create never contends with it.
//
// Retry-publish invariant (issue #1987): the retry container is created outside
// the lock, then {stop re-check, docker.Start, containerID publish,
// exitObserved reset} form one critical section — mirroring the install→launch
// handoff (issue #306). This prevents Stop from capturing a stale containerID
// and returning false success against a removed container while the retried
// installer keeps running.
func (i *instance) superviseInstall(installID string) {
	// Retry loop: on a non-zero install exit, clean artifacts and re-run the
	// install container up to maxInstallRetries additional times before giving
	// up (issue #1128). Transport-error re-attach is per-attempt (issue #881).
	var waitErr error
	for attempt := 0; ; attempt++ {
		i.captureInstallOutput(installID)

		// Re-attach on transport errors the same way supervise does: a daemon
		// blip does not mean the install container died (issue #881).
		for {
			_, waitErr = i.docker.Wait(context.Background(), installID)
			if waitErr == nil {
				break
			}
			if !isTransportError(waitErr) {
				break
			}
			if i.exitedAfterTransportError(installID) {
				waitErr = nil // container exited; fall through to re-plan (issue #895)
				break
			}
			time.Sleep(waitTransportProbeInterval)
		}

		i.mu.Lock()
		// Mark the install exit observed so the survived-kill restore, re-acquiring
		// the lock, skips its reset rather than stomping the terminal state set
		// below (mirrors supervise's exitObserved guard, issue #595/#392). Read
		// the sticky stop intent: a container that survived the kill window and
		// then died after the latch was reset is still a requested stop, so report
		// stopped (mirrors issue #257).
		i.exitObserved = true
		stopping := i.stopRequested
		i.mu.Unlock()

		if stopping {
			_ = i.docker.Remove(context.Background(), installID)
			i.finishTerminal(execution.StateStopped, "")
			return
		}

		if waitErr == nil {
			break // install succeeded, proceed to re-plan
		}

		// Install failed. Can we retry?
		_ = i.docker.Remove(context.Background(), installID)
		if attempt >= maxInstallRetries {
			i.finishTerminal(execution.StateCrashed,
				fmt.Sprintf("forge install failed after %d attempts: %s", attempt+1, waitErr))
			return
		}

		// Backoff before retry, polling for Stop so a concurrent Stop is
		// observed promptly rather than sleeping the full backoff (issue #1128).
		if i.installBackoffOrStopping(installRetryBackoff[attempt]) {
			i.finishTerminal(execution.StateStopped, "")
			return
		}

		// Re-check stopRequested after backoff: a Stop may have arrived in the
		// window between Remove and the backoff start that was too late for
		// the backoff polling to observe (issue #1128). Use the sticky
		// stopRequested, consistent with installBackoffOrStopping (issue #1442).
		i.mu.Lock()
		stopping = i.stopRequested
		i.mu.Unlock()
		if stopping {
			i.finishTerminal(execution.StateStopped, "")
			return
		}

		// Clean stale artifacts and create (but do not start) the retry container
		// outside the lock (issue #1127). The latch re-check, Start, containerID
		// publish, and exitObserved reset are one critical section — mirroring the
		// install→launch handoff (issue #306) — so a Stop racing this window either
		// wins the lock first (aborting and removing the unstarted container) or
		// blocks for the one Start call and then acts on the already-started retry
		// container. There is therefore no published-but-unstarted sub-window for
		// Stop to mishandle (issue #1987).
		_ = execution.CleanForgeInstallArtifacts(i.spec.WorkingDir)
		newID, err := i.createInstallRetryContainer()
		if err != nil {
			i.finishTerminal(execution.StateCrashed, "forge install retry failed: "+err.Error())
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
			i.finishTerminal(execution.StateCrashed, "forge install retry failed: "+err.Error())
			return
		}
		i.containerID = newID
		i.exitObserved = false
		i.mu.Unlock()
		installID = newID
	}

	// The install exited cleanly. A Stop that arrived after the wait returned
	// but before the re-plan still wins — report stopped and clean up. Read the
	// sticky stopRequested, consistent with the in-loop check (issue #595).
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
		i.finishTerminal(execution.StateCrashed, "forge re-plan after install failed: "+err.Error())
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
			i.finishTerminal(execution.StateCrashed, detail)
			return
		}
		jarPath := containerWorkDir + "/" + rel
		plan = execution.LaunchPlan{LaunchArgs: execution.JarLaunchArgs(i.spec, jarPath)}
	}

	// Create the launch container outside the lock: the create rides the Docker API
	// and may run the #233 wait-for-name-free loop, so it must not block a
	// concurrent Stop. The launch name differs from the install name, so the create
	// does not contend with the still-present install container; the install
	// container is removed only after the launch decision, so until then it remains
	// the current container and a concurrent Stop has a valid target (issue #306).
	// The created launch container is not started yet.
	id, err := i.createLaunchContainer(plan.LaunchArgs)
	if err != nil {
		_ = i.docker.Remove(context.Background(), installID)
		i.finishTerminal(execution.StateCrashed, "forge launch after install failed: "+err.Error())
		return
	}

	if i.beforeLaunch != nil {
		i.beforeLaunch()
	}

	// The latch re-check, the publish, and the start are one critical section
	// (issue #306). Holding the lock across the single docker start (not the
	// expensive create above) is the container analogue of the now-removed host-process driver
	// holding the lock across cmd.Start: a Stop racing this window either wins the
	// lock first — observed below, aborting and removing the unstarted launch
	// container — or blocks for the one start call and then acts on the
	// already-started launch. There is therefore no published-but-unstarted
	// sub-window for Stop to mishandle.
	i.mu.Lock()
	if i.stopping {
		// A Stop won the latch after the install exited but before the launch
		// started. The current container is still the (exited) install container, so
		// Stop acts on a valid target; remove the unstarted launch container we
		// created and the install container, then report stopped. Stop's waitExit is
		// released by finishTerminal closing i.exited (issue #306).
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

// createLaunchContainer creates (but does not start) the launch container after a
// successful install, reusing the driver's create helper through the captured
// fields. Starting is deferred to the latch-guarded critical section so a Stop can
// abort the launch before it starts (issue #306).
func (i *instance) createLaunchContainer(launchArgs []string) (string, error) {
	gamePort, rconPort := ports(i.spec.WorkingDir)
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
	return i.createFn(context.Background(), create)
}

// installBackoffOrStopping sleeps for d in small increments, checking the
// sticky stopRequested flag between each tick so a concurrent Stop is observed
// within one tick rather than after the full delay. Returns true if a stop was
// requested (the caller should abort), false if the full delay elapsed.
// stopRequested is used instead of the transient stopping flag because a Stop
// whose kill fails or is survived clears stopping but leaves stopRequested set
// (issue #1442).
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

// createInstallRetryContainer creates (but does not start) a new install
// container for a retry attempt, reusing the instance's captured fields. Starting
// is deferred to the latch-guarded critical section so a Stop can abort the retry
// before it starts (issue #1987). It mirrors Driver.runInstallContainer's create
// but omits Start, like createLaunchContainer for the install→launch handoff
// (issue #306).
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
	return i.createFn(context.Background(), create)
}

// captureInstallOutput follows the install container's log stream and writes it to
// logs/forge-install.log in the working dir, so an operator can read the install
// diagnostics via the files API (issue #305). It is best-effort: a failure to open
// the stream or the log file leaves the file empty/absent rather than failing the
// install (the install's own exit code is the authority on success).
func (i *instance) captureInstallOutput(installID string) {
	rc, err := i.docker.Logs(context.Background(), installID)
	if err != nil {
		return
	}
	defer func() { _ = rc.Close() }()

	logPath := filepath.Join(i.spec.WorkingDir, filepath.FromSlash(execution.ForgeInstallLogRelpath))
	if err := os.MkdirAll(filepath.Dir(logPath), 0o750); err != nil {
		return
	}
	f, err := os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o640) //nolint:gosec // logPath is the server's own working dir, not user-controlled.
	if err != nil {
		return
	}
	defer func() { _ = f.Close() }()
	demuxLogsTo(rc, f)
}

// finishTerminal records a terminal state reached during the install phase (no
// launch container started), emits it, and closes the event/exited channels so
// the manager's pump and any in-flight Stop wait observe the end (issue #305).
func (i *instance) finishTerminal(state execution.ServerState, detail string) {
	i.set(state)
	i.emit(state, detail)
	close(i.exited)
	i.logPump.Close()
	i.mu.Lock()
	i.closed = true
	close(i.events)
	i.mu.Unlock()
	// Release the cached metrics RCON connection so it never outlives the instance
	// (issue #1622). Closing i.events above stops the metrics pump, so this cannot
	// contend with a sample for long.
	i.closeMetricsControl()
}

func (i *instance) Status() execution.ServerState {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.state
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

// Sample reads a one-shot resource sample from the Engine stats endpoint
// (execution.StatsSource, FR-MON-3). An error (daemon unreachable, container
// gone) makes the manager fall back to an up-only sample. Player count is
// queried best-effort via RCON "list"; a failure leaves it zero (issue #1068).
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

// queryPlayerCount reuses a persistent per-instance RCON connection and sends
// "list" to get the online player count. Dialing once and reusing the connection
// across samples keeps the vanilla server from logging a "Thread RCON Client
// started/shutting down" pair on every metrics tick (issue #1622).
//
// It is best-effort: any error (RCON not ready, dial failure, unparseable
// response) returns 0 so the metrics sample is still emitted with honest zeroes
// rather than failing the whole sample. The rcon client poisons itself on any
// Execute error, so a failed sample discards the connection and the next sample
// redials a fresh one — at most one redial per tick, never a hot loop. Access is
// serialized by metricsMu so the terminal close (closeMetricsControl) cannot race
// a sample.
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

// closeMetricsControl closes the cached metrics RCON connection (if any) and
// latches metricsClosed so a late in-flight sample does not redial a connection
// nobody would close. Called from the terminal paths so the connection never
// outlives the instance (issue #1622).
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
	// Record the stop intent stickily so supervise reports the eventual exit as
	// stopped even if the survived-kill failure path later clears stopping (#257).
	i.stopRequested = true
	// Capture the pre-stop state before overwriting it with stopping: the
	// survived-kill failure path (below) restores it rather than hardcoding running,
	// so a stop escalation that hits the survived-kill error while the instance is
	// still starting — Stop is reachable from starting since the readiness gating of
	// issue #350 holds starting through the MC boot — does not relabel a still-booting
	// container as running and misreport it to the control plane (issue #352).
	prior := i.state
	i.state = execution.StateStopping
	// Capture the current container under the same lock that latches stopping, so
	// the install→launch handoff (which only proceeds when stopping is unset)
	// cannot race this read: Stop acts on whichever container is current, and a
	// concurrent install supervisor sees stopping set and launches nothing (#305).
	id := i.containerID
	i.mu.Unlock()
	i.emit(execution.StateStopping, "")

	// Once a stop has begun, detach the escalation from the caller's context. The
	// graceful stop usually runs on a per-server lane whose ctx is the gRPC
	// session stream's serveCtx; if the stream drops mid-stop, a cancelled ctx
	// would fail the docker Stop/Kill HTTP calls and the waits immediately, so a
	// still-healthy, still-stopping container gets recorded as a failed-stop
	// orphan — spurious churn that blocks start/hydrate until a retried stop
	// clears it (issue #770). Decouple instead: run the escalation against a
	// detached context bound by stopDeadline so the container keeps its full
	// grace period regardless of the caller, while the bound still caps a hung
	// daemon call. The post-Kill confirm (waitExitDone) already ignores caller
	// cancellation, so it stays as is.
	stopCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), i.stopDeadline())
	defer cancel()

	// Always flush before stop on the graceful path (#1007/#1008): MC's own
	// shutdown save (via RCON "stop") does NOT reliably flush dirty region
	// chunks when a player was connected — observed on MC 26.1.2 with
	// relay/tunnel connections. The flush (RCON save-all + settleWorkingSet)
	// ensures chunks are on disk BEFORE the process terminates.
	// Run on a detached context so it does not consume the stopDeadline.
	//
	// When the flush succeeds (returns true), RCON "stop" and docker stop
	// (SIGTERM) are SKIPPED: both trigger MC's own shutdown save, which
	// re-writes ALL loaded region files. If that save is interrupted by a kill
	// (the MC 26.1.2 shutdown-hang observed with relay/tunnel connections), the
	// half-written regions overwrite the safely-flushed data and corrupt the
	// post-stop snapshot — the root cause of the world-rollback bug. SIGKILL
	// cannot be intercepted, so the flushed files stay intact.
	flushed := false
	if graceful && len(preFallback) > 0 && preFallback[0] != nil {
		flushCtx, flushCancel := context.WithTimeout(context.WithoutCancel(stopCtx), 90*time.Second)
		flushed = preFallback[0](flushCtx)
		flushCancel()
	}

	if !flushed {
		if graceful && i.tryRCONStop(stopCtx) && i.waitExit(stopCtx, i.stopTimeout) {
			return nil
		}

		if err := i.docker.Stop(stopCtx, id, i.stopTimeout); err == nil && i.waitExit(stopCtx, i.stopTimeout) {
			return nil
		}
	}

	// The stop did not confirm the container's exit within the stop timeout, so it is
	// escalating to a direct kill. This is exactly the case where Minecraft's shutdown
	// may not have finished sector-padding its region files, so the stop-leg snapshot
	// captures an unpadded (live-format) world — diagnosable here at WARN with the
	// server id and the timeout, so an unpadded-scratch cause traces back to a stop
	// timeout (issue #927). The message distinguishes the two arrival paths: only the
	// graceful path actually attempted (and timed out) a clean shutdown, while a
	// forced stop (graceful=false) skips RCON/docker-stop by design and kills
	// directly, so "graceful stop timed out" would misdescribe it.
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
		// The kill call itself errored (e.g. a hung or erroring daemon), so this Stop
		// failed without confirming the container died. Reset the stopping latch (and
		// restore the pre-stop state) so a retried Stop re-runs the full sequence
		// instead of short-circuiting on the entry guard and returning a false nil
		// success — which would otherwise let the API remove the orphan record and GC
		// the authoritative-stop scratch (#766) while the container may still be alive
		// (issue #816). Mirror the survived-kill reset (#253): keep the same
		// exitObserved guard, since the container can exit concurrently and supervise
		// then owns the terminal state (#392), and leave stopRequested sticky so a
		// later exit is recorded stopped rather than a spurious crash (#257).
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
	// Confirm the kill actually terminated the container. A container that survives
	// docker kill leaves this final wait timing out; report it as a stop failure so
	// the manager reports the command failed, the API keeps the assignment, and the
	// reconciler retries (issue #211). Reporting success here would let the API
	// unassign while the container lingers. The instance was already evicted from
	// the manager's map (handleStop's take()), so the linger is owned by the startup
	// sweep and the reconciler, not re-tracked here.
	//
	// This wait does not honor ctx cancellation: the kill is already issued, so a
	// cancelled caller context must not be read as a lingering container when the
	// container did in fact exit. Only the timeout means it survived.
	if !i.waitExitDone(i.stopTimeout) {
		if i.beforeSurvivedReset != nil {
			i.beforeSurvivedReset()
		}
		// The container survived the kill, so this Stop failed but the container is
		// still alive. Reset the stopping latch (and the recorded state back to its
		// pre-stop value, since the container is still alive) so a subsequent Stop
		// re-runs the full graceful→docker stop→docker kill→confirm sequence instead
		// of short-circuiting on the entry guard and returning a false success (issue
		// #253). Restoring the prior state rather than hardcoding running keeps a
		// still-starting instance labelled starting (issue #352). The reset is
		// confined to this failure path: a successful stop keeps stopping latched so
		// concurrent stops still dedupe.
		//
		// But the container can exit during the wait above, between waitExitDone
		// timing out and re-acquiring the lock: supervise then sets a terminal state
		// and the reset would stomp it back to prior, misreporting a dead container as
		// alive (issue #392). Skip the reset entirely when supervise has observed the
		// exit — the container is gone, supervise owns the terminal state, and there
		// is nothing to retry. stopRequested stays set regardless, so supervise
		// records stopped rather than a spurious crash (issue #257).
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

// stopDeadline bounds the detached escalation context (issue #770). The
// graceful path runs an RCON wait (stopTimeout), a docker Stop whose daemon-side
// SIGTERM grace is itself stopTimeout, a post-Stop wait (stopTimeout), then a
// docker Kill — so 3*stopTimeout plus a grace covers the whole sequence without
// cutting an in-progress stop short, while still capping a hung daemon call.
func (i *instance) stopDeadline() time.Duration {
	return 3*i.stopTimeout + stopDeadlineGrace
}

// waitExitDone reports whether the container reached a terminal state within d,
// observing only the exit and the timeout (not caller-context cancellation). It
// confirms a kill terminated the container (issue #211).
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

// rconStopDeadline gives the RCON phase its own sub-budget within the detached
// stopCtx. stopCtx carries the whole escalation deadline, so rcon's own 30s
// fallback never fires there; a pathological peer could otherwise drag the
// exchange to just under the full budget and leave the docker Stop/Kill waits
// near-zero grace (the #703-adjacent shape, issue #832). Cap the phase at
// min(rconPhaseCap, remaining/3): the /3 reserves the bulk of the remaining
// budget for the escalation steps that follow, and the cap keeps the phase short
// on a large budget.
func rconStopDeadline(ctx context.Context) time.Time {
	phase := rconPhaseCap
	if deadline, ok := ctx.Deadline(); ok {
		if third := time.Until(deadline) / 3; third < phase {
			phase = third
		}
	}
	return time.Now().Add(phase)
}

// tryRCONStop opens RCON and sends "stop", reporting whether the in-band stop was
// issued successfully. A failure returns false so Stop falls back to `docker
// stop`. The exchange runs under a phase deadline so a hung peer cannot consume
// the whole stop budget (issue #832).
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

// supervise blocks on the container exit and emits the terminal state: stopped
// when a stop was requested, crashed otherwise (FR-SRV-4). It removes the
// container afterwards and closes the event and log streams.
//
// A Wait TRANSPORT error (a daemon restart/blip) is not a confirmed exit: the
// container may still be running, so emitting a terminal here would wrongly
// report a live server as crashed and — once the terminal latch (issue #835)
// fires — permanently suppress the later running emit (issue #865). Instead the
// loop re-inspects the container with a bounded poll: a still-running container
// re-attaches a waiter and supervision continues; a gone container falls through
// to the terminal emit; a daemon still unreachable past the bound re-attaches a
// waiter too, emitting nothing so the manager's last authoritative state stands
// until the next observation. A confirmed exit (clean Wait, or a non-transport
// error such as a 404 the inspect also resolves as gone) breaks the loop
// directly.
func (i *instance) supervise() {
	id := i.currentContainerID()

	var waitErr error
	for {
		_, waitErr = i.docker.Wait(context.Background(), id)
		if waitErr == nil || !isTransportError(waitErr) || i.exitedAfterTransportError(id) {
			break
		}
		// Throttle re-attach so back-to-back transport errors do not hot-spin
		// against the daemon socket (issue #881).
		time.Sleep(waitTransportProbeInterval)
	}

	i.mu.Lock()
	// Mark the exit observed before recording the terminal state so the
	// survived-kill restore, re-acquiring the lock, skips its reset rather than
	// stomping the terminal state set below (issue #392). Read the sticky stop
	// intent here too: a container that survived the kill window and then died after
	// the latch was reset is still a requested stop, so report stopped (issue #257).
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

	// Release the cached metrics RCON connection now the container is gone so it
	// does not leak an fd into the next container (issue #1622). This runs after the
	// container removal above so it never delays a restart; closing i.events stops
	// the metrics pump, so any in-flight sample unblocks and this acquires the lock
	// without waiting out a full sample.
	i.closeMetricsControl()
}

// isTransportError reports whether err from a docker call is a transport-level
// failure (daemon restart/blip, socket gone) rather than a daemon HTTP response.
// A non-2xx response is carried by statusError; everything else returned from a
// docker call is a transport failure. supervise uses this to distinguish a Wait
// daemon blip (re-inspect) from a confirmed exit or a 404-gone (terminal).
func isTransportError(err error) bool {
	if err == nil {
		return false
	}
	var status statusError
	return !errors.As(err, &status)
}

// exitedAfterTransportError re-inspects the container after a Wait transport
// error to learn its real state, bounding the daemon-unreachable case with a
// poll deadline (issue #865). It returns true when the container is confirmed
// gone or exited (supervise should emit the terminal), and false when it is
// still running or the daemon stays unreachable past the deadline (supervise
// should re-attach a waiter and keep supervising, emitting nothing). errNotFound
// (a 404) means the container is gone; any other inspect error is treated as the
// daemon still being unreachable and is retried until the deadline. Each Inspect
// call carries a context derived from the probe deadline so a wedged-but-
// connected daemon cannot hold a single Inspect call past the bound (issue #881).
func (i *instance) exitedAfterTransportError(id string) bool {
	deadline := time.Now().Add(waitTransportProbeDeadline)
	for {
		ctx, cancel := context.WithDeadline(context.Background(), deadline)
		info, err := i.docker.Inspect(ctx, id)
		cancel()
		switch {
		case errors.Is(err, errNotFound):
			return true
		case err == nil && !info.Running:
			return true
		case err == nil && info.Running:
			return false
		}
		// Daemon still unreachable: retry until the deadline, then re-attach a
		// waiter rather than guess a terminal.
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(waitTransportProbeInterval)
	}
}

func (i *instance) set(s execution.ServerState) {
	i.mu.Lock()
	i.state = s
	i.mu.Unlock()
}

// emit publishes a status event without ever blocking supervision. When the
// buffer is full it coalesces latest-state-wins: the oldest buffered event is
// discarded to make room for this one, mirroring the manager's coalescing (issue
// #96) so the terminal event is never dropped (issue #790). It returns silently
// once the stream is closed.
//
// A terminal state latches: once one is emitted, any later non-terminal event is
// dropped. awaitReady's running and Stop's stopping can be emitted after
// supervise's stopped|crashed (the terminal emit is not guaranteed to be the last
// call), and a latest-wins consumer would otherwise transiently report a dead
// container as running/stopping (issue #835).
func (i *instance) emit(state execution.ServerState, detail string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.emitLocked(state, detail)
}

// emitLocked is the lock-free core of emit. Caller must hold i.mu.
func (i *instance) emitLocked(state execution.ServerState, detail string) {
	if i.closed {
		return
	}
	if i.terminalLatched && !isTerminal(state) {
		return
	}
	if isTerminal(state) {
		i.terminalLatched = true
	}
	ev := execution.StatusEvent{ServerID: i.spec.ServerID, State: state, Detail: detail}
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

// ports reads the server's game and RCON ports from its working-dir
// server.properties, falling back to the Minecraft defaults when the file is
// absent or a key is unset. Start publishes the game port on the configured host
// interface and RCON on loopback. Keep the game-port resolution in sync with
// tunnel.gamePort (adapters/tunnel/tunnel.go), which dials the published port.
func ports(workingDir string) (game, rcon string) {
	props := readProperties(filepath.Join(workingDir, "server.properties"))
	game = props["server-port"]
	if game == "" {
		game = defaultGamePort
	}
	rcon = props["rcon.port"]
	if rcon == "" {
		rcon = defaultRCONPort
	}
	return game, rcon
}
