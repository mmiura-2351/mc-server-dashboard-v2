package session

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math/rand/v2"
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
	)

	return true, r.serve(ctx, transport, interval)
}

// maxConcurrentTransfers bounds how many long-running hydrate/snapshot handlers
// run at once off the receive loop (issue #95). A small cap keeps a burst of
// transfers from spawning unbounded goroutines while still letting independent
// servers' transfers proceed in parallel.
const maxConcurrentTransfers = 4

// serve runs the steady state: a heartbeat ticker, an inbound-command receive
// loop, and a single serialized transport-send path, until the stream errors or
// ctx is cancelled. The receive loop runs in a goroutine so a blocking
// RecvCommand never starves the heartbeat; command results (from both inline and
// off-loop handlers) flow back through the results channel so all transport
// Sends happen on this one goroutine (a gRPC stream is not safe for concurrent
// Send).
func (r *Runner) serve(ctx context.Context, transport Transport, interval time.Duration) error {
	serveCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	results := make(chan CommandResult, maxConcurrentTransfers+1)
	recvErr := make(chan error, 1)
	go func() {
		recvErr <- r.receiveLoop(serveCtx, transport, results)
	}()

	var events <-chan StatusEvent
	var logs <-chan LogEvent
	var metrics <-chan MetricsEvent
	if r.handler != nil {
		events = r.handler.Events()
		logs = r.handler.Logs()
		metrics = r.handler.Metrics()
	}

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
		case <-r.clock.After(interval):
			if err := transport.SendHeartbeat(serveCtx); err != nil {
				return fmt.Errorf("send heartbeat: %w", err)
			}
		}
	}
}

// receiveLoop reads inbound commands and answers each, never blocking on a
// long-running transfer. Lifecycle/console commands are fast and handled inline;
// the long-running data-plane triggers (Hydrate/Snapshot) are dispatched off the
// loop under a bounded semaphore so a slow transfer for one server does not delay
// commands for another (issue #95). File commands (ReadFile/EditFile) are small
// and stay inline on the loop, like ServerCommand. An unset/unknown command oneof
// gets an "unsupported" result. A command is never silently dropped
// (CONTROL_PLANE.md Section 5). Every result is pushed to results, drained by the
// single sender in serve.
func (r *Runner) receiveLoop(ctx context.Context, transport Transport, results chan<- CommandResult) error {
	sem := make(chan struct{}, maxConcurrentTransfers)
	for {
		cmd, err := transport.RecvCommand(ctx)
		if err != nil {
			return err
		}

		if isLongRunningKind(cmd.Kind) {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case sem <- struct{}{}:
			}
			go func(cmd Command) {
				defer func() { <-sem }()
				r.emitResult(ctx, results, r.handle(ctx, cmd))
			}(cmd)
			continue
		}

		r.emitResult(ctx, results, r.handle(ctx, cmd))
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
// handler is wired; otherwise it returns the "unsupported" result.
func (r *Runner) handle(ctx context.Context, cmd Command) CommandResult {
	if r.handler != nil && isHandledKind(cmd.Kind) {
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

// isHandledKind reports whether a command kind is dispatched to the handler.
func isHandledKind(kind string) bool {
	switch kind {
	case "StartServer", "StopServer", "RestartServer", "ServerCommand",
		"HydrateTrigger", "SnapshotTrigger", "ReadFile", "EditFile":
		return true
	default:
		return false
	}
}

// isLongRunningKind reports whether a command should run off the serial receive
// loop (the bulk data-plane transfers, issue #95).
func isLongRunningKind(kind string) bool {
	switch kind {
	case "HydrateTrigger", "SnapshotTrigger":
		return true
	default:
		return false
	}
}
