package session

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"sync"
	"time"
)

// ErrTerminal marks adapter-classified credential or protocol errors that must not reconnect.
var ErrTerminal = errors.New("session: terminal connection error")

// Runner drives the Worker's control-plane session: it registers, heartbeats,
// acknowledges inbound commands, and reconnects with backoff. It owns no
// transport itself; the Dialer hands it a fresh Transport per connection.
type Runner struct {
	dialer  Dialer
	caps    Capabilities
	clock   Clock
	backoff Backoff
	logger  *slog.Logger
	handler CommandHandler
	// randFloat yields a value in [0,1) for backoff jitter; injectable for
	// deterministic tests.
	randFloat func() float64

	// sem bounds concurrent long-running lane work worker-wide (not per-stream) so that maxConcurrentLanes is
	// enforced even when in-flight goroutines from a dropped stream outlive the dispatcher that started them. Quick
	// commands bypass it.
	sem chan struct{}

	// dispatcher holds the per-server command lanes for the active stream. It is
	// (re)created on every serve and torn down with the stream; nil between
	// connections. Stored on the Runner so tests can observe lane lifecycle.
	mu         sync.Mutex
	dispatcher *dispatcher
}

// Option configures a Runner.
type Option func(*Runner)

// WithBackoff overrides the reconnect backoff policy.
func WithBackoff(b Backoff) Option { return func(r *Runner) { r.backoff = b } }

// WithRandFloat overrides the jitter source (tests inject a deterministic one).
func WithRandFloat(f func() float64) Option { return func(r *Runner) { r.randFloat = f } }

// WithCommandHandler wires the command handler (the instance manager) that
// executes lifecycle/console commands and emits status events. Without it the
// Runner answers every command with an "unsupported" CommandResult.
func WithCommandHandler(h CommandHandler) Option { return func(r *Runner) { r.handler = h } }

// NewRunner builds a Runner. dialer, clock, and logger are required Ports.
func NewRunner(dialer Dialer, caps Capabilities, clock Clock, logger *slog.Logger, opts ...Option) *Runner {
	r := &Runner{
		dialer:    dialer,
		caps:      caps,
		clock:     clock,
		backoff:   DefaultBackoff,
		logger:    logger,
		randFloat: rand.Float64,
		sem:       make(chan struct{}, maxConcurrentLanes),
	}
	for _, opt := range opts {
		opt(r)
	}
	return r
}

// Run reconnects transient failures with backoff until cancellation or ErrTerminal.
// Each reconnect registers afresh; cancellation returns nil.
func (r *Runner) Run(ctx context.Context) error {
	attempt := 0
	for {
		registered, err := r.runOnce(ctx)
		switch {
		case err == nil, errors.Is(err, context.Canceled):
			if ctx.Err() != nil {
				return nil
			}
		case errors.Is(err, ErrTerminal):
			r.logger.Error("terminal connection error; not reconnecting", "error", err)
			return err
		default:
			r.logger.Warn("session ended; will reconnect", "error", err)
		}

		if ctx.Err() != nil {
			return nil
		}

		// A connection that got far enough to register cleanly resets the
		// backoff: the next drop starts the sequence over rather than inheriting
		// the growth from earlier failures.
		if registered {
			attempt = 0
		}

		delay := r.backoff.Delay(attempt, r.randFloat())
		attempt++
		r.logger.Info("reconnecting after backoff", "attempt", attempt, "delay", delay)
		select {
		case <-ctx.Done():
			return nil
		case <-r.clock.After(delay):
		}
	}
}

// runOnce dials one stream, registers, and serves it until the stream ends or
// ctx is cancelled. The bool reports whether registration was accepted, so the
// caller can reset its backoff after a healthy connection drops.
func (r *Runner) runOnce(ctx context.Context) (registered bool, err error) {
	transport, err := r.dialer.Dial(ctx)
	if err != nil {
		return false, fmt.Errorf("dial: %w", err)
	}
	defer func() {
		if cerr := transport.Close(); cerr != nil {
			r.logger.Debug("transport close error", "error", cerr)
		}
	}()

	// Refresh the held-server inventory so each (re-)registration advertises current generations, not the stale
	// boot-time snapshot.
	if provider, ok := r.handler.(HeldServerProvider); ok {
		r.caps.HeldServers = provider.HeldServers()
	}

	if err := transport.SendRegister(ctx, r.caps); err != nil {
		return false, fmt.Errorf("send register: %w", err)
	}

	ack, err := transport.RecvRegisterAck(ctx)
	if err != nil {
		return false, fmt.Errorf("recv register ack: %w", err)
	}

	interval := ack.HeartbeatInterval
	if interval <= 0 {
		return true, errors.New("session: API ack gave a non-positive heartbeat interval")
	}
	r.logger.Info("registered with API",
		"worker_id", r.caps.WorkerID,
		"heartbeat_interval", interval,
		"transfer_deadline", ack.TransferDeadline,
	)

	// Hand the ack's data-plane transfer bound to the handler so it can apply a per-transfer deadline. The handler
	// derives the bound from this one source (the API's budget + margin); a non-positive value (an older API)
	// leaves transfers unbounded, the prior behavior.
	if setter, ok := r.handler.(TransferDeadlineSetter); ok {
		setter.SetTransferDeadline(ack.TransferDeadline)
	}

	// Reclaim scratch dirs for held servers the API reports as deleted. Defense in depth: intersect the ack's list
	// with the held set this Register actually advertised, so a malformed ack cannot point the Worker at a server
	// it never claimed to hold.
	if reclaimer, ok := r.handler.(ScratchReclaimer); ok && len(ack.UnknownHeldServerIDs) > 0 {
		held := make(map[string]bool, len(r.caps.HeldServers))
		for _, hs := range r.caps.HeldServers {
			held[hs.ServerID] = true
		}
		var toReclaim []string
		for _, id := range ack.UnknownHeldServerIDs {
			if held[id] {
				toReclaim = append(toReclaim, id)
			}
		}
		if len(toReclaim) > 0 {
			reclaimer.ReclaimDeletedScratches(toReclaim)
		}
	}

	// Re-emit held states after registration; the pending-status buffer retains them until serve drains events.
	if resyncer, ok := r.handler.(StatusResyncer); ok {
		resyncer.ResyncStatus()
	}

	return true, r.serve(ctx, transport, interval)
}

// maxConcurrentLanes caps slow command execution across servers; routing and quick commands bypass it.
const maxConcurrentLanes = 4

// serve serializes transport sends; receive and command execution run separately so heartbeats can proceed.
func (r *Runner) serve(ctx context.Context, transport Transport, interval time.Duration) error {
	serveCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	results := make(chan CommandResult, maxConcurrentLanes+1)
	disp := newDispatcher(serveCtx, r, results)
	r.mu.Lock()
	r.dispatcher = disp
	r.mu.Unlock()
	defer func() {
		r.mu.Lock()
		r.dispatcher = nil
		r.mu.Unlock()
	}()

	recvErr := make(chan error, 1)
	go func() {
		recvErr <- r.receiveLoop(serveCtx, transport, disp)
	}()

	var events <-chan StatusEvent
	var logs <-chan LogEvent
	var metrics <-chan MetricsEvent
	if r.handler != nil {
		events = r.handler.Events()
		logs = r.handler.Logs()
		metrics = r.handler.Metrics()
	}

	// Reset the heartbeat timer only after sending a beat so other event traffic cannot postpone it.
	heartbeat := r.clock.NewTimer(interval)
	defer heartbeat.Stop()

	for {
		select {
		case <-serveCtx.Done():
			return serveCtx.Err()
		case err := <-recvErr:
			return err
		case result := <-results:
			if err := transport.SendCommandResult(serveCtx, result); err != nil {
				return fmt.Errorf("send command result: %w", err)
			}
		case event := <-events:
			if err := transport.SendStatusChange(serveCtx, event); err != nil {
				return fmt.Errorf("send status change: %w", err)
			}
		case logEvent := <-logs:
			if err := transport.SendLogLine(serveCtx, logEvent); err != nil {
				return fmt.Errorf("send log line: %w", err)
			}
		case metricsEvent := <-metrics:
			if err := transport.SendMetrics(serveCtx, metricsEvent); err != nil {
				return fmt.Errorf("send metrics: %w", err)
			}
		case <-heartbeat.C():
			if err := transport.SendHeartbeat(serveCtx); err != nil {
				return fmt.Errorf("send heartbeat: %w", err)
			}
			heartbeat.Reset(interval)
		}
	}
}

// receiveLoop queues server-scoped commands in FIFO lanes and rejects malformed commands inline.
// All results go through serve's single sender.
func (r *Runner) receiveLoop(ctx context.Context, transport Transport, disp *dispatcher) error {
	for {
		cmd, err := transport.RecvCommand(ctx)
		if err != nil {
			return err
		}

		if cmd.ServerID == "" {
			r.emitResult(ctx, disp.results, r.handle(ctx, cmd))
			continue
		}

		disp.dispatch(cmd)
	}
}

// emitResult hands a result to the single serialized sender, abandoning it only
// if the session is tearing down (the stream will be discarded anyway).
func (r *Runner) emitResult(ctx context.Context, results chan<- CommandResult, result CommandResult) {
	select {
	case results <- result:
	case <-ctx.Done():
	}
}

// Log every failed handler result with server ID and command kind, which the result itself does not carry.
func (r *Runner) handle(ctx context.Context, cmd Command) CommandResult {
	result := r.handleCommand(ctx, cmd)
	if !result.Success {
		r.logger.Warn("command failed",
			"command_id", cmd.CommandID,
			"server_id", cmd.ServerID,
			"kind", cmd.Kind,
			"error_code", result.ErrorCode,
			"error_message", result.ErrorMessage,
		)
	}
	return result
}

// Reject handled commands with no ServerID before dispatch; unhandled kinds return unsupported.
func (r *Runner) handleCommand(ctx context.Context, cmd Command) CommandResult {
	if r.handler != nil && IsHandledKind(cmd.Kind) {
		if cmd.ServerID == "" {
			return CommandResult{
				CommandID:    cmd.CommandID,
				Success:      false,
				ErrorCode:    CommandErrorServerNotFound,
				ErrorMessage: fmt.Sprintf("session: server-scoped command %q arrived with an empty server id", cmd.Kind),
			}
		}
		r.logger.Info("dispatching command",
			"command_id", cmd.CommandID, "server_id", cmd.ServerID, "kind", cmd.Kind)
		return r.handler.Handle(ctx, cmd)
	}

	r.logger.Info("received unsupported command; replying with error",
		"command_id", cmd.CommandID, "server_id", cmd.ServerID, "kind", cmd.Kind)
	return CommandResult{
		CommandID:    cmd.CommandID,
		Success:      false,
		ErrorCode:    CommandErrorInternal,
		ErrorMessage: fmt.Sprintf("command %q not supported by this Worker yet", cmd.Kind),
	}
}

// IsHandledKind must match Manager.Handle; omitted kinds never reach the handler.
// All listed kinds are server-scoped and subject to the empty-ServerID guard.
func IsHandledKind(kind string) bool {
	switch kind {
	case "StartServer", "StopServer", "RestartServer", "ServerCommand",
		"HydrateTrigger", "SnapshotTrigger", "ReadFile", "EditFile", "ListFiles",
		"TunnelDial", "OpenBedrockTunnel", "CloseBedrockTunnel":
		return true
	default:
		return false
	}
}

// dispatcher serializes each server's queue; enqueue and idle teardown share mu to avoid losing commands.
type dispatcher struct {
	r       *Runner
	ctx     context.Context
	results chan<- CommandResult

	mu    sync.Mutex
	lanes map[string]*lane
}

type lane struct {
	queue []Command
}

func newDispatcher(ctx context.Context, r *Runner, results chan<- CommandResult) *dispatcher {
	return &dispatcher{
		r:       r,
		ctx:     ctx,
		results: results,
		lanes:   make(map[string]*lane),
	}
}

// dispatch never waits for command execution or a global concurrency slot.
func (d *dispatcher) dispatch(cmd Command) {
	d.mu.Lock()
	l, ok := d.lanes[cmd.ServerID]
	if !ok {
		l = &lane{}
		d.lanes[cmd.ServerID] = l
	}
	l.queue = append(l.queue, cmd)
	d.mu.Unlock()

	if !ok {
		go d.runLane(cmd.ServerID, l)
	}
}

// runLane preserves per-server FIFO; quick commands bypass only the global concurrency cap.
func (d *dispatcher) runLane(serverID string, l *lane) {
	for {
		d.mu.Lock()
		if len(l.queue) == 0 {
			delete(d.lanes, serverID)
			d.mu.Unlock()
			return
		}
		cmd := l.queue[0]
		l.queue = l.queue[1:]
		d.mu.Unlock()

		if isQuickCommand(cmd.Kind) {
			d.r.emitResult(d.ctx, d.results, d.r.handle(d.ctx, cmd))
			continue
		}

		// Do not acquire a Worker-wide slot for a lane whose stream has ended.
		if d.ctx.Err() != nil {
			d.removeLane(serverID)
			return
		}

		select {
		case d.r.sem <- struct{}{}:
		case <-d.ctx.Done():
			d.removeLane(serverID)
			return
		}
		d.r.emitResult(d.ctx, d.results, d.r.handle(d.ctx, cmd))
		<-d.r.sem
	}
}

// Console and tunnel commands bypass the global cap so joins cannot queue behind another server's transfer.
// Per-server FIFO still applies; long-lived tunnel work runs outside the lane.
func isQuickCommand(kind string) bool {
	switch kind {
	case "ServerCommand", "TunnelDial", "OpenBedrockTunnel", "CloseBedrockTunnel":
		return true
	default:
		return false
	}
}

func (d *dispatcher) removeLane(serverID string) {
	d.mu.Lock()
	delete(d.lanes, serverID)
	d.mu.Unlock()
}

func (r *Runner) laneCount() int {
	r.mu.Lock()
	disp := r.dispatcher
	r.mu.Unlock()
	if disp == nil {
		return 0
	}
	disp.mu.Lock()
	defer disp.mu.Unlock()
	return len(disp.lanes)
}
