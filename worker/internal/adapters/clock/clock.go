// Package clock provides the production Clock adapter backed by the wall clock.
// It implements the session.Clock Port (internal/domain/session); tests inject
// a fake clock instead.
package clock

import (
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// System is the wall-clock implementation of session.Clock.
type System struct{}

// Now reports the current wall-clock time.
func (System) Now() time.Time { return time.Now() }

// After returns a channel that fires after d, delegating to time.After.
func (System) After(d time.Duration) <-chan time.Time { return time.After(d) }

// NewTimer returns a wall-clock timer backed by time.NewTimer.
func (System) NewTimer(d time.Duration) session.Timer { return timer{time.NewTimer(d)} }

// timer adapts *time.Timer to session.Timer.
type timer struct{ t *time.Timer }

func (t timer) C() <-chan time.Time   { return t.t.C }
func (t timer) Reset(d time.Duration) { t.t.Reset(d) }
func (t timer) Stop()                 { t.t.Stop() }
