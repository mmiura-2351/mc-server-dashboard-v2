// Package containerdriver implements the execution.ExecutionDriver Port by
// running a server inside a Docker container (FR-EXE-2, FR-EXE-4). The Docker
// Engine interaction sits behind the narrow dockerAPI seam so unit tests run
// against a fake and no Docker daemon is needed in CI; the real client is a
// hand-rolled HTTP-over-unix-socket adapter (dockerclient.go), keeping the
// dependency tree empty as the RCON client did (docs/dev/DEPENDENCIES.md).
//
// Lifecycle parity with the host-process driver: a successful create+start
// transitions starting→running immediately (M1 readiness posture; log-based
// "Done" detection is FR-MON-2). The container exiting while no Stop is in
// flight is a crash (StateCrashed, FR-SRV-4); an exit during a Stop is a clean
// StateStopped.
//
// Stop semantics (ARCHITECTURE.md Section 5.2): a graceful stop prefers the
// in-band RCON "stop" command (reusing the ServerControl seam), then falls back
// to `docker stop` (SIGTERM with a timeout, escalating to SIGKILL inside the
// daemon), then a direct `docker kill`. A forced stop skips the RCON step.
//
// Resource limits (CPU/memory quotas) are deferred to M2+ (REQUIREMENTS.md
// Section 2.2); this milestone sets none.
package containerdriver

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
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

// defaultConflictPollInterval and defaultConflictDeadline bound the
// wait-for-name-free loop createContainer runs on a create name conflict: it
// polls every interval until the deadline for the deterministic name to free as
// the async exit-watcher finishes the previous container's teardown (issue #233).
const (
	defaultConflictPollInterval = 250 * time.Millisecond
	defaultConflictDeadline     = 10 * time.Second
)

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
	}
}

// RconHost returns the host that RCON for serverID is dialed at. It is empty when no
// network is configured (the caller falls back to the host loopback), and the
// container name when a user-defined network is configured: the network's
// container-name DNS resolves it, so RCON is reached over the network rather than
// the unreachable host loopback (issue #218).
func (d *Driver) RconHost(serverID string) string {
	if d.network == "" {
		return ""
	}
	return containerName(serverID)
}

// Start resolves the base image, creates a container bind-mounting the working
// dir and publishing the game/RCON ports, starts it, and returns the running
// Instance. It emits starting then running; a successful return means the
// container is started.
func (d *Driver) Start(ctx context.Context, spec execution.InstanceSpec) (execution.Instance, error) {
	image, err := d.images.Select(spec.MinecraftVersion)
	if err != nil {
		return nil, fmt.Errorf("containerdriver: select image: %w", err)
	}

	gamePort, rconPort := ports(spec.WorkingDir)
	// The game port binds to the configured host interface (driver.container.
	// game_bind_ip) so players can reach the server.
	portMappings := []PortMapping{
		{ContainerPort: gamePort, HostIP: d.gameBindIP, HostPort: gamePort},
	}
	// RCON publication depends on the topology. With no network configured (bare-
	// metal / host-process parity) RCON is published on the host loopback and
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
		Name:       containerName(spec.ServerID),
		Image:      image,
		Cmd:        serverCmd(spec),
		WorkingDir: containerWorkDir,
		Binds:      []string{spec.WorkingDir + ":" + containerWorkDir},
		Ports:      portMappings,
		Network:    d.network,
		Labels:     d.labels(spec.ServerID),
	}

	id, err := d.createContainer(ctx, create)
	if err != nil {
		return nil, fmt.Errorf("containerdriver: create container: %w", err)
	}
	if err := d.docker.Start(ctx, id); err != nil {
		// Best-effort cleanup of the created-but-unstarted container.
		_ = d.docker.Remove(ctx, id)
		return nil, fmt.Errorf("containerdriver: start container: %w", err)
	}

	logCtx, logCancel := context.WithCancel(context.Background())
	inst := &instance{
		spec:        spec,
		docker:      d.docker,
		containerID: id,
		openControl: d.openControl,
		rconHost:    d.RconHost(spec.ServerID),
		stopTimeout: d.stopTimeout,
		events:      make(chan execution.StatusEvent, 8),
		exited:      make(chan struct{}),
		state:       execution.StateStarting,
		logPump:     execution.NewLogPump(spec.ServerID, logBufferLines),
		logCancel:   logCancel,
	}
	// Follow the container's multiplexed log stream into the per-instance pump.
	// The follow is bound to logCtx so supervise can end it on container exit;
	// supervise then waits on logWG before closing the pump (FR-MON-2).
	inst.logWG.Add(1)
	go inst.captureLogs(logCtx)

	inst.emit(execution.StateStarting, "")
	inst.set(execution.StateRunning)
	inst.emit(execution.StateRunning, "")

	go inst.supervise()
	return inst, nil
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
	id, err := d.docker.Create(ctx, create)
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
			// The name is free; retry the create.
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
// host-process driver's posture (drop-oldest + dropped-count marker, issue #96).
const logBufferLines = 256

// Sweep removes leftover containers labelled for this Worker, recovering from a
// crash that left a server's container running or stopped. It is called once at
// startup before any server is launched; the deterministic name plus the
// worker-id label scope it to this Worker's own containers so it never touches
// unrelated ones. Removal errors for individual containers are returned joined so
// the caller can log them, but a partial sweep does not block startup.
func (d *Driver) Sweep(ctx context.Context) error {
	containers, err := d.docker.List(ctx, labelWorkerID, d.workerID)
	if err != nil {
		return fmt.Errorf("containerdriver: list orphans: %w", err)
	}
	var errs []error
	for _, c := range containers {
		if err := d.docker.Remove(ctx, c.ID); err != nil {
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

// serverCmd builds the JVM command line run inside the container. Heap flags come
// from MemoryMB when set; the JAR path is resolved against the in-container work
// dir and run headless (nogui). The base image provides the `java` binary.
func serverCmd(spec execution.InstanceSpec) []string {
	cmd := []string{"java"}
	if spec.MemoryMB > 0 {
		heap := fmt.Sprintf("%dM", spec.MemoryMB)
		cmd = append(cmd, "-Xms"+heap, "-Xmx"+heap)
	}
	cmd = append(cmd, "-jar", filepath.Join(containerWorkDir, spec.JarRelpath), "nogui")
	return cmd
}

// instance is one running container.
type instance struct {
	spec        execution.InstanceSpec
	docker      dockerAPI
	containerID string
	openControl controlFunc
	// rconHost is the host the graceful-stop RCON connection dials: empty for the
	// host loopback, the container name when a user-defined network is configured.
	rconHost    string
	stopTimeout time.Duration

	events chan execution.StatusEvent
	// exited is closed by supervise once the container has reached a terminal
	// state; waitExit selects on it.
	exited chan struct{}

	// logPump captures the demuxed container log stream; logWG tracks the capture
	// goroutine and logCancel ends its follow on container exit.
	logPump   *execution.LogPump
	logWG     sync.WaitGroup
	logCancel context.CancelFunc

	mu       sync.Mutex
	state    execution.ServerState
	stopping bool
	closed   bool
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
func (i *instance) captureLogs(ctx context.Context) {
	defer i.logWG.Done()
	rc, err := i.docker.Logs(ctx, i.containerID)
	if err != nil {
		return
	}
	defer func() { _ = rc.Close() }()
	demuxLogs(rc, i.logPump)
}

// Sample reads a one-shot resource sample from the Engine stats endpoint
// (execution.StatsSource, FR-MON-3). An error (daemon unreachable, container
// gone) makes the manager fall back to an up-only sample.
func (i *instance) Sample(ctx context.Context) (execution.MetricsSample, error) {
	stats, err := i.docker.Stats(ctx, i.containerID)
	if err != nil {
		return execution.MetricsSample{}, err
	}
	return execution.MetricsSample{
		ServerID:    i.spec.ServerID,
		CPUMillis:   stats.CPUMillis,
		MemoryBytes: stats.MemoryBytes,
	}, nil
}

// Stop ends the instance. A graceful stop tries RCON "stop", then `docker stop`
// (which SIGTERMs then SIGKILLs after stopTimeout inside the daemon), then a
// direct `docker kill`; a forced stop skips the RCON step. Any terminal state
// makes Stop a prompt no-op success.
func (i *instance) Stop(ctx context.Context, graceful bool) error {
	i.mu.Lock()
	if i.stopping || isTerminal(i.state) {
		i.mu.Unlock()
		return nil
	}
	i.stopping = true
	i.state = execution.StateStopping
	i.mu.Unlock()
	i.emit(execution.StateStopping, "")

	if graceful && i.tryRCONStop(ctx) && i.waitExit(ctx, i.stopTimeout) {
		return nil
	}

	if err := i.docker.Stop(ctx, i.containerID, i.stopTimeout); err == nil && i.waitExit(ctx, i.stopTimeout) {
		return nil
	}

	if err := i.docker.Kill(ctx, i.containerID); err != nil {
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
		// The container survived the kill, so this Stop failed but the container is
		// still alive. Reset the stopping latch (and the recorded state back to
		// running, since the container is still alive) so a subsequent Stop re-runs
		// the full graceful→docker stop→docker kill→confirm sequence instead of
		// short-circuiting on the entry guard and returning a false success (issue
		// #253). The reset is confined to this failure path: a successful stop or a
		// terminal state keeps stopping latched so supervise reports the eventual
		// exit as stopped and concurrent stops still dedupe. supervise has not run
		// here (the container has not exited), so clearing stopping is safe.
		i.mu.Lock()
		i.stopping = false
		i.state = execution.StateRunning
		i.mu.Unlock()
		return fmt.Errorf("containerdriver: container survived docker kill after %s", i.stopTimeout)
	}
	return nil
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

// tryRCONStop opens RCON and sends "stop", reporting whether the in-band stop was
// issued successfully. A failure returns false so Stop falls back to `docker
// stop`.
func (i *instance) tryRCONStop(ctx context.Context) bool {
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
func (i *instance) supervise() {
	_, waitErr := i.docker.Wait(context.Background(), i.containerID)

	i.mu.Lock()
	stopping := i.stopping
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
	_ = i.docker.Remove(context.Background(), i.containerID)

	i.mu.Lock()
	i.closed = true
	close(i.events)
	i.mu.Unlock()
}

func (i *instance) set(s execution.ServerState) {
	i.mu.Lock()
	i.state = s
	i.mu.Unlock()
}

// emit publishes a status event, dropping it if the stream is closed or full so a
// slow consumer never blocks supervision.
func (i *instance) emit(state execution.ServerState, detail string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.closed {
		return
	}
	select {
	case i.events <- execution.StatusEvent{ServerID: i.spec.ServerID, State: state, Detail: detail}:
	default:
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
// interface and RCON on loopback.
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
