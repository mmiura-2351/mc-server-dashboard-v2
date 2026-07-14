package metrics

import (
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

// gatheredNames returns the set of metric family names exposed by reg.
func gatheredNames(t *testing.T, reg *prometheus.Registry) map[string]bool {
	t.Helper()
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	names := make(map[string]bool, len(families))
	for _, f := range families {
		names[f.GetName()] = true
	}
	return names
}

// TestNewExposesProcessAndBuildSeries asserts New wires the Go and process
// collectors and the build-info gauge onto the registry.
func TestNewExposesProcessAndBuildSeries(t *testing.T) {
	reg := prometheus.NewRegistry()
	New(reg, "1.2.3-test")

	names := gatheredNames(t, reg)
	for _, want := range []string{
		"relay_build_info",
		"go_goroutines",
		"process_start_time_seconds",
	} {
		if !names[want] {
			t.Errorf("registry is missing %q", want)
		}
	}
}

// TestBuildInfoIsOneLabelledByVersion asserts relay_build_info is the constant 1
// carrying the build version as its only label value.
func TestBuildInfoIsOneLabelledByVersion(t *testing.T) {
	reg := prometheus.NewRegistry()
	New(reg, "9.9.9")

	const want = `
# HELP relay_build_info Relay build information; constant 1, labelled by build version.
# TYPE relay_build_info gauge
relay_build_info{version="9.9.9"} 1
`
	if err := testutil.GatherAndCompare(reg, strings.NewReader(want), "relay_build_info"); err != nil {
		t.Error(err)
	}
}

// TestJavaPathHandlesRecord asserts every Java-path handle increments the series
// it owns, with the label value it was given.
func TestJavaPathHandlesRecord(t *testing.T) {
	reg := prometheus.NewRegistry()
	m := New(reg, "test")

	m.IPCapsReject(ListenerGame, CapKindRate)
	m.IPCapsReject(ListenerTunnel, CapKindConn)
	m.GameSessionAccepted()
	m.GameActiveSessionBegin()
	m.GameActiveSessionBegin()
	m.GameActiveSessionEnd()
	m.GameDrop(DropUnknownHost)
	m.GameDrop(DropUnknownHost)
	m.TunnelDialback(DialbackDelivered)
	m.SessionFlushFailure()

	if got := testutil.ToFloat64(m.ipcapsRejections.WithLabelValues(ListenerGame, CapKindRate)); got != 1 {
		t.Errorf("ipcaps {game,rate} = %v, want 1", got)
	}
	if got := testutil.ToFloat64(m.ipcapsRejections.WithLabelValues(ListenerTunnel, CapKindConn)); got != 1 {
		t.Errorf("ipcaps {tunnel,conn} = %v, want 1", got)
	}
	if got := testutil.ToFloat64(m.gameSessionsAccepted); got != 1 {
		t.Errorf("sessions_accepted = %v, want 1", got)
	}
	if got := testutil.ToFloat64(m.gameActiveSessions); got != 1 {
		t.Errorf("active_sessions = %v, want 1 (2 begin, 1 end)", got)
	}
	if got := testutil.ToFloat64(m.gameDrops.WithLabelValues(DropUnknownHost)); got != 2 {
		t.Errorf("drops {unknown_host} = %v, want 2", got)
	}
	if got := testutil.ToFloat64(m.tunnelDialbacks.WithLabelValues(DialbackDelivered)); got != 1 {
		t.Errorf("dialbacks {delivered} = %v, want 1", got)
	}
	if got := testutil.ToFloat64(m.sessionFlushFailures); got != 1 {
		t.Errorf("flush_failures = %v, want 1", got)
	}
}

// TestNoSourceAddressLabels is the cardinality guardrail: no series may carry a
// per-client-IP / source-address label, which a hostile client could otherwise
// use to explode the series count. Every label name must be a bounded enum key.
func TestNoSourceAddressLabels(t *testing.T) {
	reg := prometheus.NewRegistry()
	m := New(reg, "test")
	// Emit at least one child of every labelled vector so the labels materialise.
	m.IPCapsReject(ListenerGame, CapKindConn)
	m.GameDrop(DropNotFound)
	m.TunnelDialback(DialbackNoWaiter)

	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	forbidden := []string{"ip", "addr", "address", "source", "src", "remote", "client", "host"}
	for _, f := range families {
		for _, metric := range f.GetMetric() {
			for _, lp := range metric.GetLabel() {
				name := strings.ToLower(lp.GetName())
				for _, bad := range forbidden {
					if strings.Contains(name, bad) {
						t.Errorf("%s carries a source-identifying label %q", f.GetName(), lp.GetName())
					}
				}
			}
		}
	}
}

// TestNilMetricsIsNoop asserts a nil *Metrics is safe to call, so subsystems
// constructed without instrumentation (unit tests) need no guards.
func TestNilMetricsIsNoop(_ *testing.T) {
	var m *Metrics
	// None of these must panic.
	m.IPCapsReject(ListenerGame, CapKindConn)
	m.GameSessionAccepted()
	m.GameActiveSessionBegin()
	m.GameActiveSessionEnd()
	m.GameDrop(DropHandshakeInvalid)
	m.TunnelDialback(DialbackHandshakeInvalid)
	m.SessionFlushFailure()
}
