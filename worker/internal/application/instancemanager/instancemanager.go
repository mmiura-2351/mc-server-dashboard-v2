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

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/regionfsck"
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
	// response leaves it empty). It returns the authoritative store GENERATION the
	// API served (the value of its response header, issue #763); 0 when the header
	// is absent (a server with no published snapshot, or an older API).
	Hydrate(ctx context.Context, url, token, workingDir string) (uint64, error)
	// Snapshot packs workingDir and uploads it to url. It returns the NEW
	// authoritative store generation the publish produced (the value of the API's
	// response header, issue #763); 0 when the header is absent (an older API).
	Snapshot(ctx context.Context, url, token, workingDir string) (uint64, error)
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
	// reserved marks a server id as having a mutating lifecycle command in flight so
	// a duplicate re-issued after a stream reconnect cannot overlap the original
	// (issue #780). It is claimed under mu and held across the long operation, then
	// released (or, on a successful start, handed off to the registered instance
	// under the same mu so the id is never unclaimed). A command arriving while the
	// id is reserved is rejected with INVALID_STATE — the same family as "already
	// running". Which commands reserve, and over which window:
	//   - StartServer: before driver.Start, until the instance is registered, so a
	//     re-issued duplicate cannot pass the running check and launch a second
	//     process while the original is still mid-driver.Start (the primary window).
	//   - HydrateTrigger: across the transfer, so a re-issued hydrate (or a racing
	//     start/snapshot) cannot write the same working set concurrently.
	//   - StopServer / RestartServer: across the eviction -> stop-confirmed window
	//     (and a restart's relaunch). takeStoppableReserve / takeRunningReserve evict
	//     the instance AND reserve under one mu, so the id stays claimed while the
	//     detached stop confirms termination — a re-sent stop then gets INVALID_STATE,
	//     not SERVER_NOT_FOUND, and the API keeps the assignment instead of unassigning
	//     over a still-live process.
	//   - SnapshotTrigger: only the STOPPED-id path (the set is at rest and the API
	//     has typically unassigned), to block a racing hydrate from rewriting the dir
	//     mid-pack. A running-id snapshot does NOT reserve: a live instance already
	//     blocks reserve(), and its save-off bracket is the quiesce.
	// The file handlers (ReadFile / EditFile / ListFiles) act atomically on individual
	// files and take no reservation.
	reserved map[string]bool

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
		reserved:        map[string]bool{},
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
	if err := validateServerID(cmd.ServerID); err != nil {
		return fail(cmd.CommandID, session.CommandErrorFileAccessDenied, err.Error())
	}
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
	// Reserve the id for the duration of the transfer so a re-issued HydrateTrigger
	// (or a racing StartServer/SnapshotTrigger) cannot write the same working set
	// concurrently with the original after a stream reconnect (issue #780). The
	// reservation also subsumes the running / failed-stop-orphan preconditions —
	// hydrating either would replace the working set out from under a live process
	// (issue #251) — and is always released on return.
	if ok, _ := m.reserve(cmd.ServerID); !ok {
		return fail(cmd.CommandID, session.CommandErrorInvalidState,
			"instancemanager: cannot hydrate (server running, has a failed-stop orphan, or a lifecycle command is in flight)")
	}
	defer m.release(cmd.ServerID)

	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	gen, err := m.transfer.Hydrate(ctx, cmd.TransferURL, cmd.TransferToken, workingDir)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			fmt.Sprintf("instancemanager: hydrate: %v", err))
	}
	// Record the generation the working set is now at (issue #763): the API served
	// the authoritative store at this generation, so the local scratch matches it.
	// A 0 (no published snapshot, or an older API) is recorded as-is — the API then
	// treats this set as older than any published store generation and re-hydrates,
	// the safe direction. The marker write is best-effort: a failure only costs an
	// extra hydrate next start, never correctness, so it is logged not propagated.
	m.recordGeneration(workingDir, cmd.ServerID, gen)
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// recordGeneration writes the working-set generation marker, logging (not failing)
// on error: a missing/stale marker only costs an extra hydrate, never correctness.
func (m *Manager) recordGeneration(workingDir, serverID string, gen uint64) {
	if err := writeGeneration(workingDir, gen); err != nil {
		m.logger.Warn("could not record working-set generation",
			"server_id", serverID, "generation", gen, "error", err)
	}
}

// restoreSaveTimeout bounds the re-enable-auto-save RCON call on the snapshot
// exit path. It runs on a context detached from the request's (so a cancelled or
// timed-out request still re-enables auto-save), and must therefore carry its own
// deadline so the call cannot hang the goroutine forever.
const restoreSaveTimeout = 30 * time.Second

// handleSnapshot packs the server's working dir and uploads it. For a running
// server it brackets the working-dir copy with RCON save-off / save-on so the
// Minecraft server does not write to the world mid-copy and a region file cannot
// be captured torn (#694, CONTROL_PLANE.md Section 6.9): it issues save-off to
// disable auto-save, a non-blocking save-all to refresh the on-disk copy, runs the
// transfer over the now-quiescent working dir, then save-on to re-enable
// auto-save. The save-all deliberately does not flush — a synchronous main-thread
// flush can occupy a tick past max-tick-time and trip the server watchdog,
// crashing a running server (#693).
//
// The bracket is best-effort: if opening RCON or the save-off toggle fails, the
// snapshot still proceeds without the consistency guarantee (logged, not fatal).
// But once save-off succeeds, save-on is guaranteed on every exit path — success,
// transfer error, or a cancelled/timed-out request context — via a deferred
// restore that runs on a detached context, so the server is never left with
// auto-save disabled (which would risk data loss on a later crash).
//
// Once the working set is quiesced (bracketed above for a running server, at rest
// for a stopped one), a structural region fsck runs before the transfer (#741):
// on detected corruption the snapshot is refused with a coded error — failing fast
// at the source instead of after a full tar+upload the API gate would reject — and
// the deferred restore still re-enables auto-save. A fsck I/O error is best-effort
// (logged, the transfer proceeds) so it cannot wedge the snapshot.
func (m *Manager) handleSnapshot(ctx context.Context, cmd session.Command) session.CommandResult {
	if m.transfer == nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			"instancemanager: no data-plane transfer client configured")
	}
	m.mu.Lock()
	_, running := m.instances[cmd.ServerID]
	m.mu.Unlock()
	if running {
		// restore is always non-nil; it re-enables auto-save (when it was disabled)
		// and releases the RCON connection. Deferring it guarantees save-on runs on
		// every return path below, including the transfer-error path and a panic.
		restore := m.quiesceRunning(ctx, cmd.ServerID)
		defer restore()
	} else {
		// Stopped-id snapshot: the set is at rest and the API has typically already
		// unassigned (a graceful stop snapshots after unassign, so a user start can
		// re-place this id on the same Worker concurrently). Reserve the id for the
		// pack so a racing HydrateTrigger (or start) cannot rewrite the working dir
		// while it is mid-fsck/tar — a mixed capture whose .mca files are each valid
		// would slip past the #749 integrity gate (the snapshot×hydrate cross-race the
		// #780 review confirmed). A reservation already held by such a racing command
		// rejects with INVALID_STATE. Running-id snapshots stay reservation-free: a
		// running instance already blocks reserve(), and the save-off bracket above is
		// their quiesce. Released on every return below.
		if ok, _ := m.reserve(cmd.ServerID); !ok {
			return fail(cmd.CommandID, session.CommandErrorInvalidState,
				"instancemanager: cannot snapshot (a lifecycle command is in flight for this server)")
		}
		defer m.release(cmd.ServerID)
	}

	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)

	// Pre-pack structural region fsck (#741): fail fast at the source if the
	// working set is already corrupt (e.g. a region torn by a crash-during-save,
	// #703), so we refuse the snapshot here — clear signal, no wasted tar+upload —
	// rather than after a full transfer the API gate (#749) would reject anyway.
	// The set is quiesced at this point: a running server is bracketed by save-off
	// above (#694), and a stopped one is not being written. The check itself is
	// fail-closed on detected corruption but best-effort on a fsck I/O error — an
	// error reading the set must not wedge the snapshot, so it is logged and the
	// transfer proceeds (the API gate remains the correctness guarantee).
	if report, err := regionfsck.CheckWorkingSet(workingDir); err != nil {
		m.logger.Warn("snapshot pre-pack region fsck failed; proceeding without it",
			"server_id", cmd.ServerID, "error", err)
	} else if !report.Healthy() {
		first := report.Corrupt[0]
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			fmt.Sprintf("instancemanager: snapshot refused: %d/%d region files corrupt (e.g. %s: %s)",
				len(report.Corrupt), report.Scanned, filepath.Base(first.Path), first.Reason))
	}

	gen, err := m.transfer.Snapshot(ctx, cmd.TransferURL, cmd.TransferToken, workingDir)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			fmt.Sprintf("instancemanager: snapshot: %v", err))
	}
	if running {
		// Record the NEW generation the publish produced (issue #763): the scratch we
		// just pushed is the source of this store generation, so its local generation
		// advances to match. This keeps a same-Worker restart's held generation equal to
		// the store generation (the API then skips the destructive hydrate). Best-effort
		// (logged, not failed) — see recordGeneration.
		m.recordGeneration(workingDir, cmd.ServerID, gen)
	} else {
		// Stopped-id snapshot succeeded: this is the post-stop FINAL snapshot (or a
		// snapshot of an at-rest set). The working set is now captured authoritatively
		// and the API has typically already unassigned this Worker, so the local scratch
		// is redundant — GC it now to reclaim disk and shrink the stale-leftover surface
		// (#762's anti-accumulation goal, relocated here from the stop path so the final
		// snapshot can no longer pack an empty dir, issue #841). The GC is deferred to
		// AFTER a successful publish: a failed snapshot returned above with the scratch
		// intact, so nothing is lost. The reservation taken in the stopped branch is
		// still held (released by the deferred release on return), so no racing hydrate
		// or start can recreate the dir between the publish and this removal. Recording
		// the new generation would be pointless work on a dir we are about to delete.
		m.removeScratch(cmd.ServerID)
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// quiesceRunning brackets a running-server snapshot so the world is not written
// during the working-dir copy (#694). It opens RCON, disables auto-save
// (save-off), and issues a non-blocking save-all to refresh the on-disk copy, then
// returns a restore func the caller defers. The save-all does not flush — a
// synchronous main-thread flush can trip the server watchdog (#693).
//
// All steps are best-effort: an open or save-off failure is logged, not
// propagated, and leaves the server unbracketed (auto-save was never disabled, so
// the snapshot just forfeits the torn-read guarantee for this run). The returned
// restore re-enables auto-save with save-on only when save-off actually
// succeeded, and always closes the RCON connection if one was opened. It runs
// save-on on a context detached from ctx (carrying restoreSaveTimeout) so a
// cancelled or timed-out request still re-enables auto-save rather than leaving
// the server unable to persist.
func (m *Manager) quiesceRunning(ctx context.Context, serverID string) func() {
	ctrl, err := m.openControl(ctx, serverID, m.driverFor(serverID))
	if err != nil {
		m.logger.Warn("snapshot quiesce: open rcon failed", "server_id", serverID, "error", err)
		return func() {}
	}

	saveOff := true
	if _, err := ctrl.Execute(ctx, "save-off"); err != nil {
		// Could not disable auto-save: proceed without the bracket. Do not run
		// save-on on exit — auto-save was never turned off.
		m.logger.Warn("snapshot save-off failed; snapshot will not be quiesced",
			"server_id", serverID, "error", err)
		saveOff = false
	}
	if _, err := ctrl.Execute(ctx, "save-all"); err != nil {
		m.logger.Warn("snapshot save-all failed", "server_id", serverID, "error", err)
	}

	return func() {
		defer func() { _ = ctrl.Close() }()
		if !saveOff {
			return
		}
		// Detach from the request context so a cancelled/timed-out snapshot still
		// re-enables auto-save; bound it so the RCON call cannot hang.
		restoreCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), restoreSaveTimeout)
		defer cancel()
		if _, err := ctrl.Execute(restoreCtx, "save-on"); err != nil {
			m.logger.Warn("snapshot save-on failed; server may have auto-save disabled",
				"server_id", serverID, "error", err)
		}
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

	// Reserve the id before driver.Start so a duplicate StartServer re-issued after
	// a stream reconnect cannot pass the running check and launch a second instance
	// while the original is still mid-driver.Start (issue #780). The reservation is
	// released on every exit path below — including a failed start — so a retry can
	// proceed. It is not released on success: the registered instance then holds the
	// id (a duplicate sees the running instance), so releasing the reservation only
	// after registration keeps the id continuously claimed across the handoff.
	if ok, msg := m.reserve(cmd.ServerID); !ok {
		return fail(cmd.CommandID, session.CommandErrorInvalidState, msg)
	}
	return m.launchReserved(ctx, cmd, driver, launchMode)
}

// launchReserved performs the start under an ALREADY-HELD reservation (taken by
// handleStart or carried across a restart's stop, issue #780): it prepares the
// working dir, runs driver.Start, and registers the instance, releasing the
// reservation on every failure path and handing the id off to the registered
// instance under one mu critical section on success — so the id is never unclaimed.
func (m *Manager) launchReserved(ctx context.Context, cmd session.Command, driver execution.ExecutionDriver, launchMode execution.LaunchMode) session.CommandResult {
	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	if err := os.MkdirAll(workingDir, 0o750); err != nil {
		m.release(cmd.ServerID)
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
		m.release(cmd.ServerID)
		return fail(cmd.CommandID, startErrorCode(err),
			fmt.Sprintf("instancemanager: start: %v", err))
	}

	// Register the instance, then drop the reservation under the same mu: the
	// tracked instance now holds the id, so there is no window where neither the
	// reservation nor the instance claims it (a concurrent duplicate always sees
	// one or the other, issue #780).
	m.mu.Lock()
	m.instances[cmd.ServerID] = inst
	m.startCmds[cmd.ServerID] = cmd
	delete(m.reserved, cmd.ServerID)
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
	inst, outcome := m.takeStoppableReserve(cmd.ServerID)
	switch outcome {
	case takeNotFound:
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			"instancemanager: server not running")
	case takeInFlight:
		// A lifecycle command is already reserved in flight for this id (issue #780):
		// most importantly, a DETACHED stop from a dropped stream's lane is still
		// confirming termination — takeStoppableReserve evicted the instance and holds
		// the reservation across inst.Stop (up to ~3x stopTimeout). A re-sent StopServer
		// on the reconnected stream must NOT get SERVER_NOT_FOUND here: that makes the
		// API converge observed=stopped and unassign while the old process is still
		// alive and writing, after which a re-placed start's HydrateTrigger would clobber
		// the live working set. Returning INVALID_STATE makes the API's redispatch_stop
		// keep the assignment and retry on a later tick (lifecycle.py), converging safely
		// once the detached stop finishes (the id then becomes genuinely SERVER_NOT_FOUND).
		return fail(cmd.CommandID, session.CommandErrorInvalidState,
			"instancemanager: a lifecycle command is already in flight for this server")
	}
	// The id is now reserved across the eviction -> stop-confirmed window so the
	// detached stop is the sole writer; released on every return below (issue #780).
	defer m.release(cmd.ServerID)
	if err := m.attemptStop(ctx, cmd.ServerID, inst, graceful); err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: stop: %v", err))
	}
	// Do NOT GC the scratch here, even though a confirmed StopServer is an
	// AUTHORITATIVE stop (issue #841). The API sends the FINAL snapshot for this id
	// only AFTER this stop's CommandResult (StopServer.__call__, lifecycle.py,
	// FR-DATA-7): a stop-time GC would leave that SnapshotTrigger to pack an empty
	// dir, silently losing the world progressed since the last periodic snapshot.
	// The #762 reclamation moves to AFTER the post-stop final snapshot publishes
	// (handleSnapshot's stopped-id branch) — see removeScratch.
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// removeScratch deletes the server's local working-set scratch dir, plus any
// .hydrate-<id>-* temp/trash siblings a crash mid-hydrate left behind for this
// id (datatransfer.unpackAndSwap, issue #772, swept via sweepHydrateLeftovers).
// It is best-effort: a removal failure is logged, never surfaced — the working
// set has already been captured (the snapshot that triggers it succeeded), and
// leftover scratch is a hygiene problem, not a failure. A missing dir is a no-op
// (os.RemoveAll returns nil).
//
// Reclamation contract (issue #841, preserving #762's anti-accumulation goal):
//   - GC runs ONLY after a successful STOPPED-id SnapshotTrigger — the post-stop
//     final snapshot (or a snapshot of an at-rest set). At that point the working
//     set is captured authoritatively and the API has typically unassigned this
//     Worker, so the local copy is redundant and safe to reclaim. The scratch dir
//     and this id's hydrate leftovers (#842) are reclaimed together at that moment.
//   - It does NOT run on the stop itself (the final snapshot has not happened yet),
//     on a FAILED snapshot (nothing was captured — losing it would be the #841 bug),
//     or on a RUNNING-id snapshot (the live server still owns its working set).
//   - If the final snapshot NEVER arrives (API crash between stop and snapshot, or
//     the Worker/stream dropping before it lands), the scratch persists. It is then
//     reclaimed by the next authoritative event for that id: a later start hydrates
//     a fresh working set over it, or — on a same-Worker restart — ScanHeldServers
//     reports it as held and the API's generation-gated hydrate (#763/#767) either
//     reuses it (still current) or re-hydrates (stale). This bounds accumulation to
//     at most one at-rest working set per stopped server, never an unbounded leak.
func (m *Manager) removeScratch(serverID string) {
	dir := filepath.Join(m.scratchDir, serverID)
	if err := os.RemoveAll(dir); err != nil {
		m.logger.Warn("failed to remove scratch dir after final snapshot",
			"server_id", serverID, "dir", dir, "error", err)
	}
	m.sweepHydrateLeftovers(serverID)
}

// sweepHydrateLeftovers removes the .hydrate-<id>-* temp/trash siblings a crashed
// hydrate for serverID left in the scratch root. The next start's leftover sweep
// (datatransfer.sweepHydrateLeftovers) clears them too, but only if the server is
// re-placed onto this Worker; a deleted/re-placed-elsewhere id would otherwise leak
// the world-sized orphan permanently. The prefix matches datatransfer.hydrateTmpPrefix
// exactly (".hydrate-<id>-"), so only this id's leftovers are touched — not another
// server's dir or a similarly named one. Best-effort: a removal failure is ignored
// (a leftover is wasted disk, never a correctness problem).
func (m *Manager) sweepHydrateLeftovers(serverID string) {
	entries, err := os.ReadDir(m.scratchDir)
	if err != nil {
		return
	}
	prefix := ".hydrate-" + serverID + "-"
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), prefix) {
			_ = os.RemoveAll(filepath.Join(m.scratchDir, e.Name()))
		}
	}
}

// takeOutcome is the result of takeStoppableReserve: an instance to stop was
// taken and reserved, no live instance exists (genuinely unknown -> SERVER_NOT_FOUND),
// or a lifecycle command is already reserved in flight for the id (a detached stop
// still confirming, or a start/hydrate mid-operation -> INVALID_STATE, issue #780).
type takeOutcome int

const (
	takeFound takeOutcome = iota
	takeNotFound
	takeInFlight
)

// takeStoppableReserve atomically (under mu) selects the instance to stop for
// serverID and claims an in-flight reservation across the eviction -> stop-confirmed
// window so a re-sent StopServer arriving while the detached stop is still confirming
// termination is rejected rather than treated as SERVER_NOT_FOUND (issue #780).
//
// It drains either a tracked running instance (evicting it as take does) or a
// previously recorded failed-stop orphan (left in place until the retry confirms
// termination, issue #251), reserving the id in the same critical section. If neither
// is tracked it reports takeInFlight when the id is already reserved (another
// lifecycle command — typically the original detached stop — is in flight) and
// takeNotFound only for genuinely unknown ids. The caller must release on every
// return path.
func (m *Manager) takeStoppableReserve(serverID string) (execution.Instance, takeOutcome) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if inst, ok := m.instances[serverID]; ok {
		delete(m.instances, serverID)
		delete(m.startCmds, serverID)
		m.reserved[serverID] = true
		return inst, takeFound
	}
	if inst, ok := m.orphans[serverID]; ok {
		m.reserved[serverID] = true
		return inst, takeFound
	}
	if m.reserved[serverID] {
		return nil, takeInFlight
	}
	return nil, takeNotFound
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

// takeRunningReserve atomically (under mu) evicts the tracked running instance for
// serverID, captures its original StartServer spec, and claims an in-flight
// reservation so the id stays continuously claimed across the restart's
// stop -> relaunch window (issue #780). It reports takeInFlight when no instance is
// tracked but the id is already reserved (a detached stop or another lifecycle
// command still in flight) and takeNotFound for a genuinely unknown id. A restart
// applies only to a tracked running instance, so a recorded orphan is NOT taken
// here (it is left for the stop-retry path, issue #251) and reports takeNotFound.
func (m *Manager) takeRunningReserve(serverID string) (execution.Instance, session.Command, takeOutcome) {
	m.mu.Lock()
	defer m.mu.Unlock()
	inst, ok := m.instances[serverID]
	if !ok {
		if m.reserved[serverID] {
			return nil, session.Command{}, takeInFlight
		}
		return nil, session.Command{}, takeNotFound
	}
	start := m.startCmds[serverID]
	delete(m.instances, serverID)
	delete(m.startCmds, serverID)
	m.reserved[serverID] = true
	return inst, start, takeFound
}

func (m *Manager) handleRestart(ctx context.Context, cmd session.Command) session.CommandResult {
	inst, start, outcome := m.takeRunningReserve(cmd.ServerID)
	switch outcome {
	case takeNotFound:
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			"instancemanager: server not running")
	case takeInFlight:
		// A lifecycle command (e.g. a detached stop from a dropped stream, or a start/
		// hydrate mid-operation) is already reserved in flight for this id (issue #780).
		// Rejecting with INVALID_STATE rather than SERVER_NOT_FOUND keeps the API from
		// unassigning a server whose process may still be alive.
		return fail(cmd.CommandID, session.CommandErrorInvalidState,
			"instancemanager: a lifecycle command is already in flight for this server")
	}
	// The id is reserved from here across the stop and the relaunch; it is handed off
	// to the re-registered instance on a successful relaunch (launchReserved) and
	// released on every failure path so the id is never left unclaimed under the still-
	// stopping process (issue #780).
	driver, ok := m.drivers[start.Driver]
	if !ok {
		m.release(cmd.ServerID)
		return fail(cmd.CommandID, session.CommandErrorDriverUnavailable,
			fmt.Sprintf("instancemanager: driver %q not offered by this Worker", start.Driver))
	}
	launchMode, ok := launchModeFor(start.LaunchMode)
	if !ok {
		m.release(cmd.ServerID)
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: unknown launch mode %q", start.LaunchMode))
	}
	// A restart whose stop cannot confirm termination leaves the same failed-stop
	// orphan as a plain StopServer would, so the reconciler's retry path can still
	// terminate it rather than double-instancing over it (issue #251). The reservation
	// is dropped on this failure path; the orphan record then guards the id instead.
	if err := m.attemptStop(ctx, cmd.ServerID, inst, true); err != nil {
		m.release(cmd.ServerID)
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: restart stop: %v", err))
	}
	// Relaunch with the original StartServer spec under the still-held reservation;
	// RestartServer carries no driver/jar/version of its own.
	//
	// If the relaunch fails (stop succeeded, but Start does not), the server is
	// left down and already evicted from the manager. We do not attempt recovery
	// here: the API sees the coded CommandResult error plus the observed
	// stopped/crashed status event, and desired-state reconciliation (bringing the
	// server back to its intended state) is the API's job, not the Worker's.
	res := m.launchReserved(ctx, start, driver, launchMode)
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

// validateServerID rejects a ServerID that is unsafe to join into a scratch
// path before any handler does so (issue #782). The API sends the canonical
// text form of a UUID (str(uuid)); every legitimate id is therefore a single
// non-empty path component with no separator and no "." / ".." meaning. An
// empty id would make a filepath.Join collapse onto the scratch ROOT (so
// SnapshotTrigger would tar every server's world) and a "../x" id would escape
// it. This is defense-in-depth on a trusted control plane: it rejects the
// dangerous shapes without pinning to strict UUID syntax, so a future id scheme
// that stays a sane single component keeps working. Mirrors safeJoin's lexical
// discipline.
func validateServerID(id string) error {
	if id == "" {
		return errors.New("refusing empty server id")
	}
	if id == "." || id == ".." {
		return fmt.Errorf("refusing server id %q", id)
	}
	if strings.ContainsAny(id, `/\`) {
		return fmt.Errorf("refusing server id with a path separator %q", id)
	}
	if strings.ContainsRune(id, 0) {
		return fmt.Errorf("refusing server id with a NUL byte %q", id)
	}
	return nil
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

// reserve claims serverID for an in-flight mutating lifecycle command (issue
// #780). It atomically rejects — under the same mu held for the running/orphan
// checks, so there is no check-then-act gap — when the id is already running, has
// a failed-stop orphan pending, or already carries a reservation, and otherwise
// marks it reserved. ok reports whether the claim was taken; on a rejection, msg
// is the precondition message the caller fails with under INVALID_STATE (the same
// family as "already running"). It must be paired with release on every exit
// path.
func (m *Manager) reserve(serverID string) (ok bool, msg string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, running := m.instances[serverID]; running {
		return false, "instancemanager: server already running"
	}
	if _, orphaned := m.orphans[serverID]; orphaned {
		// A prior stop could not confirm termination: the process/container may
		// still be lingering. Starting/hydrating now would double-instance over it;
		// the reconciler must retry the stop first (issue #251).
		return false, "instancemanager: server has a failed-stop orphan pending termination"
	}
	if m.reserved[serverID] {
		// A re-issued duplicate arriving while the original is still in flight after
		// a stream reconnect (issue #780): reject it rather than overlap the original.
		return false, "instancemanager: a lifecycle command is already in flight for this server"
	}
	m.reserved[serverID] = true
	return true, ""
}

// release drops serverID's in-flight reservation so a later command (a retry
// after a failure, or the next lifecycle op) can claim it again (issue #780).
func (m *Manager) release(serverID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.reserved, serverID)
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
