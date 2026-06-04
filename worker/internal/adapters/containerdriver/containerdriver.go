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

// controlFunc opens an execution.ServerControl (RCON) for a server, used for the
// graceful-stop "stop" command. It returns an error when RCON is unavailable; the
// driver then falls back to `docker stop`.
type controlFunc func(ctx context.Context, spec execution.InstanceSpec) (execution.ServerControl, error)

// Options tunes the driver.
type Options struct {
	// WorkerID labels every container so a startup sweep can find and remove this
	// Worker's orphaned containers (crash-orphan recovery).
	WorkerID string
	// StopTimeout bounds the `docker stop` grace period. Zero uses
	// defaultStopTimeout.
	StopTimeout time.Duration
}

// Driver is the container ExecutionDriver.
type Driver struct {
	docker      dockerAPI
	images      *ImageSelector
	openControl controlFunc
	workerID    string
	stopTimeout time.Duration
}

// New builds a container Driver. docker is the Engine seam; images resolves a
// base image from the Minecraft version; openControl opens RCON for graceful
// stop.
func New(docker dockerAPI, images *ImageSelector, openControl controlFunc, opts Options) *Driver {
	timeout := opts.StopTimeout
	if timeout <= 0 {
		timeout = defaultStopTimeout
	}
	return &Driver{
		docker:      docker,
		images:      images,
		openControl: openControl,
		workerID:    opts.WorkerID,
		stopTimeout: timeout,
	}
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
	create := CreateSpec{
		Name:       containerName(spec.ServerID),
		Image:      image,
		Cmd:        serverCmd(spec),
		WorkingDir: containerWorkDir,
		Binds:      []string{spec.WorkingDir + ":" + containerWorkDir},
		// Publish the game and RCON ports to the loopback host interface so the
		// host-side RCON control func reaches the server the same way it does for a
		// host process.
		Ports: []PortMapping{
			{ContainerPort: gamePort, HostIP: "127.0.0.1", HostPort: gamePort},
			{ContainerPort: rconPort, HostIP: "127.0.0.1", HostPort: rconPort},
		},
		Labels: d.labels(spec.ServerID),
	}

	id, err := d.docker.Create(ctx, create)
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
	i.waitExit(ctx, i.stopTimeout)
	return nil
}

// isTerminal reports whether s is a state the container can no longer leave.
func isTerminal(s execution.ServerState) bool {
	return s == execution.StateStopped || s == execution.StateCrashed
}

// tryRCONStop opens RCON and sends "stop", reporting whether the in-band stop was
// issued successfully. A failure returns false so Stop falls back to `docker
// stop`.
func (i *instance) tryRCONStop(ctx context.Context) bool {
	ctrl, err := i.openControl(ctx, i.spec)
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
// absent or a key is unset. Both are published to the host loopback.
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
