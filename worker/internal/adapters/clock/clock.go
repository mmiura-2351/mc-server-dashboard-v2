// Package clock provides the production Clock adapter backed by the wall clock.
// It implements the session.Clock Port (internal/domain/session); tests inject
// a fake clock instead.
package clock

import "time"

// System is the wall-clock implementation of session.Clock.
type System struct{}

// Now reports the current wall-clock time.
func (System) Now() time.Time { return time.Now() }

// After returns a channel that fires after d, delegating to time.After.
func (System) After(d time.Duration) <-chan time.Time { return time.After(d) }
