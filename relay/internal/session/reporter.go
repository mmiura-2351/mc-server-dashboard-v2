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
	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/metrics"
)

// Flush triggers: the reporter flushes the buffer when it reaches FlushMaxEvents
// pending events or every DefaultFlushInterval, whichever comes first (RELAY.md
// Section 6).
const (
	DefaultFlushInterval   = 5 * time.Second
	FlushMaxEvents         = 100
	DefaultShutdownTimeout = 10 * time.Second
)

// DefaultFlushTimeout bounds each ReportSessions RPC so a black-holed API
// connection (NAT/firewall state loss) cannot wedge the single Run goroutine
// until the OS abandons TCP retransmission (issue #1719; matches the
// registerTimeout / resolveJoinTimeout posture). A timed-out flush takes the
// error-restore path, so capOldest bounds the buffer during the outage. Sized
// to match registerTimeout rather than the tighter resolveJoinTimeout because
// a post-outage flush can carry up to 2×MaxBufferedEvents events (~2.5 MB
// proto-encoded) on a slow uplink.
const DefaultFlushTimeout = 10 * time.Second

// MaxBufferedEvents caps each pending event slice (starts and ends separately)
// during a sustained API outage. Beyond it the oldest events are dropped
// (drop-oldest) with a log line — bounding memory at the cost of losing the
// oldest session records rather than the whole process. RELAY.md Section 6.
const MaxBufferedEvents = 10_000

// reportClient is the subset of the API client the reporter needs. Narrowed to
// an interface so tests inject a fake.
type reportClient interface {
	ReportSessions(ctx context.Context, starts []apiclient.SessionStart, ends []apiclient.SessionEnd) error
}

// Reporter mints session ids, tracks open sessions, and batches lifecycle
// events to the API. Safe for concurrent use.
type Reporter struct {
	client          reportClient
	logger          *slog.Logger
	metrics         *metrics.Metrics
	now             func() time.Time
	interval        time.Duration
	flushTimeout    time.Duration
	shutdownTimeout time.Duration

	flightMu sync.Mutex // held during flush and SnapshotActive to serialize them

	mu            sync.Mutex
	pendStarts    []apiclient.SessionStart
	pendEnds      []apiclient.SessionEnd
	openIDs       map[string]struct{}
	droppedStarts map[string]struct{} // tombstones for starts dropped by capOldest
	flushSignal   chan struct{}
}

// NewReporter builds a reporter over the API client. m carries the relay's
// metric handles (nil is a no-op).
func NewReporter(client reportClient, logger *slog.Logger, now func() time.Time, m *metrics.Metrics) *Reporter {
	if now == nil {
		now = time.Now
	}
	return &Reporter{
		client:          client,
		logger:          logger,
		metrics:         m,
		now:             now,
		interval:        DefaultFlushInterval,
		flushTimeout:    DefaultFlushTimeout,
		shutdownTimeout: DefaultShutdownTimeout,
		openIDs:         make(map[string]struct{}),
		droppedStarts:   make(map[string]struct{}),
		flushSignal:     make(chan struct{}, 1),
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
func (r *Reporter) Start(serverID, slug, playerIP, username, playerUUID string, source apiclient.Source) string {
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
		Source:    source,
	})
	r.openIDs[id] = struct{}{}
	over := len(r.pendStarts)+len(r.pendEnds) >= FlushMaxEvents
	r.mu.Unlock()
	if over {
		r.signalFlush()
	}
	return id
}

// End records the close of a session by id. If the session's Start was dropped
// during a buffer overflow (tombstoned in droppedStarts), the End is silently
// discarded to prevent an orphan End from reaching the API.
func (r *Reporter) End(id string) {
	r.mu.Lock()
	delete(r.openIDs, id)
	if _, dropped := r.droppedStarts[id]; dropped {
		delete(r.droppedStarts, id)
		r.mu.Unlock()
		return
	}
	r.pendEnds = append(r.pendEnds, apiclient.SessionEnd{SessionID: id, EndedAt: r.now()})
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

// SnapshotActive returns the ids of sessions still open on the relay along with
// a release function that the caller must invoke when done. While held, no flush
// can run, so the snapshot is consistent with the buffer: any session started
// after the snapshot has its start event still buffered and will not be flushed
// (and therefore cannot be orphan-healed) until the caller releases. This is the
// barrier that prevents the Register/flush race (issue #1718).
func (r *Reporter) SnapshotActive() (ids []string, release func()) {
	r.flightMu.Lock()
	return r.ActiveSessionIDs(), r.flightMu.Unlock
}

// Run drives the flush loop until ctx is cancelled, then flushes once more so a
// clean shutdown does not strand buffered events.
func (r *Reporter) Run(ctx context.Context) {
	ticker := time.NewTicker(r.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			flushCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), r.shutdownTimeout)
			defer cancel()
			r.flush(flushCtx)
			// Drain events that arrived while the flush above was in flight.
			// In-flight handle goroutines may call End after the primary flush
			// drained the buffer; a brief drain loop rescues those stragglers
			// within the existing shutdown timeout.
			drainTimer := time.NewTimer(100 * time.Millisecond)
			defer drainTimer.Stop()
			for {
				select {
				case <-r.flushSignal:
					r.flush(flushCtx)
				case <-drainTimer.C:
					r.flush(flushCtx)
					return
				case <-flushCtx.Done():
					return
				}
			}
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
	r.flightMu.Lock()
	defer r.flightMu.Unlock()
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

	// Bound the RPC so a black-holed API connection cannot wedge the Run
	// goroutine (issue #1719); a timeout falls through to the error-restore
	// path below like any other failure.
	rctx, cancel := context.WithTimeout(ctx, r.flushTimeout)
	defer cancel()
	if err := r.client.ReportSessions(rctx, starts, ends); err != nil {
		r.metrics.SessionFlushFailure()
		r.logger.Warn("session report failed; will retry", "error", err, "starts", len(starts), "ends", len(ends))
		r.mu.Lock()
		merged := append(starts, r.pendStarts...)
		merged, dropped := capOldestStarts(merged, r.logger)
		r.pendStarts = merged
		// Record tombstones so future End() calls for dropped sessions are
		// suppressed.
		for id := range dropped {
			r.droppedStarts[id] = struct{}{}
		}
		allEnds := append(ends, r.pendEnds...)
		// Remove buffered ends whose start was just dropped.
		allEnds = dropOrphanEnds(allEnds, dropped)
		r.pendEnds = capOldest(allEnds, "end", r.logger)
		r.mu.Unlock()
	} else {
		// Successful flush: clear tombstones — the outage is over, and any
		// session whose start was dropped has been definitively lost. Keeping
		// tombstones would leak memory.
		r.mu.Lock()
		clear(r.droppedStarts)
		r.mu.Unlock()
	}
}

// capOldestStarts bounds the pending start slice to MaxBufferedEvents and
// returns the set of session ids that were dropped (the tombstones). These
// tombstones are used to suppress matching Ends so an orphan End never reaches
// the API.
func capOldestStarts(buf []apiclient.SessionStart, logger *slog.Logger) ([]apiclient.SessionStart, map[string]struct{}) {
	if len(buf) <= MaxBufferedEvents {
		return buf, nil
	}
	dropped := len(buf) - MaxBufferedEvents
	logger.Warn("session retry buffer full; dropping oldest events", "kind", "start", "dropped", dropped, "cap", MaxBufferedEvents)
	tombstones := make(map[string]struct{}, dropped)
	for _, s := range buf[:dropped] {
		tombstones[s.SessionID] = struct{}{}
	}
	return buf[dropped:], tombstones
}

// dropOrphanEnds removes buffered ends whose start was just dropped
// (tombstoned), preventing orphan Ends from reaching the API.
func dropOrphanEnds(ends []apiclient.SessionEnd, tombstones map[string]struct{}) []apiclient.SessionEnd {
	if len(tombstones) == 0 {
		return ends
	}
	n := 0
	for _, e := range ends {
		if _, orphan := tombstones[e.SessionID]; !orphan {
			ends[n] = e
			n++
		}
	}
	return ends[:n]
}

// capOldest bounds a pending-event slice to MaxBufferedEvents by dropping the
// oldest (front) entries during a sustained outage, logging the loss so it is
// never silent.
func capOldest[T any](buf []T, kind string, logger *slog.Logger) []T {
	if len(buf) <= MaxBufferedEvents {
		return buf
	}
	dropped := len(buf) - MaxBufferedEvents
	logger.Warn("session retry buffer full; dropping oldest events", "kind", kind, "dropped", dropped, "cap", MaxBufferedEvents)
	return buf[dropped:]
}
