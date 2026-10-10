package containerdriver

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// statusError carries a non-2xx Engine response so callers can branch on the
// HTTP status code (e.g. Create distinguishing a 409 name conflict) while still
// surfacing the daemon's message via Error().
type statusError struct {
	method  string
	path    string
	code    int
	message string
}

func (e statusError) Error() string {
	return fmt.Sprintf("containerdriver: %s %s: status %d: %s", e.method, e.path, e.code, e.message)
}

// defaultDockerHost is the Docker Engine unix socket used when no host is
// configured.
const defaultDockerHost = "unix:///var/run/docker.sock"

// apiVersion pins the Engine API version path segment. 1.43 ships with Docker
// Engine 24+; the endpoints this client uses are stable well below it.
const apiVersion = "v1.43"

// Double the default CPU weight to favor game servers under contention, without imposing a hard cap.
// An explicit CPUMillis allocation overrides this fallback.
const gameServerCPUShares = 2048

// EngineClient speaks the Docker Engine API over a unix socket using net/http.
// It is hand-rolled (no Docker SDK) to keep the dependency tree empty, matching
// the RCON client's posture (docs/dev/DEPENDENCIES.md); only the handful of
// endpoints the driver needs are implemented.
type EngineClient struct {
	http *http.Client
}

// NewEngineClient accepts Unix socket hosts only; empty uses the default socket and TCP hosts are rejected.
func NewEngineClient(host string) (*EngineClient, error) {
	if host == "" {
		host = defaultDockerHost
	}
	socket, ok := unixSocketPath(host)
	if !ok {
		return nil, fmt.Errorf("containerdriver: unsupported docker host %q (only unix:// is supported)", host)
	}
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", socket)
		},
	}
	return &EngineClient{http: &http.Client{Transport: transport}}, nil
}

// unixSocketPath extracts the socket path from a unix:// host, reporting whether
// the scheme is supported.
func unixSocketPath(host string) (string, bool) {
	const prefix = "unix://"
	if len(host) <= len(prefix) || host[:len(prefix)] != prefix {
		return "", false
	}
	return host[len(prefix):], true
}

// createBody is the /containers/create request payload.
type createBody struct {
	Image            string              `json:"Image"`
	Cmd              []string            `json:"Cmd"`
	User             string              `json:"User,omitempty"`
	WorkingDir       string              `json:"WorkingDir"`
	Labels           map[string]string   `json:"Labels,omitempty"`
	ExposedPorts     map[string]struct{} `json:"ExposedPorts,omitempty"`
	HostConfig       hostConfig          `json:"HostConfig"`
	NetworkingConfig *networkingConfig   `json:"NetworkingConfig,omitempty"`
}

type hostConfig struct {
	Binds        []string                 `json:"Binds,omitempty"`
	PortBindings map[string][]portBinding `json:"PortBindings,omitempty"`
	// CPUShares is the container's relative CPU weight, per-server and proportional to the CPU allocation. The
	// Engine translates it to cpu.weight on cgroup v2. It is a soft share, not a hard quota, no NanoCpus is ever
	// set, so MC tick latency is not throttled.
	CPUShares int64 `json:"CpuShares,omitempty"`
	// Memory is the hard container memory limit in bytes. The Engine translates it to memory.max on cgroup v2; the
	// kernel OOM-kills the container when it exceeds this. Zero (omitted) leaves the container unconstrained.
	Memory int64 `json:"Memory,omitempty"`
	// CapDrop lists the capabilities removed from the container's default set.
	CapDrop []string `json:"CapDrop,omitempty"`
}

type portBinding struct {
	HostIP   string `json:"HostIp"`
	HostPort string `json:"HostPort"`
}

// networkingConfig attaches the container to a user-defined network at create time. The Engine keys
// EndpointsConfig by network name; an empty endpoint object is enough to join, and a user-defined network then
// resolves the container's name via its embedded DNS.
type networkingConfig struct {
	EndpointsConfig map[string]struct{} `json:"EndpointsConfig"`
}

// Create creates a container and returns its id.
func (c *EngineClient) Create(ctx context.Context, spec CreateSpec) (string, error) {
	body := createBody{
		Image:      spec.Image,
		Cmd:        spec.Cmd,
		User:       spec.User,
		WorkingDir: spec.WorkingDir,
		Labels:     spec.Labels,
		HostConfig: hostConfig{Binds: spec.Binds, CPUShares: spec.CPUShares, Memory: spec.MemoryLimitBytes, CapDrop: spec.CapDrop},
	}
	if len(spec.Ports) > 0 {
		body.ExposedPorts = map[string]struct{}{}
		body.HostConfig.PortBindings = map[string][]portBinding{}
		for _, p := range spec.Ports {
			key := p.ContainerPort + "/tcp"
			body.ExposedPorts[key] = struct{}{}
			body.HostConfig.PortBindings[key] = []portBinding{{HostIP: p.HostIP, HostPort: p.HostPort}}
		}
	}
	if spec.Network != "" {
		body.NetworkingConfig = &networkingConfig{
			EndpointsConfig: map[string]struct{}{spec.Network: {}},
		}
	}

	var resp struct {
		ID string `json:"Id"`
	}
	q := url.Values{"name": {spec.Name}}
	if err := c.do(ctx, http.MethodPost, "/containers/create", q, body, &resp); err != nil {
		var status statusError
		if errors.As(err, &status) && status.code == http.StatusConflict {
			// Surface a typed conflict so the driver can run its remove-on-conflict retry; keep the daemon message for
			// diagnostics.
			return "", fmt.Errorf("%w: %v", errNameConflict, err)
		}
		return "", err
	}
	return resp.ID, nil
}

// ImagePull must drain the progress stream: HTTP 200 can still contain an error object before EOF.
func (c *EngineClient) ImagePull(ctx context.Context, image string) error {
	name, tag := splitImageTag(image)
	q := url.Values{"fromImage": {name}}
	if tag != "" {
		q.Set("tag", tag)
	}
	u := "http://docker/" + apiVersion + "/images/create?" + q.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, nil)
	if err != nil {
		return fmt.Errorf("containerdriver: build image pull request: %w", err)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("containerdriver: POST image pull: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return statusError{method: http.MethodPost, path: "/images/create", code: resp.StatusCode, message: string(bytes.TrimSpace(msg))}
	}
	return drainPullStream(resp.Body)
}

// drainPullStream reads until EOF and surfaces error/errorDetail objects; HTTP success alone does not complete a
// pull.
func drainPullStream(r io.Reader) error {
	dec := json.NewDecoder(r)
	for {
		var msg struct {
			Error       string `json:"error"`
			ErrorDetail struct {
				Message string `json:"message"`
			} `json:"errorDetail"`
		}
		if err := dec.Decode(&msg); err != nil {
			if errors.Is(err, io.EOF) {
				return nil
			}
			return fmt.Errorf("containerdriver: decode image pull stream: %w", err)
		}
		if msg.Error != "" || msg.ErrorDetail.Message != "" {
			detail := msg.Error
			if detail == "" {
				detail = msg.ErrorDetail.Message
			}
			return fmt.Errorf("containerdriver: image pull failed: %s", detail)
		}
	}
}

// Split only a tag colon after the last slash; preserve registry ports and default untagged names to latest.
// Digest refs stay whole with an empty tag because Engine pulls them through fromImage alone.
func splitImageTag(image string) (name, tag string) {
	if strings.Contains(image, "@") {
		return image, ""
	}
	lastColon := strings.LastIndex(image, ":")
	if lastColon < 0 || strings.Contains(image[lastColon:], "/") {
		return image, "latest"
	}
	return image[:lastColon], image[lastColon+1:]
}

// Inspect returns the labels and running state of the named container, used to resolve a create name conflict.
func (c *EngineClient) Inspect(ctx context.Context, name string) (ContainerInfo, error) {
	var resp struct {
		ID    string `json:"Id"`
		State struct {
			Running   bool  `json:"Running"`
			OOMKilled bool  `json:"OOMKilled"`
			ExitCode  int64 `json:"ExitCode"`
		} `json:"State"`
		Config struct {
			Labels map[string]string `json:"Labels"`
		} `json:"Config"`
	}
	if err := c.do(ctx, http.MethodGet, "/containers/"+name+"/json", nil, nil, &resp); err != nil {
		var status statusError
		if errors.As(err, &status) && status.code == http.StatusNotFound {
			// Surface a typed not-found so the driver treats the conflict as already resolved and retries the create;
			// keep the daemon message.
			return ContainerInfo{}, fmt.Errorf("%w: %v", errNotFound, err)
		}
		return ContainerInfo{}, err
	}
	return ContainerInfo{
		ID: resp.ID, Labels: resp.Config.Labels,
		Running: resp.State.Running, OOMKilled: resp.State.OOMKilled, ExitCode: resp.State.ExitCode,
	}, nil
}

// Start starts a created container.
func (c *EngineClient) Start(ctx context.Context, id string) error {
	return c.do(ctx, http.MethodPost, "/containers/"+id+"/start", nil, nil, nil)
}

// Stop sends SIGTERM then SIGKILL after the grace period.
// An already-stopped 304 escalates to a harmless kill after a self-exit race.
func (c *EngineClient) Stop(ctx context.Context, id string, timeout time.Duration) error {
	q := url.Values{"t": {fmt.Sprintf("%d", int(timeout.Seconds()))}}
	return c.do(ctx, http.MethodPost, "/containers/"+id+"/stop", q, nil, nil)
}

// Kill force-terminates a container.
func (c *EngineClient) Kill(ctx context.Context, id string) error {
	return c.do(ctx, http.MethodPost, "/containers/"+id+"/kill", nil, nil, nil)
}

// Wait blocks until the container exits and returns its exit code.
func (c *EngineClient) Wait(ctx context.Context, id string) (int64, error) {
	var resp struct {
		StatusCode int64 `json:"StatusCode"`
	}
	if err := c.do(ctx, http.MethodPost, "/containers/"+id+"/wait", nil, nil, &resp); err != nil {
		return 0, err
	}
	return resp.StatusCode, nil
}

// Remove force-deletes a container.
func (c *EngineClient) Remove(ctx context.Context, id string) error {
	q := url.Values{"force": {"true"}}
	if err := c.do(ctx, http.MethodDelete, "/containers/"+id, q, nil, nil); err != nil {
		var status statusError
		if errors.As(err, &status) && status.code == http.StatusConflict {
			// force=true makes DELETE 409 mean removal is already in progress; without force it can also mean running.
			return fmt.Errorf("%w: %v", errRemovalInProgress, err)
		}
		return err
	}
	return nil
}

// List returns containers (including stopped) carrying labelKey=labelValue.
func (c *EngineClient) List(ctx context.Context, labelKey, labelValue string) ([]Container, error) {
	filters, err := json.Marshal(map[string][]string{
		"label": {labelKey + "=" + labelValue},
	})
	if err != nil {
		return nil, fmt.Errorf("containerdriver: marshal list filter: %w", err)
	}
	q := url.Values{"all": {"true"}, "filters": {string(filters)}}

	var raw []struct {
		ID     string            `json:"Id"`
		Names  []string          `json:"Names"`
		State  string            `json:"State"`
		Labels map[string]string `json:"Labels"`
	}
	if err := c.do(ctx, http.MethodGet, "/containers/json", q, nil, &raw); err != nil {
		return nil, err
	}
	out := make([]Container, 0, len(raw))
	for _, r := range raw {
		name := ""
		if len(r.Names) > 0 {
			name = r.Names[0]
		}
		out = append(out, Container{ID: r.ID, Name: name, State: r.State, Labels: r.Labels})
	}
	return out, nil
}

// Logs opens a following stdout+stderr log stream for a running container
// (FR-MON-2). The container has no TTY, so the body carries Docker's multiplexed
// stream frames; demuxReader (logdemux.go) splits them back into stdout/stderr.
// Closing the returned reader ends the follow.
func (c *EngineClient) Logs(ctx context.Context, id string) (io.ReadCloser, error) {
	q := url.Values{
		"follow":     {"true"},
		"stdout":     {"true"},
		"stderr":     {"true"},
		"timestamps": {"false"},
	}
	u := "http://docker/" + apiVersion + "/containers/" + id + "/logs?" + q.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, fmt.Errorf("containerdriver: build logs request: %w", err)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("containerdriver: GET logs: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		_ = resp.Body.Close()
		return nil, fmt.Errorf("containerdriver: GET logs: status %d: %s", resp.StatusCode, bytes.TrimSpace(msg))
	}
	return resp.Body, nil
}

// Stats requests two daemon samples for a recent CPU delta; one-shot samples measure lifetime usage instead.
func (c *EngineClient) Stats(ctx context.Context, id string) (ContainerStats, error) {
	var raw statsResponse
	q := url.Values{"stream": {"false"}}
	if err := c.do(ctx, http.MethodGet, "/containers/"+id+"/stats", q, nil, &raw); err != nil {
		return ContainerStats{}, err
	}
	return raw.toStats(), nil
}

// statsResponse is the subset of the Engine stats document the driver reads.
type statsResponse struct {
	CPUStats struct {
		CPUUsage struct {
			TotalUsage uint64 `json:"total_usage"`
		} `json:"cpu_usage"`
		SystemCPUUsage uint64 `json:"system_cpu_usage"`
		OnlineCPUs     uint32 `json:"online_cpus"`
	} `json:"cpu_stats"`
	PreCPUStats struct {
		CPUUsage struct {
			TotalUsage uint64 `json:"total_usage"`
		} `json:"cpu_usage"`
		SystemCPUUsage uint64 `json:"system_cpu_usage"`
	} `json:"precpu_stats"`
	MemoryStats struct {
		Usage uint64 `json:"usage"`
	} `json:"memory_stats"`
}

// toStats converts the raw stats document to ContainerStats, computing CPU in
// thousandths of a core from the cpu/precpu deltas (the formula `docker stats`
// uses). A zero or negative system delta yields cpu_millis=0.
func (r statsResponse) toStats() ContainerStats {
	cpuDelta := float64(r.CPUStats.CPUUsage.TotalUsage) - float64(r.PreCPUStats.CPUUsage.TotalUsage)
	systemDelta := float64(r.CPUStats.SystemCPUUsage) - float64(r.PreCPUStats.SystemCPUUsage)
	var cpuMillis uint32
	if systemDelta > 0 && cpuDelta > 0 {
		cores := r.CPUStats.OnlineCPUs
		if cores == 0 {
			cores = 1
		}
		cpuMillis = uint32((cpuDelta / systemDelta) * float64(cores) * 1000)
	}
	return ContainerStats{CPUMillis: cpuMillis, MemoryBytes: r.MemoryStats.Usage}
}

// do performs one Engine API request. body, when non-nil, is JSON-encoded; out,
// when non-nil, is the JSON-decoded response. A non-2xx status is an error
// carrying the daemon's message.
func (c *EngineClient) do(ctx context.Context, method, path string, query url.Values, body, out any) error {
	var reqBody io.Reader
	if body != nil {
		buf, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("containerdriver: marshal request: %w", err)
		}
		reqBody = bytes.NewReader(buf)
	}

	// The host is ignored for a unix socket but required to form a valid URL.
	u := "http://docker/" + apiVersion + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, u, reqBody)
	if err != nil {
		return fmt.Errorf("containerdriver: build request: %w", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("containerdriver: %s %s: %w", method, path, err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return statusError{method: method, path: path, code: resp.StatusCode, message: string(bytes.TrimSpace(msg))}
	}

	if out != nil {
		if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
			return fmt.Errorf("containerdriver: decode response: %w", err)
		}
	}
	return nil
}

// ensure EngineClient satisfies the seam.
var _ dockerAPI = (*EngineClient)(nil)
