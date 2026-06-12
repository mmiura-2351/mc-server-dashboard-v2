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

// Service wraps the API client with the learned base_domain and the Register
// loop. ResolveJoin and BaseDomain satisfy game.Resolver.
type Service struct {
	client         registrar
	sessions       activeSessionSource
	tunnelEndpoint string
	tunnelCAPEM    string
	logger         *slog.Logger

	baseDomain atomic.Pointer[string]
}

// New builds the service. tunnelEndpoint and tunnelCAPEM are advertised on every
// Register.
func New(client registrar, sessions activeSessionSource, tunnelEndpoint, tunnelCAPEM string, logger *slog.Logger) *Service {
	return &Service{
		client:         client,
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
// success. Exposed for tests; Run drives it with backoff.
func (s *Service) RegisterOnce(ctx context.Context) error {
	base, err := s.client.Register(ctx, s.tunnelEndpoint, s.tunnelCAPEM, s.sessions.ActiveSessionIDs())
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
func (s *Service) Run(ctx context.Context) {
	attempt := 0
	for {
		if err := s.RegisterOnce(ctx); err != nil {
			s.logger.Warn("relay register failed; retrying", "attempt", attempt, "error", err)
			if !sleepCtx(ctx, backoffDelay(attempt)) {
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
