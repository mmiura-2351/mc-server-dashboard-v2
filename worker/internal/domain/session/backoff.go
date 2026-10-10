// Package session manages registration, heartbeats, reconnects, and command dispatch.
// Transport, time, and randomness are injected so the domain uses only the standard library.
package session

import "time"

// Backoff computes capped exponential delays with full jitter to spread reconnect attempts.
// The caller owns the attempt counter.
type Backoff struct {
	// Initial is the base delay for the first reconnect attempt.
	Initial time.Duration
	// Max caps the exponential growth.
	Max time.Duration
	// Multiplier grows the base delay each attempt (typically 2).
	Multiplier float64
}

// DefaultBackoff is the reconnect policy used unless overridden: start at 1s,
// double each attempt, cap at 30s.
var DefaultBackoff = Backoff{
	Initial:    1 * time.Second,
	Max:        30 * time.Second,
	Multiplier: 2.0,
}

// base returns the un-jittered exponential delay for a zero-based attempt
// number, capped at Max. attempt 0 yields Initial.
func (b Backoff) base(attempt int) time.Duration {
	d := float64(b.Initial)
	for i := 0; i < attempt; i++ {
		d *= b.Multiplier
		if d >= float64(b.Max) {
			return b.Max
		}
	}
	if d >= float64(b.Max) {
		return b.Max
	}
	return time.Duration(d)
}

// Delay returns the jittered delay for a zero-based attempt number. randFloat
// must return a value in [0,1); the result is uniformly drawn from
// [0, base(attempt)] ("full jitter"), so it never exceeds the capped base.
func (b Backoff) Delay(attempt int, randFloat float64) time.Duration {
	maxDelay := b.base(attempt)
	return time.Duration(randFloat * float64(maxDelay))
}
