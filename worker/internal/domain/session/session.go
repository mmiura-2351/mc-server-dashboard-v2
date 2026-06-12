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

// ErrRejected is returned when the API refuses registration
// (RegisterAck.accepted=false, CONTROL_PLANE.md Section 4.1). It is terminal:
// the run loop does not reconnect, because re-dialing with the same rejected
// credential would loop forever.
var ErrRejected = errors.New("session: registration rejected by API")

// ErrTerminal marks a connection failure the run loop must not retry: the same
// dial would fail the same way (e.g. the API aborted the stream for a bad/
// missing credential or a protocol violation). The transport adapter wraps such
// errors with this sentinel; transient failures are returned unwrapped so the
// loop reconnects with backoff. The domain stays transport-neutral — the adapter
// decides which wire failures are terminal (CONTROL_PLANE.md Section 4.1).
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
	}
	for _, opt := range opts {
		opt(r)
	}
	return r
}

// Run connects and maintains the session until ctx is cancelled (clean
// shutdown) or a terminal connection error occurs. On a transient transport
// error it reconnects with backoff, re-registering from scratch each time
// (CONTROL_PLANE.md Section 4.4). It returns nil on a cancellation-driven
// shutdown and the terminal error (ErrRejected or ErrTerminal) when the API
// refused registration or aborted the stream for a non-retryable reason.
func (r *Runner) Run(ctx context.Context) error {
	attempt := 0
	for {
		registered, err := r.runOnce(ctx)
		switch {
		case err == nil, errors.Is(err, context.Canceled):
			if ctx.Err() != nil {
				return nil
			}
		case errors.Is(err, ErrRejected), errors.Is(err, ErrTerminal):
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

	if err := transport.SendRegister(ctx, r.caps); err != nil {
		return false, fmt.Errorf("send register: %w", err)
	}

	ack, err := transport.RecvRegisterAck(ctx)
	if err != nil {
		return false, fmt.Errorf("recv register ack: %w", err)
	}
	if !ack.Accepted {
		return false, fmt.Errorf("%w: %s", ErrRejected, ack.RejectionReason)
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

	// Hand the ack's data-plane transfer bound to the handler so it can apply a
	// per-transfer deadline (issue #874). The handler derives the bound from this
	// one source (the API's budget + margin); a non-positive value (an older API)
	// leaves transfers unbounded, the prior behavior.
	if setter, ok := r.handler.(TransferDeadlineSetter); ok {
		setter.SetTransferDeadline(ack.TransferDeadline)
	}

	return true, r.serve(ctx, transport, interval)
}

// maxConcurrentLanes bounds how many per-server command lanes execute commands
// at once off the receive loop (issue #95). A small cap keeps a burst of distinct
// servers from spawning unbounded goroutines while still letting independent
// servers' commands — including a slow graceful Stop — proceed in parallel. The
// cap limits concurrent execution, not routing: the receive loop never blocks on
// it, so a full pool delays only the start of additional servers' work, never the
// dispatch of further commands. Quick commands (ServerCommand) bypass the cap
// entirely so an instant op is never delayed by long-running lane work on other
// servers (issue #169); see runLane and isQuickCommand.
const maxConcurrentLanes = 4

// serve runs the steady state: a heartbeat ticker, an inbound-command receive
// loop, and a single serialized transport-send path, until the stream errors or
// ctx is cancelled. The receive loop runs in a goroutine so a blocking
// RecvCommand never starves the heartbeat; command results (from the per-server
// lanes and the inline path) flow back through the results channel so all
// transport Sends happen on this one goroutine (a gRPC stream is not safe for
// concurrent Send).
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

	// The heartbeat deadline is a persistent timer armed once and reset only after
	// a beat is sent. Sending other message types does not touch it, so the
	// cadence stays independent of event traffic — a never-idle select no longer
	// starves the heartbeat (issue #341). The old code re-armed clock.After on
	// every iteration, so a steady stream of inbound events kept resetting the
	// deadline and the heartbeat case could never win.
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

// receiveLoop reads inbound commands and routes each, never blocking on a
// slow command for one server. A command targeting a server is handed to that
// server's lane, which executes the server's commands serially (so start/stop for
// one server never interleave) while different servers' lanes run concurrently —
// a slow graceful Stop on one server no longer delays commands for another (issue
// #95). A command with no server id (or an unset/unknown oneof) has no lane to
// order against, so it is handled inline; an unhandled kind gets an "unsupported"
// result. A command is never silently dropped (CONTROL_PLANE.md Section 5). Every
// result is pushed to results, drained by the single sender in serve.
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

// handle dispatches a command to the handler when it is a handled kind and a
// handler is wired; otherwise it returns the "unsupported" result. Every result
// flows back through here, so a single failure-logging site (issue #194) covers
// every handler: a failed result is logged at WARN with the command context the
// CommandResult itself does not carry (server_id, kind).
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

// handleCommand produces the CommandResult for a command, dispatching to the
// handler for a handled kind and answering "unsupported" otherwise.
func (r *Runner) handleCommand(ctx context.Context, cmd Command) CommandResult {
	if r.handler != nil && IsHandledKind(cmd.Kind) {
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

// IsHandledKind reports whether a command kind is dispatched to the handler.
// It must stay in lockstep with the handler's own switch (the instance
// manager's Manager.Handle): a kind the handler accepts but this filter omits
// is answered with the canned "unsupported" result and never reaches the
// handler (issue #219). An instancemanager test guards that contract.
func IsHandledKind(kind string) bool {
	switch kind {
	case "StartServer", "StopServer", "RestartServer", "ServerCommand",
		"HydrateTrigger", "SnapshotTrigger", "ReadFile", "EditFile", "ListFiles",
		"TunnelDial":
		return true
	default:
		return false
	}
}

// dispatcher routes commands to per-server lanes (issue #95). Each lane runs one
// server's commands serially on its own goroutine; lanes run concurrently up to
// sem's capacity. A lane is created on first use and removed once its queue
// drains, so an ever-growing roster of servers leaks no goroutines. All state is
// guarded by mu; the lane queue lives inline on the lane so enqueue and the
// drain-and-exit decision are made under the same lock, closing the race where a
// command arrives just as a lane decides to exit.
type dispatcher struct {
	r       *Runner
	ctx     context.Context
	results chan<- CommandResult
	sem     chan struct{}

	mu    sync.Mutex
	lanes map[string]*lane
}

// lane is one server's serial command queue.
type lane struct {
	queue []Command
}

func newDispatcher(ctx context.Context, r *Runner, results chan<- CommandResult) *dispatcher {
	return &dispatcher{
		r:       r,
		ctx:     ctx,
		results: results,
		sem:     make(chan struct{}, maxConcurrentLanes),
		lanes:   make(map[string]*lane),
	}
}

// dispatch queues cmd on its server's lane, starting the lane's worker if it is
// not already running. It never blocks on command execution or the concurrency
// cap, so a slow command for one server cannot delay routing for another.
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

// runLane drains a server's queue serially until it is empty, then removes the
// lane and exits. The lane preserves per-server FIFO: one goroutine runs the
// server's commands in arrival order, so a command never races ahead of an
// earlier same-server op. The global concurrency cap (sem) is acquired
// per-command and only for long-running ops; quick commands bypass it (issue
// #169), so an instant ServerCommand for an otherwise-idle server is not delayed
// by other servers' slow hydrate/stop work holding every cap slot. The bypass
// touches only the global cap — the same-server ordering above is unaffected.
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

		select {
		case d.sem <- struct{}{}:
		case <-d.ctx.Done():
			d.removeLane(serverID)
			return
		}
		d.r.emitResult(d.ctx, d.results, d.r.handle(d.ctx, cmd))
		<-d.sem
	}
}

// isQuickCommand reports whether a command kind is an instant op that bypasses
// the global concurrency cap. ServerCommand qualifies: it sends one console/RCON
// line to an already-running server and returns at once. TunnelDial qualifies too
// (RELAY.md Section 5): it dials the relay, completes the token handshake, and
// returns once the splice is established — the long-lived splice runs on its own
// goroutines off the lane, so the command itself is instant and a join must not
// queue behind a hydrate (issue #958). Every other server-scoped kind
// (StartServer/StopServer/RestartServer, HydrateTrigger/SnapshotTrigger, and the
// file ops) can run long and stays bounded by the cap (issue #169). The bypass
// keeps per-server FIFO: it changes only whether a lane acquires a cap slot for a
// command, never the order in which a server's commands run.
func isQuickCommand(kind string) bool {
	return kind == "ServerCommand" || kind == "TunnelDial"
}

// removeLane drops a lane that never started draining (ctx already cancelled).
func (d *dispatcher) removeLane(serverID string) {
	d.mu.Lock()
	delete(d.lanes, serverID)
	d.mu.Unlock()
}

// laneCount reports the number of live lanes on the active stream's dispatcher,
// for tests asserting idle teardown. It is zero when no stream is being served.
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
