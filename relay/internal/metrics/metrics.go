// Package metrics owns the relay's Prometheus instrumentation: a dedicated
// registry (never the client_golang global default), the Go and process
// collectors (go_goroutines, process_start_time_seconds, ...), a
// relay_build_info gauge, and the metric handles the game, tunnel, session, and
// bedrock subsystems increment (RELAY.md Section 17; the Bedrock path is issue
// #1909).
//
// The handles are threaded into those subsystems by dependency injection
// (the relay uses constructor injection everywhere; no package globals). A nil
// *Metrics is a safe no-op on every increment method, so subsystems constructed
// without instrumentation — unit tests that build a listener/reporter directly —
// need no guards of their own.
//
// Cardinality is deliberately bounded: every label is a fixed enum (listener,
// cap kind, drop reason, dial-back result, datagram direction). No per-client-IP
// or source-address value is ever a label, so a hostile client cannot inflate
// the series count.
package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
)

// Listener label values for relay_ipcaps_rejections_total.
const (
	ListenerGame    = "game"
	ListenerTunnel  = "tunnel"
	ListenerBedrock = "bedrock"
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

// Direction label values for the Bedrock datagram counters
// (relay_bedrock_udp_datagrams_total and relay_bedrock_datagrams_dropped_total):
// in = client->worker ingress (read from the public UDP socket), out =
// worker->client egress (written back to the client). A single enum shared
// across both counters so an operator reads one direction vocabulary.
const (
	DirectionIn  = "in"
	DirectionOut = "out"
)

// Reason label values for relay_bedrock_datagrams_dropped_total. The in-direction
// (ingress) reasons sit in the UDP reader and the QUIC sender; the out-direction
// (egress) reasons sit in the QUIC receiver. The per-IP new-flow cap rejection is
// deliberately NOT one of these: like the Java path, that rejection is a
// relay_ipcaps_rejections_total{listener="bedrock"} series, disjoint from this
// datagram-drop counter.
const (
	BedrockDropQueueFull   = "queue_full"
	BedrockDropOversized   = "oversized"
	BedrockDropPingRateCap = "ping_rate_cap"
	BedrockDropQUICSend    = "quic_send_error"
	BedrockDropShortFrame  = "short_frame"
	BedrockDropUnknownFlow = "unknown_flow"
	BedrockDropUDPWrite    = "udp_write_error"
)

// Reason label values for relay_bedrock_tunnels_rejected_total: the handshake /
// bind reject sites in the Bedrock tunnel listener.
const (
	BedrockRejectNoStream          = "no_stream"
	BedrockRejectHandshakeRead     = "handshake_read"
	BedrockRejectValidateError     = "validate_error"
	BedrockRejectInvalidCredential = "invalid_credential"
	BedrockRejectBindFailed        = "bind_failed"
	BedrockRejectAckFailed         = "ack_failed"
)

// Metrics holds the relay's metric handles (Java path and Bedrock path). Build
// one with New; a nil *Metrics is a safe no-op.
type Metrics struct {
	ipcapsRejections     *prometheus.CounterVec // labels: listener, kind
	gameActiveSessions   prometheus.Gauge
	gameSessionsAccepted prometheus.Counter
	gameDrops            *prometheus.CounterVec // label: reason
	tunnelDialbacks      *prometheus.CounterVec // label: result
	sessionFlushFailures prometheus.Counter

	// Bedrock path (issue #1909).
	bedrockActiveTunnels    prometheus.Gauge
	bedrockTunnelsOpened    prometheus.Counter
	bedrockTunnelsRejected  *prometheus.CounterVec // label: reason
	bedrockBindFailures     prometheus.Counter
	bedrockActiveFlows      prometheus.Gauge
	bedrockFlowsCreated     prometheus.Counter
	bedrockFlowsEvicted     prometheus.Counter
	bedrockDatagrams        *prometheus.CounterVec // label: direction
	bedrockDatagramsDropped *prometheus.CounterVec // labels: direction, reason
}

// New registers the process and Go collectors, a relay_build_info{version} gauge
// pinned to 1, and the Java-path and Bedrock-path handles on reg, returning the
// handles. reg must
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
		bedrockActiveTunnels: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "relay_bedrock_active_tunnels",
			Help: "Bedrock tunnels currently bound (a Worker QUIC dial-out mapped to a public UDP port).",
		}),
		bedrockTunnelsOpened: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "relay_bedrock_tunnels_opened_total",
			Help: "Bedrock tunnels successfully opened (handshake authenticated, port bound, ack sent).",
		}),
		bedrockTunnelsRejected: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "relay_bedrock_tunnels_rejected_total",
			Help: "Bedrock tunnel dial-outs rejected during the handshake, by reason.",
		}, []string{"reason"}),
		bedrockBindFailures: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "relay_bedrock_bind_failures_total",
			Help: "Public UDP port bind (ListenPacket) failures for Bedrock tunnels.",
		}),
		bedrockActiveFlows: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "relay_bedrock_active_flows",
			Help: "Bedrock client flows currently live across all bound tunnels.",
		}),
		bedrockFlowsCreated: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "relay_bedrock_flows_created_total",
			Help: "Bedrock client flows created.",
		}),
		bedrockFlowsEvicted: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "relay_bedrock_flows_evicted_total",
			Help: "Bedrock client flows evicted for inactivity.",
		}),
		bedrockDatagrams: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "relay_bedrock_udp_datagrams_total",
			Help: "Bedrock RakNet datagrams forwarded on the public UDP socket, by direction.",
		}, []string{"direction"}),
		bedrockDatagramsDropped: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "relay_bedrock_datagrams_dropped_total",
			Help: "Bedrock datagrams dropped, by direction and reason.",
		}, []string{"direction", "reason"}),
	}
	reg.MustRegister(
		m.ipcapsRejections,
		m.gameActiveSessions,
		m.gameSessionsAccepted,
		m.gameDrops,
		m.tunnelDialbacks,
		m.sessionFlushFailures,
		m.bedrockActiveTunnels,
		m.bedrockTunnelsOpened,
		m.bedrockTunnelsRejected,
		m.bedrockBindFailures,
		m.bedrockActiveFlows,
		m.bedrockFlowsCreated,
		m.bedrockFlowsEvicted,
		m.bedrockDatagrams,
		m.bedrockDatagramsDropped,
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

// BedrockTunnelBound records a Bedrock tunnel binding its public UDP port
// (+1 active tunnels). Pair each call with BedrockTunnelTornDown.
func (m *Metrics) BedrockTunnelBound() {
	if m == nil {
		return
	}
	m.bedrockActiveTunnels.Inc()
}

// BedrockTunnelTornDown records a Bedrock tunnel tearing down (-1 active
// tunnels).
func (m *Metrics) BedrockTunnelTornDown() {
	if m == nil {
		return
	}
	m.bedrockActiveTunnels.Dec()
}

// BedrockTunnelOpened records a Bedrock tunnel opened successfully (handshake
// authenticated, port bound, ack sent).
func (m *Metrics) BedrockTunnelOpened() {
	if m == nil {
		return
	}
	m.bedrockTunnelsOpened.Inc()
}

// BedrockTunnelRejected records a Bedrock dial-out rejected during the
// handshake. reason is one of the BedrockReject* constants.
func (m *Metrics) BedrockTunnelRejected(reason string) {
	if m == nil {
		return
	}
	m.bedrockTunnelsRejected.WithLabelValues(reason).Inc()
}

// BedrockBindFailure records a public UDP port bind (ListenPacket) failure.
func (m *Metrics) BedrockBindFailure() {
	if m == nil {
		return
	}
	m.bedrockBindFailures.Inc()
}

// BedrockFlowCreated records a new Bedrock client flow (+1 active flows, and
// the created-total counter). Balance it later with BedrockFlowsEvicted (idle
// eviction) or BedrockFlowsDrained (tunnel teardown).
func (m *Metrics) BedrockFlowCreated() {
	if m == nil {
		return
	}
	m.bedrockActiveFlows.Inc()
	m.bedrockFlowsCreated.Inc()
}

// BedrockFlowsEvicted records n Bedrock flows evicted for inactivity (-n active
// flows, and the evicted-total counter).
func (m *Metrics) BedrockFlowsEvicted(n int) {
	if m == nil || n == 0 {
		return
	}
	m.bedrockActiveFlows.Sub(float64(n))
	m.bedrockFlowsEvicted.Add(float64(n))
}

// BedrockFlowsDrained records n Bedrock flows abandoned on tunnel teardown (-n
// active flows). Teardown is not eviction, so it does NOT touch the
// evicted-total counter -- it only keeps the active-flows gauge from leaking
// when a tunnel drops its whole flow table.
func (m *Metrics) BedrockFlowsDrained(n int) {
	if m == nil || n == 0 {
		return
	}
	m.bedrockActiveFlows.Sub(float64(n))
}

// BedrockDatagram records a Bedrock datagram forwarded on the public UDP socket.
// direction is DirectionIn (a successful socket read) or DirectionOut (a
// successful reply write).
func (m *Metrics) BedrockDatagram(direction string) {
	if m == nil {
		return
	}
	m.bedrockDatagrams.WithLabelValues(direction).Inc()
}

// BedrockDatagramDropped records a dropped Bedrock datagram. direction is
// DirectionIn or DirectionOut; reason is one of the BedrockDrop* constants.
func (m *Metrics) BedrockDatagramDropped(direction, reason string) {
	if m == nil {
		return
	}
	m.bedrockDatagramsDropped.WithLabelValues(direction, reason).Inc()
}
