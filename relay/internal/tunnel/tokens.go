// Package tunnel implements the relay's Worker dial-back listener and the
// token rendezvous between a waiting player connection and the Worker's
// outbound tunnel connection (docs/app/RELAY.md Section 5).
//
// Flow: a player connection that resolved to a TUNNEL decision registers a
// waiter keyed by the single-use token the API minted. The Worker dials the
// tunnel listener (TLS, NAT-safe outbound) and presents that token; the
// listener matches it to the waiter, consuming the token (single-use), and
// hands the Worker connection to the player goroutine, which then splices.
package tunnel

import (
	"context"
	"net"
	"sync"
	"time"
)

// tokenSweepInterval is how often the background sweep checks for expired waiters.
const tokenSweepInterval = 30 * time.Second

// TokenTable is the rendezvous between waiting player connections and Worker
// dial-backs. Tokens are single-use and expire; a reused, unknown, or expired
// token finds no waiter (the listener then closes the connection without a
// response — RELAY.md Section 5). Safe for concurrent use.
type TokenTable struct {
	ttl time.Duration
	now func() time.Time

	mu      sync.Mutex
	waiters map[string]*waiter
}

type waiter struct {
	// ch receives the Worker's dial-back connection exactly once.
	ch      chan net.Conn
	expires time.Time
}

// NewTokenTable builds a table whose entries expire after ttl. now is
// injectable for tests; pass time.Now in production.
func NewTokenTable(ttl time.Duration, now func() time.Time) *TokenTable {
	if now == nil {
		now = time.Now
	}
	return &TokenTable{
		ttl:     ttl,
		now:     now,
		waiters: make(map[string]*waiter),
	}
}

// Register records a waiter for token and returns a channel that will receive
// the Worker's dial-back connection (buffered, capacity 1, so Deliver never
// blocks). The caller must call Cancel(token) when it gives up so the entry is
// reclaimed. Registering a token already present overwrites it (the API never
// mints duplicates; last-writer-wins is harmless).
func (t *TokenTable) Register(token string) <-chan net.Conn {
	ch := make(chan net.Conn, 1)
	t.mu.Lock()
	defer t.mu.Unlock()
	t.waiters[token] = &waiter{ch: ch, expires: t.now().Add(t.ttl)}
	return ch
}

// Cancel removes the waiter for token and reports whether it was still present.
// A false return means a concurrent Deliver already consumed the token and a
// connection is (or will be) sent on the waiter's channel — the caller must
// drain and close it to avoid leaking the Worker's dial-back. Idempotent.
func (t *TokenTable) Cancel(token string) (removed bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if _, ok := t.waiters[token]; !ok {
		return false
	}
	delete(t.waiters, token)
	return true
}

// Deliver hands conn to the waiter registered for token, consuming the token
// (single-use). It returns true on a match. A missing, expired, or
// already-consumed token returns false; the caller closes conn without a
// response (RELAY.md Section 5).
//
// An expired entry is left in place (NOT deleted): the waiter is still blocked
// on its channel and reclaims the entry via Cancel. Deleting it here would make
// the waiter's Cancel return false — signalling "a conn is en route on the
// channel" — when nothing was sent, hanging the waiter forever. Preserves the
// invariant "Cancel returns false ⇒ a conn is en route on the channel".
func (t *TokenTable) Deliver(token string, conn net.Conn) bool {
	t.mu.Lock()
	w, ok := t.waiters[token]
	if ok && t.now().Before(w.expires) {
		delete(t.waiters, token)
		t.mu.Unlock()
		w.ch <- conn
		return true
	}
	t.mu.Unlock()
	return false
}

// StartSweep runs a background goroutine that periodically removes waiters
// whose TTL has elapsed. This is defense-in-depth: normally the waiter calls
// Cancel, but if Cancel is never called (e.g. API reconnect race) the entry
// would leak without the sweep. The goroutine exits when ctx is cancelled.
func (t *TokenTable) StartSweep(ctx context.Context) {
	go t.sweepLoop(ctx)
}

func (t *TokenTable) sweepLoop(ctx context.Context) {
	ticker := time.NewTicker(tokenSweepInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			t.sweepExpired()
		}
	}
}

// sweepExpired removes all waiters whose expiry has passed. Before deleting an
// entry, it closes the waiter's channel so any pending <-ch unblocks with the
// zero value (nil). This preserves the Cancel invariant: if Cancel runs after
// the sweep and finds the entry gone (returns false), the waiter receives nil
// from the closed channel instead of blocking forever.
func (t *TokenTable) sweepExpired() {
	now := t.now()
	t.mu.Lock()
	defer t.mu.Unlock()
	for token, w := range t.waiters {
		if !now.Before(w.expires) {
			close(w.ch)
			delete(t.waiters, token)
		}
	}
}
