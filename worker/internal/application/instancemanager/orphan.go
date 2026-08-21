package instancemanager

import (
	"context"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// defaultOrphanProbeInterval / defaultOrphanProbeMaxInterval are the per-orphan
// converger's cadence: the first probe runs one interval after the orphan is
// recorded, and every round that does not resolve it doubles the wait up to the
// cap (issue #2475). The base is short enough that a transient wedge is cleared
// in well under a minute, the cap low enough that a long outage still gets a
// dozen probes an hour without hammering a struggling daemon. They are Manager
// fields (not consts) so tests can shrink them, mirroring fsckRetryDelay.
const (
	defaultOrphanProbeInterval    = 30 * time.Second
	defaultOrphanProbeMaxInterval = 5 * time.Minute
)

// orphanUnknownState is the status name the converger reports for an orphan
// whose fate the Worker cannot determine. It is the wire `unknown`
// (SERVER_STATE_UNKNOWN, issue #2474) — controlplane.mapServerState keys on this
// exact spelling, and any other name falls through to UNSPECIFIED, which the API
// ingest drops.
const orphanUnknownState = "unknown"

// recordOrphan records serverID's failed-stop orphan and makes sure a converger
// goroutine is driving it. At most one converger runs per id: the flag is
// claimed in the same critical section as the record, so the retry stops the
// converger itself issues (which land back here on failure) never spawn a
// second one.
//
// A closed manager (issue #2493) still records the orphan — the record is what
// guards the id against every other command — but spawns nothing: Close has
// already joined the convergers, so a fresh one would be a goroutine outliving
// the manager again. The counter is incremented under the same lock that reads
// the flag, so a spawn is always visible to the Wait that Close performs.
func (m *Manager) recordOrphan(serverID string, inst execution.Instance, driverName string) {
	m.mu.Lock()
	m.orphans[serverID] = orphanEntry{inst: inst, driver: driverName}
	spawn := !m.converging[serverID] && !m.closed
	if spawn {
		m.converging[serverID] = true
		m.convergers.Add(1)
	}
	m.mu.Unlock()
	if spawn {
		go m.convergeOrphan(serverID)
	}
}

// convergeOrphan drives one server id's failed-stop orphan to a settled outcome
// without any operator action (issue #2475). Before it, a stop whose driver
// could not confirm termination recorded the orphan and stopped there: if the
// daemon was wedged nobody ever looked again, and the id stayed guarded against
// every other command indefinitely (issue #2468 item 1).
//
// Each round asks the instance itself whether it is still alive — ProbeAlive, a
// live observation rather than the cached state both driver Stop failure paths
// restore (issue #2473) — and acts on the answer:
//
//   - alive: re-run the stop through the SAME machinery an operator retry uses
//     (takeOrphanReserve -> attemptStop with graceful=true), so each retry
//     re-emits `stopping` and re-runs the flush-first escalation. This adds
//     repetition of the existing sequence, never a stronger action: SIGKILL
//     remains the ceiling and is reached only after the same save-off /
//     save-all / settle flush the first attempt ran (#1007/#1008).
//   - dead: normally `supervise` observes the same exit and the pump's
//     forgetOrphanIf retires the record. When the record instead persists past a
//     probe interval with the instance confirmed gone, it is the #2468 item-4
//     stranded record — forgetOrphanIf ran BEFORE attemptStop wrote it, so
//     nothing will ever clear it — and this retires it directly and reports the
//     terminal `stopped`.
//   - unavailable: report `unknown` for the id (level-triggered: once per
//     transition into the state) and keep probing. It NEVER gives up. A bounded
//     give-up would recreate the "Worker gives up" root cause the #2467 owner
//     decision overturned; the per-round WARN is what keeps the state
//     operator-visible meanwhile.
//
// It exits when the id has no orphan record left — the resolution above, an
// operator retry that confirmed termination, or the instance exiting on its own —
// or when the manager is closed (issue #2493): the converger belongs to the
// manager that spawned it and must not outlive it, so it parks on the shutdown
// alongside its probe interval and abandons the round the moment one lands.
// Handing over rather than pinning one instance is deliberate: an id whose
// orphan is retired, restarted, and orphaned again keeps exactly one converger,
// and the per-instance flags below reset when the record changes hands.
//
// One benign race is noted rather than "fixed": the pump emits the terminal
// `stopped` BEFORE its deferred forgetOrphanIf runs, so a SnapshotTrigger the
// API dispatches immediately on that event can find the orphan still recorded
// and be refused once. The reconciler re-drives it on the next tick. Weakening
// reserve()'s orphan guard to close it would let a snapshot run over a world
// that may still be live — the one thing this whole path exists to prevent.
func (m *Manager) convergeOrphan(serverID string) {
	defer m.convergers.Done()
	delay := m.orphanProbeInterval
	// probed is the orphan the two flags below describe; they reset if the id's
	// record changes hands.
	var probed execution.Instance
	var sawDead, reportedUnknown bool
	for {
		select {
		case <-m.clock.After(delay):
		case <-m.shutdown.Done():
			return
		}

		entry, ok := m.currentOrphan(serverID)
		if !ok {
			return
		}
		if entry.inst != probed {
			probed, sawDead, reportedUnknown = entry.inst, false, false
			// Back to the base cadence with them: this is a NEW orphan, and inheriting
			// a backoff the previous one earned would leave a freshly re-orphaned id
			// waiting out the cap before its first probe.
			delay = m.orphanProbeInterval
		}

		alive, err := probeAliveWithTimeout(m.shutdown, entry.inst, delay)
		// A Close landing mid-probe surfaces here as a cancelled probe. That is the
		// shutdown, not a daemon that cannot answer, so it must not be reported as
		// `unknown` on the way out (issue #2493).
		if m.shutdown.Err() != nil {
			return
		}
		switch {
		case err != nil:
			sawDead = false
			m.logger.Warn("failed-stop orphan: cannot determine whether the process is alive; reporting unknown and still probing",
				"server_id", serverID, "driver", entry.driver, "error", err)
			if !reportedUnknown {
				reportedUnknown = true
				m.sendStatus(session.StatusEvent{
					ServerID: serverID,
					State:    orphanUnknownState,
					Detail:   "worker cannot confirm the fate of a failed-stop orphan",
				})
			}
		case alive:
			sawDead, reportedUnknown = false, false
			m.retryOrphanStop(serverID, entry.inst)
		case sawDead:
			// Confirmed gone across two probes and the record is still here: the
			// instance's own pump can no longer clear it (issue #2468 item 4).
			if m.forgetOrphanIf(serverID, entry.inst) {
				m.logger.Warn("failed-stop orphan: instance confirmed gone but its record was stranded; retired it",
					"server_id", serverID, "driver", entry.driver)
				m.sendStatus(session.StatusEvent{
					ServerID: serverID,
					State:    execution.StateStopped.String(),
					Detail:   "failed-stop orphan confirmed gone",
				})
			}
		default:
			// First confirmed-dead probe: give the instance's own pump one interval
			// to emit the terminal and retire the record the ordinary way.
			sawDead, reportedUnknown = true, false
		}

		delay = min(2*delay, m.orphanProbeMaxInterval)
	}
}

// probeAliveWithTimeout runs one liveness probe bounded by the probe cadence,
// mirroring sampleWithTimeout: a probe that has not answered by the time the
// next one is due is abandoned. Without the bound a wedged-but-connected daemon
// could park the converger inside a single Inspect forever, which is "gives up
// probing" by another name.
//
// parent is the manager's shutdown context, so a probe in flight when the manager
// closes is abandoned immediately instead of holding Close for up to a full probe
// interval — five minutes at the backoff cap (issue #2493).
func probeAliveWithTimeout(parent context.Context, inst execution.Instance, timeout time.Duration) (bool, error) {
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	return inst.ProbeAlive(ctx)
}

// currentOrphan reads serverID's orphan record and, when there is none, clears
// the converger flag in the SAME critical section. Doing both under one lock is
// what keeps "at most one converger per id" true: a recordOrphan that lands
// after this observes the cleared flag and spawns a fresh converger, and one
// that lands before it keeps the running one.
func (m *Manager) currentOrphan(serverID string) (orphanEntry, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	entry, ok := m.orphans[serverID]
	if !ok {
		delete(m.converging, serverID)
	}
	return entry, ok
}

// retryOrphanStop re-attempts the stop for a still-alive orphan through the same
// reservation an operator retry takes, so the two can never double-drive one
// orphan: whichever claims the id first runs, and the other is refused — an
// operator stop with BUSY (takeStoppableReserve -> takeInFlight), this converger
// by skipping the round and re-probing next time.
//
// The stop runs on a background context: the command that recorded this orphan
// returned long ago, and an unbounded context is safe because every leg carries
// its own bound — the driver's Stop detaches onto its own stopDeadline, the RCON
// dial/Execute fall back to their 30s ceiling when the caller's context has no
// deadline, and the pre-stop flush gives up after settleBudget. A retry that
// could park here forever would be "gives up probing" by another name, since it
// also holds the id's reservation.
//
// attemptStop's own failure path re-records the orphan (no second converger —
// recordOrphan is idempotent on the flag), so a retry that still cannot confirm
// termination simply leaves the loop running.
func (m *Manager) retryOrphanStop(serverID string, inst execution.Instance) {
	driver, outcome := m.takeOrphanReserve(serverID, inst)
	if outcome != takeFound {
		return
	}
	defer m.release(serverID)
	if err := m.attemptStop(context.Background(), serverID, inst, true, driver); err != nil {
		m.logger.Warn("failed-stop orphan: retry stop did not confirm termination; will probe again",
			"server_id", serverID, "driver", driver, "error", err)
	}
}

// takeOrphanReserve is the converger's counterpart to takeStoppableReserve: it
// claims the id for a retry stop only while inst is STILL the recorded orphan.
// The identity guard is what takeStoppableReserve does not need and this does:
// the operator path decides and acts inside one command, while the converger
// decides on a probe and acts afterwards, so between the two the orphan can exit
// on its own and a re-placed StartServer can register a fresh instance under the
// same id — which an unguarded take would evict and stop. It reports takeInFlight
// when a lifecycle command already holds the id and takeNotFound when the record
// is gone or now names a different instance.
func (m *Manager) takeOrphanReserve(serverID string, inst execution.Instance) (string, takeOutcome) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.reserved[serverID] {
		return "", takeInFlight
	}
	entry, ok := m.orphans[serverID]
	if !ok || entry.inst != inst {
		return "", takeNotFound
	}
	m.reserved[serverID] = true
	return entry.driver, takeFound
}
