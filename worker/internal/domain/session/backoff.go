// Package session holds the Worker's control-plane session logic as a pure
// state machine: the registration handshake, the heartbeat cadence, reconnect
// with backoff, and the protocol-shape acknowledgement of inbound commands.
//
// It depends on the standard library only (docs/app/ARCHITECTURE.md Section 2):
// the gRPC transport, the wall clock, and randomness are injected as Ports
// (Transport, Clock, rand source) so the logic is exercised with fakes. The
// adapters layer translates this package's decisions to and from the generated
// control-plane messages.
package session

import "time"

// Backoff computes the reconnect delay sequence: exponential growth from an
// initial delay, capped at a maximum, with full jitter to avoid a reconnect
// thundering herd (CONTROL_PLANE.md Section 4.4 makes the Worker responsible for
// reconnecting). It is a value, safe to copy; Attempt is the only mutable state
// the caller advances.
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
