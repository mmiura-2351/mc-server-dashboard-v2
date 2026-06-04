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

// serve runs the steady state: a heartbeat ticker and an inbound-command
// receive loop, until the stream errors or ctx is cancelled. The command
// receive runs in a goroutine so a blocking RecvCommand never starves the
// heartbeat.
func (r *Runner) serve(ctx context.Context, transport Transport, interval time.Duration) error {
	serveCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	recvErr := make(chan error, 1)
	go func() {
		recvErr <- r.receiveLoop(serveCtx, transport)
	}()

	for {
		select {
		case <-serveCtx.Done():
			return serveCtx.Err()
		case err := <-recvErr:
			return err
		case <-r.clock.After(interval):
			if err := transport.SendHeartbeat(serveCtx); err != nil {
				return fmt.Errorf("send heartbeat: %w", err)
			}
		}
	}
}

// receiveLoop reads inbound commands and answers each with an "unsupported"
// CommandResult (epic #7 implements real handling; this milestone only
// acknowledges the protocol shape, never silently dropping a command —
// CONTROL_PLANE.md Section 5).
func (r *Runner) receiveLoop(ctx context.Context, transport Transport) error {
	for {
		cmd, err := transport.RecvCommand(ctx)
		if err != nil {
			return err
		}

		r.logger.Info("received unsupported command; replying with error",
			"command_id", cmd.CommandID,
			"server_id", cmd.ServerID,
			"kind", cmd.Kind,
		)

		result := CommandResult{
			CommandID:    cmd.CommandID,
			Success:      false,
			ErrorCode:    CommandErrorInternal,
			ErrorMessage: fmt.Sprintf("command %q not supported by this Worker yet", cmd.Kind),
		}
		if err := transport.SendCommandResult(ctx, result); err != nil {
			return fmt.Errorf("send command result: %w", err)
		}
	}
}
