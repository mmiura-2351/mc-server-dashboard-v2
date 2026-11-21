package main

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
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

// TestMetricsHandlerMethods pins both routes to GET (which also serves HEAD):
// any other method, CONNECT included, gets a 405 instead of a 200 (issue #3331).
func TestMetricsHandlerMethods(t *testing.T) {
	h := metricsHandler(prometheus.NewRegistry())
	for _, path := range []string{"/healthz", "/metrics"} {
		for _, tc := range []struct {
			method string
			want   int
		}{
			{http.MethodGet, http.StatusOK},
			{http.MethodHead, http.StatusOK},
			{http.MethodPost, http.StatusMethodNotAllowed},
			{http.MethodConnect, http.StatusMethodNotAllowed},
		} {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, httptest.NewRequest(tc.method, path, nil))
			if rec.Code != tc.want {
				t.Errorf("%s %s status = %d, want %d", tc.method, path, rec.Code, tc.want)
			}
		}
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

// childEnv marks a run of the test binary as runChild's child, by the name of the
// one test it runs.
const childEnv = "MCSD_TEST_CHILD"

// childTimeout bounds a child of runChild. Each exits within a second; the bound
// is there for a main that stops failing and serves instead.
const childTimeout = time.Minute

// runChild runs t again in a child process of the test binary, for a test that
// exits or changes process-wide state. The child sees no MCD_*, GRPC_GO_* or
// GODEBUG variable but those in env, so the ambient environment cannot steer it.
// It returns what the child wrote to stderr and its exit code. t must be a
// top-level test: the child runs t whole.
func runChild(t *testing.T, env map[string]string) (string, int) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), childTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^"+t.Name()+"$")
	for _, kv := range os.Environ() {
		if !strings.HasPrefix(kv, "MCD_") && !strings.HasPrefix(kv, "GRPC_GO_") && !strings.HasPrefix(kv, "GODEBUG=") {
			cmd.Env = append(cmd.Env, kv)
		}
	}
	for k, v := range env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	cmd.Env = append(cmd.Env, childEnv+"="+t.Name())
	var stderr strings.Builder
	cmd.Stderr = &stderr
	err := cmd.Run()
	if ctx.Err() != nil {
		t.Fatalf("the child did not exit within %v; it wrote:\n%s", childTimeout, stderr.String())
	}
	var exitErr *exec.ExitError
	if err != nil && !errors.As(err, &exitErr) {
		t.Fatalf("run the child: %v", err)
	}
	return stderr.String(), cmd.ProcessState.ExitCode()
}

// isChild reports whether this process is runChild's child for t.
func isChild(t *testing.T) bool {
	return os.Getenv(childEnv) == t.Name()
}
