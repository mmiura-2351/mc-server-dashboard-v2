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
	"sync/atomic"
	"time"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/rcon"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/regionfsck"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// controlFunc opens an execution.ServerControl (RCON) for a running server,
// used by ServerCommand forwarding. driver is the execution driver that runs the
// server (the one recorded on its StartServer command), so the dial host can be
// resolved per the driver's topology — a container driver with a configured
// network reaches RCON over the network, every other driver over the host
// loopback (issue #218). mcVersion is the server's Minecraft version (the one on
// the same StartServer command), which decides the charset its server.properties
// -- and so the RCON password -- is read in (issue #3116).
type controlFunc func(ctx context.Context, serverID, driver, mcVersion string) (execution.ServerControl, error)

// resilientControl wraps a ServerControl and auto-redials on ErrConnBroken
// (#919). The rcon client poisons the connection on any Execute error, so a
// multi-command bracket (save-off → save-all → save-on, or the stop sequence)
// loses all commands after the first failure. This wrapper transparently
// redials once per Execute call so trailing commands in a bracket survive a
// mid-sequence timeout or error.
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
	// Hydrate downloads the working set from url into workingDir (an empty/204
	// response leaves it empty). It returns the authoritative store GENERATION the
	// API served (the value of its response header, issue #763); 0 when the header
	// is absent (a server with no published snapshot, or an older API).
	Hydrate(ctx context.Context, url, token, workingDir string) (uint64, error)
	// PackSnapshot packs workingDir into a tar spool file and returns its path.
	// The returned cleanup function removes the spool; the caller must invoke it
	// when the spool is no longer needed. Only the pack reads the working
	// directory, so the caller can release the quiesce bracket after PackSnapshot
	// returns (issue #1710).
	PackSnapshot(ctx context.Context, workingDir string) (spoolPath string, cleanup func(), err error)
	// UploadSnapshot streams the spool file at spoolPath to url, declaring
	// baseGeneration and workerID for the API's publish-time generation guard
	// (issue #847). It returns the NEW store generation the publish produced.
	UploadSnapshot(ctx context.Context, url, token, spoolPath string, baseGeneration uint64, workerID string) (uint64, error)
	// Snapshot packs workingDir and uploads it to url, declaring baseGeneration as
	// the store generation this working set was hydrated from (issue #847) and
	// workerID as this Worker's own id (issue #847 bug 3): the API refuses the publish
	// if the store has since advanced past baseGeneration AND current was published by
	// a different worker. It returns the NEW authoritative store generation the publish
	// produced (the value of the API's response header, issue #763); 0 when the header
	// is absent (an older API).
	Snapshot(ctx context.Context, url, token, workingDir string, baseGeneration uint64, workerID string) (uint64, error)
}

// TunnelDialer is the relay dial-back Port (RELAY.md Section 5): for one player
// session it dials the relay's tunnel listener over TLS, presents the token, dials
// the local server's loopback game port, and splices the two. Dial returns once
// the splice is established (or with an error on dial/handshake failure); the
// splice runs on the adapter's own long-lived context, off this command, and is
// torn down on Worker shutdown.
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

// BedrockTunneler is the Bedrock relay QUIC tunnel Port
// (docs/app/BEDROCK_TUNNEL.md, issue #1546): Open starts — or, for a repeated
// spec, idempotently confirms — the per-server QUIC tunnel that forwards
// RakNet datagrams to the container's Geyser port, reconnecting with backoff
// while it drops; Close tears it down gracefully (CONNECTION_CLOSE plus every
// per-flow socket). Both run off the caller: Open returns once the tunnel is
// registered, not once the handshake completes, and Close only signals
// teardown, so neither blocks the calling command.
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
	// workerID is this Worker's own id, stamped on a snapshot publish so the API's
	// publish-time generation guard can tell a same-Worker re-publish from a
	// different-Worker stale publish (issue #847 bug 3). Empty until WithWorkerID is
	// called (older wiring / tests): the guard then treats the publisher as unknown.
	workerID string

	clock           session.Clock
	metricsInterval time.Duration

	// fsckRetryDelay is the backoff between pre-pack fsck attempts for a RUNNING
	// server's periodic snapshot (#907). A non-chunk writer can still tear a region
	// just after the async save settles, so a transient torn read must not veto the
	// snapshot; the check is retried snapshotFsckAttempts times with this delay. It
	// is a field (not a const) so tests can shrink it to zero; the stopped-server
	// (at-rest, fail-closed) path does not retry. Defaulted in New.
	fsckRetryDelay time.Duration

	// settlePollInterval / settleBudget tune the quiesce settle-wait for a RUNNING
	// server's periodic snapshot (#907): after the async save-all, the working set's
	// .mca files are polled every settlePollInterval and considered settled once two
	// consecutive scans observe identical (mtime, size) for every region file, bounded
	// by settleBudget. They are fields (not consts) so tests can shrink them, mirroring
	// fsckRetryDelay. Defaulted in New.
	settlePollInterval time.Duration
	settleBudget       time.Duration

	// scanRegion reads the (mtime, size) of a working set's .mca files for the
	// settle-wait. It is a field defaulted to scanRegionState in New so a test can
	// inject a deterministic "still changing" / "now stable" sequence without racing
	// the real filesystem.
	scanRegion func(root string) (regionState, error)

	// snapshotAfterRunningCheck, when non-nil, runs between handleSnapshot's
	// UNRESERVED running check and the stopped path's reserve() -- the window in
	// which a start can register an instance and turn that reserve() into the
	// "already running" INVALID_STATE refusal. Production leaves it nil; the
	// contract test sets it so the {SnapshotTrigger, snapshot_reserve_race} row of
	// proto/contract/command_error_contract.json is driven by the real interleaving
	// instead of asserted by prose (issue #2472). A field, not a package var, so it
	// belongs to the manager under test -- mirroring scanRegion above.
	snapshotAfterRunningCheck func(serverID string)

	// orphanProbeInterval / orphanProbeMaxInterval are the failed-stop-orphan
	// converger's probe cadence and its exponential-backoff cap (issue #2475).
	// They are fields (not consts) so tests can shrink them to milliseconds,
	// mirroring fsckRetryDelay. Defaulted in New; the waits go through m.clock.
	orphanProbeInterval    time.Duration
	orphanProbeMaxInterval time.Duration

	// shutdown is cancelled by Close and is the lifetime the background goroutines
	// the manager owns run under — the orphan convergers (issue #2493), the status
	// dispatcher, and the per-instance status/log/metrics pumps (issue #2777). Each
	// parks on it alongside whatever it normally waits for, so closing the manager
	// ends everything it started instead of leaving goroutines running against a
	// manager nobody owns any more. Two manager-owned goroutines do not park on it
	// that way, for different reasons: the metrics pump's teardown watcher parks on
	// the instance's done channel, so the cancellation reaches it one hop away,
	// through the status pump whose return closes that channel; and the
	// deleted-scratch reclaim reads it only at the top of its per-id loop, after a
	// release and before the next reserve (issue #2933), so the id already in
	// flight runs to its release uninterruptibly and Close reaches that one by
	// waiting (issue #2878).
	// background counts every one of them — both of those included — so Close can
	// join them: "signalled" is not "gone", and a signal a goroutine cannot act on
	// yet still has to be waited out. A converger caught mid-round is still driving
	// driver calls, and a pump past its WaitGroup Done still holds its frame.
	shutdown       context.Context
	stopBackground context.CancelFunc
	background     sync.WaitGroup

	// transferDeadlineNanos bounds a single data-plane transfer (snapshot upload /
	// hydrate download) Worker-side (issue #874). The session pushes it from the
	// RegisterAck after registration (SetTransferDeadline); the hydrate/snapshot
	// handlers apply it as a per-transfer context deadline. It is read on lane
	// goroutines and written on the session goroutine, so it is atomic. 0 (an
	// older API, or before the first ack) leaves the transfer unbounded as before.
	transferDeadlineNanos atomic.Int64

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
	// #251). Keeping the Instance and its driver name here lets a retry re-attempt
	// the driver Stop against the same handle and resolve RCON identically (issue
	// #1712), reporting success only on confirmed termination; until then every
	// other command over the id is refused naming the orphan, so no path claims
	// the server is not running about a process that is probably alive (issue
	// #2466). The refusal CODE splits by whether the refused command will succeed
	// once the orphan converges (issue #2476): BUSY for start / hydrate /
	// stopped-id snapshot through reserve, which the API retries until it is let
	// through; INVALID_STATE for restart through takeRunningReserve and console /
	// relay tunnel dial / Bedrock tunnel open through notRunningRefusal, which are
	// refused for what the state IS and are never executed later. CloseBedrockTunnel is
	// the deliberate exception: it takes no running check and stays a success, so
	// a tunnel that outlived its server can still be torn down. The instance's
	// status pump clears the record if the orphan finally exits on its own, and
	// the per-id converger (issue #2475) drives it to a settled outcome meanwhile
	// instead of leaving the id guarded until an operator intervenes.
	orphans map[string]orphanEntry
	// converging marks server ids that already have a convergeOrphan goroutine
	// running, so the orphan its own retry stop re-records does not spawn a second
	// one (issue #2475). It is claimed with the record in recordOrphan and cleared
	// in currentOrphan, in the same critical section that observes the record gone,
	// so the flag can never outlive its goroutine or block its successor.
	converging map[string]bool
	// pendingSaveOn names the servers a pre-stop flush MAY have disabled auto-save on
	// and whose stop has not resolved yet — the save-off bracket
	// flushBeforeStopWithDriver opens and only a confirmed termination (nothing to
	// restore) or restoreSaveOnAfterFailedStop (the survivor) closes. It holds the
	// RCON target the restore needs, because by then the instance is evicted from
	// startCmds and controlTargetFor would answer empty (issue #2021).
	//
	// "MAY have" is the honest tense at both ends, and both are deliberate. The entry
	// is written BEFORE save-off goes on the wire, because rcon.Execute writes the
	// command and only then waits for a reply, so a failed round trip does not mean
	// the server did not run it; and it is removed only once attemptStop has done
	// whatever its outcome calls for, never before the restore that outcome triggers.
	// Both ends err towards one redundant, idempotent save-on rather than towards a
	// surviving world that saves nothing.
	//
	// A flush can also be REFUSED an entry, once saveOnSealed is set, and then it does
	// not disable auto-save at all. That is the third side of the same bias: past the
	// seal there is no longer anyone to restore, so the only safe bracket is the one
	// that is never opened.
	//
	// It exists so the bracket can be closed by the WORKER rather than by the next
	// boot (issue #3166). The escalation between the two points is tens of seconds
	// to minutes long — the kill call plus the post-kill exit confirmation after a
	// flush that succeeded, the whole stopDeadline before them after one that did
	// not — and a Worker that goes down inside it leaves a SURVIVING Minecraft
	// container with auto-save off: the container is not a Compose service, so
	// nothing stops it on the way out. Close drains this map and issues the save-on
	// itself, beside its join rather than in front of it and bounded by
	// closingSaveOnTimeout, so its worst-case bound is unchanged — and it reaches the
	// one lane no timer can, the operator stop that Runner.serve abandons without
	// waiting (#3168).
	pendingSaveOn map[string]saveOnTarget
	// saveOnSealed forbids any further entry in pendingSaveOn. Close sets it in the
	// same critical section as its LAST read of the ledger, which is what makes that
	// read final: a flush either lands its mark before the seal and is in the map
	// Close took, or observes the seal and does not disable auto-save at all. Without
	// it the drain would need a loop, and the loop's termination would be an argument
	// about whether some other lane can keep re-arming.
	saveOnSealed bool
	// closingSaveOnTimeout bounds each save-on Close issues from pendingSaveOn,
	// which is the whole of the latency this change can add to a shutdown. A field
	// (not the const it defaults to) so a test can shrink it, mirroring
	// fsckRetryDelay. Read only by Close, which has already set closed under mu, so
	// it needs no synchronisation of its own.
	closingSaveOnTimeout time.Duration
	// closed records that Close has run, so a command still in flight during
	// shutdown does not spawn a background goroutine nothing will ever join — a
	// converger for an orphan it records (issue #2493), or the pumps for an
	// instance it starts (issue #2777). It is set under the SAME mu that guards
	// every spawn, which is what keeps the WaitGroup honest: a spawn either
	// happens before Close takes the lock (and is counted, so Close waits for it)
	// or observes the flag and does not happen at all — never an Add racing the
	// Wait.
	closed bool
	// reserved marks a server id as having a mutating lifecycle command in flight so
	// a duplicate re-issued after a stream reconnect cannot overlap the original
	// (issue #780). It is claimed under mu and held across the long operation, then
	// released (or, on a successful start, handed off to the registered instance
	// under the same mu so the id is never unclaimed). A command arriving while the
	// id is reserved is rejected with BUSY (issue #824) — distinct from the settled
	// "already running" INVALID_STATE, since the in-flight command's outcome is not
	// yet known, so the API retries rather than converging on it. Which commands
	// reserve, and over which window:
	//   - StartServer: before driver.Start, until the instance is registered, so a
	//     re-issued duplicate cannot pass the running check and launch a second
	//     process while the original is still mid-driver.Start (the primary window).
	//   - HydrateTrigger: across the transfer, so a re-issued hydrate (or a racing
	//     start/snapshot) cannot write the same working set concurrently.
	//   - StopServer / RestartServer: across the eviction -> stop-confirmed window
	//     (and a restart's relaunch). takeStoppableReserve / takeRunningReserve evict
	//     the instance AND reserve under one mu, so the id stays claimed while the
	//     detached stop confirms termination — a re-sent stop then gets BUSY (#824),
	//     not SERVER_NOT_FOUND, and the API keeps the assignment instead of unassigning
	//     over a still-live process.
	//   - SnapshotTrigger: only the STOPPED-id path (the set is at rest and the API
	//     has typically unassigned), to block a racing hydrate from rewriting the dir
	//     mid-pack. A running-id snapshot does NOT reserve: a live instance already
	//     blocks reserve(), and its save-off bracket is the quiesce.
	// The file handlers (ReadFile / EditFile / ListFiles) act atomically on individual
	// files and take no reservation.
	reserved map[string]bool

	// sweepingSlot marks a server id whose .displaced-<id> slot a displaced sweep is
	// currently deciding about — from the Lstat that finds the tree through the rename
	// out of the slot to the put-back or the commit to remove (issue #3118, PR #3121
	// review round 3). Only that window is claimed, not the world-sized traversal that
	// follows it.
	//
	// It exists because the slot holds ONE tree and two sweeps for one id can reach it
	// at once: running-id snapshots take no reservation (#829 item 4), so an old dropped
	// stream's sweep can overlap a newer one's. While both are past their rename,
	// whatever one of them puts back occupies the slot against the other — and the other
	// may be holding the hydrate's live recovery copy, which then goes under .sweeping-
	// for the next boot to delete. No rule about the individual trees closes that: a tree
	// whose classification FAILED is retained on purpose (putBackSweptTree), and that
	// retention is what costs the other tree.
	//
	// The claim is NON-BLOCKING: a sweep that finds the id claimed declines, renaming
	// nothing and leaving the tree where it is. Declining is the established posture for
	// this GC — a declined sweep leaks one tree until the next successful snapshot
	// reclaims it (#906/#2291) — and it keeps a sweep from waiting on another sweep's
	// filesystem work. It is NOT the per-id reservation #829 item 4 declined: that one
	// would span a whole running-id snapshot and reject concurrent commands with BUSY,
	// while this is in-process, covers a handful of syscalls in the GC tail, and refuses
	// no command.
	sweepingSlot map[string]bool

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

// goBackground starts fn as a goroutine the manager owns and Close joins. It
// reports whether the goroutine was started: a CLOSED manager starts nothing,
// because Close has already run the Wait that a later Add would race — and,
// with the counter back at zero, panic against. The Add happens under the SAME
// mu that Close sets closed under, so a start either lands before Close takes
// the lock and is therefore waited for, or does not happen at all.
//
// recordOrphan performs the same Add inline rather than calling this: its spawn
// decision must also claim the per-id converging flag, and both have to be taken
// in one critical section.
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

// Close ends EVERY goroutine the manager started and waits for them to exit: the
// failed-stop-orphan convergers (issue #2493), the status dispatcher New starts,
// the deleted-scratch reclaim ReclaimDeletedScratches starts (issue #2878), and
// the per-instance status/log/metrics pumps startPumps starts (issue #2777).
// Nothing joined the latter group before, and none of them ended on their own: a
// pump parks on an instance channel that a server still running never closes, and
// the dispatcher parks on a notify channel nothing ever closes. In the Worker that
// only ever showed up at process exit; in the test binary, where a manager's
// lifetime is one test, one package run left ~91k of them parked against managers
// their tests had finished with.
//
// The wait is the point: a converger caught mid-round is inside a driver call, so
// returning on the signal alone would leave exactly the window this closes. The
// probe is bound to the same cancelled context and returns at once, but a retry
// stop already in flight is not interruptible by design — the driver detaches the
// escalation from its caller's context so a dropped stream cannot abandon a
// half-stopped container (issue #770) — so Close can take that stop's remaining
// budget to return. Waiting out a stop the Worker is already driving is the right
// end of that trade: the alternative is exiting while a SIGKILL escalation is
// half-issued. The pumps and the dispatcher add nothing to that bound: each parks
// on the shutdown alongside its own wait and leaves at once. A reclaim in flight
// does add to it, for the same reason and by the same trade, but only for the ONE
// id it is on: that id's body is uninterruptible filesystem work, and the
// alternative is exiting mid-RemoveAll and leaving a half-removed working set
// behind (issue #2878). The ids after it cost nothing — the reclaim reads the
// shutdown at the top of its per-id loop, where it holds neither a reservation nor
// a half-done removal, and returns (issue #2933). The reservation it holds across
// the in-flight id is NOT part of the trade — reserved is in-memory and dies with
// the process.
//
// WHAT IS IN FLIGHT IS DROPPED, deliberately, and this changes nothing an operator
// or the API can observe. Close runs after the session runner has returned
// (main.go), so by then nothing drains the merged status/log/metrics streams —
// which is also why the dispatcher must observe the shutdown ON ITS SEND and not
// only between events, or a full sink would hold Close forever. The alternative,
// draining first, would deliver into channels no session reads, and before this
// the same events died with the process anyway. Each site states its own drop:
// pump, logPump, metricsPump, statusDispatcher.
//
// THIS WAIT IS WHAT compose.yaml's stop_grace_period ON THE WORKER IS SIZED FOR
// (issue #2934, owner decision 2026-09-23). Under compose the process gets that
// long between SIGTERM and SIGKILL, and the two numbers are a pair: change this
// bound and the compose value is wrong, and vice versa.
//
// The bound is roughly 280 s. A retry stop already inside inst.Stop costs
// flushTimeout + stopDeadline + the kill call + the post-kill exit confirmation +
// restoreSaveTimeout (90 + 100 + 30 + 30 + 30 s at the containerdriver defaults).
// Those last three ADD rather than share: the kill runs on a context detached from
// stopDeadline so it reaches the daemon even when the earlier phases consumed the
// whole budget, and waitExitDone honours no context at all. The one reclaim id in
// flight costs a RemoveAll per tree instead (3.4 s for a 4 GB / 21k-file working
// set, measured warm on ext4). Docker's 10 s default cut both.
//
// ONE LEG IS WHY THE VALUE EXISTS, and the other two are honest about not needing
// it:
//
//   - reserved, orphans and converging are in-memory and die with the process
//     either way — at 10 s or at 280 s.
//   - a reclaim cut anywhere in its per-id body leaves <scratch>/<id> on disk, and
//     that dir IS the advertisement that re-offers the id: the held-set scans
//     report it, the API re-derives unknown_held_server_ids from that report, and
//     the next registration's reclaim finishes the removal. The sweep order in
//     reclaimDeletedScratches is what makes this true at every point in the body
//     rather than most of them. The residual is a dir holding nothing but its
//     generation marker: not advertised, and not data.
//   - a retry stop cut between the flush's save-off and restoreSaveOnAfterFailedStop
//     leaves a SURVIVING MC container with auto-save disabled: the container is not
//     a compose service, so nothing stops it on the way out, and
//     containerdriver.sweepSaveOn (issue #1710) issues its RCON save-on to every
//     running orphan only at the NEXT Worker boot — which `restart: unless-stopped`
//     does not bring after an explicit stop, so `docker compose down` leaves that
//     container running and saving nothing until the stack returns.
//     THIS LEG NO LONGER WAITS FOR THE GRACE PERIOD: Close settles it up front,
//     from pendingSaveOn, at the moment the shutdown starts (issue #3166), and again
//     after the join for a bracket that was opened in between. What the timer still
//     buys on this leg is the escalation itself — a stop the Worker is already driving
//     gets to finish rather than being cut half-issued.
//     The drain's own cost is closingSaveOnTimeout (5 s), paid once and only when a
//     server does not answer, which leaves this bound where it was.
//
// The cost is bounded: the value is a CEILING, not a wait, so a Close with nothing
// in flight still returns in milliseconds and the timer is never observed. What it
// does NOT reach is the same escalation dispatched as an operator StopServer: that
// runs on a session lane nothing joins, so it is abandoned the moment run()
// returns, at any value. Closing that one is a change to the lanes, not to this
// timer (#3168) — though the auto-save half of it is already covered, because the
// drain above is keyed on the outstanding save-off rather than on which lane
// issued it.
//
// TWO CASES REMAIN UNCOVERED, by the drain and by the timer alike, because neither
// is a shutdown: a host power loss inside the bracket, and the MC server crashing
// inside it. Nothing runs Close in the first, and in the second the world the
// save-off was protecting is already lost. The only repair for them is
// containerdriver.sweepSaveOn at the next boot, which reaches a still-running
// container whenever a boot follows — and does not arrive at all while the stack
// stays down.
//
// Close is idempotent and terminal: it is safe to call on a manager that has
// already been closed (the flag is monotonic, cancelling a cancelled context is a
// no-op, and a settled WaitGroup returns from Wait immediately), and a closed
// manager still records orphans (the record is what guards the id) and still
// registers a started instance, but spawns no convergers and no pumps for them.
func (m *Manager) Close() {
	m.mu.Lock()
	m.closed = true
	m.mu.Unlock()
	m.stopBackground()
	// Settle the outstanding save-off debts BESIDE the join, never in front of it
	// (issue #3166). Started here, each restore overlaps the escalation Close is
	// already waiting out, so a Close that had work to do pays nothing for them and
	// one with nothing outstanding starts none.
	//
	// THE LEDGER IS READ TWICE, and the second read is why. A stop dispatched before
	// the shutdown began has its own RCON dial between the command and its save-off —
	// a TCP connect plus an AUTH handshake, up to rcon's 30 s ceiling — so it can open
	// a bracket long after the first read, and its lane is one Close never joins
	// (#3168): the Worker would exit under a survivor with auto-save off. The second
	// read happens after the join, when nothing Close joins is left to arm anything,
	// and it SEALS the ledger in the same critical section, so a flush that arrives
	// later declines to disable auto-save instead. Two passes, no loop, and the
	// termination argument is local: after the seal no debt can exist.
	//
	// The second pass costs no extra time. Both passes share one WaitGroup and the
	// first pass's restores are already bounded, so the join below ends no earlier
	// than they do — the wait is still one closingSaveOnTimeout past the join, not two.
	//
	// THE WAIT IS JOINED AND BOUNDED, and both halves are required. Unjoined, the
	// process exit that follows Close (main.go returns immediately after) kills a
	// restore still in flight, which would move the loss rather than close it.
	// Unbounded, a server that never answers would put the failure path's generous
	// restoreSaveTimeout on a shutdown leg that nothing overlaps. So each restore
	// carries closingSaveOnTimeout instead — 5 s, derived at that constant — and
	// THE WHOLE ADDED LATENCY OF THIS CHANGE IS THAT ONE BOUND, paid once however
	// many servers are draining, and only when a server does not answer.
	//
	// Close's ~280 s worst case is therefore unchanged, and so is the compose value
	// paired with it: 5 s is well inside the restoreSaveTimeout leg that bound already
	// counts. What CAN get slower is the best case — a quiesced stop that resolves in
	// the same instant leaves no join for its restore to hide behind, so a Close that
	// would have returned at once pays up to those 5 s, and only if the dial hangs
	// rather than being refused, which a just-exited container normally is.
	//
	// They ride a LOCAL WaitGroup, not m.background: the flag above is already set,
	// so goBackground would (correctly) refuse them, and joining them here is what
	// keeps them from outliving the manager the way issue #2777's pumps did.
	var restores sync.WaitGroup
	settle := func(pending map[string]saveOnTarget) {
		for serverID, target := range pending {
			restores.Add(1)
			go func() {
				defer restores.Done()
				m.restoreSaveOnWhileClosing(serverID, target.driver, target.mcVersion)
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

// WithBedrockTunneler wires the Bedrock relay QUIC tunnel used by
// OpenBedrockTunnel/CloseBedrockTunnel (docs/app/BEDROCK_TUNNEL.md, issue
// #1546). Without it, an OpenBedrockTunnel fails with an internal error and a
// CloseBedrockTunnel is a no-op success.
func (m *Manager) WithBedrockTunneler(t BedrockTunneler) *Manager {
	m.bedrock = t
	return m
}

// SetTransferDeadline records the per-transfer bound the API advertised in
// RegisterAck (session.TransferDeadlineSetter, issue #874). The hydrate/snapshot
// handlers apply it as a context deadline so an upload/download cannot outlive
// the API's budget indefinitely (#869). A non-positive value clears the bound,
// leaving transfers unbounded as before.
func (m *Manager) SetTransferDeadline(d time.Duration) {
	if d < 0 {
		d = 0
	}
	m.transferDeadlineNanos.Store(int64(d))
}

// transferContext derives the context a data-plane transfer runs under: the
// request ctx bounded by the configured transfer deadline (issue #874) when one
// is set, else the request ctx unchanged. The per-request deadline is the clean
// mechanism — it bounds one transfer without capping the http.Client's streaming
// reads globally. The returned cancel is always non-nil and must be called.
func (m *Manager) transferContext(ctx context.Context) (context.Context, context.CancelFunc) {
	d := time.Duration(m.transferDeadlineNanos.Load())
	if d <= 0 {
		return context.WithCancel(ctx)
	}
	return context.WithTimeout(ctx, d)
}

// WithWorkerID sets this Worker's own id, stamped on a snapshot publish so the
// API's publish-time generation guard can distinguish a same-Worker re-publish
// from a different-Worker stale publish (issue #847 bug 3).
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
	// Reserve the id for the duration of the transfer so a re-issued HydrateTrigger
	// (or a racing StartServer/SnapshotTrigger) cannot write the same working set
	// concurrently with the original after a stream reconnect (issue #780). The
	// reservation also subsumes the running / failed-stop-orphan preconditions —
	// hydrating either would replace the working set out from under a live process
	// (issue #251) — and is always released on return.
	if ok, code, msg := m.reserve(cmd.ServerID); !ok {
		return fail(cmd.CommandID, code, msg)
	}
	defer m.release(cmd.ServerID)

	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	// Bound the download with the per-transfer deadline (issue #874) so a stalled
	// hydrate cannot hang the lane indefinitely.
	transferCtx, cancel := m.transferContext(ctx)
	defer cancel()
	gen, err := m.transfer.Hydrate(transferCtx, cmd.TransferURL, cmd.TransferToken, workingDir)
	if err != nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			fmt.Sprintf("instancemanager: hydrate: %v", err))
	}
	// Record the generation the working set is now at (issue #763): the API served
	// the authoritative store at this generation, so the local scratch matches it.
	// A 0 (no published snapshot, or an older API) is recorded as-is — the API then
	// treats this set as older than any published store generation and re-hydrates,
	// the safe direction. On the 200 path the marker was already written atomically
	// into the temp tree before the swap-in rename (issue #917), so this call is
	// idempotent; on the 204 path it is the only write. Best-effort: a failure only
	// costs an extra hydrate next start, never correctness, so it is logged not
	// propagated.
	//
	// Declare the served generation to the API only when that stamp actually landed
	// (issue #2500). The API prefers this over its own pre-dispatch store read (#2477),
	// which can only understate; but a declaration the marker does not back would let a
	// later start skip a hydrate it needs, so the value is taken FROM recordGeneration's
	// report of the write — the same &gen it stamped — not from the transfer succeeding.
	// This is the snapshot path's argument (#2481) with its guard removed: handleHydrate
	// holds the per-id reservation across the whole transfer AND is the writer that
	// produced the tree, so no concurrent stream can have replaced it, and the marker
	// write is the sole thing that can decline to declare.
	var declaredGeneration *uint64
	if m.recordGeneration(workingDir, cmd.ServerID, gen) {
		declaredGeneration = &gen
	}
	return session.CommandResult{
		CommandID: cmd.CommandID, Success: true, HeldGeneration: declaredGeneration,
	}
}

// recordGeneration writes the working-set generation marker, logging (not failing) on
// error: a marker this call fails to write is one OLDER than the tree, which costs an
// extra hydrate and nothing else. It is unconditional, and its single caller is what
// makes that sound — handleHydrate holds a per-id reservation across the whole transfer
// AND is the writer that produced the tree, so the marker it writes always describes
// what is on disk. The running-id snapshot's tail holds no such reservation and goes
// through recordGenerationIfUnchanged instead. Do not gate this one: a marker that is
// never written reads as generation 0, so the API could never skip a hydrate again.
//
// It REPORTS whether the marker was published, and that return value is the sole source
// of the Worker-declared held generation on the hydrate's CommandResult (issue #2500).
// The API mirrors that declaration into the inventory its skip-hydrate gate reads, so the
// declaration has to be the write's own outcome rather than the transfer merely having
// succeeded: a marker this call could not write is older than the tree, so declaring the
// served generation anyway would let a later start skip the corrective hydrate.
func (m *Manager) recordGeneration(workingDir, serverID string, gen uint64) bool {
	if err := writeGeneration(workingDir, gen); err != nil {
		m.logger.Warn("could not record working-set generation",
			"server_id", serverID, "generation", gen, "error", err)
		return false
	}
	return true
}

// recordGenerationIfUnchanged records the generation only while workingDir is still the
// directory ref pinned (issue #2284). It is the running-id snapshot's stamp, which runs
// in the one tail that holds no per-id reservation, so a NEW stream can have re-placed
// this server here and hydrated it while this (old, dropped) stream was uploading. The
// hydrate replaces the working dir by rename, so the identity check sees it.
//
// A mismatch SKIPS the stamp and the caller still returns success: the publish really
// did happen and minted this generation server-side, so failing the command would
// report a valid publish as a transfer failure. The marker then keeps the hydrate's
// generation, which is older than the store's, so the #767 gate does not skip and the
// API re-hydrates — one extra hydrate, correct world. Stamping instead would leave the
// marker NEWER than the tree, which makes that same gate skip the corrective hydrate and
// boot the wrong generation silently. Nothing is reported to the API (a CommandResult
// carries no warning channel); the WARN is the only signal.
//
// The check is repeated as writeGenerationGuarded's pre-rename guard, and that second
// check is what closes the one interleaving this one cannot: a replacement landing after
// the check here but before the marker temp is created gets a temp inside the REPLACEMENT
// tree, which then publishes cleanly. Once the temp exists the marker can no longer land
// in a replacement tree at all — the temp rides the pinned inode to .displaced-<id> and
// the rename fails ENOENT on its source — so between them no wrong stamp survives except
// a swap interleaved inside the marker rename's own path resolution. That is why the
// renameat-against-the-pinned-descriptor rewrite is NOT done here: it would buy only that
// last sliver, while rewriting the shared, fsync-ordered writeGeneration the hydrate path
// also uses and writing a marker into the displaced tree that STORAGE.md Section 4.6's
// manual recovery reads. See writeGenerationGuarded for the case analysis.
//
// It REPORTS whether the marker was published, and that return value is the sole source
// of the Worker-declared held generation on the snapshot's CommandResult (issue #2481).
// The API mirrors that declaration into the inventory its skip-hydrate gate reads, so the
// declaration has to be the marker write's own outcome rather than a second opinion about
// it: every skip above returns false through the same statement that suppresses the write,
// so no code path can report a generation this function did not stamp.
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

// closingSaveOnTimeout bounds the SAME RCON call when Close issues it for a stop
// bracket still outstanding at shutdown (issue #3166). It is deliberately far
// shorter than restoreSaveTimeout above, and the reason is what the two budgets
// have to overlap with rather than any difference in the work:
//
//   - restoreSaveTimeout is spent inside a Close that is already waiting out the
//     escalation it follows, so it is hidden. This one has nothing to hide behind —
//     Close joins no command lane at all — so whatever it spends is added to the
//     Worker's shutdown, which is the cost the whole change exists to avoid.
//   - the work is one rcon.Dial (TCP connect plus the AUTH handshake) and one
//     Execute round trip, both of which are sub-second whenever the server answers
//     at all — rcon's own defaultExecuteTimeout comment says so, and its 30 s is a
//     ceiling against a wedged peer rather than a duration anything spends.
//   - a server that has NOT answered within this bound is, in this exact window, a
//     server whose stop is escalating precisely because it is not answering. Holding
//     the shutdown open longer for it buys nothing: containerdriver.sweepSaveOn
//     repairs a container that survives at the next boot, and one that does not
//     survive needs no restore. containerdriver bounds that same boot-time dial with
//     sweepCallMargin for the same reason.
//
// 5 s is an order of magnitude above the healthy case and half of Docker's own 10 s
// default grace, so the drain can never be what an unconfigured deployment is
// SIGKILLed for. It is also the WHOLE of the latency this change can add to a
// shutdown: Close's restores run concurrently, so a drain of any size costs at most
// this once, and Close's ~280 s worst case — which compose's stop_grace_period is
// sized for — is untouched, since 5 s is well inside the restoreSaveTimeout leg that
// bound already counts.
//
// Manager.closingSaveOnTimeout defaults to it so a test can shrink it, mirroring
// fsckRetryDelay.
const closingSaveOnTimeout = 5 * time.Second

// snapshotFsckAttempts is how many times the pre-pack region fsck is run for a
// RUNNING server's periodic snapshot before the snapshot is refused (#907). The
// quiesce settle-wait already blocks until the async save's region writes have
// stopped changing, so the chunk-save tearing is gone by the time the fsck runs;
// this small retry is the secondary backstop for a residual tear from a NON-chunk
// writer (a plugin or background task save-off does not gate) racing the scan. The
// first failing attempt is retried (snapshotFsckAttempts-1 retries) with
// defaultFsckRetryDelay backoff, and the snapshot proceeds as soon as one attempt
// is clean. The stopped-server (at-rest) path uses a single fail-closed attempt —
// a failure there is real signal, not a mid-write race.
const snapshotFsckAttempts = 3

// defaultFsckRetryDelay is the default backoff between the running-server fsck
// attempts above. Three attempts with two ~2s gaps stay well within the snapshot
// command budget (control.snapshot_timeout_seconds=600). Tests shrink it to zero.
const defaultFsckRetryDelay = 2 * time.Second

// defaultSettlePollInterval / defaultSettleBudget bound the quiesce settle-wait
// (#907): after the async save-all the working set's .mca files are re-scanned
// every defaultSettlePollInterval, and the save is considered settled once two
// consecutive scans observe identical (mtime, size) for every region file. The
// wait gives up after defaultSettleBudget and the snapshot is refused
// quiesce_unavailable (the next tick retries). The budget is generous yet well
// inside the snapshot command budget (control.snapshot_timeout_seconds=600), so
// the settle-wait never races the command timeout. Tests shrink both.
const (
	defaultSettlePollInterval = 2 * time.Second
	defaultSettleBudget       = 60 * time.Second
)

// handleSnapshot packs the server's working dir and uploads it. For a running
// server it brackets the working-dir copy with RCON save-off / save-on so the
// Minecraft server does not write to the world mid-copy and a region file cannot
// be captured torn (#694, CONTROL_PLANE.md Section 6.9): it issues save-off to
// disable auto-save, a plain non-blocking save-all to drive the world to disk,
// then a settle-wait that polls the region files until their (mtime, size) stops
// changing across a quiet window — so the asynchronous save has provably completed
// before the fsck/copy reads it — runs the transfer over the now-quiescent working
// dir, then save-on to re-enable auto-save.
//
// We deliberately do NOT use save-all flush. The synchronous flush runs on the
// Minecraft main thread and, on a live world with a player online, parked the tick
// past max-tick-time and tripped the Server Watchdog into forcibly shutting the
// server down mid-saveAllChunks — a demonstrated production crash (issue #693,
// survival-main 2026-06-08, a 13 MB world on defaults; removed by commit 0bf86a6).
// The async save + settle-wait quiesces the on-disk state (a plain non-blocking
// save-all returns before the asynchronous save completes, so an immediate fsck
// would race the in-flight writes and read healthy regions as torn — the #907
// false-positive of 35/35 region files reported corrupt on a world that scans
// clean at rest) without ever parking the main thread.
//
// For a RUNNING server the quiesce is fail-closed (#907): if RCON cannot be
// opened, save-off / save-all fail, or the save never settles within the budget,
// the working set is NOT actually quiesced, so packing it would reproduce exactly
// those torn-read false positives and waste a full tar+upload the API gate would
// reject. The periodic snapshot is instead refused with a distinct
// quiesce_unavailable error so operators can tell "could not quiesce" from "world
// is corrupt"; the next tick (5 min) retries. The tradeoff: a server whose RCON is
// permanently broken never gets a PERIODIC snapshot — but its FINAL post-stop
// snapshot (the stopped-id path below, which needs no RCON) still captures the
// world, so this bounds the loss to progression since the last good periodic
// snapshot, not the whole world. Once save-off succeeds, save-on is guaranteed on
// every exit path — success, transfer error, or a cancelled/timed-out request
// context — via a deferred restore that runs on a detached context (redialing RCON
// if the connection was poisoned), so the server is never left with auto-save
// disabled.
//
// Once the working set is quiesced (bracketed above for a running server, at rest
// for a stopped one), a structural region fsck runs before the transfer (#741):
// on detected corruption the snapshot is refused with a coded error — failing fast
// at the source instead of after a full tar+upload the API gate would reject — and
// the deferred restore still re-enables auto-save. For a RUNNING server the fsck is
// retried a small bounded number of times with backoff (#907): the settle-wait has
// already absorbed the chunk-save tearing, so this retry is the secondary backstop
// for a residual tear from a non-chunk writer (one save-off does not gate) racing
// the scan. The STOPPED (at-rest) path stays single-shot fail-closed — a failure
// there is real corruption signal, not a race. A fsck I/O error is best-effort
// (logged, the transfer proceeds) so it cannot wedge the snapshot.
func (m *Manager) handleSnapshot(ctx context.Context, cmd session.Command) session.CommandResult {
	if m.transfer == nil {
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			"instancemanager: no data-plane transfer client configured")
	}
	m.mu.Lock()
	_, running := m.instances[cmd.ServerID]
	m.mu.Unlock()
	// The running flag is read outside any reservation, so a start can register an
	// instance between here and the stopped path's reserve() below; the contract
	// table records what that race emits, and this seam is how the test enters it
	// deterministically (issue #2472).
	if hook := m.snapshotAfterRunningCheck; hook != nil {
		hook(cmd.ServerID)
	}
	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	// restore re-enables auto-save after the quiesce. Declared here so it is
	// accessible after the if/else for the explicit restore() call between pack and
	// upload (issue #1710). Made idempotent via sync.Once so the deferred safety-net
	// and the explicit call do not double-issue save-on.
	var restore func()
	// pin guards the running path's marker stamp; nil (and unused) on the stopped path,
	// which records no generation. Declared here so the post-upload tail below can see it.
	var pin *workingDirRef
	// declaredGeneration is what this command DECLARES the Worker still holds when it
	// returns (issue #2481); nil declares nothing. It has exactly one assignment, in
	// the running branch's tail, and its value comes from recordGenerationIfUnchanged
	// reporting that the marker write landed — so the declaration cannot disagree with
	// the marker, and the stopped-id branch (which calls removeScratch instead of that
	// function, and is the else of the same if) has no statement that can set it.
	//
	// Deliberately NOT named heldGeneration: that is the package function
	// (scratchscan.go) computing the register-time advertisement this value mirrors,
	// and shadowing it here would hide the very identifier a reader needs to follow to
	// see that the two report the same marker.
	var declaredGeneration *uint64
	if running {
		// Pin the working dir's IDENTITY for the whole window, from before the quiesce
		// to the post-upload marker stamp (issue #2284). Because this branch takes no
		// reservation (see below), a concurrent stream can replace this directory while
		// the snapshot runs; the stamp must then not claim the newly published
		// generation for a tree it never packed. Captured this early rather than just
		// before the pack because it is strictly more conservative at zero cost in
		// normal operation — reserve() rejects a hydrate while the instance is
		// registered, and it is registered precisely because we are in this branch, so
		// the only thing that can replace the dir from here on is the cross-stream
		// stop-then-hydrate this detects. The running path always has the dir (the
		// start created it). Closed on every return out of this handler.
		pin = pinWorkingDir(workingDir)
		defer pin.close()

		// The running-id snapshot takes NO reservation across its quiesce window, and
		// that is safe (issue #829, item 4):
		//   - Same stream: SnapshotTrigger and a StopServer/RestartServer for one id are
		//     queued on the same per-server lane (session dispatcher, #95) and run
		//     serially in FIFO order; the snapshot runs inline holding a concurrency
		//     slot and does not detach, so a same-stream stop cannot overlap it.
		//   - Cross stream: an old dropped stream's snapshot can still be running when a
		//     new stream's lane runs a stop/restart, which (holding no reservation here)
		//     evicts and terminates the process mid-tar. The worst this yields is a TORN
		//     capture — the stop's shutdown re-saves regions while the tar reads them.
		//     A tear that happens DURING the tar is caught downstream by the API's #739
		//     content-integrity gate. That gate runs the byte-precise region check (issue
		//     #927: one rule set, no source-keyed mode), which still catches realistic
		//     tears: any referenced chunk whose byte extent overruns EOF, any entry
		//     pointing at/past EOF, garbage prefixes. Those the gate REFUSES — the publish
		//     aborts, the staging area is dropped, and current/
		//     keeps the last good generation: no silent corruption and no overwrite. The
		//     only escape from the byte-precise bound is a truncation landing exactly at
		//     the final referenced chunk's byte boundary with no entries beyond; that one
		//     PASSES the gate, which is acceptable because it is indistinguishable from a
		//     consistent older state (the lost bytes are unreferenced). And this is a
		//     PERIODIC snapshot of a still-running server, not
		//     the post-stop FINAL one (a stopped-id snapshot, which DOES reserve below),
		//     so a refused capture simply retries on the next tick — nothing is lost.
		// A reservation would only convert that refused-and-retried outcome into a
		// BUSY-rejected one — same net effect, more coordination state — so it
		// is intentionally not taken.
		//
		// The same no-reservation choice leaves TWO further cross-stream edges in the
		// post-upload tail, and BOTH are closed by the one mechanism pinned above — the
		// working dir's identity. Neither needed a reservation, so the item-4 decision
		// stands for both.
		//
		// The sharper one (issue #2284) was the marker stamp: a stale snapshot writing the
		// newly published generation onto a tree a concurrent hydrate had just swapped in,
		// leaving a marker NEWER than its tree — which does not merely cost a hydrate, it
		// defeats the #767 gate that would have corrected the tree, so the server boots the
		// wrong generation silently. The stamp is refused when the identity no longer
		// matches.
		//
		// The second, narrower edge (issue #917 item 3, closed by issue #2291): an old
		// dropped stream's snapshot can SUCCEED and call
		// sweepDisplaced(serverID) below while a NEW stream's re-placement hydrate for the
		// same id has just renamed the live working set aside to .displaced-<id>
		// (datatransfer.unpackAndSwap step (2)) — an ungated sweep then deletes THAT
		// hydrate's recovery copy. Same-stream overlap is excluded by the per-server FIFO
		// lanes as above. Cross-stream is bounded by a ctx asymmetry: the upload runs on
		// transferContext, derived from the stream's serveCtx, so a stream drop cancels the
		// in-flight upload and the snapshot fails before any sweep. Only the post-upload
		// tail was exposed — and the new stream must meanwhile reconnect, register, and
		// download+unpack a whole working set to reach its displace. PackSnapshot ignores
		// ctx, which is why the torn-capture case above stays wide while this one was
		// narrow to begin with.
		//
		// What made it worth closing rather than accepting is what the snapshot's success
		// does NOT prove. It publishes the state as of its PACK, not the tree the sweep
		// would remove: restore() re-enables auto-save at the pack/upload split (below),
		// and a GRACEFUL stop on the racing stream additionally drives a shutdown save into
		// the same dir before the hydrate displaces it (a forced stop does not, but the
		// resumed auto-save has already written), so that tree is the published prefix PLUS
		// an unpublished delta — bounded by the pack, not by the displacement, so if two
		// hydrate cycles fit inside one upload window it can be an entire session. And the
		// bound assumes the racing hydrate CREATED the tree: under oldest-wins (issue
		// #2278) a hydrate finding the slot occupied creates none, so an ungated sweep
		// destroys the older RETAINED tree instead, whose age no bound here describes, and
		// since that hydrate also drops the set it superseded the race could leave no local
		// branch at all — only the store's pack generation. Gating the sweep also removes
		// the derived failure where the hydrate's own swap-in fails and its restore rename
		// finds the parked tree gone (ENOENT), leaving destDir absent.
		//
		// The cost, stated plainly: the GC will occasionally decline to reclaim disk it
		// would have reclaimed before. That is the safe direction — a declined sweep leaks
		// one world-sized tree until the next successful snapshot for the id reclaims it,
		// which is the #906 GC-on-success contract itself, where the ungated sweep's
		// failure was an unrecoverable delete. The microseconds between the check and the
		// sweep's rename (issue #2799: the removal itself runs on the renamed tree, off
		// the slot) were left open on the reading that they could only leak. They could
		// not: the check passes until the racing hydrate renames the working dir aside, so
		// the hydrate can clear world-less junk from the slot and park its own live set
		// there in between, and the rename takes THAT (issue #3118). The pin therefore
		// goes into sweepDisplaced as well and is checked again after the rename, while
		// putting the tree back is still possible — closing the window without the per-id
		// reservation item 4 declined, so that decision still stands.
		var quiesced bool
		var rawRestore func()
		quiesced, rawRestore = m.quiesceRunning(ctx, cmd.ServerID, workingDir)
		var once sync.Once
		restore = func() { once.Do(rawRestore) }
		defer restore()
		if !quiesced {
			// The world could not be quiesced (RCON down, save-off/save-all failed, or the
			// async save never settled within the budget): packing it live is what produced
			// the #907 35/35 false positives, a wasted tar+upload the API gate rejects.
			// Refuse this PERIODIC snapshot with a distinct classification so operators can
			// tell "could not quiesce" from "world is corrupt"; the next tick retries. The
			// post-stop FINAL snapshot still covers a permanently-RCON-broken server (the
			// stopped-id path needs no RCON).
			m.logger.Warn("snapshot refused: could not quiesce running world",
				"server_id", cmd.ServerID, "reason", "quiesce_unavailable")
			return fail(cmd.CommandID, session.CommandErrorTransferFailed,
				"instancemanager: snapshot refused: quiesce_unavailable (could not quiesce running world)")
		}
	} else {
		// Stopped-id snapshot: the set is at rest and the API has typically already
		// unassigned (a graceful stop snapshots after unassign, so a user start can
		// re-place this id on the same Worker concurrently). Reserve the id for the
		// pack so a racing HydrateTrigger (or start) cannot rewrite the working dir
		// while it is mid-fsck/tar — a mixed capture whose .mca files are each valid
		// would slip past the #749 integrity gate (the snapshot×hydrate cross-race the
		// #780 review confirmed). A reservation already held by such a racing command
		// rejects with BUSY. Running-id snapshots stay reservation-free: a
		// running instance already blocks reserve(), and the save-off bracket above is
		// their quiesce. Released on every return below.
		if ok, code, msg := m.reserve(cmd.ServerID); !ok {
			return fail(cmd.CommandID, code, msg)
		}
		defer m.release(cmd.ServerID)
	}

	if !running {
		// Refuse a stopped-id snapshot whose scratch holds no working set (issue
		// #1713): there is nothing to capture, so packing would upload an empty tar
		// as a candidate new generation with the staleness guard disabled
		// (readGeneration on an unmarked dir is 0, so the base-generation header is
		// omitted) — leaving the API-side empty-staging refusal as the only defense
		// and burning a full pack+upload+refusal cycle, again on every scheduler
		// tick. The usual cause is a benign duplicate: the final snapshot published,
		// removeScratch GC'd the dir, but the CommandResult was lost on a dropped
		// stream so the API re-dispatched. The worker keeps no tombstone that could
		// tell that apart from a genuinely missing working set (e.g. never
		// hydrated), so one distinct refusal covers both. SERVER_NOT_FOUND (not
		// TRANSFER_FAILED): no working set is held for this id and no retry can
		// succeed without a hydrate — a terminal condition, not a transient transfer
		// failure. The check is race-free: the reservation above already holds off
		// any hydrate/start that could create the working set concurrently. The
		// running path needs no guard — a tracked instance's working dir was created
		// by its start.
		//
		// The PREDICATE is the working set's CONTENT, not a directory stat (issue
		// #2813): a scratch emptied in place, and one holding only the generation
		// marker (or a crashed stamp's ".mcsd_generation-*" temp), passed the stat and
		// reached the pack — which excludes exactly those files, so the upload staged
		// zero files and the API refused it 400 empty_snapshot after the whole cycle,
		// reporting the environment-dependent transfer_failed that points away from
		// the cause. It is deliberately NOT the launch guard's marker predicate (issue
		// #2802), which the sibling refusal below uses: what a launch needs is the
		// held-claim token, so a marker-ONLY dir passes there (the 204 fresh-boot
		// contract), while what a snapshot needs is something to capture, so the same
		// dir is refused here. Content WITHOUT a marker packs, which is right — there
		// is a world to publish, and that direction is pinned by
		// TestSnapshotTriggerPacksContentWithoutGenerationMarker.
		//
		// The directory is read HERE rather than through hasWorkingSet (PR #2840
		// review): that helper answers false when it cannot read, which is the safe
		// direction for the scans that ADVERTISE held sets but the wrong one for this
		// decision. The refusal below is what makes StopServer._final_snapshot
		// downgrade its data-loss ERROR to a benign-duplicate INFO, so reporting it on
		// an EACCES/EMFILE/EIO would say "nothing was lost" about a world that was
		// never captured — the #841 swallowed-failure shape, and a direct
		// contradiction of is_working_set_absent_refusal's own contract. Only
		// os.IsNotExist IS the statement (the dir is gone); every other read error is
		// a failed operation and carries the unpinned transfer_failed, the same call
		// datatransfer.displacedSlotHoldsWorkingSet already made for its own
		// durability decision.
		//
		// The "working dir absent" phrase in the message is load-bearing (issue
		// #1790): the API's final-snapshot path keys on it (together with the
		// SERVER_NOT_FOUND code) to downgrade this refusal from its data-loss
		// ERROR to a benign-duplicate INFO, and the periodic scheduler reads the same
		// pair as "nothing left to capture" (issue #2480) — see
		// _WORKING_SET_ABSENT_MARKER in
		// api/src/mc_server_dashboard_api/servers/application/lifecycle.py.
		// Reword only together with that discriminator (and both sides' tests): the
		// message below is declared as "working_set_absent.snapshot" in
		// proto/contract/command_error_contract.json, which TestCommandErrorContract
		// asserts this emission against and the API's fixtures are built from, so a
		// reword here is red until that declaration and the API's phrase follow (issue
		// #2843). It is kept verbatim for the emptied and marker-only shapes too: the
		// prose is a shade imprecise there, but the discriminator is exact — the same
		// trade the launch guard made.
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

	// Pre-pack structural region fsck (#741): fail fast at the source if the
	// working set is already corrupt (e.g. a region torn by a crash-during-save,
	// #703), so we refuse the snapshot here — clear signal, no wasted tar+upload —
	// rather than after a full transfer the API gate (#749) would reject anyway.
	// The set is quiesced at this point: a running server is bracketed by save-off +
	// async save-all + settle-wait above (#694/#907), and a stopped one is not being
	// written. For a running server the check is retried with backoff (#907) so a
	// residual tear from a non-chunk writer racing the scan after the save settled
	// cannot, as a transient torn read, veto a periodic snapshot; a stopped (at-rest)
	// set is checked once, fail-closed. The check is fail-closed on detected
	// corruption but best-effort on a fsck I/O error
	// — an error reading the set must not wedge the snapshot, so it is logged and the
	// transfer proceeds (the API gate remains the correctness guarantee).
	if report, err := m.checkWorkingSet(ctx, cmd.ServerID, workingDir, running); err != nil {
		m.logger.Warn("snapshot pre-pack region fsck failed; proceeding without it",
			"server_id", cmd.ServerID, "error", err)
	} else if !report.Healthy() {
		first := report.Corrupt[0]
		return fail(cmd.CommandID, session.CommandErrorTransferFailed,
			fmt.Sprintf("instancemanager: snapshot refused: %d/%d region files corrupt (e.g. %s: %s)",
				len(report.Corrupt), report.Scanned, filepath.Base(first.Path), first.Reason))
	}

	// Declare the store generation this set was hydrated from (issue #847) so the API
	// can refuse the publish if the store advanced past it. 0 (an unknown/never-
	// hydrated set) leaves the guard to compare against the store's current value.
	baseGeneration := readGeneration(workingDir)
	// Bound the pack+upload with the per-transfer deadline (issue #874): without it
	// the upload has no deadline at all and could outlive the API's snapshot_timeout
	// indefinitely (#869). The bound is the API budget + a margin (the ack value),
	// so the API-side timeout fires first and this is the cleanup backstop.
	transferCtx, cancel := m.transferContext(ctx)
	defer cancel()

	if running {
		// Running-server snapshot (issue #1710): split pack from upload so save-on is
		// restored as soon as the pack (the only phase that reads the live working dir)
		// completes. The upload reads only the spool file and does not need the server
		// quiesced. A multi-GB upload can take minutes; keeping auto-save disabled for
		// that entire window risked permanent save-off on a worker crash mid-upload.
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
		// Record the NEW generation the publish produced (issue #763): the scratch we
		// just pushed is the source of this store generation, so its local generation
		// advances to match. This keeps a same-Worker restart's held generation equal to
		// the store generation (the API then skips the destructive hydrate). Best-effort
		// (logged, not failed) — see recordGeneration.
		//
		// Conditional on the identity pinned above (issue #2284): this is the unreserved
		// tail, so the tree that was packed may already have been replaced by a new
		// stream's hydrate. Stamping the published generation onto THAT tree is the one
		// outcome that must not happen — it is what the skip-hydrate gate reads, so a
		// marker newer than its tree makes the API skip the hydrate that would correct
		// it. Skipped instead: see recordGenerationIfUnchanged.
		//
		// Declare the generation to the API only when that stamp actually landed (issue
		// #2481). The API mirrors the declaration into the same inventory the gate reads,
		// so a declaration the marker does not back would defeat the guard above over the
		// wire instead of on disk — hence the value is taken FROM the write's report, not
		// from being in this branch.
		if m.recordGenerationIfUnchanged(pin, workingDir, cmd.ServerID, gen) {
			declaredGeneration = &gen
		}
		// GC the displaced tree a prior hydrate kept aside (issue #906): a successful
		// publish proves the store now holds (and supersedes) this server's world, so the
		// recovery copy is no longer needed. Mirrors the #845 GC-on-success pattern.
		//
		// Conditional on the same identity pin as the stamp above (issue #2291): what the
		// success supersedes is the tree this snapshot PACKED, so once a concurrent
		// hydrate has replaced that tree the proof no longer covers whatever now sits at
		// .displaced-<id> — which is that hydrate's own recovery copy. Checked against the
		// pin directly rather than against the stamp's return value: that value is also
		// false for an ordinary marker-write I/O error, which is no reason to decline the
		// GC. The tradeoff is stated in STORAGE.md Section 4.6 — the sweep now sometimes
		// leaks a tree it would have reclaimed, until the next successful snapshot for the
		// id reclaims it.
		//
		// The pin goes INTO the sweep as well (issue #3118), because this check cannot
		// cover what the sweep's rename takes: it passes until the racing hydrate renames
		// the working dir aside, and the hydrate can park its live set in the slot in
		// between. The sweep checks again once the tree is out of the slot, while putting
		// it back is still possible.
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
		// the new generation would be pointless work on a dir we are about to delete —
		// and declaring one to the API (issue #2481) would be a lie about what this
		// Worker holds: the API would record held == store for a scratch that no longer
		// exists, take the short held-start grace, and start with skip_hydrate over
		// nothing. That start is now REFUSED at launch rather than booted into an empty
		// directory (handleStart, issue #2499), so the declaration would cost a refusal
		// and a corrective hydrate instead of a #696-class world rollback — still wrong,
		// and still not worth making the guard earn its keep on.
		m.removeScratch(cmd.ServerID)
	}
	return session.CommandResult{
		CommandID: cmd.CommandID, Success: true, HeldGeneration: declaredGeneration,
	}
}

// checkWorkingSet runs the pre-pack region fsck (issue #927: ONE rule set — a
// non-4096-aligned tail is the normal on-disk shape, not a tear, on both the
// running and the stopped path; the `stopped => padded` invariant the old strict
// mode relied on does not survive a sweep-stop timeout / SIGKILL / crash). For a
// stopped (at-rest) set it is a single fail-closed scan. For a RUNNING server it
// retries on detected corruption up to snapshotFsckAttempts times with
// fsckRetryDelay backoff (#907): the quiesce settle-wait has already let the async
// save's region writes complete, so a residual tear is a non-chunk writer racing
// the scan, and that transient should not veto a periodic snapshot — so the latest
// clean attempt wins, and only a corruption that persists across every attempt
// refuses the snapshot. A fsck I/O error is returned as-is (the caller treats it as
// best-effort) and is not retried. On ctx cancellation it returns ctx.Err() (not the
// last corrupt report) so a cancelled snapshot is not misclassified as corruption.
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

// quiesceRunning brackets a running-server snapshot so the world is not written
// during the working-dir copy (#694). It opens RCON, disables auto-save
// (save-off), issues a plain non-blocking save-all, then waits for the
// asynchronous save to settle (settleWorkingSet: the region files' (mtime, size)
// stop changing across a quiet window) so the fsck/copy reads a fully-written
// world. It returns (quiesced, restore). quiesced is true only when the on-disk
// state is actually quiesced — RCON opened AND save-off AND save-all succeeded AND
// the save settled within the budget; the caller refuses the periodic snapshot
// otherwise rather than packing a live world (#907).
//
// It deliberately uses a non-blocking save-all, NOT save-all flush: the
// synchronous flush runs on the Minecraft main thread and crashed survival-main in
// production on 2026-06-08 by parking a tick past max-tick-time and tripping the
// Server Watchdog (issue #693). The settle-wait recovers the on-disk guarantee a
// plain save-all lacks (it returns before the async save completes) without ever
// parking the main thread.
//
// The save-on restore is still guaranteed whenever save-off succeeded: the
// returned restore re-enables auto-save with save-on (only when save-off actually
// succeeded) and always closes the RCON connection. It runs save-on on a context
// detached from ctx (carrying restoreSaveTimeout) so a cancelled or timed-out
// request still re-enables auto-save. Because the rcon client poisons its
// connection on ANY Execute error (a failed/timed-out save-all leaves the same
// client returning ErrConnBroken), the restore redials a fresh connection via
// openControl and retries save-on once if the first attempt fails — otherwise a
// running server would be left with auto-save permanently OFF (#694 hard
// requirement). A final failure is logged loudly: auto-save stuck off is
// operator-actionable.
func (m *Manager) quiesceRunning(ctx context.Context, serverID, workingDir string) (bool, func()) {
	driverName, mcVersion := m.controlTargetFor(serverID)
	raw, err := m.openControl(ctx, serverID, driverName, mcVersion)
	if err != nil {
		m.logger.Warn("snapshot quiesce: open rcon failed", "server_id", serverID, "error", err)
		return false, func() {}
	}
	// Wrap in resilientControl (#919): a mid-bracket Execute error poisons the
	// rcon connection, so save-all after a timed-out save-off (or save-on after
	// a timed-out save-all) would return ErrConnBroken instantly. The wrapper
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

// flushBeforeStopWithDriver drives the live world's dirty chunks to disk before
// a graceful stop (issue #1007). The driver calls it always before tryRCONStop
// on the graceful path, because MC's own shutdown save does NOT reliably flush
// dirty region chunks when a player was connected.
//
// It issues a non-blocking save-all (the SAME mechanism quiesceRunning uses —
// NOT save-all flush, whose synchronous flush parked a tick past max-tick-time
// and tripped the Server Watchdog into a production crash, #693) and waits for
// the asynchronous save to settle (settleWorkingSet: the region files' (mtime,
// size) stop changing) so the chunks have landed on disk before the terminate.
//
// driverName is the driver that runs this server and mcVersion its Minecraft
// version, both captured before the instance was evicted from the manager's map
// (controlTargetFor would return empty after eviction).
//
// It is best-effort and bounded: any failure — RCON cannot be opened, save-off or
// save-all errors, or the save never settles within the budget — is logged and the
// stop proceeds anyway. Wedging a stop on a save failure would be strictly worse
// than the pre-fix behavior; the common path completes the flush. The settle
// budget (m.settleBudget, default 60s) stays well inside the API's stop dispatch
// budget (stop_timeout_seconds=600).
//
// save-off is issued first to disable MC's auto-save disk writes (#1038): without
// it, an active player's actions continuously generate new chunk writes, so
// settleWorkingSet never converges within the budget. save-on is NOT sent — the
// server is about to be stopped, so there is nothing to restore, and re-enabling
// writes during the settle window would reintroduce the convergence problem.
//
// That leaves auto-save off on a server that is still running until the stop
// resolves, so a save-off that LANDED is recorded in pendingSaveOn: a stop that
// confirms termination has nothing to restore, one that fails reaches
// restoreSaveOnAfterFailedStop, and a Worker that starts closing before either
// happens settles the debt itself rather than leaving it to the next boot (issue
// #3166).
func (m *Manager) flushBeforeStopWithDriver(ctx context.Context, serverID, driverName, mcVersion string) bool {
	raw, err := m.openControl(ctx, serverID, driverName, mcVersion)
	if err != nil {
		m.logger.Warn("stop flush: open rcon failed; stopping without a final save",
			"server_id", serverID, "error", err)
		return false
	}
	// Wrap in resilientControl (#919/#1040): a save-off failure poisons the rcon
	// connection, so save-all on the same client returns ErrConnBroken instantly.
	// The wrapper auto-redials so save-all degrades to the pre-save-off behavior
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

	// Record the debt BEFORE the command goes on the wire, not after it answers.
	// rcon.Execute writes save-off and only then waits for a reply, so a round trip
	// that times out says nothing about whether Minecraft ran it — and even a clean
	// success can be interrupted between the reply and the record. Recording first is
	// the only ordering with no window, and it errs the way that costs least: a debt
	// for a save-off that never landed is one idempotent save-on, while a missing one
	// is a surviving world that saves nothing (issue #3166).
	//
	// The bracket is opened here rather than at the dial above because a flush that
	// could not dial never sends anything, and the forced stop path never calls this
	// function at all (TestForcedFailedStopSkipsSaveOn).
	//
	// A REFUSED debt means the ledger is sealed, and then auto-save must not be
	// disabled at all: Close has joined everything it joins and read the ledger for
	// the last time, so nothing would ever re-enable it. This lane reaches the dial
	// only because it was dispatched before the shutdown began, and it is not joined,
	// so the process exits under it mid-escalation — the stop the quiesce exists to
	// protect never completes anyway. Skipping costs this one stop its quiesce, no
	// more than a save-off that fails already does (#1038), and it is the only shape
	// that cannot leave auto-save off: nothing was turned off.
	quiesce := m.markPendingSaveOn(serverID, driverName, mcVersion)

	// Disable auto-save so settleWorkingSet converges quickly even with active
	// players (#1038). Best-effort: if save-off fails, save-all still runs — the
	// settle may time out but the flush is no worse than before this fix.
	if !quiesce {
		m.logger.Warn("stop flush: worker is closing; skipping save-off so auto-save cannot be left disabled",
			"server_id", serverID)
	} else if _, err := ctrl.Execute(ctx, "save-off"); err != nil {
		m.logger.Warn("stop flush: save-off failed; proceeding with save-all",
			"server_id", serverID, "error", err)
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

// restoreSaveOn re-enables auto-save after a running-server snapshot quiesce.
// It runs on a context detached from the request's (carrying restoreSaveTimeout)
// so a cancelled/timed-out snapshot still re-enables auto-save. The save-all/settle
// step may have failed and poisoned ctrl's connection (the rcon client marks the
// connection broken on any Execute error), so a save-on on the same ctrl can return
// ErrConnBroken instantly; on any failure it redials a fresh RCON connection via
// openControl and retries save-on once with a short backoff inside the timeout, so
// a running server is never left with auto-save permanently OFF (#694). A final
// failure is logged loudly — auto-save stuck off is operator-actionable.
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

// restoreSaveOnAfterFailedStop re-enables auto-save on a server whose graceful
// stop failed (the container/process survived Kill). The pre-stop flush issued
// save-off to quiesce the world; because the stop never confirmed termination,
// the server keeps running with auto-save disabled — every block edit, chest
// open, and mob move since the flush is at risk if the JVM crashes before the
// reconciler retries (issue #2021).
//
// Unlike restoreSaveOn (the snapshot path), the caller's RCON connection is
// already closed and the instance was evicted from startCmds by
// takeStoppableReserve, so controlTargetFor would return empty. The helper
// accepts driverName and mcVersion explicitly (captured before eviction) and
// dials a fresh RCON connection on a context detached from the (possibly
// cancelled) request.
//
// It is no longer the only closer of that bracket: restoreSaveOnWhileClosing
// settles the same debt from pendingSaveOn when the Worker starts closing with the
// escalation still in flight (issue #3166). The two are independent and both
// idempotent — this one runs whenever the graceful stop failed, whether or not the
// shutdown already pre-empted it.
func (m *Manager) restoreSaveOnAfterFailedStop(ctx context.Context, serverID, driverName, mcVersion string) {
	if err := m.dialAndSaveOn(ctx, restoreSaveTimeout, serverID, driverName, mcVersion); err != nil {
		m.logger.Error("failed stop: auto-save NOT restored; surviving server is running with auto-save disabled",
			"server_id", serverID, "driver", driverName, "error", err)
		return
	}
	m.logger.Warn("stop failed with the server possibly still alive; re-enabled auto-save on the survivor",
		"server_id", serverID)
}

// restoreSaveOnWhileClosing re-enables auto-save on a server whose pre-stop flush
// disabled it and whose stop is STILL IN FLIGHT when the Worker starts closing
// (issue #3166). It is the same RCON call restoreSaveOnAfterFailedStop makes,
// issued at the start of the shutdown instead of at the end of the escalation:
// both outcomes of that escalation are fine to have pre-empted — a stop that
// confirms termination leaves nobody to read the setting, and one that does not
// reaches its own restore, which is idempotent.
//
// It logs its own framing rather than borrowing the failed-stop one because the
// two are different things to tell an operator about the same server: here the
// stop has not failed, it has simply not finished. It also carries its own, much
// shorter budget (closingSaveOnTimeout): the failed-stop restore is hidden inside a
// join Close is doing anyway, while this one is added to the Worker's shutdown.
func (m *Manager) restoreSaveOnWhileClosing(serverID, driverName, mcVersion string) {
	if err := m.dialAndSaveOn(context.Background(), m.closingSaveOnTimeout, serverID, driverName, mcVersion); err != nil {
		m.logger.Error("worker closing: auto-save NOT restored on a server whose stop is still in flight; if it survives the stop it runs with auto-save disabled until the next Worker boot",
			"server_id", serverID, "driver", driverName, "error", err)
		return
	}
	m.logger.Warn("worker closing: re-enabled auto-save on a server whose stop is still in flight",
		"server_id", serverID)
}

// dialAndSaveOn dials a FRESH RCON connection for serverID and issues save-on on
// a context detached from ctx and bounded by restoreSaveTimeout, so a cancelled
// request — or a Worker already shutting down — still re-enables auto-save. It is
// the mechanism both out-of-band restores share; each caller logs its own framing.
//
// A fresh dial rather than a reused one is the point: the caller's connection is
// closed by then (the flush's ctrl) or poisoned (the rcon client marks the
// connection broken on any Execute error), and the instance is evicted from
// startCmds, so driverName and mcVersion have to be passed in — controlTargetFor
// would answer empty (issues #2021, #1712, #3116).
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

// saveOnTarget is the RCON target of an outstanding pre-stop save-off: the driver
// that runs the server and its Minecraft version, the pair every out-of-band
// save-on needs to resolve the dial host (#1712) and the password's charset
// (#3116) after the instance has been evicted.
type saveOnTarget struct {
	driver    string
	mcVersion string
}

// markPendingSaveOn records that serverID's pre-stop flush is about to disable
// auto-save. It reports whether the debt was taken on, and a FALSE means the caller
// must not disable auto-save at all: the ledger is sealed, Close has already read it
// for the last time, and a bracket opened now would be closed by nobody.
//
// The seal is what bounds Close's drain to two passes instead of a loop. Without it
// the drain would have to keep re-reading — a stop dispatched before the shutdown
// began has its own RCON dial (a TCP connect plus an AUTH handshake, up to rcon's
// 30 s ceiling) between the command and its save-off, so it can arm a debt long after
// the first read — and a loop whose exit depends on no lane re-arming is a loop whose
// termination is an argument about other code. Refusing instead makes it a local
// invariant: after the seal, no debt can exist, so the pass that set it is the last
// one needed.
func (m *Manager) markPendingSaveOn(serverID, driverName, mcVersion string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.saveOnSealed {
		return false
	}
	m.pendingSaveOn[serverID] = saveOnTarget{driver: driverName, mcVersion: mcVersion}
	return true
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

// settleWorkingSet waits for an asynchronous save-all to finish writing the
// working set's region files before the fsck/copy reads them (#907). It snapshots
// the (mtime, size) of every .mca under workingDir, re-scans every settlePollInterval,
// and reports settled (true) once two consecutive scans are identical — the save's
// region writes have stopped. It gives up (false) after settleBudget so a world
// that never settles refuses the periodic snapshot (quiesce_unavailable) instead of
// waiting unbounded, and returns false on ctx cancellation. A scan I/O error is
// transient (a region file being rewritten can momentarily vanish), so it is treated
// as "not yet settled" and retried within the budget rather than aborting.
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
	if ok, code, msg := m.reserve(cmd.ServerID); !ok {
		return fail(cmd.CommandID, code, msg)
	}

	return m.launchReserved(ctx, cmd, driver, launchMode)
}

// launchReserved performs the start under an ALREADY-HELD reservation (taken by
// handleStart or carried across a restart's stop, issue #780): it refuses a launch
// whose working set this Worker does not hold, runs driver.Start, and registers the
// instance, releasing the reservation on every failure path and handing the id off
// to the registered instance under one mu critical section on success — so the id is
// never unclaimed.
//
// The working-set guard lives HERE rather than in handleStart (issue #2802) because
// this is the single place a launch happens: handleStart's start and handleRestart's
// RELAUNCH both pass through it. While it sat in handleStart only, a restart of a
// running server whose scratch had been destroyed out of band went straight to the
// MkdirAll this function used to open with and booted the live server into an empty
// directory — #2499's hole reached through a different verb. Replacing that MkdirAll
// with the refusal also removes the "silently manufacture an empty dir" hazard
// structurally: nothing here creates a working dir any more, so past the guard the
// directory is known to be one the Worker really holds.
func (m *Manager) launchReserved(ctx context.Context, cmd session.Command, driver execution.ExecutionDriver, launchMode execution.LaunchMode) session.CommandResult {
	// Refuse a launch whose working set is not on disk (issue #2499, extended to the
	// restart's relaunch and to an emptied-in-place scratch by issue #2802). The API
	// issues a HydrateTrigger before every StartServer that needs one
	// (control_plane.proto, StartServer), so by the time a launch happens the working
	// set is always held: a 200 hydrate swaps the unpacked tree in, and even a 204
	// ("nothing published yet") stamps the generation marker (writeGenerationGuarded).
	// A missing one therefore means the API skipped the hydrate — its held-working-set
	// inventory says this Worker holds a generation at least as fresh as the store
	// (skip_hydrate = held >= store, lifecycle.py) — over a working set this Worker
	// does not actually hold. Every input to that belief is honest about the moment it
	// was taken (the register-time scan, issue #2477's hydrate-side recording, issue
	// #2481's publish-side declaration) and none of them re-checks at launch, so an
	// out-of-band destruction of a live Worker's scratch has no floor between two
	// registrations. Without this check the start boots into a directory holding
	// nothing: the world is replaced by nothing, and the next snapshot publishes that
	// — the #696 class, and the one residual of the inventory design that fails toward
	// a SKIPPED hydrate instead of an extra one.
	//
	// The PREDICATE is the GENERATION MARKER, not a directory stat (issue #2802): the
	// marker is precisely the claim the skipped hydrate relied on. The register-time
	// advertisement reads it (scratchscan.go, readGeneration), a 200 hydrate embeds it
	// in the tree before the swap-in rename (issue #917), a 204 stamps it as its only
	// write, and the Worker declares a held generation to the API only when the stamp
	// landed (issue #2500). One stat covers both destructions: the dir gone (ENOENT on
	// the path) and the dir emptied in place (the marker gone with the contents), which
	// a bare directory stat passed. The name is matched EXACTLY — a ".mcsd_generation-*"
	// temp sibling from a crashed stamp is not a marker to any consumer, so it does not
	// count here either. A marker-ONLY dir PASSES: that is the 204 nothing-published
	// contract, where booting a fresh world is intended — which is why a content
	// predicate ("level.dat present") would be wrong. Residual: a destruction that
	// spares the marker but eats the content still boots.
	//
	// REFUSE rather than hydrate here (the owner's call on issue #2499). Hydrating
	// anyway would be self-healing and invisible, which is its weakness: it masks a
	// host whose disk is being destroyed underneath a running Worker and the operator
	// learns nothing. The refusal is the loud version of the same recovery — the API
	// answers a start by re-launching WITH a full hydrate (_launch, lifecycle.py), and
	// a restart's refused relaunch by leaving the server down for the reconciler's
	// redispatch_start, which meets this same guard and takes that replay. So the
	// recovery is stop (the restart's own) -> hydrate -> start, assembled from shipped
	// parts. The WARN below plus the API's own WARN are what tell the operator it
	// happened.
	//
	// The check is race-free: the reservation is already held, so it holds off the
	// hydrate or start that could create the working set concurrently. It sits after
	// reserve() so the unsettled states keep their own codes — a running instance still
	// answers INVALID_STATE and an orphan/in-flight command still answers BUSY, which
	// the API converges and retries on respectively.
	//
	// SERVER_NOT_FOUND, with the same "working dir absent" phrase handleSnapshot's
	// sibling refusal uses (issue #1713): no working set is held for this id and no
	// retry can succeed without a hydrate. The phrase is load-bearing — the API keys
	// on it together with the code (_WORKING_SET_ABSENT_MARKER in
	// api/src/mc_server_dashboard_api/servers/application/lifecycle.py) to tell this
	// refusal from a plain SERVER_NOT_FOUND. It is kept verbatim for the emptied case
	// too: the prose is a shade imprecise there, but the discriminator is exact, and
	// rewording it would mean touching every pinned site on both sides at once — which
	// is now enforced rather than remembered: the message below is declared as
	// "working_set_absent.launch" in proto/contract/command_error_contract.json,
	// TestCommandErrorContract asserts this emission against that declaration, and the
	// API's phrase is pinned to the same entry (issue #2843).
	workingDir := filepath.Join(m.scratchDir, cmd.ServerID)
	if _, err := os.Stat(filepath.Join(workingDir, generationFile)); os.IsNotExist(err) {
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
//
// A terminal state is the only thing that used to end them, and a server the
// Worker is shut down underneath never reaches one — so they are manager-owned
// goroutines Close joins (issue #2777). A start that lands on an already-closed
// manager therefore starts NONE of them: the instance is registered (the map is
// what guards the id) but its events go nowhere, which is what they did anyway
// with no session left to forward them to. The status pump is started first and
// gates the other two, so the pumps that watch its done channel can never be
// started without the pump that closes it.
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
		// A lifecycle command is already reserved in flight for this id (issue #780):
		// most importantly, a DETACHED stop from a dropped stream's lane is still
		// confirming termination — takeStoppableReserve evicted the instance and holds
		// the reservation across inst.Stop (up to ~3x stopTimeout). A re-sent StopServer
		// on the reconnected stream must NOT get SERVER_NOT_FOUND here: that makes the
		// API converge observed=stopped and unassign while the old process is still
		// alive and writing, after which a re-placed start's HydrateTrigger would clobber
		// the live working set. Returning BUSY (issue #824) makes the API's redispatch_stop
		// keep the assignment and retry on a later tick (lifecycle.py), converging safely
		// once the detached stop finishes (the id then becomes genuinely SERVER_NOT_FOUND).
		return fail(cmd.CommandID, session.CommandErrorBusy,
			"instancemanager: a lifecycle command is already in flight for this server")
	}
	// The id is now reserved across the eviction -> stop-confirmed window so the
	// detached stop is the sole writer; released on every return below (issue #780).
	defer m.release(cmd.ServerID)
	if err := m.attemptStop(ctx, cmd.ServerID, inst, graceful, driver, mcVersion); err != nil {
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

// removeScratchTree is the os.RemoveAll removeScratch takes the scratch dir out with,
// indirected through a package var (mirroring removeDisplacedTree) so a test can observe
// the exact instant the id stops being advertised — the point after which no per-id pass
// is ever offered it again, and therefore the point the hydrate-leftover sweep has to
// precede (issue #3167). The deleted-server reclaim needs no such seam: its own removal
// logs on success, so the test there parks on that record. Production always uses
// os.RemoveAll.
var removeScratchTree = os.RemoveAll

// removeScratch sweeps any .hydrate-<id>-* temp/trash siblings a crash mid-hydrate left
// behind for this id (datatransfer.unpackAndSwap, issue #772, swept via
// sweepHydrateLeftovers) and then deletes the server's local working-set scratch dir.
// That order is load-bearing rather than incidental — see the body (issue #3167).
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
//   - A server DELETED while its scratch was live is reclaimed at the next
//     registration via ReclaimDeletedScratches (issue #924). The API computes the
//     unknown subset of held_servers and returns it in RegisterAck; the Worker
//     removes the scratch dir and hydrate leftovers but NOT .displaced-<id> trees
//     (issue #911).
func (m *Manager) removeScratch(serverID string) {
	// The leftovers go FIRST, and the order is load-bearing (issue #3167, the same
	// hazard issue #2934 closed on the deleted-server reclaim). <scratch>/<id> is what
	// keeps the id advertised — both held-set scans skip .hydrate-<id>-*
	// (isReservedScratchName) — so the instant it is removed the id leaves held_servers
	// and no PER-ID pass is ever offered it again: a deleted or re-placed-elsewhere
	// server gets no further stopped-id snapshot, the API stops deriving the id into
	// unknown_held_server_ids, and datatransfer's own sweep runs only if the server comes
	// back to this Worker. Sweeping after the removal therefore left every interruption
	// in that window a world-sized tree only a Worker BOOT reclaims
	// (ReclaimHydrateLeftovers) — the backstop, not the plan, on a Worker that runs for
	// months. This way round, an interruption anywhere in here leaves the scratch dir
	// standing, and with it the advertisement that re-offers the id.
	//
	// This path is MORE exposed than that reclaim, not less: it runs on a session command
	// lane, which shutdown abandons without waiting at all (Runner.serve joins no lane),
	// so an ordinary SIGTERM reaches the window a crash reaches there.
	m.sweepHydrateLeftovers(serverID)
	dir := filepath.Join(m.scratchDir, serverID)
	if err := removeScratchTree(dir); err != nil {
		m.logger.Warn("failed to remove scratch dir after final snapshot",
			"server_id", serverID, "dir", dir, "error", err)
	}
	// The successful stopped-id snapshot proves the store supersedes this server's
	// world, so a displaced tree a prior hydrate kept aside for recovery (issue #906)
	// is now redundant and reclaimed alongside the scratch. No identity re-check is
	// passed: this path holds the per-id reservation, so no hydrate can park a fresh
	// recovery copy in the slot mid-sweep (issue #3118).
	m.sweepDisplaced(serverID, nil)
}

// sweepDisplaced removes the .displaced-<id> tree a prior hydrate moved aside for
// recovery (issue #906). It runs on the next SUCCESSFUL snapshot for the id — the
// moment the store provably supersedes the displaced world — mirroring the #845
// GC-on-success reclamation. The name matches datatransfer.displacedDir exactly
// (".displaced-<id>"), so only this id's displaced tree is touched. Best-effort: a
// failure is ignored (the leftover is wasted disk, never a correctness problem). A
// missing tree is a no-op.
//
// RENAME, THEN REMOVE (issue #2799). The tree is first renamed out of the slot to a
// unique .sweeping-<id>-* sibling, and only that name is traversed. The slot is what a
// hydrate's oldest-wins check (datatransfer.displacedSlotHoldsWorkingSet) reads, by
// name, and removing the tree in place is a traversal that takes seconds for a
// world-sized tree: a check landing inside it read the half-deleted tree as an occupied
// slot, retained it — while the traversal went on deleting it — and dropped the live set
// the hydrate displaced. The rename empties the slot atomically before any traversal
// starts, so a hydrate finds either the whole tree or nothing; the scratch root is
// fsynced before the traversal, so not even a power loss can put a half-deleted tree
// back in the slot. A failed rename therefore returns WITHOUT removing anything: falling
// back to an in-place removal would reopen that window, and declining costs only a leak,
// retried by the next successful snapshot. A traversal that does not finish (a crash,
// or a removal error) leaves the tree under its .sweeping- name, which
// ReclaimInterruptedDisplacedSweeps removes at the next Worker boot.
//
// The function itself removes nothing unconditionally; the CALLERS establish that the
// success really does supersede the tree being removed, and they do it differently. The
// stopped-id caller (removeScratch) holds a per-id reservation, so no hydrate can be
// racing it and it passes a nil stillPinned. The running-id caller takes no reservation
// (#829 item 4), so it gates this call on the working-dir identity pin instead (issue
// #2291, reusing the #2284 pin) and hands that pin's check in as stillPinned: an old
// dropped stream's snapshot can still succeed after a NEW stream re-placed the server
// here and hydrated it, and the .displaced-<id> it would sweep is then that hydrate's
// recovery copy — a tree this snapshot never published, holding the published state
// plus whatever the world progressed since its PACK — rather than a world the success
// supersedes. That is the window issue #917 item 3 named and left open.
//
// RE-CHECK AFTER THE RENAME (issue #3118), because the caller's gate alone cannot cover
// what the rename takes. The pin keeps passing right up to the moment the racing hydrate
// renames the working dir aside, and the rename below takes whatever sits in the slot at
// THAT instant, not what the Lstat above saw: a hydrate can clear world-less junk from
// the slot and park its live set there in between, so the sweep's residual was never a
// leak-only direction — it could take the fresh recovery copy. The identity is therefore
// checked again once the tree is out of the slot and before anything is unlinked. A
// removal then happens only while the working dir is still the tree this snapshot
// packed; a tree that holds a working set and was taken from a slot whose working dir was
// replaced meanwhile goes back where it came from, and only when the slot is empty —
// putBackSweptTree has the full rule and what it costs. Every uncertainty resolves to "not current"
// (workingDirRef.current), so an unreadable identity puts the tree back too: the leak
// direction, at worst one more tree until the next successful snapshot.
//
// What a decline costs is that LEAK — one world-sized tree until the next successful
// snapshot for the id reclaims it, which is the #906 contract itself. The one loss left
// is crash-conditional: a power loss after the rename out of the slot and before the
// put-back is durable rolls the tree back to its .sweeping- name, which the next boot
// reclaims. That window is strictly narrower than the unconditional removal it replaced,
// and the boot reclaim is deliberately not taught to put trees back — a .sweeping- tree
// is garbage in every other case.
func (m *Manager) sweepDisplaced(serverID string, stillPinned func() (bool, string)) {
	trash, remove := m.detachDisplacedTree(serverID, stillPinned)
	if !remove {
		return
	}
	// Make the rename durable before the traversal unlinks anything, so a power loss
	// cannot roll it back over a half-deleted tree and put that tree back in the slot.
	// Both still happen after the rename and before the first unlink (#2799); they run
	// OUTSIDE the slot claim because they no longer touch the slot, and a world-sized
	// traversal is not something another sweep for this id should have to wait behind.
	if err := syncSweepScratchRoot(m.scratchDir); err != nil {
		return
	}
	_ = removeDisplacedTree(trash)
}

// detachDisplacedTree performs the slot-visible half of a sweep under this id's slot
// claim (sweepingSlot): find the tree, rename it out of the slot, re-check the caller's
// identity pin and either hand the tree over for removal or put it back. It reports the
// name the tree now sits under and whether removing it is justified.
//
// Everything that reads or writes .displaced-<id> is inside the claim, and nothing else
// is. A sweep that cannot take the claim returns having touched nothing: the tree it
// would have swept stays in the slot, and the next successful snapshot for the id sweeps
// it instead. That decline is what keeps two sweeps from deciding about one slot at once,
// which is the precondition for a put-back of one tree costing the other (issue #3118).
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

// putBackSweptTree renames a tree the sweep had taken out of the .displaced-<id> slot
// back into it, for the re-check above (issue #3118). It runs before any unlink, so the
// tree is still whole.
//
// THE TREE must hold a working set, by sweptTreeHoldsWorkingSet — the rule the HYDRATE
// applies to this same slot, type-aware and error-returning, pinned to the adapter's copy
// by a twin test rather than shared through an import the layering does not allow. It is
// deliberately not hasWorkingSet: that one reads through a symlink and folds every read
// failure into "no working set", and both answers are "delete this at the next boot" here
// (PR #3121 review, round 2). Running-id sweeps take NO cross-stream
// reservation, so TWO can be in this window at once, and world-less junk put back by one
// of them occupies the slot against the other, which may be holding the hydrate's live
// set. That set would then go under .sweeping- for the next boot to delete, which is the
// loss this function exists to prevent (PR #3121 review, round 1). Junk is left under
// .sweeping- instead: it is the garbage the boot reclaim expects, and what this sweep was
// going to do with it anyway.
//
// THE SLOT must be empty, and empty is the whole rule, not a proxy for "holds nothing
// worth keeping": os.Rename REFUSES any existing directory as its target (an EEXIST it
// raises itself, before the syscall — unlike rename(2), which would replace an empty
// one), so a slot holding marker-only junk blocks the put-back whatever this function
// decides about it. Emptying it first is not an option: the sweep holds no reservation,
// so RemoveAll on the slot is exactly the in-place removal issue #2799 forbids — a
// hydrate can clear that junk and park its live set between the read and the removal, and
// the removal would then delete a live world. Only the hydrate, under its per-id
// reservation, can clear the slot. The Lstat is therefore an early-out and a log
// distinction; what makes the put-back safe is the rename refusing to clobber whatever a
// concurrent hydrate parked since.
//
// ON READ UNCERTAINTY the tree is KEPT, not dropped: an unclassifiable tree falls through
// to the put-back. The asymmetry is the point — putting back a tree that turns out to be
// junk costs at worst an occupied slot, while leaving one that turns out to be real costs
// the only copy of the unpublished delta at the next boot. It is the direction the
// hydrate takes on the same read (an unreadable slot fails the hydrate rather than being
// reclassified into a discard).
//
// That retention is only safe because THIS id's slot claim (sweepingSlot) makes this
// function the only sweep deciding about the slot: the tree put back here can no longer
// occupy the slot against a concurrent sweep holding a proven recovery tree. The
// invariant the pair upholds: a tree that holds a working set is never deleted because
// some OTHER tree's classification was junk or uncertain.
//
// A tree that cannot go back stays under its .sweeping- name for
// ReclaimInterruptedDisplacedSweeps, and that is logged: it decides what the next boot
// deletes, and a .sweeping- tree is garbage everywhere else. Junk left behind is not
// logged — it IS ordinary garbage, indistinguishable from an interrupted sweep's.
//
// The put-back can also lose a race to a LATER hydrate's own park into the same empty
// slot: that park then fails with ENOTEMPTY and fails the hydrate, which deletes nothing
// before its park (datatransfer.unpackAndSwap) and is simply retried.
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

// renameSweptTree is the os.Rename sweepDisplaced empties the slot with, indirected
// through a package var (mirroring removeDisplacedTree) so a test can land a racing
// hydrate's park in the one gap that decides what this rename takes — between the Lstat
// that found the slot occupied and the rename itself — rather than race for it. Only the
// rename OUT of the slot goes through it; putBackSweptTree renames back with os.Rename
// directly, so a test's seam cannot also intercept the recovery. Production always uses
// os.Rename.
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

// sweepHydrateLeftovers removes the .hydrate-<id>-* temp/trash siblings a crashed
// hydrate for serverID left in the scratch root. The next start's leftover sweep
// (datatransfer.sweepHydrateLeftovers) clears them too, but only if the server is
// re-placed onto this Worker; a deleted/re-placed-elsewhere id would otherwise leak
// the world-sized orphan until the next Worker boot, where ReclaimHydrateLeftovers takes
// it (issue #3167) — months away on a Worker that does not restart, which is why this
// per-id sweep stays the one that runs at the time it matters, and why its CALLERS run it
// before the scratch removal that ends the id's advertisement.
//
// The prefix is built from hydratePrefix — the
// same constant the held-set scans skip on — and matches datatransfer.hydrateTmpPrefix
// exactly (".hydrate-<id>-"), so only this id's leftovers are touched — not another
// server's dir or a similarly named one. Best-effort: a removal failure is ignored
// (a leftover is wasted disk, never a correctness problem).
func (m *Manager) sweepHydrateLeftovers(serverID string) {
	entries, err := os.ReadDir(m.scratchDir)
	if err != nil {
		return
	}
	prefix := hydratePrefix + serverID + "-"
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), prefix) {
			_ = os.RemoveAll(filepath.Join(m.scratchDir, e.Name()))
		}
	}
}

// ReclaimDeletedScratches removes scratch dirs for server ids the API confirmed
// no longer exist (issue #924). It runs asynchronously on a goroutine so it does
// not block heartbeats or command dispatch. Per id it validates the id, claims a
// reservation (skipping running/orphaned/reserved ids), sweeps this id's hydrate
// leftovers, removes the scratch dir, then releases the reservation. That order
// is load-bearing rather than incidental — see the body. .displaced-<id> trees are
// intentionally NOT reclaimed (issue #911: retained for operator recovery).
//
// Reclamation contract update (issue #924, extending #841):
//   - The post-stop final snapshot path (removeScratch) remains the primary GC.
//   - This method covers the gap: a server deleted while its scratch was live
//     (the final snapshot never arrived), reclaimed at the next registration.
//   - Phase 2 (refresh held inventory per re-registration) is implemented:
//     HeldServers() (issue #1711) refreshes the advertised set each register.
//
// The goroutine is manager-owned, so it goes through goBackground and Close JOINS
// it (issue #2878). The join is what lets the PER-ID body stay uninterruptible: from
// reserve to release the id holds a reservation and, for part of that window, a
// half-removed working set, so a cancellation landing there would let the process
// exit inside exactly the window the join closes. Between ids nothing is held, so
// the loop TOP does read the shutdown (issue #2933) and what Close pays is the one
// id already in flight rather than every id still on the list.
//
// A reclaim requested AFTER Close is dropped whole, and silently: goBackground
// starts nothing on a closed manager, and ScratchReclaimer is void so there is
// nothing to report back to the session. Nothing is lost either — the API
// recomputes the unknown subset of held_servers on every registration, so an id
// dropped here is offered again at the next one.
func (m *Manager) ReclaimDeletedScratches(serverIDs []string) {
	m.goBackground(func() { m.reclaimDeletedScratches(serverIDs) })
}

// reclaimDeletedScratches is the synchronous body of ReclaimDeletedScratches.
// Tests call this directly to avoid timing dependencies on the goroutine.
func (m *Manager) reclaimDeletedScratches(serverIDs []string) {
	for _, id := range serverIDs {
		// The body's one cancellation point, deliberately HERE and nowhere else
		// (issue #2933). The loop top sits after the previous id's release and
		// before this id's reserve, so a return holds no reservation and leaves no
		// half-removed working set — safe by the same reasoning that makes Close's
		// join safe, and it bounds Close to the id already in flight instead of
		// every id still on the list. The ids left unreached are re-offered, not
		// lost: they still hold their scratch dirs, so the next registration
		// advertises them in held_servers again and the API re-derives the unknown
		// subset from that advertisement.
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
		// The leftovers go FIRST, and the order is load-bearing (issue #2934).
		// <scratch>/<id> is what keeps the id advertised — both held-set scans skip
		// .hydrate-<id>-* (isReservedScratchName) — so the instant it is removed the
		// id leaves held_servers, the API stops deriving it into
		// unknown_held_server_ids, and this pass is the only one that would ever be
		// offered the id again; only a Worker BOOT reclaims a .hydrate- tree
		// (ReclaimHydrateLeftovers, issue #3167), and that is the backstop rather than
		// the plan — a Worker runs for months between boots. Sweeping after the removal
		// made every interruption in that window a world-sized leak nothing on this
		// Worker's runtime ever reclaims. This way round, an interruption anywhere
		// in the body leaves the scratch dir standing, and with it the advertisement that
		// re-offers the id — which is what makes "a partial reclaim is finished
		// idempotently by the next registration" true at EVERY point in the body,
		// not merely at most of them. It is also what keeps this leg out of the
		// shutdown budget: compose.yaml's stop_grace_period is sized for Close's
		// retry-stop leg, and an interruption here costs nothing at any value of it —
		// or in a crash or a power loss, which no value reaches.
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
		// NOTE: .displaced-<id> trees are intentionally NOT reclaimed here
		// (issue #911). They are retained for operator recovery.
		m.release(id)
	}
}

// orphanEntry pairs a failed-stop orphan instance with the execution driver name
// and Minecraft version it was started under, so the retry stop can resolve the
// RCON dial host exactly as stop #1 did (issue #1712) and read the RCON password
// in the same charset (issue #3116).
type orphanEntry struct {
	inst      execution.Instance
	driver    string
	mcVersion string
}

// takeOutcome is the result of takeStoppableReserve / takeRunningReserve: an
// instance was taken and reserved, no live instance exists (genuinely unknown ->
// SERVER_NOT_FOUND), a lifecycle command is already reserved in flight for the id
// (a detached stop still confirming, or a start/hydrate mid-operation -> BUSY,
// issue #780/#824), or a failed-stop orphan is recorded for the id (a process
// this Worker could not confirm dead -> INVALID_STATE, issue #2466).
// takeOrphaned is reachable only from takeRunningReserve: takeStoppableReserve
// TAKES the orphan (that is the retry path that terminates it).
type takeOutcome int

const (
	takeFound takeOutcome = iota
	takeNotFound
	takeInFlight
	takeOrphaned
)

// orphanPendingMsg is the precondition message every command refused over a
// recorded failed-stop orphan carries. One constant so the reserve()-gated
// commands (start/hydrate/stopped-id snapshot) and the ones that check the
// orphan directly (restart/console/tunnel dial) say the same thing about the
// same state — the point of issue #2466 is that the refusal reads honestly
// wherever it surfaces. The API discriminates on the CODE, not this text, and
// the code deliberately differs between those two groups (issue #2476): the
// message describes the STATE, which is identical; the code answers whether THIS
// command will succeed once the converger resolves it, which is not.
const orphanPendingMsg = "instancemanager: server has a failed-stop orphan pending termination"

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
//
// The returned driver is the execution driver name for the instance and
// mcVersion its Minecraft version: read from startCmds for a running instance
// (before deletion), or from the orphan entry for a failed-stop orphan (issues
// #1712, #3116). This makes the capture atomic with the take, eliminating the
// TOCTOU between a separate controlTargetFor call and the eviction.
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
	// Check the reservation BEFORE the orphan branch. A failed-stop orphan retains
	// its instance record while a stop for the id is in flight (attemptStop deletes
	// the orphan only on a confirmed termination), so an orphan-retry stop1 holds
	// the reservation AND keeps the orphan recorded across its inst.Stop. If a
	// re-sent stop2 walked into the orphan branch here it would take the same orphan
	// instance a second time; both stops then run their deferred release, and stop1's
	// release steals the reservation out from under the still-running stop2 — worst
	// case leaving stop2 to drive removeScratch unreserved. Honoring the reservation
	// first rejects stop2 with takeInFlight (-> BUSY) instead, exactly as it
	// already does for a detached running-instance stop (issue #780).
	if m.reserved[serverID] {
		return nil, "", "", takeInFlight
	}
	if entry, ok := m.orphans[serverID]; ok {
		m.reserved[serverID] = true
		return entry.inst, entry.driver, entry.mcVersion, takeFound
	}
	return nil, "", "", takeNotFound
}

// attemptStop runs the driver Stop for serverID's instance. On failure it
// records the instance as a failed-stop orphan so a retry can re-attempt
// termination against the same handle rather than returning SERVER_NOT_FOUND; on
// success it forgets any orphan record for the id (issue #251) and closes the
// server's Bedrock relay tunnel, if any (docs/app/BEDROCK_TUNNEL.md, issue
// #1546) — a Worker-local safety net so the tunnel comes down as soon as this
// Worker confirms the stop, without waiting on the API's own CloseBedrockTunnel
// dispatch to arrive. Close is idempotent, so this is a no-op for a non-Bedrock
// server or one with no tunnel open. attemptStop is shared by StopServer and
// the stop phase of RestartServer, so a restart also closes and later reopens
// the tunnel — matching the API's own "any transition away from running closes
// it" semantics (PR #1558).
//
// driverName is the driver that runs this server and mcVersion its Minecraft
// version (returned atomically by takeStoppableReserve / takeRunningReserve
// alongside the instance). On a graceful stop, attemptStop passes a pre-fallback
// flush closure so the driver can flush the live world (save-all + settle) before
// stop — the driver calls it always before tryRCONStop on the graceful path
// (#1007). On failure both are preserved on the orphan entry so a retry resolves
// RCON identically (issues #1712, #3116).
func (m *Manager) attemptStop(ctx context.Context, serverID string, inst execution.Instance, graceful bool, driverName, mcVersion string) error {
	var preFallback func(context.Context) bool
	if graceful {
		preFallback = func(flushCtx context.Context) bool {
			return m.flushBeforeStopWithDriver(flushCtx, serverID, driverName, mcVersion)
		}
	}
	err := inst.Stop(ctx, graceful, preFallback)
	// The flush's save-off is settled only once this call has done whatever the
	// stop's outcome calls for: a confirmed termination leaves nobody to restore it
	// for, and a failure reaches restoreSaveOnAfterFailedStop below. DEFERRED rather
	// than placed here, so the clear can never precede that restore — Close joins no
	// command lane, so a drain landing in between would find an empty map and let the
	// process exit under a survivor with auto-save still off (issue #3166).
	defer m.clearPendingSaveOn(serverID)
	if err != nil {
		// Record the orphan and hand it to a converger, so the Worker keeps working
		// the stop on its own instead of waiting for an operator to notice (issue
		// #2475). recordOrphan is idempotent on the converger: the retries the
		// converger itself issues land back here and re-record without spawning a
		// second one.
		m.recordOrphan(serverID, inst, driverName, mcVersion)
		// The orphan record is otherwise invisible: nothing enumerates m.orphans, so
		// "why is every command for this server refused?" was a code-reading exercise
		// (issue #2466). Say it once, at the moment the state is entered — the id is
		// now guarded against start / hydrate / restart / console / relay tunnel
		// dial / Bedrock tunnel open until a retry stop confirms termination or the
		// process exits on its own.
		m.logger.Warn("recorded failed-stop orphan; the process may still be running",
			"server_id", serverID, "driver", driverName, "graceful", graceful, "error", err)
		// Close the Bedrock relay tunnel here too, not only on a confirmed stop
		// (issue #2468 item 2): the stop intent is the operator's, and an instance
		// this Worker is still trying to terminate must not keep taking joins for
		// however long convergence takes. Close is idempotent and takes no running
		// check, so the operator can still tear it down by hand either way.
		if m.bedrock != nil {
			m.bedrock.Close(serverID)
		}
		// The graceful path issued save-off before the flush; because the stop
		// failed, the server may still be alive with auto-save disabled. Re-enable
		// it so player progress is not silently lost (issue #2021).
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

// takeRunningReserve atomically (under mu) evicts the tracked running instance for
// serverID, captures its original StartServer spec, and claims an in-flight
// reservation so the id stays continuously claimed across the restart's
// stop -> relaunch window (issue #780). It reports takeInFlight when no instance is
// tracked but the id is already reserved (a detached stop or another lifecycle
// command still in flight) and takeNotFound for a genuinely unknown id. A restart
// applies only to a tracked running instance, so a recorded orphan is NOT taken
// here (it is left for the stop-retry path, issue #251) — it reports takeOrphaned,
// so the restart is refused as a settled INVALID_STATE naming the orphan rather
// than as SERVER_NOT_FOUND, which would tell the operator the server is not
// running about a process that is probably still alive (issue #2466). The
// reservation is checked BEFORE the orphan for the same reason as in
// takeStoppableReserve: while an orphan-retry stop is in flight the id carries
// both, and the in-flight command's outcome is not yet known, so BUSY (retry
// later) is the honest answer rather than a settled state.
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
	// Single atomic take: this is the sole decision point for the restart path.
	// It distinguishes all three outcomes without a TOCTOU window (issue #1950).
	inst, start, outcome := m.takeRunningReserve(cmd.ServerID)
	switch outcome {
	case takeNotFound:
		return fail(cmd.CommandID, session.CommandErrorServerNotFound,
			"instancemanager: server not running")
	case takeOrphaned:
		// A prior stop for this id could not confirm termination, so the process is
		// probably still alive — restarting it is refused, but as the settled state
		// it is, not as "not running" (issue #2466). The retry stop (StopServer) is
		// the path that terminates the orphan; only once it confirms does the id
		// become genuinely unknown.
		return fail(cmd.CommandID, session.CommandErrorInvalidState, orphanPendingMsg)
	case takeInFlight:
		// A lifecycle command (e.g. a detached stop from a dropped stream, or a start/
		// hydrate mid-operation) is already reserved in flight for this id (issue #780).
		// Rejecting with BUSY (issue #824) rather than SERVER_NOT_FOUND keeps the API from
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
	// The id is reserved from here across the stop and the relaunch; it is handed off
	// to the re-registered instance on a successful relaunch (launchReserved) and
	// released on every failure path so the id is never left unclaimed under the still-
	// stopping process (issue #780).
	//
	// A restart whose stop cannot confirm termination leaves the same failed-stop
	// orphan as a plain StopServer would, so the reconciler's retry path can still
	// terminate it rather than double-instancing over it (issue #251). The reservation
	// is dropped on this failure path; the orphan record then guards the id instead.
	if err := m.attemptStop(ctx, cmd.ServerID, inst, true, start.Driver, start.MinecraftVersion); err != nil {
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

// notRunningRefusal reports whether serverID has no tracked running instance and,
// when it has none, the coded refusal the command must fail with. Both facts are
// read in one critical section so the classification cannot straddle a concurrent
// orphan record.
//
// A recorded failed-stop orphan is refused as INVALID_STATE naming the orphan,
// not SERVER_NOT_FOUND: this Worker could not confirm the process dead, so it is
// probably still alive, holding its port and writing its world, and "server not
// running" is a false statement about it (issue #2466). Every other untracked id
// keeps SERVER_NOT_FOUND — the code stays reserved for ids this Worker genuinely
// knows nothing about.
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

// handleTunnelDial opens a relay dial-back tunnel for one player session (RELAY.md
// Section 5). The server must be running locally — a not-running server returns
// SERVER_NOT_FOUND, a failed-stop orphan INVALID_STATE (issue #2466) — and the
// dialer resolves its published loopback game port from the working dir, dials
// the relay endpoint, presents the token, and splices the
// two. It returns once the splice is established; the splice itself runs on the
// dialer's own long-lived context, off this command, so it outlives the result. A
// TunnelDial is a quick command: it bypasses the slow-lane cap (session layer) so
// a join never queues behind a hydrate.
//
// The dial is dispatched fire-and-forget (RELAY.md Section 4): the API awaits no
// result and the relay's real answer is the dial-back arriving, so this refusal
// reaches nobody but the API's diagnostic log
// (fleet/adapters/control_plane.py _log_fire_and_forget_result, which logs the
// message at WARN). The joining player's experience is unchanged either way —
// the join stalls until the relay times it out, because the route still says
// running. Only the log line differs, and that is exactly the line an operator
// reads when a "running" server refuses joins.
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

// handleOpenBedrockTunnel starts (or, for a repeated command with the same
// credential, idempotently confirms) this server's Bedrock relay QUIC tunnel
// (docs/app/BEDROCK_TUNNEL.md, issue #1546). Like TunnelDial, the server must
// be running locally, and a failed-stop orphan is refused as INVALID_STATE
// rather than SERVER_NOT_FOUND (issue #2466) — an orphan reaches this handler
// because the API dispatches Open off an observed=running WRITE (the sink's hook
// on an applied StatusChange(running), or a lifecycle convergence;
// servers/adapters/bedrock_tunnel_sync.py) and that dispatch is fire-and-forget,
// so a stop whose driver Stop cannot confirm termination in the gap records the
// orphan before the command lands: the API's cached running state does not move
// until the Worker's own next report. The refusal therefore reaches only the
// API's WARN log, which must not say the server is not running about a process
// that may still be alive. This verb keeps INVALID_STATE where start / hydrate /
// stopped-id snapshot moved to BUSY (issue #2476): it is refused for what the
// state IS and is never carried out once the orphan converges, so BUSY would
// promise a success that never comes.
//
// Unlike TunnelDial, Open does not itself dial/handshake
// synchronously: it registers the tunnel and returns, while the QUIC dial,
// handshake, datagram pump, and any reconnect-with-backoff run off this
// command on the tunneler's own long-lived context — a slow or rejected relay
// dial must not hold up the command result.
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

// handleCloseBedrockTunnel tears down this server's Bedrock relay tunnel, if
// any (docs/app/BEDROCK_TUNNEL.md Section 3, issue #1546). Unlike Open it does
// not require the server to still be tracked as running: it is also the
// Worker-local safety net a successful StopServer triggers on its own
// (attemptStop), so a Close arriving after the instance is already evicted —
// or for a server this Worker never opened a tunnel for — must still succeed,
// not SERVER_NOT_FOUND.
//
// That makes it the one running-server command with nothing for the failed-stop
// orphan refusal to fix (issue #2466): it takes no running check at all, so it
// never reported an orphan as not-running, and over an orphan it does the useful
// thing — a failed stop leaves the tunnel open (issue #2468) and this closes it.
// Refusing it for an orphan would remove the only way to take that tunnel down
// without terminating the process.
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

// controlTargetFor returns the execution driver and Minecraft version recorded
// for serverID's running instance (its StartServer command's Driver and
// MinecraftVersion), so the RCON dial host can be resolved per driver and the
// RCON password read in the charset of that version (issue #3116). Both are
// empty for a server that is not running, in which case the caller resolves the
// loopback host — but every caller first confirms the server is running, so the
// recorded command is present.
func (m *Manager) controlTargetFor(serverID string) (driver, mcVersion string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	start := m.startCmds[serverID]
	return start.Driver, start.MinecraftVersion
}

// reserve claims serverID for an in-flight mutating lifecycle command (issue
// #780). It atomically rejects — under the same mu held for the running/orphan
// checks, so there is no check-then-act gap — when the id is already running, has
// a failed-stop orphan pending, or already carries a reservation, and otherwise
// marks it reserved. ok reports whether the claim was taken; on a rejection, code
// classifies the failure (CommandErrorInvalidState for the one SETTLED state this
// gate sees, "already running"; CommandErrorBusy for the two unsettled ones, the
// reservation race of issue #824 and the pending orphan of issue #2476) and msg is
// the precondition message the caller fails with. It must be paired with release
// on every exit path.
func (m *Manager) reserve(serverID string) (ok bool, code session.CommandErrorCode, msg string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, running := m.instances[serverID]; running {
		return false, session.CommandErrorInvalidState, "instancemanager: server already running"
	}
	if _, orphaned := m.orphans[serverID]; orphaned {
		// A prior stop could not confirm termination: the process/container may
		// still be lingering. Starting/hydrating now would double-instance over it,
		// so the command is refused (issue #251) — but as BUSY, not INVALID_STATE
		// (issue #2476). Since issue #2475 the orphan is never a settled state: a
		// converger is probing the process, retrying the stop while it is alive and
		// retiring the record once it is confirmed gone, so THIS command succeeds on
		// a later retry. That is exactly the BUSY contract (issue #824) — outcome not
		// yet known, retry rather than converge.
		//
		// INVALID_STATE here was the #2467 wedge: reserve() cannot tell an orphan
		// whose process is alive from one already dead, yet it answered the same code
		// the API reads as "already running" on a start, so a dead orphan's refusal
		// manufactured observed=running on a server that was down. The verbs that
		// check m.orphans directly — restart / console / tunnel dial / Bedrock tunnel
		// open — keep INVALID_STATE on purpose: they are refused for what the state
		// IS and will never be executed later, so BUSY would promise a success that
		// never comes.
		return false, session.CommandErrorBusy, orphanPendingMsg
	}
	if m.reserved[serverID] {
		// A re-issued duplicate arriving while the original is still in flight after
		// a stream reconnect (issue #780): reject it as BUSY rather than overlap the
		// original. The original's outcome is unknown, so the API must NOT converge
		// observed=running on this — it keeps the assignment and retries (issue #824).
		return false, session.CommandErrorBusy, "instancemanager: a lifecycle command is already in flight for this server"
	}
	m.reserved[serverID] = true
	return true, 0, ""
}

// release drops serverID's in-flight reservation so a later command (a retry
// after a failure, or the next lifecycle op) can claim it again (issue #780).
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

// pump forwards an instance's status events onto the merged stream, mapping the
// domain state to its wire name. It also forgets a crashed instance so the server
// id can be started again. It exits when the instance closes its event channel —
// or when the manager is closed (issue #2777), because a server that is still
// running when the Worker goes down never closes it — closing done either way to
// release the log/metrics pumps for the same instance.
func (m *Manager) pump(serverID string, inst execution.Instance, done chan struct{}) {
	defer close(done)
	events := inst.Events()
	for {
		select {
		case ev, ok := <-events:
			if !ok {
				// The instance closed its stream: it reached a terminal state on its
				// own. If it was recorded as a failed-stop orphan (issue #251), forget
				// the record so a later stop for the id is a genuinely unknown server,
				// not a lingering retry target.
				m.forgetOrphanIf(serverID, inst)
				return
			}
			if ev.State == execution.StateCrashed {
				m.forgetIf(serverID, inst)
			}
			m.sendStatus(session.StatusEvent{ServerID: ev.ServerID, State: ev.State.String(), Detail: ev.Detail})
		case <-m.shutdown.Done():
			// Close. A status the instance has already queued is DROPPED: nothing
			// drains the merged stream by then (Close runs after the session runner
			// returns, main.go), so forwarding it would only move it into a channel
			// no one reads — which is what happened before, one process exit later.
			// The orphan record is left alone on this path: the instance has NOT
			// exited, and retiring its record would claim a fate the Worker never
			// observed.
			return
		}
	}
}

// forgetOrphanIf removes serverID's failed-stop orphan record only if it is still
// the given inst, so it does not clear a record belonging to a different instance
// (issue #251). It reports whether it removed anything: the pump ignores that (it
// is retiring a record that may not exist), while the converger emits the terminal
// `stopped` only when this call is the one that actually retired the record
// (issue #2475).
func (m *Manager) forgetOrphanIf(serverID string, inst execution.Instance) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if e, ok := m.orphans[serverID]; ok && e.inst == inst {
		delete(m.orphans, serverID)
		return true
	}
	return false
}

// ResyncStatus re-emits a StatusChange for every instance the manager still
// holds, so a control-plane (re-)register moves those servers out of the API's
// post-restart observed=unknown state within seconds instead of waiting out the
// reconciler grace window (issue #985). The instance manager persists across
// control-plane reconnects, so its instances map still names the live servers;
// re-emitting their current Status() reflects reality (running/starting/etc.).
// On a fresh process both maps are empty (the orphan sweep removed leftovers and
// no instances are re-created), so this is a harmless no-op.
//
// Failed-stop orphans are reported too, as `unknown` (issue #2468 item 3): they
// have been evicted from instances, so a resync that snapshotted only that map
// left the reconnected API's row asserting a staler state as fact about a process
// this Worker could not confirm dead. `unknown` is the honest answer and the one
// the API's #1599 arm already redispatches a stop for. The two maps are disjoint —
// an instance is evicted before its orphan record is written — so no id is
// reported twice.
//
// Both maps are snapshotted under the lock, which is then RELEASED before any
// emit: sendStatus can coalesce and wake the dispatcher, so it must never run
// while m.mu is held.
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
// absorbed (not dropped). It runs for the Manager's lifetime and ends with Close
// (issue #2777): statusNotify is never closed, so before that it simply parked
// forever on a quiet sink, one leaked goroutine per manager ever built.
//
// It observes the shutdown on BOTH waits, and the send is the one that matters:
// Close runs after the session runner has returned (main.go), so nothing drains
// events any more, and a dispatcher watching the shutdown only between events
// would hold Close forever on a full sink. Whatever is parked in pendingStatus
// at that moment is DROPPED — the same fate it had when the process exited under
// this goroutine, and the coalescing contract is about converging observed_state
// for a session that is still there to read it.
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

// logPump forwards an instance's captured log lines onto the merged log stream
// (FR-MON-2). It exits when the instance closes its log channel (terminal
// state). Under sink backpressure it drops the line: logs are a stream, not
// state, so they keep the lossy posture (unlike status, which coalesces; issue
// #96). The per-instance LogPump already bounds and marks drops at the capture
// edge. Drops here are counted silently and reported as one aggregated summary
// per congestion episode — when a line next gets through, or when the stream
// ends — instead of one WARN per dropped line, which flooded the worker's own
// log for the whole length of a control-plane outage (issue #1716). The counter
// is goroutine-local: one pump goroutine runs per server, so no locking is
// needed.
//
// Like the status pump it also ends on the manager's shutdown (issue #2777): a
// server still running when the Worker goes down never closes its log stream.
// Lines still queued in it are DROPPED, which is the posture this pump already
// has for a congested sink, and by then nothing drains the merged stream anyway.
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

// reportDroppedLogs emits the aggregated summary for one sink-congestion
// episode: a single WARN on the worker's own logger (operator observability)
// and, best-effort, an in-band marker on the merged stream so downstream log
// viewers learn about the gap — mirroring the per-instance LogPump's
// dropped-count marker (execution/logpump.go). The marker send never blocks
// and never displaces a real line: it is only attempted after a real line got
// through (or the stream ended), and is skipped when the sink is still full —
// the WARN already carries the count.
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

// metricsPump samples the instance on the configured interval and forwards a
// Metrics event per tick until the instance terminates (done closed). When the
// instance is not a StatsSource, or a sample errors, it emits an up-only sample
// (server id with zero stats) so the API still learns the server is running
// (FR-MON-3). A full sink drops the sample: metrics are a stream, not state, so
// they keep the lossy posture (unlike status, which coalesces; issue #96).
// Drops are counted silently and reported as one aggregated WARN per congestion
// episode — when a sample next gets through, or when the pump exits — mirroring
// logPump (issues #1716, #1783). The counter is goroutine-local: one pump
// goroutine runs per server, so no locking is needed.
func (m *Manager) metricsPump(serverID string, inst execution.Instance, done chan struct{}) {
	stats, _ := inst.(execution.StatsSource)

	// Bound every Sample by a context cancelled when the instance tears down (done
	// closes) or when the manager is closed, so a hung Engine stats call does not
	// leak this goroutine past stop/crash and cannot hold Close (issue #2777).
	// Each sample additionally carries a timeout proportionate to the interval so
	// a single slow-but-not-stuck call cannot stall the cadence. The watcher is a
	// manager-owned goroutine too: it parks on done, so Close has to join it to be
	// able to say nothing of the manager's is still running.
	pumpCtx, cancel := context.WithCancel(m.shutdown)
	defer cancel()
	m.goBackground(func() {
		<-done
		cancel()
	})

	dropped := 0
	for {
		// The tick is not taken at teardown, and an unfired one is not waited out:
		// metrics are a periodic stream, not state, so a sample the shutdown lands
		// on is simply never produced — a gap consumers already read as missing
		// points. The shutdown is watched here as well as through done so the
		// cadence (15s in production) can never sit between Close and the exit.
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

// reportDroppedMetrics emits the aggregated summary for one metrics
// sink-congestion episode: a single WARN with the drop count, the metrics
// counterpart of reportDroppedLogs. Unlike logs, no in-band marker is sent:
// MetricsEvent carries only numeric fields, so a marker would have to be a
// fabricated sample, and a gap in a periodic series is already visible to
// consumers as missing points.
func (m *Manager) reportDroppedMetrics(serverID string, dropped int) {
	m.logger.Warn("dropped metrics samples; sink full", "server_id", serverID, "count", dropped)
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
