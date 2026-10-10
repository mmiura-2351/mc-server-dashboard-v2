//go:build e2e

// Reap old containers from prior E2E runs whose timeout or panic bypassed cleanup.
// Filter by E2E worker-ID prefix and minimum age to exclude live deployment containers and recent test runs.
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

// E2E IDs carry this prefix; deployment Worker IDs are bare UUIDs.
const e2eWorkerIDPrefix = "e2e-"

// Keep this label key synchronized with containerdriver.labelWorkerID.
const reapWorkerIDLabel = "mcsd.worker.id"

// Age reduces the chance of deleting a concurrent test run; it is not an active-run lock.
const reapMinAge = 10 * time.Minute

// The E2E harness requires a local Docker daemon.
const reapDockerHost = "/var/run/docker.sock"

// Keep the Engine API version aligned with dockerclient.go.
const reapAPIVersion = "v1.43"

// reapStaleE2EContainers is best-effort and runs only after the E2E environment gates pass.
func reapStaleE2EContainers(ctx context.Context) error {
	c := &http.Client{
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				var d net.Dialer
				return d.DialContext(ctx, "unix", reapDockerHost)
			},
		},
	}

	// Engine label filters cannot match value prefixes; enforce the E2E-only prefix below before removal.
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
		// Exclude deployment containers, which also carry this label key.
		if !strings.HasPrefix(cont.Labels[reapWorkerIDLabel], e2eWorkerIDPrefix) {
			continue
		}
		// Leave recently created containers for concurrent test runs.
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
