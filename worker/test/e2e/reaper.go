//go:build e2e

// Cross-run reaper for the container-driver restart harness (issue #256).
//
// restart_e2e_test.go cleans up its stub container via t.Cleanup, which does
// NOT run when the process dies on a hard panic or a `go test -timeout` kill.
// Each run also uses a fresh random worker id ("e2e-restart-<uuid>"), so the
// driver's own startup sweep — scoped to a single worker id — never reclaims a
// previous run's orphan. The orphan therefore lingers until a human prunes it.
//
// reapStaleE2EContainers closes that gap: before a run starts it removes the
// stub containers that PREVIOUS harness runs leaked, identifying them by the
// e2e worker-id label prefix and an age threshold.
//
// Two safety properties matter:
//
//   - It must never touch the live stack. The live container-driver worker id
//     is a plain UUID; harness runs prefix theirs with "e2e-restart-". The
//     reaper filters on that VALUE prefix (not merely the label key, which the
//     live stack also carries), so a live worker's containers are never matched.
//
//   - It must not race a CONCURRENT harness run. Two runs never share a worker
//     id, but a sibling run's container is a valid e2e-prefixed container the
//     reaper would otherwise be free to delete. The age threshold guards this:
//     only containers created more than reapMinAge ago are removed, comfortably
//     longer than a single run's wall time, so an in-flight sibling is left be.
package e2e

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// e2eWorkerIDPrefix is the worker-id value prefix every restart-harness run
// uses (restart_e2e_test.go: "e2e-restart-"+uuid). The reaper matches on this
// prefix so it only ever removes harness containers, never the live stack's
// (whose worker id is a bare UUID).
const e2eWorkerIDPrefix = "e2e-restart-"

// reapWorkerIDLabel is the Docker label key the container driver stamps the
// worker id under (mirrors containerdriver.labelWorkerID, which is unexported).
const reapWorkerIDLabel = "mcsd.worker.id"

// reapMinAge is how old a harness container must be before the reaper removes
// it. It is far longer than one run takes (the test's own context deadline is
// 120s), so a concurrent sibling run's container is never reaped mid-flight.
const reapMinAge = 10 * time.Minute

// reapDockerHost is the Docker Engine unix socket. The harness only ever runs
// against a local daemon (see restart_e2e_test.go gating), so the fixed default
// is enough; a remote daemon is out of scope.
const reapDockerHost = "/var/run/docker.sock"

// reapAPIVersion pins the Engine API version path segment, matching the driver's
// EngineClient (dockerclient.go).
const reapAPIVersion = "v1.43"

// reapStaleE2EContainers removes stub containers leaked by PREVIOUS restart-
// harness runs. It is best-effort: any daemon error is reported to the caller,
// which logs but does not fail the run (a leaked orphan must not block a green
// scenario). It only ever runs once the test's env gates have already passed,
// so it stays inert in the ordinary `go test ./...` pass.
func reapStaleE2EContainers(ctx context.Context) error {
	c := &http.Client{
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				var d net.Dialer
				return d.DialContext(ctx, "unix", reapDockerHost)
			},
		},
	}

	// Filter by the worker-id label KEY only; the Engine cannot filter on a value
	// prefix, so the e2e-prefix discrimination happens client-side below. This
	// list therefore includes the live stack's containers — which is exactly why
	// the prefix check that follows is load-bearing, not just defence in depth.
	filters, err := json.Marshal(map[string][]string{"label": {reapWorkerIDLabel}})
	if err != nil {
		return fmt.Errorf("reaper: marshal list filter: %w", err)
	}
	q := url.Values{"all": {"true"}, "filters": {string(filters)}}

	var listed []struct {
		ID      string            `json:"Id"`
		Created int64             `json:"Created"`
		Labels  map[string]string `json:"Labels"`
	}
	if err := reapDo(ctx, c, http.MethodGet, "/containers/json", q, &listed); err != nil {
		return fmt.Errorf("reaper: list containers: %w", err)
	}

	cutoff := time.Now().Add(-reapMinAge)
	var errs []string
	for _, cont := range listed {
		// Prefix discrimination: skip anything whose worker id is not an e2e
		// harness id. This is what keeps the live stack untouched.
		if !strings.HasPrefix(cont.Labels[reapWorkerIDLabel], e2eWorkerIDPrefix) {
			continue
		}
		// Age guard: leave young containers be so a concurrent harness run is not
		// reaped out from under itself.
		if time.Unix(cont.Created, 0).After(cutoff) {
			continue
		}
		dq := url.Values{"force": {"true"}}
		if err := reapDo(ctx, c, http.MethodDelete, "/containers/"+cont.ID, dq, nil); err != nil {
			errs = append(errs, fmt.Sprintf("%s: %v", cont.ID, err))
		}
	}
	if len(errs) > 0 {
		return fmt.Errorf("reaper: remove stale containers: %s", strings.Join(errs, "; "))
	}
	return nil
}

// reapDo performs one Engine API request over the unix socket, decoding a JSON
// response into out when out is non-nil. A non-2xx status is an error.
func reapDo(ctx context.Context, c *http.Client, method, path string, query url.Values, out any) error {
	u := "http://docker/" + reapAPIVersion + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, u, nil)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	resp, err := c.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("%s %s: status %d", method, path, resp.StatusCode)
	}
	if out != nil {
		if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
			return fmt.Errorf("decode response: %w", err)
		}
	}
	return nil
}
