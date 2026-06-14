// Package relaysvc coordinates the relay's API registration: it runs the
// Register-with-backoff loop, holds the learned base_domain, and exposes the
// ResolveJoin surface the game listener consumes (docs/app/RELAY.md Section 6).
// It is the small stateful glue between the stateless API client and the
// listeners.
package relaysvc

import (
	"context"
	"log/slog"
	"math/rand/v2"
	"sync/atomic"
	"time"

	"google.golang.org/grpc/connectivity"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
)

// Backoff bounds the Register retry cadence: exponential growth with full
// jitter, mirroring the Worker's reconnect policy (worker DefaultBackoff).
var (
	backoffInitial = 1 * time.Second
	backoffMax     = 30 * time.Second
)

// reRegisterInterval is how often Run re-registers after a successful Register.
// Register is idempotent and cheap; re-running it heals an API restart (the API
// re-learns the tunnel endpoint/CA and re-runs orphan healing against the
// relay's active session set) without restarting the relay. RELAY.md Section 6.
var reRegisterInterval = 60 * time.Second

// registerTimeout bounds each Register RPC so a black-holed API connection
// cannot stall the re-register loop indefinitely (issue #971; matches the
// resolveJoinTimeout posture on the game path). Shutdown still unblocks via ctx.
var registerTimeout = 10 * time.Second

// registrar is the subset of the API client relaysvc needs. Narrowed for tests.
type registrar interface {
	Register(ctx context.Context, tunnelEndpoint, tunnelCAPEM string, activeSessionIDs []string) (string, error)
	ResolveJoin(ctx context.Context, slug, playerIP string, intent apiclient.Intent) (apiclient.ResolveResult, error)
}

// activeSessionSource yields the still-open session ids for re-registration
// (the session reporter implements it).
type activeSessionSource interface {
	ActiveSessionIDs() []string
}

// apiConn is the subset of the gRPC client connection Run watches to re-register
// promptly when the API connection recovers (issue #987). *grpc.ClientConn
// satisfies it. Narrowed for tests.
type apiConn interface {
	GetState() connectivity.State
	WaitForStateChange(ctx context.Context, sourceState connectivity.State) bool
	Connect()
}

// Service wraps the API client with the learned base_domain and the Register
// loop. ResolveJoin and BaseDomain satisfy game.Resolver.
type Service struct {
	client         registrar
	conn           apiConn
	sessions       activeSessionSource
	tunnelEndpoint string
	tunnelCAPEM    string
	logger         *slog.Logger

	baseDomain atomic.Pointer[string]
}

// New builds the service. tunnelEndpoint and tunnelCAPEM are advertised on every
// Register. conn is the API gRPC connection Run watches to re-register promptly
// when the API connection recovers (issue #987); it may be nil, in which case
// Run relies on the periodic backstop alone.
func New(client registrar, conn apiConn, sessions activeSessionSource, tunnelEndpoint, tunnelCAPEM string, logger *slog.Logger) *Service {
	return &Service{
		client:         client,
		conn:           conn,
		sessions:       sessions,
		tunnelEndpoint: tunnelEndpoint,
		tunnelCAPEM:    tunnelCAPEM,
		logger:         logger,
	}
}

// BaseDomain returns the base_domain learned from the last successful Register,
// or "" before the first one (the game listener treats "" as "match nothing",
// so connections are dropped until registration succeeds).
func (s *Service) BaseDomain() string {
	if p := s.baseDomain.Load(); p != nil {
		return *p
	}
	return ""
}

// ResolveJoin proxies to the API client.
func (s *Service) ResolveJoin(ctx context.Context, slug, playerIP string, intent apiclient.Intent) (apiclient.ResolveResult, error) {
	return s.client.ResolveJoin(ctx, slug, playerIP, intent)
}

// RegisterOnce attempts a single Register, storing the learned base_domain on
// success. The call is bounded by registerTimeout so a black-holed API
// connection cannot stall the loop (issue #971). Exposed for tests; Run drives
// it with backoff.
func (s *Service) RegisterOnce(ctx context.Context) error {
	rctx, cancel := context.WithTimeout(ctx, registerTimeout)
	defer cancel()
	base, err := s.client.Register(rctx, s.tunnelEndpoint, s.tunnelCAPEM, s.sessions.ActiveSessionIDs())
	if err != nil {
		return err
	}
	s.baseDomain.Store(&base)
	return nil
}

// Run registers at startup and re-registers on failure with backoff until ctx
// is cancelled. A successful registration learns base_domain; a later failure
// keeps serving against the last known base_domain while retrying (RELAY.md
// Sections 6 and 10). After a success it re-registers every reReg interval so
// an API restart heals (the API re-learns the endpoint/CA and re-runs orphan
// healing against the relay's active session set) without a relay restart.
//
// On a Register failure (e.g. the API restarted), the backoff wait is cut short
// the moment the gRPC connection transitions back to Ready, so the relay
// re-registers within seconds of the API coming back rather than waiting up to
// a full periodic interval (issue #987). The periodic re-register remains the
// backstop when no connection is available to watch.
func (s *Service) Run(ctx context.Context) {
	attempt := 0
	for {
		if err := s.RegisterOnce(ctx); err != nil {
			s.logger.Warn("relay register failed; retrying", "attempt", attempt, "error", err)
			if !s.waitRetry(ctx, backoffDelay(attempt)) {
				return
			}
			attempt++
			continue
		}
		s.logger.Info("relay registered with API", "base_domain", s.BaseDomain())
		attempt = 0
		// Re-register periodically so an API restart re-learns the relay's tunnel
		// endpoint/CA and re-delivers the active session ids for orphan healing.
		if !sleepCtx(ctx, reRegisterInterval) {
			return
		}
	}
}

// waitRetry waits before the next Register attempt. The Register that just
// failed left the connection either non-Ready (the API is unreachable — e.g. it
// restarted) or Ready (a non-connection failure, e.g. the API rejected the
// call). In the non-Ready case it returns the instant the connection recovers
// to Ready so the relay re-registers within seconds of the API coming back
// (issue #987); otherwise it waits out the full backoff to avoid hammering.
// Returns false if ctx is cancelled (shutdown). With no connection to watch it
// degrades to a plain backoff sleep.
func (s *Service) waitRetry(ctx context.Context, backoff time.Duration) bool {
	if s.conn == nil {
		return sleepCtx(ctx, backoff)
	}

	// Nudge a lazy/Idle connection to start reconnecting so WaitForStateChange
	// can observe it reach Ready.
	s.conn.Connect()
	if s.conn.GetState() == connectivity.Ready {
		// Already connected: the failure was not a dropped connection, so just
		// back off before retrying.
		return sleepCtx(ctx, backoff)
	}

	// Connection is down: wait up to backoff for it to recover, returning early
	// the moment it reaches Ready.
	wctx, cancel := context.WithTimeout(ctx, backoff)
	defer cancel()
	for {
		state := s.conn.GetState()
		if state == connectivity.Ready {
			return true
		}
		if !s.conn.WaitForStateChange(wctx, state) {
			// Backoff elapsed (wctx) or shutdown (ctx). Distinguish so shutdown
			// stops the loop while a timeout proceeds to the next attempt.
			return ctx.Err() == nil
		}
	}
}

// backoffDelay returns the jittered exponential delay for a zero-based attempt.
func backoffDelay(attempt int) time.Duration {
	d := float64(backoffInitial)
	for i := 0; i < attempt; i++ {
		d *= 2
		if d >= float64(backoffMax) {
			d = float64(backoffMax)
			break
		}
	}
	if d > float64(backoffMax) {
		d = float64(backoffMax)
	}
	return time.Duration(rand.Float64() * d)
}

// sleepCtx sleeps for d or until ctx is cancelled, returning false if cancelled.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}
