// Package metrics owns the relay's Prometheus instrumentation: a dedicated
// registry (never the client_golang global default), the Go and process
// collectors (go_goroutines, process_start_time_seconds, ...), a
// relay_build_info gauge, and the Java-path metric handles the game, tunnel,
// and session subsystems increment (RELAY.md Section 17).
//
// The handles are threaded into those subsystems by dependency injection
// (the relay uses constructor injection everywhere; no package globals). A nil
// *Metrics is a safe no-op on every increment method, so subsystems constructed
// without instrumentation — unit tests that build a listener/reporter directly —
// need no guards of their own.
//
// Cardinality is deliberately bounded: every label is a fixed enum (listener,
// cap kind, drop reason, dial-back result). No per-client-IP or source-address
// value is ever a label, so a hostile client cannot inflate the series count.
package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
)

// Listener label values for relay_ipcaps_rejections_total.
const (
	ListenerGame   = "game"
	ListenerTunnel = "tunnel"
)

// Cap-kind label values for relay_ipcaps_rejections_total: a
// concurrent-connection cap hit vs a join-rate cap hit.
const (
	CapKindConn = "conn"
	CapKindRate = "rate"
)

// Reason label values for relay_game_drops_total.
const (
	DropHandshakeInvalid   = "handshake_invalid"
	DropUnknownHost        = "unknown_host"
	DropNotFound           = "not_found"
	DropResolveUnavailable = "resolve_unavailable"
)

// Result label values for relay_tunnel_dialbacks_total.
const (
	DialbackDelivered        = "delivered"
	DialbackNoWaiter         = "no_waiter"
	DialbackHandshakeInvalid = "handshake_invalid"
)

// Metrics holds the Java-path metric handles. Build one with New; a nil *Metrics
// is a safe no-op. Bedrock-path handles are intentionally absent here — the
// Bedrock instrumentation PR adds them, so this build declares no dead
// constant-0 series for a path it does not yet touch.
type Metrics struct {
	ipcapsRejections     *prometheus.CounterVec // labels: listener, kind
	gameActiveSessions   prometheus.Gauge
	gameSessionsAccepted prometheus.Counter
	gameDrops            *prometheus.CounterVec // label: reason
	tunnelDialbacks      *prometheus.CounterVec // label: result
	sessionFlushFailures prometheus.Counter
}

// New registers the process and Go collectors, a relay_build_info{version} gauge
// pinned to 1, and the Java-path handles on reg, returning the handles. reg must
// be a dedicated registry (not the global default) so the endpoint exposes only
// the relay's own series. It panics on a duplicate registration, which is a
// programmer error (New is called once at wiring time).
func New(reg prometheus.Registerer, version string) *Metrics {
	reg.MustRegister(
		collectors.NewGoCollector(),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)

	buildInfo := prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "relay_build_info",
		Help: "Relay build information; constant 1, labelled by build version.",
	}, []string{"version"})
	reg.MustRegister(buildInfo)
	buildInfo.WithLabelValues(version).Set(1)

	m := &Metrics{
		ipcapsRejections: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "relay_ipcaps_rejections_total",
			Help: "Per-IP hygiene-cap rejections, by listener and cap kind.",
		}, []string{"listener", "kind"}),
		gameActiveSessions: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "relay_game_active_sessions",
			Help: "Player sessions currently spliced through the game listener.",
		}),
		gameSessionsAccepted: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "relay_game_sessions_accepted_total",
			Help: "Player logins accepted and spliced to a worker.",
		}),
		gameDrops: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "relay_game_drops_total",
			Help: "Player connections dropped on the game listener, by reason.",
		}, []string{"reason"}),
		tunnelDialbacks: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "relay_tunnel_dialbacks_total",
			Help: "Worker tunnel dial-backs on the tunnel listener, by result.",
		}, []string{"result"}),
		sessionFlushFailures: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "relay_session_report_flush_failures_total",
			Help: "Failed ReportSessions flushes in the session reporter.",
		}),
	}
	reg.MustRegister(
		m.ipcapsRejections,
		m.gameActiveSessions,
		m.gameSessionsAccepted,
		m.gameDrops,
		m.tunnelDialbacks,
		m.sessionFlushFailures,
	)
	return m
}

// IPCapsReject records a per-IP hygiene-cap rejection. listener is ListenerGame
// or ListenerTunnel; kind is CapKindConn or CapKindRate.
func (m *Metrics) IPCapsReject(listener, kind string) {
	if m == nil {
		return
	}
	m.ipcapsRejections.WithLabelValues(listener, kind).Inc()
}

// GameSessionAccepted records a player login that spliced to a worker.
func (m *Metrics) GameSessionAccepted() {
	if m == nil {
		return
	}
	m.gameSessionsAccepted.Inc()
}

// GameActiveSessionBegin records a splice starting (+1 active session). Pair
// each call with GameActiveSessionEnd.
func (m *Metrics) GameActiveSessionBegin() {
	if m == nil {
		return
	}
	m.gameActiveSessions.Inc()
}

// GameActiveSessionEnd records a splice ending (-1 active session).
func (m *Metrics) GameActiveSessionEnd() {
	if m == nil {
		return
	}
	m.gameActiveSessions.Dec()
}

// GameDrop records a dropped game connection. reason is one of the Drop*
// constants.
func (m *Metrics) GameDrop(reason string) {
	if m == nil {
		return
	}
	m.gameDrops.WithLabelValues(reason).Inc()
}

// TunnelDialback records a worker dial-back outcome. result is one of the
// Dialback* constants.
func (m *Metrics) TunnelDialback(result string) {
	if m == nil {
		return
	}
	m.tunnelDialbacks.WithLabelValues(result).Inc()
}

// SessionFlushFailure records a failed ReportSessions flush.
func (m *Metrics) SessionFlushFailure() {
	if m == nil {
		return
	}
	m.sessionFlushFailures.Inc()
}
