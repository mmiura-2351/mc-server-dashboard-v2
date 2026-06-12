// Package session tracks live player sessions on the relay and batches their
// lifecycle events to the API (docs/app/RELAY.md Sections 6 and 8). One row per
// accepted login; status pings are not recorded. The reporter mints the
// session_id (UUID, the idempotency key), buffers SessionStart / SessionEnd
// events, and flushes them to the API every ~5 s or 100 events. It also exposes
// the set of still-open session ids for the relay's Register call (orphan
// healing after a crash).
package session

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/google/uuid"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/adapters/apiclient"
)

// Flush triggers: the reporter flushes the buffer when it reaches FlushMaxEvents
// pending events or every DefaultFlushInterval, whichever comes first (RELAY.md
// Section 6).
const (
	DefaultFlushInterval = 5 * time.Second
	FlushMaxEvents       = 100
)

// reportClient is the subset of the API client the reporter needs. Narrowed to
// an interface so tests inject a fake.
type reportClient interface {
	ReportSessions(ctx context.Context, starts []apiclient.SessionStart, ends []apiclient.SessionEnd) error
}

// Reporter mints session ids, tracks open sessions, and batches lifecycle
// events to the API. Safe for concurrent use.
type Reporter struct {
	client   reportClient
	logger   *slog.Logger
	now      func() time.Time
	interval time.Duration

	mu          sync.Mutex
	pendStarts  []apiclient.SessionStart
	pendEnds    []apiclient.SessionEnd
	openIDs     map[string]struct{}
	flushSignal chan struct{}
}

// NewReporter builds a reporter over the API client.
func NewReporter(client reportClient, logger *slog.Logger, now func() time.Time) *Reporter {
	if now == nil {
		now = time.Now
	}
	return &Reporter{
		client:      client,
		logger:      logger,
		now:         now,
		interval:    DefaultFlushInterval,
		openIDs:     make(map[string]struct{}),
		flushSignal: make(chan struct{}, 1),
	}
}

// WithFlushInterval overrides the periodic flush cadence. Used by tests to drain
// the buffer faster than the production default.
func (r *Reporter) WithFlushInterval(d time.Duration) *Reporter {
	r.interval = d
	return r
}

// Start records a new accepted login session and returns its minted id. The
// SessionStart event is buffered for the next flush; the id is tracked as open
// until End is called.
func (r *Reporter) Start(serverID, slug, playerIP, username, playerUUID string) string {
	id := uuid.NewString()
	r.mu.Lock()
	r.pendStarts = append(r.pendStarts, apiclient.SessionStart{
		SessionID: id,
		ServerID:  serverID,
		Slug:      slug,
		PlayerIP:  playerIP,
		Username:  username,
		PlayerUID: playerUUID,
		StartedAt: r.now(),
	})
	r.openIDs[id] = struct{}{}
	over := len(r.pendStarts)+len(r.pendEnds) >= FlushMaxEvents
	r.mu.Unlock()
	if over {
		r.signalFlush()
	}
	return id
}

// End records the close of a session by id.
func (r *Reporter) End(id string) {
	r.mu.Lock()
	r.pendEnds = append(r.pendEnds, apiclient.SessionEnd{SessionID: id, EndedAt: r.now()})
	delete(r.openIDs, id)
	over := len(r.pendStarts)+len(r.pendEnds) >= FlushMaxEvents
	r.mu.Unlock()
	if over {
		r.signalFlush()
	}
}

// ActiveSessionIDs returns the ids of sessions still open on the relay, for the
// Register call (RELAY.md Section 6).
func (r *Reporter) ActiveSessionIDs() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	ids := make([]string, 0, len(r.openIDs))
	for id := range r.openIDs {
		ids = append(ids, id)
	}
	return ids
}

// Run drives the flush loop until ctx is cancelled, then flushes once more so a
// clean shutdown does not strand buffered events.
func (r *Reporter) Run(ctx context.Context) {
	ticker := time.NewTicker(r.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			r.flush(context.WithoutCancel(ctx))
			return
		case <-ticker.C:
			r.flush(ctx)
		case <-r.flushSignal:
			r.flush(ctx)
		}
	}
}

// signalFlush nudges the run loop to flush now (the buffer hit the size cap),
// without blocking if a nudge is already pending.
func (r *Reporter) signalFlush() {
	select {
	case r.flushSignal <- struct{}{}:
	default:
	}
}

// flush drains the buffer and reports it. On error the events are restored to
// the front of the buffer so the next flush retries them (ReportSessions is
// idempotent server-side — RELAY.md Section 6).
func (r *Reporter) flush(ctx context.Context) {
	r.mu.Lock()
	if len(r.pendStarts) == 0 && len(r.pendEnds) == 0 {
		r.mu.Unlock()
		return
	}
	starts := r.pendStarts
	ends := r.pendEnds
	r.pendStarts = nil
	r.pendEnds = nil
	r.mu.Unlock()

	if err := r.client.ReportSessions(ctx, starts, ends); err != nil {
		r.logger.Warn("session report failed; will retry", "error", err, "starts", len(starts), "ends", len(ends))
		r.mu.Lock()
		r.pendStarts = append(starts, r.pendStarts...)
		r.pendEnds = append(ends, r.pendEnds...)
		r.mu.Unlock()
	}
}
