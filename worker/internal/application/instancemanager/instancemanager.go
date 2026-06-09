// Package instancemanager is the Worker use case that turns control-plane
// lifecycle/console commands into ExecutionDriver calls and surfaces observed
// state transitions back onto the session (CONTROL_PLANE.md Section 5/6). It
// implements session.CommandHandler. It tracks one running instance per server
// id and owns the per-server working dir under the scratch root.
//
// Working-set posture: HydrateTrigger pulls the server's working set from the
// API data plane into scratchDir/<server_id> before launch; the API issues it
// before StartServer (FR-DATA-4). A server with no published working set yet
// hydrates to an empty dir (the endpoint is 204). SnapshotTrigger pushes the
// working set back. Hydrate/snapshot are long-running and run off the session's
// serial receive loop (issue #95); the session bounds their concurrency.
package instancemanager

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// controlFunc opens an execution.ServerControl (RCON) for a running server,
// used by ServerCommand forwarding. driver is the execution driver that runs the
// server (the one recorded on its StartServer command), so the dial host can be
// resolved per the driver's topology — a container driver with a configured
// network reaches RCON over the network, every other driver over the host
// loopback (issue #218).
type controlFunc func(ctx context.Context, serverID, driver string) (execution.ServerControl, error)

// Transfer is the data-plane Port: move a server's working set between the API's
// authoritative Storage and the local working dir (FR-DATA-3/4). The trigger
// command carries the URL + token; the bytes ride the HTTP data plane, off the
// control-plane stream (CONTROL_PLANE.md Section 5).
type Transfer interface {
	// Hydrate downloads the working set from url into workingDir (an empty/204
	// response leaves it empty).
	Hydrate(ctx context.Context, url, token, workingDir string) error
	// Snapshot packs workingDir and uploads it to url.
	Snapshot(ctx context.Context, url, token, workingDir string) error
}

// systemClock is the default wall-clock used for the metrics ticker when
// WithMetrics injects no other clock. It satisfies session.Clock with stdlib
// time so the application layer stays adapter-free (ARCHITECTURE.md Section 2).
type systemClock struct{}

func (systemClock) Now() time.Time                         { return time.Now() }
func (systemClock) After(d time.Duration) <-chan time.Time { return time.After(d) }
func (systemClock) NewTimer(d time.Duration) session.Timer { return systemTimer{time.NewTimer(d)} }

// systemTimer adapts *time.Timer to session.Timer for the default clock.
type systemTimer struct{ t *time.Timer }

func (t systemTimer) C() <-chan time.Time   { return t.t.C }
func (t systemTimer) Reset(d time.Duration) { t.t.Reset(d) }
func (t systemTimer) Stop()                 { t.t.Stop() }

// defaultMetricsInterval is the metrics-sampling cadence when WithMetrics is not
// wired (or given a non-positive interval). It mirrors a typical heartbeat
// cadence so a server's resource picture stays roughly fresh (FR-MON-3).
const defaultMetricsInterval = 15 * time.Second

// Manager tracks running instances and dispatches commands to their drivers.
type Manager struct {
	drivers     map[string]execution.ExecutionDriver
	scratchDir  string
	openControl controlFunc
	transfer    Transfer
	logger      *slog.Logger

	clock           session.Clock
	metricsInterval time.Duration

	mu        sync.Mutex
	instances map[string]execution.Instance
	// startCmds remembers the StartServer command per running server so a
	// RestartServer (which carries no driver/version) can relaunch with the same
	// spec.
	startCmds map[string]session.Command
	// orphans remembers instances whose driver Stop failed (could not confirm
	// termination, issue #211): take() already evicted them from instances, so a
	// retry stop would otherwise find no tracked instance and return
	// SERVER_NOT_FOUND, which the API's stop convergence reads as "no live process"
	// and unassigns — over a process/container that may still be lingering (issue
	// #251). Keeping the running Instance here lets a retry re-attempt the driver
	// Stop against the same handle and report success only on confirmed
	// termination; until then start/hydrate over the id are rejected as they are
	// for a running server. The instance's status pump clears the record if the
	// orphan finally exits on its own.
	orphans map[string]execution.Instance

	// events/logs/metrics are the merged streams the session forwards. Per-instance
	// pumps fan their events into them (FR-MON-2, FR-MON-3).
	events  chan session.StatusEvent
	logs    chan session.LogEvent
	metrics chan session.MetricsEvent

	// Status coalescing (issue #96): observed_state must converge to the latest
	// state per server even under sink backpressure, so status events are never
	// dropped. When the events sink is full, the newest status for a server
	// replaces any older pending one (latest-state-wins) in pendingStatus, and a
	// single statusDispatcher goroutine drains it into events as the sink admits.
	// coalescing marks a server whose status is being funneled through the
	// dispatcher; while set, every status for that server is routed through the
	// pending slot so a fast-path send can never overtake an in-flight dispatch
	// (order is preserved per server). dirtyStatus is the FIFO of servers awaiting
	// dispatch. statusNotify wakes the dispatcher (capacity 1: a coalesced signal).
	statusMu      sync.Mutex
	pendingStatus map[string]session.StatusEvent
	coalescing    map[string]bool
	dirtyStatus   []string
	statusNotify  chan struct{}
}

// New builds a Manager. drivers maps an advertised driver name to its adapter;
// scratchDir is the working-set root (worker.scratch_dir); openControl opens RCON
// for ServerCommand forwarding.
func New(drivers map[string]execution.ExecutionDriver, scratchDir string, openControl controlFunc) *Manager {
	m := &Manager{
		drivers:         drivers,
		scratchDir:      scratchDir,
		openControl:     openControl,
		logger:          slog.Default(),
		clock:           systemClock{},
		metricsInterval: defaultMetricsInterval,
		instances:       map[string]execution.Instance{},
		startCmds:       map[string]session.Command{},
		orphans:         map[string]execution.Instance{},
		events:          make(chan session.StatusEvent, 32),
		logs:            make(chan session.LogEvent, 256),
		metrics:         make(chan session.MetricsEvent, 32),
		pendingStatus:   map[string]session.StatusEvent{},
		coalescing:      map[string]bool{},
		statusNotify:    make(chan struct{}, 1),
	}
	go m.statusDispatcher()
	return m
}

// WithLogger sets the manager's logger.
func (m *Manager) WithLogger(l *slog.Logger) *Manager {
	m.logger = l
	return m
}

// WithTransfer wires the data-plane Transfer client used by HydrateTrigger /
// SnapshotTrigger. Without it, those commands fail with a transfer error.
func (m *Manager) WithTransfer(t Transfer) *Manager {
	m.transfer = t
	return m
}

// WithMetrics sets the clock and sampling interval for periodic Metrics events
// (FR-MON-3, worker.metrics_interval_seconds). A non-positive interval keeps the
// default; the clock is injectable for deterministic tests.
func (m *Manager) WithMetrics(clock session.Clock, interval time.Duration) *Manager {
	m.clock = clock
	if interval > 0 {
		m.metricsInterval = interval
	}
	return m
}

// Events streams observed state transitions for all managed servers.
func (m *Manager) Events() <-chan session.StatusEvent { return m.events }

// Logs streams captured console output for all managed servers (FR-MON-2).
func (m *Manager) Logs() <-chan session.LogEvent { return m.logs }

// Metrics streams periodic runtime samples for all running servers (FR-MON-3).
func (m *Manager) Metrics() <-chan session.MetricsEvent { return m.metrics }

// Handle dispatches one command (session.CommandHandler).
func (m *Manager) Handle(ctx context.Context, cmd session.Command) session.CommandResult {
	switch cmd.Kind {
	case "StartServer":
		return m.handleStart(ctx, cmd)
	case "StopServer":
		return m.handleStop(ctx, cmd, !cmd.Force)
	case "RestartServer":
		return m.handleRestart(ctx, cmd)
	case "ServerCommand":
		return m.handleServerCommand(ctx, cmd)
	case "HydrateTrigger":
		return m.handleHydrate(ctx, cmd)
	case "SnapshotTrigger":
		return m.handleSnapshot(ctx, cmd)
	case "ReadFile":
		return m.handleReadFile(cmd)
	case "EditFile":
		return m.handleEditFile(cmd)
	case "ListFiles":
		return m.handleListFiles(cmd)
	default:
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: unhandled command %q", cmd.Kind))
	}
}

// handleHydrate pulls the working set into the server's working dir. It is only
// valid when the instance is stopped: hydrating a running server would replace
// the live working set out from under the process. The API issues this before
// StartServer, so the not-running precondition holds on the start path.
func (m *Manager) handleHydrate(ctx context.Context, cmd session.Command) session.CommandResult {
	if m.transfer == nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			"instancemanager: no data-plane transfer client configured")
	}
	m.mu.Lock()
	_, running := m.instances[cmd.ServerID]
	_, orphaned := m.orphans[cmd.ServerID]
	m.mu.Unlock()
	if running {
		return fail(cmd.CommandID, session.CommandErrorInvalidState,
			"instancemanager: cannot hydrate a running server")
	}
	if orphaned {
		// A failed-stop orphan may still be alive; hydrating would replace the
		// working set out from under the lingering process (issue #251).
		return fail(cmd.CommandID, session.CommandErrorInvalidState,
			"instancemanager: cannot hydrate a server with a failed-stop orphan pending termination")
	}

	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	if err := m.transfer.Hydrate(ctx, cmd.TransferURL, cmd.TransferToken, workingDir); err != nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			fmt.Sprintf("instancemanager: hydrate: %v", err))
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// handleSnapshot packs the server's working dir and uploads it. For a running
// server it first issues a non-blocking save-all over RCON (best-effort; a
// failure is logged, not fatal) to initiate a chunk save so the captured copy is
// as fresh as possible (CONTROL_PLANE.md Section 6.9). It deliberately does not
// flush: a synchronous main-thread flush can occupy a tick past max-tick-time and
// trip the server watchdog, crashing a running server (#693).
func (m *Manager) handleSnapshot(ctx context.Context, cmd session.Command) session.CommandResult {
	if m.transfer == nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			"instancemanager: no data-plane transfer client configured")
	}
	m.mu.Lock()
	_, running := m.instances[cmd.ServerID]
	m.mu.Unlock()
	if running {
		m.flushRunning(ctx, cmd.ServerID)
	}

	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	if err := m.transfer.Snapshot(ctx, cmd.TransferURL, cmd.TransferToken, workingDir); err != nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			fmt.Sprintf("instancemanager: snapshot: %v", err))
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// flushRunning issues a non-blocking save-all over RCON before a snapshot of a
// running server: it initiates a chunk save so the captured copy is fresh, but
// does not flush — a synchronous main-thread flush can occupy a tick past
// max-tick-time and trip the server watchdog (#693). Failures are logged, not
// propagated: a snapshot of a not-quite-saved working set is still useful and
// bounded by FR-DATA-5. (Name is historical; this no longer issues a flush.)
func (m *Manager) flushRunning(ctx context.Context, serverID string) {
	ctrl, err := m.openControl(ctx, serverID, m.driverFor(serverID))
	if err != nil {
		m.logger.Warn("snapshot save-all: open rcon failed", "server_id", serverID, "error", err)
		return
	}
	defer func() { _ = ctrl.Close() }()
	if _, err := ctrl.Execute(ctx, "save-all"); err != nil {
		m.logger.Warn("snapshot save-all failed", "server_id", serverID, "error", err)
	}
}

func (m *Manager) handleStart(ctx context.Context, cmd session.Command) session.CommandResult {
	driver, ok := m.drivers[cmd.Driver]
	if !ok {
		return fail(cmd.CommandID, session.CommandErrorDriverUnavailable,
			fmt.Sprintf("instancemanager: driver %q not offered by this Worker", cmd.Driver))
	}

	launchMode, ok := launchModeFor(cmd.LaunchMode)
	if !ok {
		// An unrecognized launch mode is a malformed command, not a per-precondition
		// case in the #294 contract table; it surfaces as the unpinned INTERNAL code.
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: unknown launch mode %q", cmd.LaunchMode))
	}

	m.mu.Lock()
	if _, running := m.instances[cmd.ServerID]; running {
		m.mu.Unlock()
		return fail(cmd.CommandID, session.CommandErrorInvalidState,
			"instancemanager: server already running")
	}
	if _, orphaned := m.orphans[cmd.ServerID]; orphaned {
		// A prior stop could not confirm termination: the process/container may
		// still be lingering. Starting now would double-instance over it; the
		// reconciler must retry the stop first (issue #251).
		m.mu.Unlock()
		return fail(cmd.CommandID, session.CommandErrorInvalidState,
			"instancemanager: server has a failed-stop orphan pending termination")
	}
	m.mu.Unlock()

	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: prepare working dir: %v", err))
	}

	inst, err := driver.Start(ctx, execution.InstanceSpec{
		ServerID:         cmd.ServerID,
		WorkingDir:       workingDir,
		MinecraftVersion: cmd.MinecraftVersion,
		JarRelpath:       cmd.JarRelpath,
		LaunchMode:       launchMode,
		// The wire carries the memory LIMIT in bytes (#706); the spec carries it in
		// MiB. 0 stays 0 (unset -> default heap). Truncating to MiB is exact for any
		// real limit (the API only ever sends whole-MiB values).
		MemoryLimitMB: uint32(cmd.MemoryLimitBytes / (1024 * 1024)),
		// The CPU allocation (millicores, #723) is carried as-is onto the spec; no
		// derivation. 0 stays 0 (unset -> default weight).
		CPUMillis: cmd.CPUMillis,
	})
	if err != nil {
		return fail(cmd.CommandID, startErrorCode(err),
			fmt.Sprintf("instancemanager: start: %v", err))
	}

	m.mu.Lock()
	m.instances[cmd.ServerID] = inst
	m.startCmds[cmd.ServerID] = cmd
	m.mu.Unlock()
	m.startPumps(cmd.ServerID, inst)

	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// startPumps launches the per-instance fan-in goroutines for an instance:
// status events, captured logs (if the instance is a LogSource), and periodic
// metrics (always; up-only when the instance is not a StatsSource). The status
// pump owns a done channel it closes when the instance reaches a terminal state;
// the log and metrics pumps watch it so all three tear down cleanly on
// stop/crash/eviction without leaking goroutines (FR-MON-2, FR-MON-3).
func (m *Manager) startPumps(serverID string, inst execution.Instance) {
	done := make(chan struct{})
	go m.pump(serverID, inst, done)
	if src, ok := inst.(execution.LogSource); ok {
		go m.logPump(serverID, src)
	}
	go m.metricsPump(serverID, inst, done)
}

func (m *Manager) handleStop(ctx context.Context, cmd session.Command, graceful bool) session.CommandResult {
	inst, ok := m.takeStoppable(cmd.ServerID)
	if !ok {
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			"instancemanager: server not running")
	}
	if err := m.attemptStop(ctx, cmd.ServerID, inst, graceful); err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: stop: %v", err))
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// takeStoppable returns the instance to stop for serverID, draining either a
// tracked running instance (evicting it via take) or a previously recorded
// failed-stop orphan (left in place until the retry confirms termination). It
// reports false only for genuinely unknown ids, so SERVER_NOT_FOUND stays
// reserved for those (issue #251).
func (m *Manager) takeStoppable(serverID string) (execution.Instance, bool) {
	if inst, _, ok := m.take(serverID); ok {
		return inst, true
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	inst, ok := m.orphans[serverID]
	return inst, ok
}

// attemptStop runs the driver Stop for serverID's instance. On failure it
// records the instance as a failed-stop orphan so a retry can re-attempt
// termination against the same handle rather than returning SERVER_NOT_FOUND; on
// success it forgets any orphan record for the id (issue #251).
func (m *Manager) attemptStop(ctx context.Context, serverID string, inst execution.Instance, graceful bool) error {
	if err := inst.Stop(ctx, graceful); err != nil {
		m.mu.Lock()
		m.orphans[serverID] = inst
		m.mu.Unlock()
		return err
	}
	m.mu.Lock()
	delete(m.orphans, serverID)
	m.mu.Unlock()
	return nil
}

func (m *Manager) handleRestart(ctx context.Context, cmd session.Command) session.CommandResult {
	inst, start, ok := m.take(cmd.ServerID)
	if !ok {
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			"instancemanager: server not running")
	}
	// A restart whose stop cannot confirm termination leaves the same failed-stop
	// orphan as a plain StopServer would, so the reconciler's retry path can still
	// terminate it rather than double-instancing over it (issue #251).
	if err := m.attemptStop(ctx, cmd.ServerID, inst, true); err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: restart stop: %v", err))
	}
	// Relaunch with the original StartServer spec; RestartServer carries no
	// driver/jar/version of its own.
	//
	// If the relaunch fails (stop succeeded, but Start does not), the server is
	// left down and already evicted from the manager. We do not attempt recovery
	// here: the API sees the coded CommandResult error plus the observed
	// stopped/crashed status event, and desired-state reconciliation (bringing the
	// server back to its intended state) is the API's job, not the Worker's.
	res := m.handleStart(ctx, start)
	// Carry the RestartServer's correlation id so the API can match the result to
	// the command it issued, not the internal StartServer command.
	res.CommandID = cmd.CommandID
	return res
}

func (m *Manager) handleServerCommand(ctx context.Context, cmd session.Command) session.CommandResult {
	m.mu.Lock()
	_, running := m.instances[cmd.ServerID]
	m.mu.Unlock()
	if !running {
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			"instancemanager: server not running")
	}

	ctrl, err := m.openControl(ctx, cmd.ServerID, m.driverFor(cmd.ServerID))
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: open rcon: %v", err))
	}
	defer func() { _ = ctrl.Close() }()

	out, err := ctrl.Execute(ctx, cmd.Line)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: server command: %v", err))
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true, Output: out}
}

// MaxFileBytes bounds a ReadFile response and an EditFile payload. File access
// rides the control plane for small, interactive files (ARCHITECTURE.md
// Section 7.2), not bulk world data — that moves on the data plane. 4 MiB matches
// the API edge cap; an oversized read or edit is refused with a coded
// FILE_ACCESS_DENIED error rather than streaming megabytes onto the stream.
const MaxFileBytes = 4 * 1024 * 1024

// handleReadFile reads a working-set-relative file and returns its bytes
// (Section 6.9, 7.2). The path is sanitized against traversal (FR-FILE-4); a
// missing file maps to SERVER_NOT_FOUND (the API turns it into a 404) and an
// oversized file to FILE_ACCESS_DENIED. It is executed on the server's
// per-server lane (issue #95): a small file read is fast, unlike the bulk
// transfers the session takes off the lane.
func (m *Manager) handleReadFile(cmd session.Command) session.CommandResult {
	root := filepath.Join(m.scratchDir, cmd.ServerID)
	target, err := safeJoin(root, cmd.Path)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
			fmt.Sprintf("instancemanager: read file: %v", err))
	}
	// Resolve the parent to a dirfd that is guaranteed beneath the root, then
	// open the leaf relative to that fd: a symlink on any intermediate component
	// (which the running MC process can plant inside its own working set) is
	// refused rather than followed, and the open acts on the same resolved fd, so
	// a concurrent symlink swap between resolution and open cannot redirect it.
	parentFd, leaf, err := openParentBeneath(root, target, false)
	if err != nil {
		if errors.Is(err, unix.ENOENT) {
			// A missing working dir or intermediate dir: the file is simply not
			// there, not an escape attempt.
			return fail(cmd.CommandID, session.CommandErrorServerNotFound,
				fmt.Sprintf("instancemanager: read file: %q not found", cmd.Path))
		}
		// O_NOFOLLOW refused an intermediate-component symlink (ELOOP); any other
		// resolution failure is the generic path denial.
		if errors.Is(err, unix.ELOOP) {
			return failFileAccess(cmd.CommandID, session.FileAccessReasonSymlinkRefused,
				fmt.Sprintf("instancemanager: read file: %v", err))
		}
		return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
			fmt.Sprintf("instancemanager: read file: %v", err))
	}
	defer func() { _ = unix.Close(parentFd) }()

	content, err := readLeafNoFollow(parentFd, leaf)
	switch {
	case errors.Is(err, errIsDir):
		return failFileAccess(cmd.CommandID, session.FileAccessReasonIsADirectory,
			fmt.Sprintf("instancemanager: %q is a directory", cmd.Path))
	case errors.Is(err, errTooLarge):
		return failFileAccess(cmd.CommandID, session.FileAccessReasonPayloadTooLarge,
			fmt.Sprintf("instancemanager: %q exceeds the %d-byte read cap", cmd.Path, MaxFileBytes))
	case errors.Is(err, unix.ELOOP):
		// O_NOFOLLOW refused a final-component symlink: the classic escape vector.
		return failFileAccess(cmd.CommandID, session.FileAccessReasonSymlinkRefused,
			fmt.Sprintf("instancemanager: refusing symlink %q", cmd.Path))
	case errors.Is(err, unix.ENOENT):
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			fmt.Sprintf("instancemanager: read file: %q not found", cmd.Path))
	case err != nil:
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: read file: %v", err))
	}
	// Use a non-nil empty slice so an empty file still rides the file_content arm
	// of the result oneof (the transport distinguishes nil from empty).
	if content == nil {
		content = []byte{}
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true, FileContent: content}
}

// handleEditFile writes bytes to a working-set-relative file (Section 6.9, 7.2).
// The path is sanitized against traversal and the payload is size-bounded; the
// write is atomic (temp sibling + rename) so a concurrent reader never sees a
// torn file. It is executed on the server's per-server lane, issue #95 (a small,
// interactive edit).
func (m *Manager) handleEditFile(cmd session.Command) session.CommandResult {
	if len(cmd.Content) > MaxFileBytes {
		return failFileAccess(cmd.CommandID, session.FileAccessReasonPayloadTooLarge,
			fmt.Sprintf("instancemanager: edit exceeds the %d-byte cap", MaxFileBytes))
	}

	root := filepath.Join(m.scratchDir, cmd.ServerID)
	target, err := safeJoin(root, cmd.Path)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
			fmt.Sprintf("instancemanager: edit file: %v", err))
	}

	// Resolve (and, for missing intermediate dirs, create) the parent as a dirfd
	// beneath the root via a per-component O_NOFOLLOW walk, then write relative to
	// that fd. An intermediate-component symlink the MC process could plant is
	// refused rather than followed, the dir creation cannot traverse a link out of
	// the root, and the temp-create + rename act on the same resolved fd, so a
	// concurrent symlink swap between the walk and the rename cannot redirect it.
	parentFd, leaf, err := openParentBeneath(root, target, true)
	if err != nil {
		// O_NOFOLLOW refused an intermediate-component symlink (ELOOP); any other
		// resolution failure is the generic path denial.
		if errors.Is(err, unix.ELOOP) {
			return failFileAccess(cmd.CommandID, session.FileAccessReasonSymlinkRefused,
				fmt.Sprintf("instancemanager: edit file: %v", err))
		}
		return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
			fmt.Sprintf("instancemanager: edit file: %v", err))
	}
	defer func() { _ = unix.Close(parentFd) }()

	if err := atomicWriteAt(parentFd, leaf, cmd.Content); err != nil {
		switch {
		case errors.Is(err, errIsDir):
			return failFileAccess(cmd.CommandID, session.FileAccessReasonIsADirectory,
				fmt.Sprintf("instancemanager: %q is a directory", cmd.Path))
		case errors.Is(err, unix.ELOOP):
			return failFileAccess(cmd.CommandID, session.FileAccessReasonSymlinkRefused,
				fmt.Sprintf("instancemanager: refusing symlink %q", cmd.Path))
		default:
			return fail(cmd.CommandID, session.CommandErrorInternal,
				fmt.Sprintf("instancemanager: edit file: %v", err))
		}
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// MaxDirEntries bounds a ListFiles response. A pathological directory (a world
// with tens of thousands of region files) must not fill the control-plane stream
// with one enormous result; the listing is clipped to this many entries and the
// result carries a Truncated marker the browse view surfaces. The cap is generous
// enough for any realistic config directory.
const MaxDirEntries = 4096

// handleListFiles lists a directory in the live working set (Section 6.9, 7.2).
// The listing is read-only. The path is sanitized against traversal (FR-FILE-4)
// exactly like read/edit, the directory is opened through the hardened dirfd
// resolution refusing intermediate or final symlinks, and the result is bounded
// to MaxDirEntries with a truncation marker. A missing directory maps to
// SERVER_NOT_FOUND (the API turns it into a 404); a path that is a regular file
// (not a directory) is FILE_ACCESS_DENIED. It is executed on the server's
// per-server lane (issue #95): a single directory read is fast, unlike the bulk
// transfers the session takes off the lane.
func (m *Manager) handleListFiles(cmd session.Command) session.CommandResult {
	root := filepath.Join(m.scratchDir, cmd.ServerID)

	dirFd, err := m.openListDir(root, cmd.Path)
	switch {
	case errors.Is(err, unix.ELOOP):
		return failFileAccess(cmd.CommandID, session.FileAccessReasonSymlinkRefused,
			fmt.Sprintf("instancemanager: refusing symlink %q", cmd.Path))
	case errors.Is(err, unix.ENOTDIR):
		return failFileAccess(cmd.CommandID, session.FileAccessReasonNotADirectory,
			fmt.Sprintf("instancemanager: %q is not a directory", cmd.Path))
	case errors.Is(err, unix.ENOENT):
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			fmt.Sprintf("instancemanager: list files: %q not found", cmd.Path))
	case errors.Is(err, errPathDenied):
		return fail(cmd.CommandID, session.CommandErrorFileAccessDenied,
			fmt.Sprintf("instancemanager: list files: %v", err))
	case err != nil:
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: list files: %v", err))
	}
	defer func() { _ = unix.Close(dirFd) }()

	listing, err := readDirEntries(dirFd)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: list files: %v", err))
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true, FileListing: listing}
}

// openListDir resolves the directory at relPath beneath root to a dirfd, refusing
// to follow any intermediate or final symlink. relPath == "." (or empty) lists
// the working-set root directly (safeJoin rejects the root as a file path, so the
// listing handles it here). For any other path it reuses the same hardened
// resolution as read/edit (openParentBeneath) and opens the leaf as a directory
// relative to the resolved parent fd, so a concurrent symlink swap cannot
// redirect it. The caller owns the returned fd.
func (m *Manager) openListDir(root, relPath string) (int, error) {
	if relPath == "" || relPath == "." {
		return unix.Open(root, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	}
	target, err := safeJoin(root, relPath)
	if err != nil {
		return -1, errPathDenied
	}
	parentFd, leaf, err := openParentBeneath(root, target, false)
	if err != nil {
		return -1, err
	}
	defer func() { _ = unix.Close(parentFd) }()

	// O_DIRECTORY makes opening a regular file fail with ENOTDIR, and O_NOFOLLOW
	// makes a final-component symlink fail with ELOOP; both surface as denials.
	return unix.Openat(parentFd, leaf,
		unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
}

// readDirEntries reads the immediate children of dirFd (not recursive), bounded
// to MaxDirEntries. It dups the fd into an *os.File so os.File.ReadDir does the
// getdents loop; the dup keeps the caller's fd ownership intact (os.File closes
// its own copy). Each entry is stat'd relative to dirFd without following a
// symlink, so an entry's type/size reflect the link itself, not its target.
func readDirEntries(dirFd int) (*session.FileListing, error) {
	dup, err := unix.Dup(dirFd)
	if err != nil {
		return nil, err
	}
	dir := os.NewFile(uintptr(dup), ".")
	defer func() { _ = dir.Close() }()

	names, err := dir.Readdirnames(MaxDirEntries + 1)
	if err != nil && !errors.Is(err, io.EOF) {
		return nil, err
	}
	truncated := false
	if len(names) > MaxDirEntries {
		names = names[:MaxDirEntries]
		truncated = true
	}

	entries := make([]session.FileEntry, 0, len(names))
	for _, name := range names {
		var st unix.Stat_t
		if err := unix.Fstatat(dirFd, name, &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
			// An entry that vanished between readdir and stat is simply skipped; a
			// live working set mutates under the listing and a best-effort snapshot
			// is the documented contract.
			continue
		}
		isDir := st.Mode&unix.S_IFMT == unix.S_IFDIR
		size := uint64(0)
		if !isDir && st.Size > 0 {
			size = uint64(st.Size)
		}
		entries = append(entries, session.FileEntry{Name: name, IsDir: isDir, Size: size})
	}
	return &session.FileListing{Entries: entries, Truncated: truncated}, nil
}

// safeJoin joins name under root and verifies the result stays inside root.
// Absolute paths and any ".." component are rejected outright (not clamped),
// mirroring the data-plane extractor's discipline (FR-FILE-4). The string-level
// check below does not resolve symlinks; the handlers additionally resolve the
// parent through openParentBeneath (a per-component O_NOFOLLOW walk beneath root)
// and act on the resulting dirfd, so no in-path link can redirect the access.
func safeJoin(root, name string) (string, error) {
	slashed := filepath.ToSlash(name)
	if path.IsAbs(slashed) {
		return "", fmt.Errorf("refusing absolute path %q", name)
	}
	for _, part := range strings.Split(slashed, "/") {
		if part == ".." {
			return "", fmt.Errorf("refusing path escape %q", name)
		}
	}
	joined := filepath.Join(root, filepath.FromSlash(slashed))
	if joined != root && !strings.HasPrefix(joined, root+string(os.PathSeparator)) {
		return "", fmt.Errorf("refusing path escape %q", name)
	}
	if joined == root {
		// The working-set root itself is a directory, never a readable/writable
		// file; reject "." / "" so the caller gets a coded error, not an EISDIR.
		return "", fmt.Errorf("refusing working-set root as a file path")
	}
	return joined, nil
}

// errIsDir / errTooLarge are sentinel results from the leaf helpers, mapped by
// the handlers to their coded FILE_ACCESS_DENIED responses.
var (
	errIsDir    = errors.New("path is a directory")
	errTooLarge = errors.New("file exceeds the read cap")
	// errPathDenied marks a ListFiles path rejected by the lexical traversal check
	// (safeJoin), mapped by the handler to a FILE_ACCESS_DENIED response.
	errPathDenied = errors.New("path rejected")
)

// readLeafNoFollow opens leaf relative to parentFd refusing to follow a final
// symlink (O_NOFOLLOW yields ELOOP, which the handler maps to a denial), then
// reads the regular file. A directory or an oversized file yields the matching
// sentinel; ENOENT surfaces for a missing file.
func readLeafNoFollow(parentFd int, leaf string) ([]byte, error) {
	fd, err := unix.Openat(parentFd, leaf, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), leaf)
	defer func() { _ = f.Close() }()

	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if info.IsDir() {
		return nil, errIsDir
	}
	if info.Size() > MaxFileBytes {
		return nil, errTooLarge
	}
	return io.ReadAll(f)
}

// atomicWriteAt writes data to a temp file created under parentFd, fsyncs it, and
// renames it over leaf relative to the same dirfd, so a concurrent reader sees
// either the old or the complete new content, never a partial write. The whole
// operation rides parentFd (already resolved beneath the root), so it cannot be
// redirected by a concurrently swapped intermediate symlink. An existing symlink
// or directory at leaf is refused before the write (errIsDir / ELOOP) rather than
// replaced silently.
func atomicWriteAt(parentFd int, leaf string, data []byte) error {
	if err := refuseExistingLeaf(parentFd, leaf); err != nil {
		return err
	}

	tmpName := ".edit-" + filepath.Base(leaf) + "-tmp"
	fd, err := unix.Openat(parentFd, tmpName,
		unix.O_WRONLY|unix.O_CREAT|unix.O_TRUNC|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0o640)
	if err != nil {
		return err
	}
	tmp := os.NewFile(uintptr(fd), tmpName)
	defer func() {
		_ = tmp.Close()
		_ = unix.Unlinkat(parentFd, tmpName, 0)
	}()

	if _, err := tmp.Write(data); err != nil {
		return err
	}
	if err := tmp.Sync(); err != nil {
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return unix.Renameat(parentFd, tmpName, parentFd, leaf)
}

// refuseExistingLeaf rejects an existing symlink or directory at leaf relative to
// parentFd, so the atomic rename never silently replaces a symlink (the escape
// vector) and never targets a directory.
func refuseExistingLeaf(parentFd int, leaf string) error {
	var st unix.Stat_t
	if err := unix.Fstatat(parentFd, leaf, &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		if errors.Is(err, unix.ENOENT) {
			return nil
		}
		return err
	}
	switch st.Mode & unix.S_IFMT {
	case unix.S_IFLNK:
		return unix.ELOOP
	case unix.S_IFDIR:
		return errIsDir
	}
	return nil
}

// driverFor returns the execution driver recorded for serverID's running
// instance (its StartServer command's Driver), so the RCON dial host can be
// resolved per driver. It is empty for a server that is not running, in which
// case the caller resolves the loopback host — but both RCON call sites first
// confirm the server is running, so the recorded driver is present.
func (m *Manager) driverFor(serverID string) string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.startCmds[serverID].Driver
}

// take removes and returns the instance and its StartServer command for
// serverID, reporting whether it was present.
func (m *Manager) take(serverID string) (execution.Instance, session.Command, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	inst, ok := m.instances[serverID]
	if !ok {
		return nil, session.Command{}, false
	}
	start := m.startCmds[serverID]
	delete(m.instances, serverID)
	delete(m.startCmds, serverID)
	return inst, start, true
}

// pump forwards an instance's status events onto the merged stream, mapping the
// domain state to its wire name. It also forgets a crashed instance so the server
// id can be started again. It exits when the instance closes its event channel,
// closing done to release the log/metrics pumps for the same instance.
func (m *Manager) pump(serverID string, inst execution.Instance, done chan struct{}) {
	defer close(done)
	// If this instance was recorded as a failed-stop orphan (issue #251) and then
	// exits on its own, the channel closes here: forget the orphan so a later stop
	// for the id is a genuinely unknown server, not a lingering retry target.
	defer m.forgetOrphanIf(serverID, inst)
	for ev := range inst.Events() {
		if ev.State == execution.StateCrashed {
			m.forgetIf(serverID, inst)
		}
		m.sendStatus(session.StatusEvent{ServerID: ev.ServerID, State: ev.State.String(), Detail: ev.Detail})
	}
}

// forgetOrphanIf removes serverID's failed-stop orphan record only if it is still
// the given inst, so it does not clear a record belonging to a different instance
// (issue #251).
func (m *Manager) forgetOrphanIf(serverID string, inst execution.Instance) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.orphans[serverID] == inst {
		delete(m.orphans, serverID)
	}
}

// sendStatus forwards a status event with latest-state-wins coalescing under
// backpressure (issue #96). The fast path is a non-blocking send onto events,
// which preserves order and every transition while the sink has room. When the
// sink is full, the event is parked in the per-server pending slot (replacing any
// older pending status for that server) and the dispatcher is woken to deliver it
// once the sink drains. While a server is being routed through the dispatcher
// (coalescing), every event for it goes through the slot so a fast-path send can
// never overtake an in-flight dispatch: per-server ordering is preserved and only
// superseded intermediate states are skipped.
func (m *Manager) sendStatus(ev session.StatusEvent) {
	m.statusMu.Lock()
	if m.coalescing[ev.ServerID] {
		m.pendingStatus[ev.ServerID] = ev
		m.statusMu.Unlock()
		return
	}
	select {
	case m.events <- ev:
		m.statusMu.Unlock()
		return
	default:
	}
	m.coalescing[ev.ServerID] = true
	m.pendingStatus[ev.ServerID] = ev
	m.dirtyStatus = append(m.dirtyStatus, ev.ServerID)
	m.statusMu.Unlock()
	select {
	case m.statusNotify <- struct{}{}:
	default:
	}
}

// statusDispatcher drains coalesced status events onto the events sink, one
// server at a time in arrival order, using blocking sends so backpressure is
// absorbed (not dropped). It runs for the Manager's lifetime; events is never
// closed, mirroring the existing stream posture, so the goroutine simply parks on
// a quiet sink and exits with the process.
func (m *Manager) statusDispatcher() {
	for range m.statusNotify {
		for {
			m.statusMu.Lock()
			if len(m.dirtyStatus) == 0 {
				m.statusMu.Unlock()
				break
			}
			serverID := m.dirtyStatus[0]
			m.dirtyStatus = m.dirtyStatus[1:]
			ev := m.pendingStatus[serverID]
			delete(m.pendingStatus, serverID)
			m.statusMu.Unlock()

			m.events <- ev

			m.statusMu.Lock()
			if _, ok := m.pendingStatus[serverID]; ok {
				// A newer status arrived while we were sending; keep coalescing
				// and requeue so the latest is delivered after this one, in order.
				m.dirtyStatus = append(m.dirtyStatus, serverID)
			} else {
				delete(m.coalescing, serverID)
			}
			m.statusMu.Unlock()
		}
	}
}

// logPump forwards an instance's captured log lines onto the merged log stream
// (FR-MON-2). It exits when the instance closes its log channel (terminal
// state). Under sink backpressure it drops the line with a warning: logs are a
// stream, not state, so they keep the lossy posture (unlike status, which
// coalesces; issue #96). The per-instance LogPump already bounds and marks drops
// at the capture edge.
func (m *Manager) logPump(serverID string, src execution.LogSource) {
	for ev := range src.Logs() {
		select {
		case m.logs <- session.LogEvent{ServerID: ev.ServerID, Line: ev.Line, Stream: mapLogStream(ev.Stream)}:
		default:
			m.logger.Warn("dropped log line; sink full", "server_id", serverID)
		}
	}
}

// metricsPump samples the instance on the configured interval and forwards a
// Metrics event per tick until the instance terminates (done closed). When the
// instance is not a StatsSource, or a sample errors, it emits an up-only sample
// (server id with zero stats) so the API still learns the server is running
// (FR-MON-3). A full sink drops the sample with a warning: metrics are a stream,
// not state, so they keep the lossy posture (unlike status, which coalesces;
// issue #96).
func (m *Manager) metricsPump(serverID string, inst execution.Instance, done chan struct{}) {
	stats, _ := inst.(execution.StatsSource)

	// Bound every Sample by a context cancelled when the instance tears down (done
	// closes), so a hung Engine stats call does not leak this goroutine past
	// stop/crash. Each sample additionally carries a timeout proportionate to the
	// interval so a single slow-but-not-stuck call cannot stall the cadence.
	pumpCtx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		<-done
		cancel()
	}()

	for {
		select {
		case <-done:
			return
		case <-m.clock.After(m.metricsInterval):
		}

		sample := session.MetricsEvent{ServerID: serverID}
		if stats != nil {
			if s, err := sampleWithTimeout(pumpCtx, stats, m.metricsInterval); err == nil {
				sample.CPUMillis = s.CPUMillis
				sample.MemoryBytes = s.MemoryBytes
				sample.PlayerCount = s.PlayerCount
			} else {
				m.logger.Debug("metrics sample failed; emitting up-only", "server_id", serverID, "error", err)
			}
		}

		select {
		case m.metrics <- sample:
		default:
			m.logger.Warn("dropped metrics sample; sink full", "server_id", serverID)
		}
	}
}

// sampleWithTimeout calls Sample under a context that is cancelled when parent is
// (instance teardown) or when the per-sample timeout elapses, whichever comes
// first. The timeout is the sampling interval: a sample that has not returned by
// the time the next one is due is abandoned so a stuck Engine call cannot wedge
// the cadence.
func sampleWithTimeout(parent context.Context, stats execution.StatsSource, timeout time.Duration) (execution.MetricsSample, error) {
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	return stats.Sample(ctx)
}

// mapLogStream maps a domain log stream onto the session log stream.
func mapLogStream(s execution.LogStream) session.LogStream {
	if s == execution.LogStreamStderr {
		return session.LogStreamStderr
	}
	return session.LogStreamStdout
}

// forgetIf removes serverID's instance only if it is still the given inst, so a
// crash event does not evict a freshly restarted instance.
func (m *Manager) forgetIf(serverID string, inst execution.Instance) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.instances[serverID] == inst {
		delete(m.instances, serverID)
		delete(m.startCmds, serverID)
	}
}

// launchModeFor maps the command's wire launch-mode name to the execution
// LaunchMode, reporting false for an unrecognized name (issue #305). An empty
// name (an unset field) maps to LaunchModeJar, so a command from an API that
// does not set the field launches exactly as before this field existed.
func launchModeFor(name string) (execution.LaunchMode, bool) {
	switch name {
	case "", "jar":
		return execution.LaunchModeJar, true
	case "forge-argsfile":
		return execution.LaunchModeForgeArgsfile, true
	default:
		return 0, false
	}
}

// startErrorCode classifies a driver Start failure into a CommandResult error
// code. A driver (the container driver) wraps a known operational failure with a
// sanitized execution sentinel so the API can surface a friendlier 409 reason
// than the generic one; any other failure stays internal (issue #225).
func startErrorCode(err error) session.CommandErrorCode {
	switch {
	case errors.Is(err, execution.ErrPortConflict):
		return session.CommandErrorPortConflict
	case errors.Is(err, execution.ErrImageMissing):
		return session.CommandErrorImageMissing
	default:
		return session.CommandErrorInternal
	}
}

// fail builds a failed CommandResult.
func fail(commandID string, code session.CommandErrorCode, msg string) session.CommandResult {
	return session.CommandResult{
		CommandID:    commandID,
		Success:      false,
		ErrorCode:    code,
		ErrorMessage: msg,
	}
}

// failFileAccess builds a CommandErrorFileAccessDenied result carrying the
// specific reason that refines it (issue #548). The API maps the reason to an
// honest problem reason and HTTP status instead of a blanket invalid_path.
func failFileAccess(commandID string, reason session.FileAccessReason, msg string) session.CommandResult {
	return session.CommandResult{
		CommandID:        commandID,
		Success:          false,
		ErrorCode:        session.CommandErrorFileAccessDenied,
		ErrorMessage:     msg,
		FileAccessReason: reason,
	}
}

// ensure the satisfied-interface assertion stays compile-checked.
var _ session.CommandHandler = (*Manager)(nil)
