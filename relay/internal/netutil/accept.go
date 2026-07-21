package netutil

import (
	"context"
	"errors"
	"syscall"
	"time"
)

// IsTransientAcceptError reports whether err is a transient OS-level error
// that Accept can recover from once resources are freed (fd pressure, memory
// pressure). It does NOT use the deprecated net.Error.Temporary() (SA1019).
func IsTransientAcceptError(err error) bool {
	return errors.Is(err, syscall.EMFILE) ||
		errors.Is(err, syscall.ENFILE) ||
		errors.Is(err, syscall.ENOBUFS) ||
		errors.Is(err, syscall.ENOMEM)
}

// AcceptBackoff implements exponential backoff for transient Accept errors,
// mirroring net/http.Server.Serve: 5ms initial, doubling, capped at 1s.
type AcceptBackoff struct {
	cur time.Duration
}

// Next returns the next backoff duration and advances the internal state.
func (b *AcceptBackoff) Next() time.Duration {
	if b.cur == 0 {
		b.cur = 5 * time.Millisecond
	} else {
		b.cur *= 2
		if b.cur > time.Second {
			b.cur = time.Second
		}
	}
	return b.cur
}

// Reset clears the backoff state so the next call to Next starts at 5ms.
func (b *AcceptBackoff) Reset() {
	b.cur = 0
}

// Sleep blocks for the next backoff duration or until ctx is cancelled.
// It returns true if the sleep completed, false if ctx was cancelled.
func (b *AcceptBackoff) Sleep(ctx context.Context) bool {
	timer := time.NewTimer(b.Next())
	defer timer.Stop()
	select {
	case <-timer.C:
		return true
	case <-ctx.Done():
		return false
	}
}
