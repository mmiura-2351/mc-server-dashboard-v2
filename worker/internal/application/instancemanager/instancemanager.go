// Package instancemanager handles lifecycle, file, and transfer commands and streams instance telemetry.
// It owns each server's scratch working set and implements session.CommandHandler.
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
	"sync/atomic"
	"time"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/rcon"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/regionfsck"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/scratchformat"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// controlFunc resolves RCON using the driver's network topology and Minecraft version's password charset.
type controlFunc func(ctx context.Context, serverID, driver, mcVersion string) (execution.ServerControl, error)

// resilientControl redials once per Execute after a poisoned connection so trailing bracket commands can run.
type resilientControl struct {
	inner    execution.ServerControl
	dial     func(ctx context.Context) (execution.ServerControl, error)
	logger   *slog.Logger
	serverID string
}

func (r *resilientControl) Execute(ctx context.Context, line string) (string, error) {
	reply, err := r.inner.Execute(ctx, line)
	if err == nil || !errors.Is(err, rcon.ErrConnBroken) {
		return reply, err
	}
	r.logger.Warn("rcon connection poisoned; redialing for next command",
		"server_id", r.serverID, "line", line)
	_ = r.inner.Close()
	fresh, dialErr := r.dial(ctx)
	if dialErr != nil {
		return "", fmt.Errorf("redial after broken connection: %w", dialErr)
	}
	r.inner = fresh
	return r.inner.Execute(ctx, line)
}

func (r *resilientControl) Close() error { return r.inner.Close() }

// Transfer is the data-plane Port: move a server's working set between the API's
// authoritative Storage and the local working dir (FR-DATA-3/4). The trigger
// command carries the URL + token; the bytes ride the HTTP data plane, off the
// control-plane stream (CONTROL_PLANE.md Section 5).
type Transfer interface {
	// Hydrate downloads the working set from url into workingDir (an empty/204 response leaves it empty). It
	// returns the authoritative store GENERATION the API served (the value of its response header); 0 when the
	// header is absent (a server with no published snapshot, or an older API).
	Hydrate(ctx context.Context, url, token, workingDir string) (uint64, error)
	// PackSnapshot alone reads the working set, so quiescence may end when it returns.
	// The caller must invoke cleanup to remove the returned spool.
	PackSnapshot(ctx context.Context, workingDir string) (spoolPath string, cleanup func(), err error)
	// UploadSnapshot streams the spool file at spoolPath to url, declaring baseGeneration and workerID for the
	// API's publish-time generation guard. It returns the NEW store generation the publish produced.
	UploadSnapshot(ctx context.Context, url, token, spoolPath string, baseGeneration uint64, workerID string) (uint64, error)
	// Snapshot returns the newly published generation, or zero if absent.
	// The API refuses a stale baseGeneration when a different worker published the current generation.
	Snapshot(ctx context.Context, url, token, workingDir string, baseGeneration uint64, workerID string) (uint64, error)
}

// TunnelDialer returns after setup; the adapter owns the splice until peer close or Worker shutdown.
type TunnelDialer interface {
	Dial(ctx context.Context, spec TunnelSpec) error
}

// TunnelSpec carries everything one TunnelDial needs: the local server's working
// dir (for its published game port) and the relay endpoint, token, and optional
// CA bundle to dial back to (RELAY.md Section 5).
type TunnelSpec struct {
	ServerID   string
	WorkingDir string
	Endpoint   string
	Token      string
	CAPEM      string
}

// BedrockTunneler registers tunnels and signals teardown without waiting for network I/O.
// Repeated Open with the same spec is idempotent; dropped connections retry with backoff.
type BedrockTunneler interface {
	Open(spec BedrockTunnelSpec) error
	Close(serverID string)
}

// BedrockTunnelSpec carries everything one OpenBedrockTunnel needs
// (docs/app/BEDROCK_TUNNEL.md Section 3): the relay's Bedrock tunnel endpoint,
// the public port it binds for this server, the tunnel-lifetime credential,
// and the optional CA bundle to verify the relay's QUIC certificate.
type BedrockTunnelSpec struct {
	ServerID      string
	RelayEndpoint string
	BedrockPort   uint32
	Token         string
	CAPEM         string
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
	tunnel      TunnelDialer
	bedrock     BedrockTunneler
	logger      *slog.Logger
	// workerID is this Worker's own id, stamped on a snapshot publish so the API's publish-time generation guard
	// can tell a same-Worker re-publish from a different-Worker stale publish. Empty until WithWorkerID is called
	// (older wiring / tests): the guard then treats the publisher as unknown.
	workerID string

	clock           session.Clock
	metricsInterval time.Duration

	// Retry running-world fsck to tolerate residual non-chunk writes; stopped worlds are checked once.
	fsckRetryDelay time.Duration

	// After async save-all, two identical region (mtime, size) scans indicate settling, bounded by settleBudget.
	settlePollInterval time.Duration
	settleBudget       time.Duration

	// scanRegion reads the (mtime, size) of a working set's .mca files for the
	// settle-wait. It is a field defaulted to scanRegionState in New so a test can
	// inject a deterministic "still changing" / "now stable" sequence without racing
	// the real filesystem.
	scanRegion func(root string) (regionState, error)

	// Test hook for a start racing the stopped snapshot's reservation; nil in production.
	snapshotAfterRunningCheck func(serverID string)

	// orphanProbeInterval / orphanProbeMaxInterval are the failed-stop-orphan converger's probe cadence and its
	// exponential-backoff cap. They are fields (not consts) so tests can shrink them to milliseconds, mirroring
	// fsckRetryDelay. Defaulted in New; the waits go through m.clock.
	orphanProbeInterval    time.Duration
	orphanProbeMaxInterval time.Duration

	// Close cancels background work and joins every counted goroutine, including watcher teardown.
	// An in-flight scratch reclaim finishes its current ID before observing shutdown.
	shutdown       context.Context
	stopBackground context.CancelFunc
	background     sync.WaitGroup

	// The session writes the transfer bound while lane goroutines read it; zero means unbounded.
	transferDeadlineNanos atomic.Int64

	mu        sync.Mutex
	instances map[string]execution.Instance
	// startCmds remembers the StartServer command per running server so a
	// RestartServer (which carries no driver/version) can relaunch with the same
	// spec.
	startCmds map[string]session.Command
	// Retain failed-stop handles until termination is confirmed; absence must never imply a live orphan is gone.
	// Retryable operations return BUSY, running-only operations INVALID_STATE; tunnel close remains allowed.
	orphans map[string]orphanEntry
	// Allow one converger per orphan ID; claim and clear the flag with the orphan record under mu.
	converging map[string]bool
	// Record the RCON target before save-off can be sent, and clear it only after stop resolution or save-on.
	// Close restores outstanding brackets; a sealed ledger refuses new save-off writes.
	pendingSaveOn map[string]saveOnTarget
	// Seal the ledger with Close's final read so a late flush cannot disable auto-save without a restore.
	saveOnSealed bool
	// Bound shutdown save-on restores separately from the longer failed-stop restore.
	closingSaveOnTimeout time.Duration
	// Guard spawning and Close with the same mu so WaitGroup.Add cannot race Wait after shutdown.
	closed bool
	// Reserve IDs across start, hydrate, stop/restart, and stopped snapshots, returning BUSY on overlap.
	// Running snapshots and atomic file operations do not reserve; registration replaces a start's claim under mu.
	reserved map[string]bool

	// Claim only the displaced-slot decision window, preventing overlapping sweeps from displacing recovery copies.
	// A competing sweep declines immediately; recursive removal runs after the claim is released.
	sweepingSlot map[string]bool

	// events/logs/metrics are the merged streams the session forwards. Per-instance
	// pumps fan their events into them (FR-MON-2, FR-MON-3).
	events  chan session.StatusEvent
	logs    chan session.LogEvent
	metrics chan session.MetricsEvent

	// Coalesce status by server so backpressure retains the latest state.
	// Route all updates through the pending slot until dispatch completes to prevent overtaking.
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
		drivers:            drivers,
		scratchDir:         scratchDir,
		openControl:        openControl,
		logger:             slog.Default(),
		clock:              systemClock{},
		metricsInterval:    defaultMetricsInterval,
		fsckRetryDelay:     defaultFsckRetryDelay,
		settlePollInterval: defaultSettlePollInterval,
		settleBudget:       defaultSettleBudget,
		scanRegion:         scanRegionState,
		instances:          map[string]execution.Instance{},
		startCmds:          map[string]session.Command{},
		orphans:            map[string]orphanEntry{},
		reserved:           map[string]bool{},
		sweepingSlot:       map[string]bool{},
		events:             make(chan session.StatusEvent, 32),
		logs:               make(chan session.LogEvent, 256),
		metrics:            make(chan session.MetricsEvent, 32),
		pendingStatus:      map[string]session.StatusEvent{},
		coalescing:         map[string]bool{},
		statusNotify:       make(chan struct{}, 1),

		orphanProbeInterval:    defaultOrphanProbeInterval,
		orphanProbeMaxInterval: defaultOrphanProbeMaxInterval,
		converging:             map[string]bool{},
		pendingSaveOn:          map[string]saveOnTarget{},
		closingSaveOnTimeout:   closingSaveOnTimeout,
	}
	m.shutdown, m.stopBackground = context.WithCancel(context.Background())
	m.goBackground(m.statusDispatcher)
	return m
}

// goBackground counts work under the same lock that closes the manager; closed managers spawn nothing.
func (m *Manager) goBackground(fn func()) bool {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return false
	}
	m.background.Add(1)
	m.mu.Unlock()
	go func() {
		defer m.background.Done()
		fn()
	}()
	return true
}

// Close cancels and joins background work, and restores outstanding stop save-off brackets with a bounded drain.
// It is terminal and idempotent; session command lanes are not joined.
func (m *Manager) Close() {
	m.mu.Lock()
	m.closed = true
	m.mu.Unlock()
	m.stopBackground()
	// Restore outstanding brackets concurrently with the join, then drain and seal once more for late flushes.
	// Join restores locally with closingSaveOnTimeout so they cannot outlive Close or stall shutdown indefinitely.
	var restores sync.WaitGroup
	settle := func(pending map[string]saveOnTarget) {
		for serverID, target := range pending {
			restores.Add(1)
			go func() {
				defer restores.Done()
				m.restoreSaveOnWhileClosing(serverID, target)
			}()
		}
	}
	settle(m.takePendingSaveOn())
	m.background.Wait()
	settle(m.sealPendingSaveOn())
	restores.Wait()
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

// WithTunnelDialer wires the relay dial-back TunnelDialer used by TunnelDial
// (RELAY.md Section 5). Without it, a TunnelDial fails with an internal error.
func (m *Manager) WithTunnelDialer(t TunnelDialer) *Manager {
	m.tunnel = t
	return m
}

// WithBedrockTunneler wires the Bedrock relay QUIC tunnel used by OpenBedrockTunnel/CloseBedrockTunnel
// (docs/app/BEDROCK_TUNNEL.md). Without it, an OpenBedrockTunnel fails with an internal error and a
// CloseBedrockTunnel is a no-op success.
func (m *Manager) WithBedrockTunneler(t BedrockTunneler) *Manager {
	m.bedrock = t
	return m
}

// SetTransferDeadline applies the API's cleanup backstop; non-positive values clear the bound.
func (m *Manager) SetTransferDeadline(d time.Duration) {
	if d < 0 {
		d = 0
	}
	m.transferDeadlineNanos.Store(int64(d))
}

// transferContext bounds one transfer without globally timing out streaming HTTP reads.
// The caller must cancel the returned context.
func (m *Manager) transferContext(ctx context.Context) (context.Context, context.CancelFunc) {
	d := time.Duration(m.transferDeadlineNanos.Load())
	if d <= 0 {
		return context.WithCancel(ctx)
	}
	return context.WithTimeout(ctx, d)
}

// WithWorkerID sets this Worker's own id, stamped on a snapshot publish so the API's publish-time generation
// guard can distinguish a same-Worker re-publish from a different-Worker stale publish.
func (m *Manager) WithWorkerID(id string) *Manager {
	m.workerID = id
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
	case "TunnelDial":
		return m.handleTunnelDial(ctx, cmd)
	case "OpenBedrockTunnel":
		return m.handleOpenBedrockTunnel(cmd)
	case "CloseBedrockTunnel":
		return m.handleCloseBedrockTunnel(cmd)
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
	// Reserve across hydrate to prevent reconnects from concurrently replacing a live or in-flight working set.
	if ok, code, msg := m.reserve(cmd.ServerID); !ok {
		return fail(cmd.CommandID, code, msg)
	}
	defer m.release(cmd.ServerID)

	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	// Bound the download with the per-transfer deadline so a stalled hydrate cannot hang the lane indefinitely.
	transferCtx, cancel := m.transferContext(ctx)
	defer cancel()
	gen, err := m.transfer.Hydrate(transferCtx, cmd.TransferURL, cmd.TransferToken, workingDir)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			fmt.Sprintf("instancemanager: hydrate: %v", err))
	}
	// Declare the served generation only if its local marker was published; a transfer alone is not proof.
	var declaredGeneration *uint64
	if m.recordGeneration(workingDir, cmd.ServerID, gen) {
		declaredGeneration = &gen
	}
	return session.CommandResult{
		CommandID: cmd.CommandID, Success: true, HeldGeneration: declaredGeneration,
	}
}

// recordGeneration is unconditional because hydrate owns the reserved tree it produced.
// Return whether the marker was published; write failures log and leave the API to hydrate again.
func (m *Manager) recordGeneration(workingDir, serverID string, gen uint64) bool {
	if err := writeGeneration(workingDir, gen); err != nil {
		m.logger.Warn("could not record working-set generation",
			"server_id", serverID, "generation", gen, "error", err)
		return false
	}
	return true
}

// Stamp and declare only while the pinned directory remains current; replacement skips the stamp without failing
// publish.
// Repeat the identity check immediately before rename; path resolution inside that rename remains a residual
// race.
func (m *Manager) recordGenerationIfUnchanged(ref *workingDirRef, workingDir, serverID string, gen uint64) bool {
	reason := ""
	guard := func() bool {
		ok, why := ref.current()
		if !ok {
			reason = why
		}
		return ok
	}
	warnSkipped := func() {
		m.logger.Warn("skipped recording working-set generation: the working dir is no longer the directory this snapshot packed",
			"server_id", serverID, "generation", gen, "reason", reason)
	}
	if !guard() {
		warnSkipped()
		return false
	}
	if err := writeGenerationGuarded(workingDir, gen, guard); err != nil {
		if errors.Is(err, errWorkingDirReplaced) {
			warnSkipped()
			return false
		}
		m.logger.Warn("could not record working-set generation",
			"server_id", serverID, "generation", gen, "error", err)
		return false
	}
	return true
}

// restoreSaveTimeout bounds the re-enable-auto-save RCON call on the snapshot
// exit path. It runs on a context detached from the request's (so a cancelled or
// timed-out request still re-enables auto-save), and must therefore carry its own
// deadline so the call cannot hang the goroutine forever.
const restoreSaveTimeout = 30 * time.Second

// Bound concurrent shutdown restores to five seconds; failed-stop restores use the longer restoreSaveTimeout.
const closingSaveOnTimeout = 5 * time.Second

// Retry running-world corruption briefly for non-chunk writers that save-off does not gate.
// Stopped worlds use a single scan.
const snapshotFsckAttempts = 3

// defaultFsckRetryDelay is the default backoff between the running-server fsck
// attempts above. Three attempts with two ~2s gaps stay well within the snapshot
// command budget (control.snapshot_timeout_seconds=600). Tests shrink it to zero.
const defaultFsckRetryDelay = 2 * time.Second

// Bound the async-save settle wait; lack of two identical scans refuses the periodic snapshot.
// Tests shrink the interval and budget.
const (
	defaultSettlePollInterval = 2 * time.Second
	defaultSettleBudget       = 60 * time.Second
)

// handleSnapshot quiesces running worlds with save-off, async save-all, and a settle wait before fsck and
// packing.
// Restore save-on after packing; stopped worlds reserve their ID and need no RCON.
func (m *Manager) handleSnapshot(ctx context.Context, cmd session.Command) session.CommandResult {
	if m.transfer == nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			"instancemanager: no data-plane transfer client configured")
	}
	m.mu.Lock()
	_, running := m.instances[cmd.ServerID]
	m.mu.Unlock()
	// The running flag is read outside any reservation, so a start can register an instance between here and the
	// stopped path's reserve below; the contract table records what that race emits, and this seam is how the test
	// enters it deterministically.
	if hook := m.snapshotAfterRunningCheck; hook != nil {
		hook(cmd.ServerID)
	}
	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	// restore re-enables auto-save after the quiesce. Declared here so it is accessible after the if/else for the
	// explicit restore call between pack and upload. Made idempotent via sync.Once so the deferred safety-net and
	// the explicit call do not double-issue save-on.
	var restore func()
	// pin guards the running path's marker stamp; nil (and unused) on the stopped path,
	// which records no generation. Declared here so the post-upload tail below can see it.
	var pin *workingDirRef
	// Declare only a successfully stamped, still-held generation; stopped snapshots remove scratch and declare
	// nothing.
	var declaredGeneration *uint64
	if running {
		// Pin before quiescence so an old stream's upload tail cannot stamp a replacement tree.
		pin = pinWorkingDir(workingDir)
		defer pin.close()

		// Running snapshots take no reservation: per-stream FIFO orders stops, and the API validates captured regions.
		// Across reconnects, use the identity pin for both marker stamping and displaced-tree cleanup.
		var quiesced bool
		var rawRestore func()
		quiesced, rawRestore = m.quiesceRunning(ctx, cmd.ServerID, workingDir)
		var once sync.Once
		restore = func() { once.Do(rawRestore) }
		defer restore()
		if !quiesced {
			// Refuse an unquiesced periodic snapshot; the final stopped snapshot does not require RCON.
			m.logger.Warn("snapshot refused: could not quiesce running world",
				"server_id", cmd.ServerID, "reason", "quiesce_unavailable")
			return fail(cmd.CommandID, session.CommandErrorTransferFailed,
				"instancemanager: snapshot refused: quiesce_unavailable (could not quiesce running world)")
		}
	} else {
		// Reserve stopped scratch through packing and removal so a racing hydrate cannot mix valid files from two
		// trees.
		if ok, code, msg := m.reserve(cmd.ServerID); !ok {
			return fail(cmd.CommandID, code, msg)
		}
		defer m.release(cmd.ServerID)
	}

	if !running {
		// Require capturable content, not just a marker; propagate read errors instead of claiming the world is
		// absent.
		// Keep "working dir absent" synchronized with the API discriminator and command-error contract.
		entries, err := os.ReadDir(workingDir)
		if err != nil && !os.IsNotExist(err) {
			return fail(cmd.CommandID, session.CommandErrorTransferFailed,
				fmt.Sprintf("instancemanager: snapshot: read working dir: %v", err))
		}
		if !holdsWorkingSet(entries) {
			m.logger.Warn("snapshot refused: working dir absent for stopped id",
				"server_id", cmd.ServerID, "reason", "working_set_absent")
			return fail(cmd.CommandID, session.CommandErrorServerNotFound,
				"instancemanager: snapshot refused: working dir absent (no working set held for this id: "+
					"scratch already GC'd after a published final snapshot, emptied out of band, or never hydrated)")
		}
	}

	// Refuse detected corruption before packing; log fsck I/O failures and defer validation to the API gate.
	if report, err := m.checkWorkingSet(ctx, cmd.ServerID, workingDir, running); err != nil {
		m.logger.Warn("snapshot pre-pack region fsck failed; proceeding without it",
			"server_id", cmd.ServerID, "error", err)
	} else if !report.Healthy() {
		first := report.Corrupt[0]
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			fmt.Sprintf("instancemanager: snapshot refused: %d/%d region files corrupt (e.g. %s: %s)",
				len(report.Corrupt), report.Scanned, filepath.Base(first.Path), first.Reason))
	}

	// Declare the store generation this set was hydrated from so the API can refuse the publish if the store
	// advanced past it. 0 (an unknown/never- hydrated set) leaves the guard to compare against the store's current
	// value.
	baseGeneration := readGeneration(workingDir)
	// Bound the pack+upload with the per-transfer deadline: without it the upload has no deadline at all and could
	// outlive the API's snapshot_timeout indefinitely. The bound is the API budget + a margin (the ack value), so
	// the API-side timeout fires first and this is the cleanup backstop.
	transferCtx, cancel := m.transferContext(ctx)
	defer cancel()

	if running {
		// Restore save-on after packing; upload reads only the spool and may take minutes.
		spoolPath, cleanup, err := m.transfer.PackSnapshot(transferCtx, workingDir)
		if err != nil {
			return fail(cmd.CommandID, session.CommandErrorTransferFailed,
				fmt.Sprintf("instancemanager: snapshot pack: %v", err))
		}
		defer cleanup()
		// Restore save-on immediately after the pack — the working dir is no longer
		// being read, so auto-save can safely resume. The deferred restore() is still
		// in place as a safety net for early returns above this point.
		restore()
		gen, err := m.transfer.UploadSnapshot(transferCtx, cmd.TransferURL, cmd.TransferToken, spoolPath, baseGeneration, m.workerID)
		if err != nil {
			return fail(cmd.CommandID, session.CommandErrorTransferFailed,
				fmt.Sprintf("instancemanager: snapshot upload: %v", err))
		}
		// Stamp and declare only the tree this snapshot packed; never advance a replacement tree's generation.
		if m.recordGenerationIfUnchanged(pin, workingDir, cmd.ServerID, gen) {
			declaredGeneration = &gen
		}
		// A successful publish supersedes only the pinned tree; pass the pin into cleanup to protect a racing
		// hydrate's recovery copy.
		if ok, why := pin.current(); ok {
			m.sweepDisplaced(cmd.ServerID, pin.current)
		} else {
			m.logger.Info("skipped sweeping the displaced recovery tree: the working dir is no longer the directory this snapshot packed",
				"server_id", cmd.ServerID, "reason", why)
		}
	} else {
		// Stopped-id snapshot: the set is at rest, no quiesce bracket needed. Use the
		// combined Snapshot (pack+upload) — there is no save-off to release between them.
		_, err := m.transfer.Snapshot(transferCtx, cmd.TransferURL, cmd.TransferToken, workingDir, baseGeneration, m.workerID)
		if err != nil {
			return fail(cmd.CommandID, session.CommandErrorTransferFailed,
				fmt.Sprintf("instancemanager: snapshot: %v", err))
		}
		// Remove stopped scratch only after publish succeeds, while the reservation still blocks replacement.
		// Do not stamp or declare a generation for scratch being removed.
		m.removeScratch(cmd.ServerID)
	}
	return session.CommandResult{
		CommandID: cmd.CommandID, Success: true, HeldGeneration: declaredGeneration,
	}
}

// checkWorkingSet retries running-world corruption and checks stopped worlds once.
// I/O errors are not retried; cancellation returns ctx.Err() rather than a corruption report.
func (m *Manager) checkWorkingSet(ctx context.Context, serverID, workingDir string, running bool) (regionfsck.Report, error) {
	if !running {
		return regionfsck.CheckWorkingSet(workingDir)
	}
	var report regionfsck.Report
	for attempt := 1; ; attempt++ {
		var err error
		report, err = regionfsck.CheckWorkingSet(workingDir)
		if err != nil || report.Healthy() || attempt >= snapshotFsckAttempts {
			return report, err
		}
		m.logger.Warn("snapshot pre-pack region fsck found corruption on a running world; retrying",
			"server_id", serverID, "attempt", attempt, "corrupt", len(report.Corrupt), "scanned", report.Scanned)
		select {
		case <-ctx.Done():
			// A cancelled/timed-out snapshot must not surface the last (corrupt) report
			// with a nil error — that would misclassify the cancel as "N/N region files
			// corrupt". Return ctx.Err() so the caller routes it through the best-effort
			// branch (logged, transfer not forced) instead.
			return report, ctx.Err()
		case <-time.After(m.fsckRetryDelay):
		}
	}
}

// quiesceRunning uses async save-all plus settling; synchronous flush can trip the Minecraft tick watchdog.
// After acknowledged save-off, restore on a detached context and redial poisoned RCON connections.
func (m *Manager) quiesceRunning(ctx context.Context, serverID, workingDir string) (bool, func()) {
	driverName, mcVersion := m.controlTargetFor(serverID)
	raw, err := m.openControl(ctx, serverID, driverName, mcVersion)
	if err != nil {
		m.logger.Warn("snapshot quiesce: open rcon failed", "server_id", serverID, "error", err)
		return false, func() {}
	}
	// Wrap in resilientControl: a mid-bracket Execute error poisons the rcon connection, so save-all after a
	// timed-out save-off (or save-on after a timed-out save-all) would return ErrConnBroken instantly. The wrapper
	// auto-redials so the bracket's trailing commands still reach the server.
	ctrl := &resilientControl{
		inner: raw,
		dial: func(dialCtx context.Context) (execution.ServerControl, error) {
			return m.openControl(dialCtx, serverID, driverName, mcVersion)
		},
		logger:   m.logger,
		serverID: serverID,
	}

	saveOff := true
	quiesced := true
	if _, err := ctrl.Execute(ctx, "save-off"); err != nil {
		m.logger.Warn("snapshot save-off failed; snapshot will not be quiesced",
			"server_id", serverID, "error", err)
		saveOff = false
		quiesced = false
	} else {
		if _, err := ctrl.Execute(ctx, "save-all"); err != nil {
			m.logger.Warn("snapshot save-all failed", "server_id", serverID, "error", err)
			quiesced = false
		} else if !m.settleWorkingSet(ctx, serverID, workingDir) {
			quiesced = false
		}
	}

	return quiesced, func() {
		defer func() { _ = ctrl.Close() }()
		if !saveOff {
			return
		}
		m.restoreSaveOn(ctx, serverID, ctrl)
	}
}

// Flush asynchronously before graceful termination, disabling auto-save so region writes can settle.
// Failures do not block stop; pendingSaveOn restores survivors and brackets outstanding during shutdown.
func (m *Manager) flushBeforeStopWithDriver(ctx context.Context, serverID, driverName, mcVersion string) bool {
	raw, err := m.openControl(ctx, serverID, driverName, mcVersion)
	if err != nil {
		m.logger.Warn("stop flush: open rcon failed; stopping without a final save",
			"server_id", serverID, "error", err)
		return false
	}
	// Wrap in resilientControl: a save-off failure poisons the rcon connection, so save-all on the same client
	// returns ErrConnBroken instantly. The wrapper auto-redials so save-all degrades to the pre-save-off behavior
	// (flush without quiesce) instead of silently losing the flush entirely.
	ctrl := &resilientControl{
		inner: raw,
		dial: func(dialCtx context.Context) (execution.ServerControl, error) {
			return m.openControl(dialCtx, serverID, driverName, mcVersion)
		},
		logger:   m.logger,
		serverID: serverID,
	}
	defer func() { _ = ctrl.Close() }()

	// Record before sending: a failed save-off round trip may still have disabled auto-save.
	// A sealed ledger refuses the bracket because no shutdown restore can cover a new write.
	written, quiesce := m.markPendingSaveOn(serverID, driverName, mcVersion)

	// Disable auto-save so settleWorkingSet converges quickly even with active players. Best-effort: if save-off
	// fails, save-all still runs, the settle may time out but the flush is no worse than before this fix.
	if !quiesce {
		m.logger.Warn("stop flush: worker is closing; skipping save-off so auto-save cannot be left disabled",
			"server_id", serverID)
	} else {
		_, err := ctrl.Execute(ctx, "save-off")
		// Signal write completion even on error so a shutdown restore never overtakes save-off.
		close(written)
		if err != nil {
			m.logger.Warn("stop flush: save-off failed; proceeding with save-all",
				"server_id", serverID, "error", err)
		}
	}

	if _, err := ctrl.Execute(ctx, "save-all"); err != nil {
		m.logger.Warn("stop flush: save-all failed; stopping without a final save",
			"server_id", serverID, "error", err)
		return false
	}
	if !m.settleWorkingSet(ctx, serverID, filepath.Join(m.scratchDir, serverID)) {
		m.logger.Warn("stop flush: working set did not settle within budget; stopping anyway",
			"server_id", serverID)
		return false
	}
	return true
}

// Restore snapshot auto-save on a detached context; redial and retry once if the existing connection fails.
// Log final failure because the server may remain with auto-save disabled.
func (m *Manager) restoreSaveOn(ctx context.Context, serverID string, ctrl execution.ServerControl) {
	restoreCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), restoreSaveTimeout)
	defer cancel()

	_, err := ctrl.Execute(restoreCtx, "save-on")
	if err == nil {
		return
	}
	m.logger.Warn("snapshot save-on failed on the quiesce connection; redialing",
		"server_id", serverID, "error", err)

	// The quiesce connection is poisoned (a prior failed Execute closed it). Back
	// off briefly inside the restore budget, then redial a fresh connection and
	// retry save-on once.
	select {
	case <-restoreCtx.Done():
		m.logger.Error("snapshot save-on NOT restored; server left with auto-save disabled",
			"server_id", serverID, "error", restoreCtx.Err())
		return
	case <-time.After(m.fsckRetryDelay):
	}

	driverName, mcVersion := m.controlTargetFor(serverID)
	fresh, err := m.openControl(restoreCtx, serverID, driverName, mcVersion)
	if err != nil {
		m.logger.Error("snapshot save-on NOT restored; redial failed, server left with auto-save disabled",
			"server_id", serverID, "error", err)
		return
	}
	defer func() { _ = fresh.Close() }()
	if _, err := fresh.Execute(restoreCtx, "save-on"); err != nil {
		m.logger.Error("snapshot save-on NOT restored after redial; server left with auto-save disabled",
			"server_id", serverID, "error", err)
	}
}

// Restore failed graceful stops with a fresh RCON connection and a detached timeout.
// Pass the captured driver and version because the instance has already been evicted.
func (m *Manager) restoreSaveOnAfterFailedStop(ctx context.Context, serverID, driverName, mcVersion string) {
	if err := m.dialAndSaveOn(ctx, restoreSaveTimeout, serverID, driverName, mcVersion); err != nil {
		m.logger.Error("failed stop: auto-save NOT restored; surviving server is running with auto-save disabled",
			"server_id", serverID, "driver", driverName, "error", err)
		return
	}
	m.logger.Warn("stop failed with the server possibly still alive; re-enabled auto-save on the survivor",
		"server_id", serverID)
}

// Shutdown restore waits for save-off completion, sharing one short budget between the wait and RCON.
// A later failed-stop restore is harmless because save-on is idempotent.
func (m *Manager) restoreSaveOnWhileClosing(serverID string, target saveOnTarget) {
	// Wait for save-off completion before restoring; the wait and dial share one deadline.
	deadline := time.Now().Add(m.closingSaveOnTimeout)
	hold := time.NewTimer(m.closingSaveOnTimeout)
	defer hold.Stop()
	select {
	case <-target.written:
	case <-hold.C:
		// Do not send save-on before an outstanding save-off finishes; log the unresolved bracket for recovery.
		m.logger.Error("worker closing: auto-save NOT restored; the server never confirmed its save-off, so a restore could be overtaken by it",
			"server_id", serverID, "driver", target.driver, "timeout", m.closingSaveOnTimeout)
		return
	}
	if err := m.dialAndSaveOn(context.Background(), time.Until(deadline), serverID, target.driver, target.mcVersion); err != nil {
		m.logger.Error("worker closing: auto-save NOT restored on a server whose stop is still in flight; if it survives the stop it runs with auto-save disabled until the next Worker boot",
			"server_id", serverID, "driver", target.driver, "error", err)
		return
	}
	m.logger.Warn("worker closing: re-enabled auto-save on a server whose stop is still in flight",
		"server_id", serverID)
}

// dialAndSaveOn uses a fresh connection on a detached context bounded by the supplied timeout.
// The captured driver and version remain available after instance eviction.
func (m *Manager) dialAndSaveOn(ctx context.Context, timeout time.Duration, serverID, driverName, mcVersion string) error {
	restoreCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), timeout)
	defer cancel()
	ctrl, err := m.openControl(restoreCtx, serverID, driverName, mcVersion)
	if err != nil {
		return fmt.Errorf("open rcon: %w", err)
	}
	defer func() { _ = ctrl.Close() }()
	if _, err := ctrl.Execute(restoreCtx, "save-on"); err != nil {
		return fmt.Errorf("save-on: %w", err)
	}
	return nil
}

// saveOnTarget retains RCON routing and a write-completion edge for a pre-stop bracket.
// Record before save-off; restore only after written closes, and never open a bracket after the ledger is
// sealed.
type saveOnTarget struct {
	driver    string
	mcVersion string
	written   chan struct{}
}

// markPendingSaveOn returns the channel to close after save-off finishes, even on error.
// A false result forbids save-off because shutdown has sealed the ledger.
func (m *Manager) markPendingSaveOn(serverID, driverName, mcVersion string) (chan struct{}, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.saveOnSealed {
		return nil, false
	}
	written := make(chan struct{})
	m.pendingSaveOn[serverID] = saveOnTarget{driver: driverName, mcVersion: mcVersion, written: written}
	return written, true
}

// clearPendingSaveOn forgets serverID's outstanding save-off, because its stop has
// resolved: the server is either gone (nothing to restore) or about to reach
// restoreSaveOnAfterFailedStop. Without it a later Close would dial a server whose
// stop confirmed termination long ago.
func (m *Manager) clearPendingSaveOn(serverID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.pendingSaveOn, serverID)
}

// takePendingSaveOn hands Close the whole outstanding set and empties it, so the
// restores it issues cannot be issued a second time.
func (m *Manager) takePendingSaveOn() map[string]saveOnTarget {
	return m.drainPendingSaveOn(false)
}

// sealPendingSaveOn is Close's LAST read: it takes the outstanding set and, in the
// same critical section, forbids any further debt. Doing both under one lock is what
// makes it final — a mark either lands before this and is in the map it returns, or
// observes the seal and never opens a bracket at all.
func (m *Manager) sealPendingSaveOn() map[string]saveOnTarget {
	return m.drainPendingSaveOn(true)
}

func (m *Manager) drainPendingSaveOn(seal bool) map[string]saveOnTarget {
	m.mu.Lock()
	defer m.mu.Unlock()
	if seal {
		m.saveOnSealed = true
	}
	pending := m.pendingSaveOn
	m.pendingSaveOn = map[string]saveOnTarget{}
	return pending
}

// settleWorkingSet requires two identical region (mtime, size) scans within settleBudget.
// Scan errors retry within the budget; cancellation or expiry returns false.
func (m *Manager) settleWorkingSet(ctx context.Context, serverID, workingDir string) bool {
	deadline := time.Now().Add(m.settleBudget)
	prev, err := m.scanRegion(workingDir)
	for {
		select {
		case <-ctx.Done():
			return false
		case <-time.After(m.settlePollInterval):
		}
		cur, curErr := m.scanRegion(workingDir)
		if err == nil && curErr == nil && regionStateEqual(prev, cur) {
			return true
		}
		if time.Now().After(deadline) {
			m.logger.Warn("snapshot quiesce: working set did not settle within budget; refusing",
				"server_id", serverID, "budget", m.settleBudget)
			return false
		}
		prev, err = cur, curErr
	}
}

// regionState is a region file's identity for settle detection: its path mapped to
// (mtime, size). Two scans are equal when every file matches on both.
type regionState map[string]struct {
	modTime time.Time
	size    int64
}

// scanRegionState records the (mtime, size) of every .mca under root. An absent
// root yields an empty map (a server with no published working set), not an error.
func scanRegionState(root string) (regionState, error) {
	state := regionState{}
	err := filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			if errors.Is(err, os.ErrNotExist) && p == root {
				return filepath.SkipAll
			}
			return err
		}
		if d.IsDir() || !strings.HasSuffix(d.Name(), ".mca") {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		state[p] = struct {
			modTime time.Time
			size    int64
		}{info.ModTime(), info.Size()}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return state, nil
}

// regionStateEqual reports whether two region-state scans are identical: the same
// set of files, each with the same (mtime, size). Any add/remove/change means the
// save is still in flight.
func regionStateEqual(a, b regionState) bool {
	if len(a) != len(b) {
		return false
	}
	for p, sa := range a {
		sb, ok := b[p]
		if !ok || !sa.modTime.Equal(sb.modTime) || sa.size != sb.size {
			return false
		}
	}
	return true
}

func (m *Manager) handleStart(ctx context.Context, cmd session.Command) session.CommandResult {
	driver, ok := m.drivers[cmd.Driver]
	if !ok {
		return fail(cmd.CommandID, session.CommandErrorDriverUnavailable,
			fmt.Sprintf("instancemanager: driver %q not offered by this Worker", cmd.Driver))
	}

	launchMode, ok := launchModeFor(cmd.LaunchMode)
	if !ok {
		// Unknown launch modes are malformed commands and return INTERNAL.
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: unknown launch mode %q", cmd.LaunchMode))
	}

	// Claim the ID before Start; register the instance under the same lock that releases the claim.
	if ok, code, msg := m.reserve(cmd.ServerID); !ok {
		return fail(cmd.CommandID, code, msg)
	}

	return m.launchReserved(ctx, cmd, driver, launchMode)
}

// launchReserved requires held scratch for both start and restart, then atomically hands the claim to the
// instance.
// Every failure releases the reservation; this path never creates a missing working set.
func (m *Manager) launchReserved(ctx context.Context, cmd session.Command, driver execution.ExecutionDriver, launchMode execution.LaunchMode) session.CommandResult {
	// Require the exact generation marker; marker-only scratch is valid for a fresh 204 hydrate.
	// Keep "working dir absent" synchronized with the API replay discriminator and command-error contract.
	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	if _, err := os.Stat(filepath.Join(workingDir, scratchformat.GenerationMarkerFile)); os.IsNotExist(err) {
		m.release(cmd.ServerID)
		m.logger.Warn("launch refused: working dir absent",
			"server_id", cmd.ServerID, "working_dir", workingDir, "reason", "working_set_absent")
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			fmt.Sprintf("instancemanager: start refused: working dir absent (%s): the hydrate was "+
				"skipped for a working set this Worker does not hold", workingDir))
	}

	inst, err := driver.Start(ctx, execution.InstanceSpec{
		ServerID:         cmd.ServerID,
		WorkingDir:       workingDir,
		MinecraftVersion: cmd.MinecraftVersion,
		JarRelpath:       cmd.JarRelpath,
		LaunchMode:       launchMode,
		// The wire carries the memory LIMIT in bytes; the spec carries it in MiB. 0 stays 0 (unset -> default heap).
		// Truncating to MiB is exact for any real limit (the API only ever sends whole-MiB values).
		MemoryLimitMB: uint32(cmd.MemoryLimitBytes / (1024 * 1024)),
		// The CPU allocation (millicores) is carried as-is onto the spec; no derivation. 0 stays 0 (unset -> default
		// weight).
		CPUMillis: cmd.CPUMillis,
	})
	if err != nil {
		m.release(cmd.ServerID)
		return fail(cmd.CommandID, startErrorCode(err),
			fmt.Sprintf("instancemanager: start: %v", err))
	}

	// Register the instance, then drop the reservation under the same mu: the tracked instance now holds the id, so
	// there is no window where neither the reservation nor the instance claims it (a concurrent duplicate always
	// sees one or the other).
	m.mu.Lock()
	m.instances[cmd.ServerID] = inst
	m.startCmds[cmd.ServerID] = cmd
	delete(m.reserved, cmd.ServerID)
	m.mu.Unlock()
	m.startPumps(cmd.ServerID, inst)

	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// Start status first so its done channel can end log and metrics pumps.
// Close joins all pumps; an already-closed manager starts none.
func (m *Manager) startPumps(serverID string, inst execution.Instance) {
	done := make(chan struct{})
	if !m.goBackground(func() { m.pump(serverID, inst, done) }) {
		return
	}
	if src, ok := inst.(execution.LogSource); ok {
		m.goBackground(func() { m.logPump(serverID, src) })
	}
	m.goBackground(func() { m.metricsPump(serverID, inst, done) })
}

func (m *Manager) handleStop(ctx context.Context, cmd session.Command, graceful bool) session.CommandResult {
	inst, driver, mcVersion, outcome := m.takeStoppableReserve(cmd.ServerID)
	switch outcome {
	case takeNotFound:
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			"instancemanager: server not running")
	case takeInFlight:
		// An in-flight detached stop must return BUSY so the API cannot unassign a process still writing its world.
		return fail(cmd.CommandID, session.CommandErrorBusy,
			"instancemanager: a lifecycle command is already in flight for this server")
	}
	// The id is now reserved across the eviction -> stop-confirmed window so the detached stop is the sole writer;
	// released on every return below.
	defer m.release(cmd.ServerID)
	if err := m.attemptStop(ctx, cmd.ServerID, inst, graceful, driver, mcVersion); err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: stop: %v", err))
	}
	// Retain scratch after stop: the API sends its final snapshot only after this result.
	// Reclaim it only after that stopped snapshot publishes successfully.
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// Inject scratch removal to assert hydrate leftovers are gone before the ID stops being advertised.
var removeScratchTree = os.RemoveAll

// removeScratch runs only after a successful stopped snapshot and preserves data on publish failure.
// Deleted-server scratch is reclaimed separately at registration; displaced recovery copies have their own
// sweep.
func (m *Manager) removeScratch(serverID string) {
	// Sweep hydrate leftovers before deleting scratch: its presence keeps the ID advertised for retry after
	// interruption.
	m.sweepHydrateLeftovers(serverID)
	dir := filepath.Join(m.scratchDir, serverID)
	if err := removeScratchTree(dir); err != nil {
		m.logger.Warn("failed to remove scratch dir after final snapshot",
			"server_id", serverID, "dir", dir, "error", err)
	}
	// The reservation excludes hydrates, so no identity pin is needed, but another stream's sweep can still claim
	// the slot.
	// A declined sweep after scratch removal leaves recovery copies for re-placement or manual cleanup.
	m.sweepDisplaced(serverID, nil)
}

// Rename displaced trees out of their slot and fsync before recursive removal, preventing hydrates from seeing
// partial deletion.
// Recheck running-snapshot identity after rename; uncertain recovery copies are put back rather than deleted.
func (m *Manager) sweepDisplaced(serverID string, stillPinned func() (bool, string)) {
	trash, remove := m.detachDisplacedTree(serverID, stillPinned)
	if !remove {
		return
	}
	// Make the rename durable before the traversal unlinks anything, so a power loss cannot roll it back over a
	// half-deleted tree and put that tree back in the slot. Both still happen after the rename and before the first
	// unlink; they run OUTSIDE the slot claim because they no longer touch the slot, and a world-sized traversal is
	// not something another sweep for this id should have to wait behind.
	if err := syncSweepScratchRoot(m.scratchDir); err != nil {
		return
	}
	_ = removeDisplacedTree(trash)
}

// Claim the slot only through rename, identity recheck, and possible put-back.
// A competing sweep declines without touching it; recursive removal runs outside the claim.
func (m *Manager) detachDisplacedTree(serverID string, stillPinned func() (bool, string)) (string, bool) {
	if !m.claimDisplacedSlot(serverID) {
		return "", false
	}
	defer m.releaseDisplacedSlot(serverID)
	displaced := filepath.Join(m.scratchDir, displacedPrefix+serverID)
	if _, err := os.Lstat(displaced); err != nil {
		return "", false
	}
	trash, err := os.MkdirTemp(m.scratchDir, sweepingPrefix+serverID+"-*")
	if err != nil {
		return "", false
	}
	// MkdirTemp creates the dir; remove it so Rename can use the name.
	_ = os.Remove(trash)
	if err := renameSweptTree(displaced, trash); err != nil {
		return "", false
	}
	pinned, why := true, ""
	if stillPinned != nil {
		pinned, why = stillPinned()
	}
	if !pinned {
		m.putBackSweptTree(serverID, displaced, trash, why)
		return "", false
	}
	return trash, true
}

// claimDisplacedSlot takes this id's displaced-slot claim for the window above, reporting
// whether it was free. Mirrors reserve/release, and like them it must be paired with
// releaseDisplacedSlot on every exit path. It never waits: see the sweepingSlot field.
func (m *Manager) claimDisplacedSlot(serverID string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.sweepingSlot[serverID] {
		return false
	}
	m.sweepingSlot[serverID] = true
	return true
}

// releaseDisplacedSlot drops the claim so the next sweep for the id can take it.
func (m *Manager) releaseDisplacedSlot(serverID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.sweepingSlot, serverID)
}

// Put back real or unreadable recovery trees only into an empty slot; never clear a racing hydrate's slot.
// Junk stays under .sweeping- for boot cleanup; a crash before durable put-back can still lose a recovery copy.
func (m *Manager) putBackSweptTree(serverID, displaced, trash, why string) {
	if holds, err := sweptTreeHoldsWorkingSet(trash); err == nil && !holds {
		// PROVABLY world-less junk is not worth putting back, and putting it back is
		// actively harmful: it occupies the slot against a CONCURRENT sweep that is
		// holding the real thing. Left under .sweeping-, where it is the garbage the boot
		// reclaim expects — which is also what this sweep would have done with it.
		// A tree that could not be classified falls through and is kept.
		return
	}
	back := false
	if _, err := os.Lstat(displaced); os.IsNotExist(err) {
		back = os.Rename(trash, displaced) == nil
	}
	if !back {
		m.logger.Info("left a swept displaced tree for the next boot to reclaim: the working dir was replaced while this snapshot swept it, and it could not go back into the slot",
			"server_id", serverID, "swept_to", trash, "reason", why)
		return
	}
	// The put-back needs the durability its rename out of the slot was about to get: a
	// power loss that rolled it back would strand the tree under a name the next boot
	// reclaims. Best-effort, like every other step of the sweep.
	_ = syncSweepScratchRoot(m.scratchDir)
	m.logger.Info("put back the displaced tree this snapshot was sweeping: the working dir was replaced mid-sweep, so the tree may be the replacing hydrate's recovery copy",
		"server_id", serverID, "retained", displaced, "reason", why)
}

// Inject only the rename out of the slot so tests can race a hydrate without intercepting recovery put-back.
var renameSweptTree = os.Rename

// syncSweepScratchRoot is the fsyncDir sweepDisplaced makes its rename durable with,
// indirected through a package var (mirroring removeDisplacedTree) so a test can pin
// that the sync lands between the rename and the removal, and that a failed sync stops
// the sweep before the traversal unlinks anything — an ordering no power loss can be
// staged to observe. Production always uses fsyncDir.
var syncSweepScratchRoot = fsyncDir

// removeDisplacedTree is the os.RemoveAll sweepDisplaced removes a swept tree with,
// indirected through a package var (mirroring statWorkingDirRef) so a test can land a
// racing hydrate INSIDE the removal — a traversal that takes seconds for a world-sized
// tree — rather than race for it, and can stop it short the way a crash does.
// Production always uses os.RemoveAll.
var removeDisplacedTree = os.RemoveAll

// Sweep per-ID hydrate leftovers before scratch removal, including IDs never placed on this Worker again.
// Boot reclaim is the backstop for interrupted cleanup.
func (m *Manager) sweepHydrateLeftovers(serverID string) {
	entries, err := os.ReadDir(m.scratchDir)
	if err != nil {
		return
	}
	prefix := scratchformat.HydratePrefix + serverID + "-"
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), prefix) {
			_ = os.RemoveAll(filepath.Join(m.scratchDir, e.Name()))
		}
	}
}

// ReclaimDeletedScratches runs asynchronously, reserving each ID while removing hydrate leftovers then scratch.
// Check shutdown between IDs; Close joins the current ID, and displaced recovery copies remain for manual
// recovery.
func (m *Manager) ReclaimDeletedScratches(serverIDs []string) {
	m.goBackground(func() { m.reclaimDeletedScratches(serverIDs) })
}

// reclaimDeletedScratches is the synchronous body of ReclaimDeletedScratches.
// Tests call this directly to avoid timing dependencies on the goroutine.
func (m *Manager) reclaimDeletedScratches(serverIDs []string) {
	for _, id := range serverIDs {
		// Stop only between IDs, when no reservation is held; unreached scratch remains advertised for the next
		// registration.
		if m.shutdown.Err() != nil {
			return
		}
		if err := validateServerID(id); err != nil {
			m.logger.Warn("refusing to reclaim scratch for unsafe server id",
				"server_id", id, "error", err)
			continue
		}
		ok, _, _ := m.reserve(id)
		if !ok {
			// The id is running, has a failed-stop orphan, or is reserved for
			// an in-flight command — skip it rather than interfere.
			continue
		}
		// Delete hydrate leftovers first: scratch presence is what advertises the ID for cleanup retry after
		// interruption.
		m.sweepHydrateLeftovers(id)
		dir := filepath.Join(m.scratchDir, id)
		if _, statErr := os.Stat(dir); statErr == nil {
			if err := os.RemoveAll(dir); err != nil {
				m.logger.Warn("failed to reclaim deleted-server scratch",
					"server_id", id, "dir", dir, "error", err)
			} else {
				m.logger.Info("reclaimed orphaned scratch for deleted server",
					"server_id", id, "dir", dir)
			}
		}
		// NOTE:.displaced-<id> trees are intentionally NOT reclaimed here. They are retained for operator recovery.
		m.release(id)
	}
}

// orphanEntry retains the driver and Minecraft version needed for retry-stop RCON routing and password decoding.
type orphanEntry struct {
	inst      execution.Instance
	driver    string
	mcVersion string
}

// takeOutcome distinguishes an acquired handle, an unknown ID, an in-flight reservation, and a failed-stop
// orphan.
type takeOutcome int

const (
	takeFound takeOutcome = iota
	takeNotFound
	takeInFlight
	takeOrphaned
)

// Share orphan-state text across commands; the API uses the error code to distinguish retryable and settled
// refusals.
const orphanPendingMsg = "instancemanager: server has a failed-stop orphan pending termination"

// takeStoppableReserve captures the instance, driver, and version while atomically reserving the ID.
// Orphans remain recorded until termination is confirmed; the caller must release the reservation.
func (m *Manager) takeStoppableReserve(serverID string) (execution.Instance, string, string, takeOutcome) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if inst, ok := m.instances[serverID]; ok {
		start := m.startCmds[serverID]
		delete(m.instances, serverID)
		delete(m.startCmds, serverID)
		m.reserved[serverID] = true
		return inst, start.Driver, start.MinecraftVersion, takeFound
	}
	// Check reservations before orphans: two stops sharing one handle would let the first release the second's
	// claim.
	if m.reserved[serverID] {
		return nil, "", "", takeInFlight
	}
	if entry, ok := m.orphans[serverID]; ok {
		m.reserved[serverID] = true
		return entry.inst, entry.driver, entry.mcVersion, takeFound
	}
	return nil, "", "", takeNotFound
}

// attemptStop retains failed stops as orphans and closes Bedrock tunnels even on failure to block new joins.
// Use the captured driver and version for pre-stop flush and any later orphan retry.
func (m *Manager) attemptStop(ctx context.Context, serverID string, inst execution.Instance, graceful bool, driverName, mcVersion string) error {
	var preFallback func(context.Context) bool
	if graceful {
		preFallback = func(flushCtx context.Context) bool {
			return m.flushBeforeStopWithDriver(flushCtx, serverID, driverName, mcVersion)
		}
	}
	err := inst.Stop(ctx, graceful, preFallback)
	// Clear the save-off debt only after any failed-stop restore finishes so Close cannot miss an outstanding
	// bracket.
	defer m.clearPendingSaveOn(serverID)
	if err != nil {
		// Record the orphan and hand it to a converger, so the Worker keeps working the stop on its own instead of
		// waiting for an operator to notice. recordOrphan is idempotent on the converger: the retries the converger
		// itself issues land back here and re-record without spawning a second one.
		m.recordOrphan(serverID, inst, driverName, mcVersion)
		// Log entry into the orphan state so command refusals remain diagnosable while convergence attempts
		// termination.
		m.logger.Warn("recorded failed-stop orphan; the process may still be running",
			"server_id", serverID, "driver", driverName, "graceful", graceful, "error", err)
		// Close the Bedrock relay tunnel here too, not only on a confirmed stop: the stop intent is the operator's,
		// and an instance this Worker is still trying to terminate must not keep taking joins for however long
		// convergence takes. Close is idempotent and takes no running check, so the operator can still tear it down by
		// hand either way.
		if m.bedrock != nil {
			m.bedrock.Close(serverID)
		}
		// The graceful path issued save-off before the flush; because the stop failed, the server may still be alive
		// with auto-save disabled. Re-enable it so player progress is not silently lost.
		if graceful {
			m.restoreSaveOnAfterFailedStop(ctx, serverID, driverName, mcVersion)
		}
		return err
	}
	m.mu.Lock()
	delete(m.orphans, serverID)
	m.mu.Unlock()
	if m.bedrock != nil {
		m.bedrock.Close(serverID)
	}
	return nil
}

// takeRunningReserve atomically evicts the instance, captures its start spec, and claims the ID through
// relaunch.
// Check an in-flight reservation before the orphan record so an unresolved stop returns BUSY.
func (m *Manager) takeRunningReserve(serverID string) (execution.Instance, session.Command, takeOutcome) {
	m.mu.Lock()
	defer m.mu.Unlock()
	inst, ok := m.instances[serverID]
	if !ok {
		if m.reserved[serverID] {
			return nil, session.Command{}, takeInFlight
		}
		if _, orphaned := m.orphans[serverID]; orphaned {
			return nil, session.Command{}, takeOrphaned
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
	// Single atomic take: this is the sole decision point for the restart path. It distinguishes all three outcomes
	// without a TOCTOU window.
	inst, start, outcome := m.takeRunningReserve(cmd.ServerID)
	switch outcome {
	case takeNotFound:
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			"instancemanager: server not running")
	case takeOrphaned:
		// A prior stop for this id could not confirm termination, so the process is probably still alive, restarting
		// it is refused, but as the settled state it is, not as "not running". The retry stop (StopServer) is the path
		// that terminates the orphan; only once it confirms does the id become genuinely unknown.
		return fail(cmd.CommandID, session.CommandErrorInvalidState, orphanPendingMsg)
	case takeInFlight:
		// A lifecycle command (e.g. a detached stop from a dropped stream, or a start/ hydrate mid-operation) is
		// already reserved in flight for this id. Rejecting with BUSY rather than SERVER_NOT_FOUND keeps the API from
		// unassigning a server whose process may still be alive.
		return fail(cmd.CommandID, session.CommandErrorBusy,
			"instancemanager: a lifecycle command is already in flight for this server")
	}
	// Resolve the driver and launch mode from the authoritative start spec returned
	// by takeRunningReserve. This is defensive (the recorded spec came from a
	// StartServer that already validated both); on failure we re-register the instance
	// so the still-running process stays tracked and reachable.
	driver, ok := m.drivers[start.Driver]
	if !ok {
		m.restoreRunning(cmd.ServerID, inst, start)
		return fail(cmd.CommandID, session.CommandErrorDriverUnavailable,
			fmt.Sprintf("instancemanager: driver %q not offered by this Worker", start.Driver))
	}
	launchMode, ok := launchModeFor(start.LaunchMode)
	if !ok {
		m.restoreRunning(cmd.ServerID, inst, start)
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: unknown launch mode %q", start.LaunchMode))
	}
	// Keep the ID reserved through stop and relaunch; a failed stop transfers protection to the orphan record.
	if err := m.attemptStop(ctx, cmd.ServerID, inst, true, start.Driver, start.MinecraftVersion); err != nil {
		m.release(cmd.ServerID)
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: restart stop: %v", err))
	}
	// Relaunch with the original start spec; if it fails after stop, API desired-state reconciliation owns
	// recovery.
	res := m.launchReserved(ctx, start, driver, launchMode)
	// Carry the RestartServer's correlation id so the API can match the result to
	// the command it issued, not the internal StartServer command.
	res.CommandID = cmd.CommandID
	return res
}

// Read running and orphan state under one lock; an orphan returns INVALID_STATE because termination is
// unconfirmed.
func (m *Manager) notRunningRefusal(serverID string) (session.CommandErrorCode, string, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, running := m.instances[serverID]; running {
		return 0, "", false
	}
	if _, orphaned := m.orphans[serverID]; orphaned {
		return session.CommandErrorInvalidState, orphanPendingMsg, true
	}
	return session.CommandErrorServerNotFound, "instancemanager: server not running", true
}

func (m *Manager) handleServerCommand(ctx context.Context, cmd session.Command) session.CommandResult {
	if code, msg, refused := m.notRunningRefusal(cmd.ServerID); refused {
		return fail(cmd.CommandID, code, msg)
	}

	driverName, mcVersion := m.controlTargetFor(cmd.ServerID)
	ctrl, err := m.openControl(ctx, cmd.ServerID, driverName, mcVersion)
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

// handleTunnelDial requires a locally running server and returns after splice setup.
// The API dispatches without awaiting the result, so refusals appear only in its diagnostic log.
func (m *Manager) handleTunnelDial(ctx context.Context, cmd session.Command) session.CommandResult {
	if code, msg, refused := m.notRunningRefusal(cmd.ServerID); refused {
		return fail(cmd.CommandID, code, msg)
	}
	if m.tunnel == nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			"instancemanager: tunnel dialer not configured")
	}

	if err := m.tunnel.Dial(ctx, TunnelSpec{
		ServerID:   cmd.ServerID,
		WorkingDir: filepath.Join(m.scratchDir, cmd.ServerID),
		Endpoint:   cmd.TunnelEndpoint,
		Token:      cmd.TunnelToken,
		CAPEM:      cmd.TunnelCAPEM,
	}); err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: tunnel dial: %v", err))
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// handleOpenBedrockTunnel requires a locally running server; orphans return INVALID_STATE.
// Registration returns before asynchronous dial, handshake, and retries.
func (m *Manager) handleOpenBedrockTunnel(cmd session.Command) session.CommandResult {
	if code, msg, refused := m.notRunningRefusal(cmd.ServerID); refused {
		return fail(cmd.CommandID, code, msg)
	}
	if m.bedrock == nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			"instancemanager: bedrock tunneler not configured")
	}

	if err := m.bedrock.Open(BedrockTunnelSpec{
		ServerID:      cmd.ServerID,
		RelayEndpoint: cmd.BedrockRelayEndpoint,
		BedrockPort:   cmd.BedrockPort,
		Token:         cmd.BedrockToken,
		CAPEM:         cmd.BedrockCAPEM,
	}); err != nil {
		return fail(cmd.CommandID, session.CommandErrorInternal,
			fmt.Sprintf("instancemanager: bedrock tunnel open: %v", err))
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// Allow tunnel close even for untracked servers or failed-stop orphans so stale tunnels can always be torn down.
func (m *Manager) handleCloseBedrockTunnel(cmd session.Command) session.CommandResult {
	if m.bedrock != nil {
		m.bedrock.Close(cmd.ServerID)
	}
	return session.CommandResult{CommandID: cmd.CommandID, Success: true}
}

// MaxFileBytes bounds a ReadFile response and an EditFile payload. File access
// rides the control plane for small, interactive files (ARCHITECTURE.md
// Section 7.2), not bulk world data — that moves on the data plane. 4 MiB matches
// the API edge cap; an oversized read or edit is refused with a coded
// FILE_ACCESS_DENIED error rather than streaming megabytes onto the stream.
const MaxFileBytes = 4 * 1024 * 1024

// handleReadFile rejects traversal and symlinks, returning SERVER_NOT_FOUND for absence and FILE_ACCESS_DENIED
// for oversize.
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

// handleEditFile bounds the payload and atomically replaces the target beneath scratch without following
// symlinks.
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

	// Resolve and create parents beneath the root with O_NOFOLLOW, then write and rename through that pinned
	// directory fd.
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

// handleListFiles refuses traversal and symlinks and caps results at MaxDirEntries.
// Missing directories return SERVER_NOT_FOUND; non-directories return FILE_ACCESS_DENIED.
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

// openListDir accepts dot/empty for the root and refuses symlinks in every other component.
// The caller owns the returned directory fd.
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

// readDirEntries caps immediate children and stats them relative to dirFd without following links.
// Duplicate the fd so os.File owns only the copy.
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

// Require a non-empty path component other than dot or dot-dot so handlers cannot access the scratch root or
// escape it.
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

// safeJoin rejects absolute paths and dot-dot components but does not resolve symlinks.
// Handlers must also use openParentBeneath and operate relative to its descriptor.
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

// atomicWriteAt fsyncs a temp and renames it relative to the pinned parent fd, preventing intermediate symlink
// redirection.
// Reject a symlink or directory at the target leaf.
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

	// The replacement is the Worker's creation; give it the directory's owner before it is published, so a server
	// running as another user can still read and rewrite its own file.
	if err := inheritOwner(parentFd, fd); err != nil {
		return fmt.Errorf("setting owner: %w", err)
	}
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

// controlTargetFor reads the running instance's driver and version for RCON routing and charset.
// Untracked servers return empty values.
func (m *Manager) controlTargetFor(serverID string) (driver, mcVersion string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	start := m.startCmds[serverID]
	return start.Driver, start.MinecraftVersion
}

// reserve atomically claims an idle ID; running returns INVALID_STATE, reservations and orphans return BUSY.
// Pair a successful claim with release on every failure path.
func (m *Manager) reserve(serverID string) (ok bool, code session.CommandErrorCode, msg string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, running := m.instances[serverID]; running {
		return false, session.CommandErrorInvalidState, "instancemanager: server already running"
	}
	if _, orphaned := m.orphans[serverID]; orphaned {
		// An orphan is unresolved while convergence runs; return BUSY so the API retries without claiming it is
		// running.
		return false, session.CommandErrorBusy, orphanPendingMsg
	}
	if m.reserved[serverID] {
		// A re-issued duplicate arriving while the original is still in flight after a stream reconnect: reject it as
		// BUSY rather than overlap the original. The original's outcome is unknown, so the API must NOT converge
		// observed=running on this, it keeps the assignment and retries.
		return false, session.CommandErrorBusy, "instancemanager: a lifecycle command is already in flight for this server"
	}
	m.reserved[serverID] = true
	return true, 0, ""
}

// release drops serverID's in-flight reservation so a later command (a retry after a failure, or the next
// lifecycle op) can claim it again.
func (m *Manager) release(serverID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.reserved, serverID)
}

// restoreRunning reverses a takeRunningReserve: it puts the instance and its
// start spec back into the tracked maps and clears the reservation, so the
// still-running process remains reachable. Used when post-eviction validation
// fails (e.g. driver no longer offered) and the process must stay tracked.
func (m *Manager) restoreRunning(serverID string, inst execution.Instance, start session.Command) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.instances[serverID] = inst
	m.startCmds[serverID] = start
	delete(m.reserved, serverID)
}

// pump forwards status and retires exited instances; Close ends it even if a live instance never closes events.
// Closing done releases the corresponding log and metrics pumps.
func (m *Manager) pump(serverID string, inst execution.Instance, done chan struct{}) {
	defer close(done)
	events := inst.Events()
	for {
		select {
		case ev, ok := <-events:
			if !ok {
				// The instance closed its stream: it reached a terminal state on its own. If it was recorded as a
				// failed-stop orphan, forget the record so a later stop for the id is a genuinely unknown server, not a
				// lingering retry target.
				m.forgetOrphanIf(serverID, inst)
				return
			}
			if ev.State == execution.StateCrashed {
				m.forgetIf(serverID, inst)
			}
			m.sendStatus(session.StatusEvent{
				ServerID: ev.ServerID, State: ev.State.String(), Detail: ev.Detail,
				CrashReason: ev.CrashReason.String(),
			})
		case <-m.shutdown.Done():
			// Drop queued status on shutdown because the session no longer drains it; do not retire an orphan whose exit
			// was not observed.
			return
		}
	}
}

// forgetOrphanIf removes only the matching handle; only the successful remover may emit converger retirement.
func (m *Manager) forgetOrphanIf(serverID string, inst execution.Instance) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if e, ok := m.orphans[serverID]; ok && e.inst == inst {
		delete(m.orphans, serverID)
		return true
	}
	return false
}

// ResyncStatus reports tracked instances and unknown failed-stop orphans after registration.
// Snapshot both maps under mu, then release it before sendStatus to avoid lock inversion.
func (m *Manager) ResyncStatus() {
	m.mu.Lock()
	type snap struct {
		serverID string
		state    string
	}
	snaps := make([]snap, 0, len(m.instances)+len(m.orphans))
	for serverID, inst := range m.instances {
		snaps = append(snaps, snap{serverID: serverID, state: inst.Status().String()})
	}
	for serverID := range m.orphans {
		snaps = append(snaps, snap{serverID: serverID, state: orphanUnknownState})
	}
	m.mu.Unlock()

	for _, s := range snaps {
		m.sendStatus(session.StatusEvent{ServerID: s.serverID, State: s.state})
	}
}

// Coalesce latest status per server under backpressure, routing all updates through the pending slot until
// dispatch completes.
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

// statusDispatcher watches shutdown while waiting and sending so a full, undrained sink cannot hold Close.
// Pending statuses are dropped once the session has ended.
func (m *Manager) statusDispatcher() {
	for {
		select {
		case <-m.statusNotify:
		case <-m.shutdown.Done():
			return
		}
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

			select {
			case m.events <- ev:
			case <-m.shutdown.Done():
				return
			}

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

// logPump drops lines under backpressure and reports one aggregate count per congestion episode.
// Shutdown also drops queued lines because no session remains to drain them.
func (m *Manager) logPump(serverID string, src execution.LogSource) {
	dropped := 0
	logs := src.Logs()
loop:
	for {
		select {
		case ev, ok := <-logs:
			if !ok {
				break loop
			}
			select {
			case m.logs <- session.LogEvent{ServerID: ev.ServerID, Line: ev.Line, Stream: mapLogStream(ev.Stream)}:
				if dropped > 0 {
					m.reportDroppedLogs(serverID, dropped)
					dropped = 0
				}
			default:
				dropped++
			}
		case <-m.shutdown.Done():
			break loop
		}
	}
	if dropped > 0 {
		m.reportDroppedLogs(serverID, dropped)
	}
}

// Report a congestion episode once in Worker logs and best-effort in-band, without displacing a real line.
func (m *Manager) reportDroppedLogs(serverID string, dropped int) {
	m.logger.Warn("dropped log lines; sink full", "server_id", serverID, "count", dropped)
	select {
	case m.logs <- session.LogEvent{
		ServerID: serverID,
		Line:     fmt.Sprintf("[mcsd] dropped %d earlier log line(s); control-plane log stream backlogged", dropped),
		Stream:   session.LogStreamStderr,
	}:
	default:
	}
}

// metricsPump emits up-only samples when stats are unavailable and drops samples under backpressure.
// Report one aggregate warning per congestion episode.
func (m *Manager) metricsPump(serverID string, inst execution.Instance, done chan struct{}) {
	stats, _ := inst.(execution.StatsSource)

	// Cancel sampling on instance teardown or manager shutdown, and bound each sample by the interval.
	// Count the teardown watcher too so Close joins it.
	pumpCtx, cancel := context.WithCancel(m.shutdown)
	defer cancel()
	m.goBackground(func() {
		<-done
		cancel()
	})

	dropped := 0
	for {
		// Watch shutdown while waiting for the next tick so Close never waits out the metrics interval.
		stop := false
		select {
		case <-done:
			stop = true
		case <-m.shutdown.Done():
			stop = true
		case <-m.clock.After(m.metricsInterval):
		}
		if stop {
			if dropped > 0 {
				m.reportDroppedMetrics(serverID, dropped)
			}
			return
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
			if dropped > 0 {
				m.reportDroppedMetrics(serverID, dropped)
				dropped = 0
			}
		default:
			dropped++
		}
	}
}

// Report aggregate metrics drops only in Worker logs; numeric samples cannot carry an honest in-band loss
// marker.
func (m *Manager) reportDroppedMetrics(serverID string, dropped int) {
	m.logger.Warn("dropped metrics samples; sink full", "server_id", serverID, "count", dropped)
}

// Bound a sample to one interval so an unresponsive Engine cannot stall cadence indefinitely.
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

// launchModeFor maps the command's wire launch-mode name to the execution LaunchMode, reporting false for an
// unrecognized name. An empty name (an unset field) maps to LaunchModeJar, so a command from an API that does
// not set the field launches exactly as before this field existed.
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

// startErrorCode classifies a driver Start failure into a CommandResult error code. A driver (the container
// driver) wraps a known operational failure with a sanitized execution sentinel so the API can surface a
// friendlier 409 reason than the generic one; any other failure stays internal.
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

// failFileAccess builds a CommandErrorFileAccessDenied result carrying the specific reason that refines it. The
// API maps the reason to an honest problem reason and HTTP status instead of a blanket invalid_path.
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
