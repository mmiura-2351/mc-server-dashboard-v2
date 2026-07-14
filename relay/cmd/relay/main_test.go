package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/mmiura-2351/mc-server-dashboard-v2/relay/internal/metrics"
)

// TestControlPlaneKeepaliveMatchesServerContract pins the client-side keepalive
// parameters the control-plane dial applies (issue #1808). The values are a
// cross-module contract with the API server's enforcement policy
// (api/src/mc_server_dashboard_api/fleet/adapters/grpc_server.py
// _keepalive_options): Time must stay at or above twice the server's
// grpc.http2.min_ping_interval_without_data_ms (10s) or the server answers the
// ping cadence with GOAWAY ENHANCE_YOUR_CALM, and at or above gRPC-Go's 10s
// client floor (below it the library silently raises it).
func TestControlPlaneKeepaliveMatchesServerContract(t *testing.T) {
	if got, want := controlPlaneKeepalive.Time, 20*time.Second; got != want {
		t.Errorf("controlPlaneKeepalive.Time = %v, want %v", got, want)
	}
	if got, want := controlPlaneKeepalive.Timeout, 10*time.Second; got != want {
		t.Errorf("controlPlaneKeepalive.Timeout = %v, want %v", got, want)
	}
	if !controlPlaneKeepalive.PermitWithoutStream {
		t.Error("controlPlaneKeepalive.PermitWithoutStream = false, want true (probe between Session streams)")
	}
}

// TestMetricsHandlerHealthz asserts /healthz is a static liveness 200.
func TestMetricsHandlerHealthz(t *testing.T) {
	h := metricsHandler(prometheus.NewRegistry())
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Errorf("/healthz status = %d, want 200", rec.Code)
	}
}

// TestMetricsHandlerExposesSeries asserts /metrics serves the registry's series,
// including the build-info gauge and the Go collector.
func TestMetricsHandlerExposesSeries(t *testing.T) {
	reg := prometheus.NewRegistry()
	metrics.New(reg, "9.9.9")
	h := metricsHandler(reg)

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("/metrics status = %d, want 200", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{
		`relay_build_info{version="9.9.9"} 1`,
		"go_goroutines",
		"process_start_time_seconds",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("/metrics body missing %q", want)
		}
	}
}
