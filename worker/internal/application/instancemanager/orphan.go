package instancemanager

import (
	"context"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// Back off unresolved orphan probes to the cap, balancing quick recovery against daemon load.
// Tests can shrink the Manager's interval fields.
const (
	defaultOrphanProbeInterval    = 30 * time.Second
	defaultOrphanProbeMaxInterval = 5 * time.Minute
)

// Keep the exact wire spelling unknown; unrecognized state names map to UNSPECIFIED and are dropped by the API.
const orphanUnknownState = "unknown"

// Record the orphan and claim its sole converger under one lock.
// Closed managers retain the guarding record but spawn no unjoined work.
func (m *Manager) recordOrphan(serverID string, inst execution.Instance, driverName, mcVersion string) {
	m.mu.Lock()
	m.orphans[serverID] = orphanEntry{inst: inst, driver: driverName, mcVersion: mcVersion}
	spawn := !m.converging[serverID] && !m.closed
	if spawn {
		m.converging[serverID] = true
		m.background.Add(1)
	}
	m.mu.Unlock()
	if spawn {
		go m.convergeOrphan(serverID)
	}
}

// convergeOrphan probes until termination or manager shutdown: retry live instances, retire dead ones, and
// report unknown on errors.
// A retry uses the normal stop reservation and escalation; state changes never rely on cached liveness.
func (m *Manager) convergeOrphan(serverID string) {
	defer m.background.Done()
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
		// Discard probe results after shutdown; cancellation is not evidence of an unknown server state.
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
			// Confirmed gone across two probes and the record is still here: the instance's own pump can no longer clear
			// it.
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

// Bound probes by the cadence so an unresponsive Inspect cannot stop convergence; manager shutdown cancels them
// immediately.
func probeAliveWithTimeout(parent context.Context, inst execution.Instance, timeout time.Duration) (bool, error) {
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	return inst.ProbeAlive(ctx)
}

// Clear converging with the absent-record check under the same lock so a new orphan can start its successor.
func (m *Manager) currentOrphan(serverID string) (orphanEntry, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	entry, ok := m.orphans[serverID]
	if !ok {
		delete(m.converging, serverID)
	}
	return entry, ok
}

// retryOrphanStop shares the operator-stop reservation; a competing stop makes this round skip.
// The manager joins this stop during shutdown, allowing detached escalation to finish.
func (m *Manager) retryOrphanStop(serverID string, inst execution.Instance) {
	driver, mcVersion, outcome := m.takeOrphanReserve(serverID, inst)
	if outcome != takeFound {
		return
	}
	defer m.release(serverID)
	if err := m.attemptStop(context.Background(), serverID, inst, true, driver, mcVersion); err != nil {
		m.logger.Warn("failed-stop orphan: retry stop did not confirm termination; will probe again",
			"server_id", serverID, "driver", driver, "error", err)
	}
}

// Reserve only if the probed handle is still the orphan; it may have exited and been replaced since the probe.
// Never evict a newly running instance for an old convergence round.
func (m *Manager) takeOrphanReserve(serverID string, inst execution.Instance) (string, string, takeOutcome) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.reserved[serverID] {
		return "", "", takeInFlight
	}
	entry, ok := m.orphans[serverID]
	if !ok || entry.inst != inst {
		return "", "", takeNotFound
	}
	m.reserved[serverID] = true
	return entry.driver, entry.mcVersion, takeFound
}
