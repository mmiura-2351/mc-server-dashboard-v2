package containerdriver

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

// fakeDaemon serves the Docker Engine API over a unix socket so the EngineClient
// is exercised end to end (request encoding, query params, response decoding)
// without a real Docker daemon. It records the requests it sees.
type fakeDaemon struct {
	socket   string
	server   *http.Server
	requests []recordedRequest
}

type recordedRequest struct {
	method string
	path   string
	query  string
	body   string
}

func startFakeDaemon(t *testing.T, handler http.HandlerFunc) *fakeDaemon {
	t.Helper()
	socket := filepath.Join(t.TempDir(), "docker.sock")
	ln, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatalf("listen unix: %v", err)
	}

	d := &fakeDaemon{socket: socket}
	d.server = &http.Server{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			body, _ := io.ReadAll(r.Body)
			d.requests = append(d.requests, recordedRequest{
				method: r.Method,
				path:   r.URL.Path,
				query:  r.URL.RawQuery,
				body:   string(body),
			})
			handler(w, r)
		}),
		ReadHeaderTimeout: time.Second,
	}
	go func() { _ = d.server.Serve(ln) }()
	t.Cleanup(func() { _ = d.server.Close() })
	return d
}

func (d *fakeDaemon) client(t *testing.T) *EngineClient {
	t.Helper()
	c, err := NewEngineClient("unix://" + d.socket)
	if err != nil {
		t.Fatalf("NewEngineClient: %v", err)
	}
	return c
}

func TestEngineClientCreateEncodesSpec(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"Id": "abc123"})
	})
	c := d.client(t)

	id, err := c.Create(context.Background(), CreateSpec{
		Name:       "mcsd-s1",
		Image:      "eclipse-temurin:21-jre",
		Cmd:        []string{"java", "-jar", "/data/server.jar", "nogui"},
		WorkingDir: "/data",
		Binds:      []string{"/scratch/s1:/data"},
		Ports:      []PortMapping{{ContainerPort: "25565", HostIP: "127.0.0.1", HostPort: "25565"}},
		Labels:     map[string]string{labelWorkerID: "w1"},
	})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if id != "abc123" {
		t.Fatalf("id = %q, want abc123", id)
	}

	req := d.requests[0]
	if req.method != http.MethodPost || req.path != "/v1.43/containers/create" {
		t.Fatalf("request = %s %s", req.method, req.path)
	}
	if req.query != "name=mcsd-s1" {
		t.Fatalf("query = %q, want name=mcsd-s1", req.query)
	}

	var body createBody
	if err := json.Unmarshal([]byte(req.body), &body); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if body.Image != "eclipse-temurin:21-jre" {
		t.Fatalf("Image = %q", body.Image)
	}
	if len(body.HostConfig.Binds) != 1 || body.HostConfig.Binds[0] != "/scratch/s1:/data" {
		t.Fatalf("Binds = %v", body.HostConfig.Binds)
	}
	pb, ok := body.HostConfig.PortBindings["25565/tcp"]
	if !ok || len(pb) != 1 || pb[0].HostPort != "25565" || pb[0].HostIP != "127.0.0.1" {
		t.Fatalf("PortBindings = %v", body.HostConfig.PortBindings)
	}
}

// A non-zero memory ceiling is encoded as the host-config Memory field (in bytes)
// so the daemon caps the container at that hard limit; a zero ceiling omits it,
// leaving the container unconstrained (issue #707).
func TestEngineClientCreateEncodesMemory(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"Id": "abc123"})
	})
	c := d.client(t)

	const wantBytes = int64(2048) * 1024 * 1024
	if _, err := c.Create(context.Background(), CreateSpec{
		Name:             "mcsd-s1",
		Image:            "img",
		MemoryLimitBytes: wantBytes,
	}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	var body createBody
	if err := json.Unmarshal([]byte(d.requests[0].body), &body); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if body.HostConfig.Memory != wantBytes {
		t.Fatalf("HostConfig.Memory = %d, want %d", body.HostConfig.Memory, wantBytes)
	}
}

// A zero memory ceiling omits the Memory field from the wire payload (Memory has
// the omitempty tag), so the container runs unconstrained (issue #707).
func TestEngineClientCreateOmitsMemoryWhenZero(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"Id": "abc123"})
	})
	c := d.client(t)

	if _, err := c.Create(context.Background(), CreateSpec{Name: "mcsd-s1", Image: "img"}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if strings.Contains(d.requests[0].body, "\"Memory\"") {
		t.Fatalf("body carries Memory, want it omitted: %s", d.requests[0].body)
	}
}

// A configured network is encoded as a NetworkingConfig endpoint so the daemon
// attaches the container to that user-defined network at create time (issue
// #218). An empty network omits it, keeping the default bridge.
func TestEngineClientCreateEncodesNetwork(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"Id": "abc123"})
	})
	c := d.client(t)

	if _, err := c.Create(context.Background(), CreateSpec{
		Name:    "mcsd-s1",
		Image:   "eclipse-temurin:21-jre",
		Network: "mcsd",
	}); err != nil {
		t.Fatalf("Create: %v", err)
	}

	var body createBody
	if err := json.Unmarshal([]byte(d.requests[0].body), &body); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if body.NetworkingConfig == nil {
		t.Fatal("NetworkingConfig = nil, want mcsd endpoint")
	}
	if _, ok := body.NetworkingConfig.EndpointsConfig["mcsd"]; !ok {
		t.Fatalf("EndpointsConfig = %v, want an mcsd endpoint", body.NetworkingConfig.EndpointsConfig)
	}
}

// An empty network omits NetworkingConfig entirely, preserving the default-bridge
// behavior.
func TestEngineClientCreateOmitsNetworkWhenEmpty(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"Id": "abc123"})
	})
	c := d.client(t)

	if _, err := c.Create(context.Background(), CreateSpec{Name: "mcsd-s1", Image: "img"}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	var body createBody
	if err := json.Unmarshal([]byte(d.requests[0].body), &body); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if body.NetworkingConfig != nil {
		t.Fatalf("NetworkingConfig = %v, want nil when no network configured", body.NetworkingConfig)
	}
}

// The CreateSpec's per-server CPU weight rides through to the host-config
// CpuShares field, the relative weight the Engine translates to cpu.weight on
// cgroup v2 (issues #518/#724). It is a soft share, never a hard quota: no
// NanoCpus (or any hard CPU cap) is encoded.
func TestEngineClientCreateEncodesCPUShares(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"Id": "abc123"})
	})
	c := d.client(t)

	const wantShares = int64(2048)
	if _, err := c.Create(context.Background(), CreateSpec{
		Name:      "mcsd-s1",
		Image:     "img",
		CPUShares: wantShares,
	}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	var body createBody
	if err := json.Unmarshal([]byte(d.requests[0].body), &body); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if body.HostConfig.CPUShares != wantShares {
		t.Fatalf("CPUShares = %d, want %d", body.HostConfig.CPUShares, wantShares)
	}
	if strings.Contains(d.requests[0].body, "NanoCpus") {
		t.Fatalf("body carries a hard CPU quota (NanoCpus), want only soft CpuShares: %s", d.requests[0].body)
	}
}

func TestEngineClientWaitDecodesStatusCode(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]int{"StatusCode": 137})
	})
	c := d.client(t)

	code, err := c.Wait(context.Background(), "abc123")
	if err != nil {
		t.Fatalf("Wait: %v", err)
	}
	if code != 137 {
		t.Fatalf("code = %d, want 137", code)
	}
}

func TestEngineClientListFiltersByLabel(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode([]map[string]any{
			{"Id": "a", "Names": []string{"/mcsd-s1"}, "State": "running"},
		})
	})
	c := d.client(t)

	got, err := c.List(context.Background(), labelWorkerID, "w1")
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(got) != 1 || got[0].ID != "a" || got[0].Name != "/mcsd-s1" || got[0].State != "running" {
		t.Fatalf("List = %v", got)
	}
	req := d.requests[0]
	if req.method != http.MethodGet || req.path != "/v1.43/containers/json" {
		t.Fatalf("request = %s %s", req.method, req.path)
	}
	q, err := url.ParseQuery(req.query)
	if err != nil {
		t.Fatalf("parse query %q: %v", req.query, err)
	}
	if q.Get("all") != "true" {
		t.Fatalf("all = %q, want true", q.Get("all"))
	}
	var filters map[string][]string
	if err := json.Unmarshal([]byte(q.Get("filters")), &filters); err != nil {
		t.Fatalf("decode filters %q: %v", q.Get("filters"), err)
	}
	want := map[string][]string{"label": {labelWorkerID + "=w1"}}
	if !reflect.DeepEqual(filters, want) {
		t.Fatalf("filters = %v, want %v", filters, want)
	}
}

func TestEngineClientStatsComputesCPU(t *testing.T) {
	// cpuDelta=2_000_000_000, systemDelta=8_000_000_000, 2 cpus →
	// (0.25) * 2 * 1000 = 500 millis.
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{
			"cpu_stats": {"cpu_usage": {"total_usage": 3000000000}, "system_cpu_usage": 10000000000, "online_cpus": 2},
			"precpu_stats": {"cpu_usage": {"total_usage": 1000000000}, "system_cpu_usage": 2000000000},
			"memory_stats": {"usage": 1048576}
		}`))
	})
	c := d.client(t)

	stats, err := c.Stats(context.Background(), "abc123")
	if err != nil {
		t.Fatalf("Stats: %v", err)
	}
	if stats.CPUMillis != 500 {
		t.Fatalf("CPUMillis = %d, want 500", stats.CPUMillis)
	}
	if stats.MemoryBytes != 1048576 {
		t.Fatalf("MemoryBytes = %d, want 1048576", stats.MemoryBytes)
	}
	req := d.requests[0]
	if req.method != http.MethodGet || req.path != "/v1.43/containers/abc123/stats" {
		t.Fatalf("request = %s %s", req.method, req.path)
	}
	q, err := url.ParseQuery(req.query)
	if err != nil {
		t.Fatalf("parse query: %v", err)
	}
	if q.Get("stream") != "false" {
		t.Fatalf("stream = %q, want false", q.Get("stream"))
	}
	// one-shot must NOT be set: without it the daemon collects two internal
	// samples and returns meaningful precpu_stats; with it the daemon returns
	// immediately with precpu_stats zeroed, making the CPU delta cover the
	// entire container lifetime and truncating to 0 on a long-running host
	// (issue #1068).
	if q.Get("one-shot") != "" {
		t.Fatalf("one-shot = %q, want absent", q.Get("one-shot"))
	}
}

func TestEngineClientLogsStreamsBody(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("multiplexed-bytes"))
	})
	c := d.client(t)

	rc, err := c.Logs(context.Background(), "abc123")
	if err != nil {
		t.Fatalf("Logs: %v", err)
	}
	defer func() { _ = rc.Close() }()
	body, err := io.ReadAll(rc)
	if err != nil {
		t.Fatalf("read logs: %v", err)
	}
	if string(body) != "multiplexed-bytes" {
		t.Fatalf("body = %q", body)
	}
	req := d.requests[0]
	if req.method != http.MethodGet || req.path != "/v1.43/containers/abc123/logs" {
		t.Fatalf("request = %s %s", req.method, req.path)
	}
	q, err := url.ParseQuery(req.query)
	if err != nil {
		t.Fatalf("parse query: %v", err)
	}
	if q.Get("follow") != "true" || q.Get("stdout") != "true" || q.Get("stderr") != "true" {
		t.Fatalf("query = %q", req.query)
	}
}

func TestEngineClientNonUnixHostRejected(t *testing.T) {
	if _, err := NewEngineClient("tcp://127.0.0.1:2375"); err == nil {
		t.Fatal("expected NewEngineClient to reject a non-unix host")
	}
}

func TestEngineClientSurfacesDaemonError(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"message":"no such container"}`))
	})
	c := d.client(t)

	if err := c.Start(context.Background(), "missing"); err == nil {
		t.Fatal("expected an error on a non-2xx daemon response")
	}
}

// A 409 from /containers/create is surfaced as errNameConflict so the driver can
// run its remove-on-conflict retry (issue #226).
func TestEngineClientCreateConflictIsTyped(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"message":"Conflict. The container name \"/mcsd-s1\" is already in use"}`))
	})
	c := d.client(t)

	_, err := c.Create(context.Background(), CreateSpec{Name: "mcsd-s1", Image: "img"})
	if !errors.Is(err, errNameConflict) {
		t.Fatalf("Create err = %v, want errNameConflict", err)
	}
}

// A non-409 create failure is not reported as a name conflict.
func TestEngineClientCreateNonConflictIsNotTyped(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"message":"boom"}`))
	})
	c := d.client(t)

	_, err := c.Create(context.Background(), CreateSpec{Name: "mcsd-s1", Image: "img"})
	if err == nil {
		t.Fatal("expected Create to fail")
	}
	if errors.Is(err, errNameConflict) {
		t.Fatalf("Create err = %v, want a plain error (not a name conflict)", err)
	}
}

// ImagePull splits the image ref into fromImage/tag, drains the progress stream
// to completion, and returns nil when the stream ends without an error (issue
// #904).
func TestEngineClientImagePullDrainsStream(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"status":"Pulling from library/eclipse-temurin"}` + "\n"))
		_, _ = w.Write([]byte(`{"status":"Download complete"}` + "\n"))
	})
	c := d.client(t)

	if err := c.ImagePull(context.Background(), "eclipse-temurin:21-jre"); err != nil {
		t.Fatalf("ImagePull: %v", err)
	}
	req := d.requests[0]
	if req.method != http.MethodPost || req.path != "/v1.43/images/create" {
		t.Fatalf("request = %s %s", req.method, req.path)
	}
	q, err := url.ParseQuery(req.query)
	if err != nil {
		t.Fatalf("parse query %q: %v", req.query, err)
	}
	if q.Get("fromImage") != "eclipse-temurin" || q.Get("tag") != "21-jre" {
		t.Fatalf("query = %q, want fromImage=eclipse-temurin&tag=21-jre", req.query)
	}
}

// A pull whose progress stream ends on an error object (an offline host, a denied
// or unknown image) fails even though the HTTP status was 200: the Engine returns
// 200 then reports the failure in the stream (issue #904).
func TestEngineClientImagePullStreamErrorFails(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"status":"Pulling from library/eclipse-temurin"}` + "\n"))
		_, _ = w.Write([]byte(`{"errorDetail":{"message":"pull access denied"},"error":"pull access denied"}` + "\n"))
	})
	c := d.client(t)

	err := c.ImagePull(context.Background(), "eclipse-temurin:21-jre")
	if err == nil {
		t.Fatal("expected ImagePull to fail on an in-stream error")
	}
	if !strings.Contains(err.Error(), "pull access denied") {
		t.Fatalf("ImagePull err = %v, want it to carry the daemon's pull error", err)
	}
}

// A non-2xx response (the daemon rejecting the request outright) fails the pull
// directly (issue #904).
func TestEngineClientImagePullNon2xxFails(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"message":"boom"}`))
	})
	c := d.client(t)

	if err := c.ImagePull(context.Background(), "img"); err == nil {
		t.Fatal("expected ImagePull to fail on a non-2xx daemon response")
	}
}

// splitImageTag splits on the tag colon, defaulting to latest when none is given
// and leaving a registry host:port (which precedes a "/") untagged (issue #904). A
// digest-pinned ref (name@sha256:...) stays whole as the fromImage with no tag, so
// the Engine pulls by digest rather than choking on a "tag" of the hex digest
// (issue #915).
func TestSplitImageTag(t *testing.T) {
	cases := []struct {
		image, name, tag string
	}{
		{"eclipse-temurin:21-jre", "eclipse-temurin", "21-jre"},
		{"azul/zulu-openjdk:7", "azul/zulu-openjdk", "7"},
		{"img", "img", "latest"},
		{"registry:5000/img", "registry:5000/img", "latest"},
		{"registry:5000/img:1.21", "registry:5000/img", "1.21"},
		{"img@sha256:abc123", "img@sha256:abc123", ""},
		{"eclipse-temurin@sha256:abc123", "eclipse-temurin@sha256:abc123", ""},
		{"registry:5000/img@sha256:abc123", "registry:5000/img@sha256:abc123", ""},
	}
	for _, tc := range cases {
		name, tag := splitImageTag(tc.image)
		if name != tc.name || tag != tc.tag {
			t.Errorf("splitImageTag(%q) = (%q, %q), want (%q, %q)", tc.image, name, tag, tc.name, tc.tag)
		}
	}
}

// ImagePull passes a digest-pinned ref as fromImage with no tag param, the Engine
// /images/create contract for pulling by digest; splitting it on the last colon
// would send tag=<hex digest>, which the Engine rejects, so lazy pull would never
// succeed for a digest-pinned base image (issue #915).
func TestEngineClientImagePullByDigest(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"status":"Download complete"}` + "\n"))
	})
	c := d.client(t)

	const ref = "eclipse-temurin@sha256:abc123"
	if err := c.ImagePull(context.Background(), ref); err != nil {
		t.Fatalf("ImagePull: %v", err)
	}
	q, err := url.ParseQuery(d.requests[0].query)
	if err != nil {
		t.Fatalf("parse query %q: %v", d.requests[0].query, err)
	}
	if q.Get("fromImage") != ref {
		t.Fatalf("fromImage = %q, want %q", q.Get("fromImage"), ref)
	}
	if _, ok := q["tag"]; ok {
		t.Fatalf("tag = %q, want no tag param for a digest pull", q.Get("tag"))
	}
}

// Inspect decodes the container id, labels, and running state used to resolve a
// create name conflict (issue #226).
func TestEngineClientInspectDecodesLabelsAndState(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{
			"Id": "abc123",
			"State": {"Running": false},
			"Config": {"Labels": {"mcsd.worker.id": "w1", "mcsd.server.id": "s1"}}
		}`))
	})
	c := d.client(t)

	info, err := c.Inspect(context.Background(), "mcsd-s1")
	if err != nil {
		t.Fatalf("Inspect: %v", err)
	}
	if info.ID != "abc123" || info.Running {
		t.Fatalf("info = %+v, want id abc123, not running", info)
	}
	if info.Labels[labelWorkerID] != "w1" || info.Labels[labelServerID] != "s1" {
		t.Fatalf("Labels = %v", info.Labels)
	}
	req := d.requests[0]
	if req.method != http.MethodGet || req.path != "/v1.43/containers/mcsd-s1/json" {
		t.Fatalf("request = %s %s", req.method, req.path)
	}
}

// A 404 from Inspect is surfaced as errNotFound so the driver treats the
// conflict as already resolved and retries the create (issue #229).
func TestEngineClientInspectNotFoundIsTyped(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"message":"No such container: mcsd-s1"}`))
	})
	c := d.client(t)

	_, err := c.Inspect(context.Background(), "mcsd-s1")
	if !errors.Is(err, errNotFound) {
		t.Fatalf("Inspect err = %v, want errNotFound", err)
	}
}

// A non-404 Inspect failure is not reported as not-found, so the driver keeps the
// conservative fallback (issue #229).
func TestEngineClientInspectNonNotFoundIsNotTyped(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"message":"boom"}`))
	})
	c := d.client(t)

	_, err := c.Inspect(context.Background(), "mcsd-s1")
	if err == nil {
		t.Fatal("expected Inspect to fail")
	}
	if errors.Is(err, errNotFound) {
		t.Fatalf("Inspect err = %v, want a plain error (not errNotFound)", err)
	}
}

// A 409 from Remove ("removal already in progress") is surfaced as
// errRemovalInProgress so the wait-for-name-free loop treats the in-flight
// removal as progress and keeps polling (issue #233).
func TestEngineClientRemoveInProgressIsTyped(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"message":"removal of container mcsd-s1 is already in progress"}`))
	})
	c := d.client(t)

	if err := c.Remove(context.Background(), "mcsd-s1"); !errors.Is(err, errRemovalInProgress) {
		t.Fatalf("Remove err = %v, want errRemovalInProgress", err)
	}
}

// A non-409 Remove failure is not reported as removal-in-progress.
func TestEngineClientRemoveNonConflictIsNotTyped(t *testing.T) {
	d := startFakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"message":"boom"}`))
	})
	c := d.client(t)

	err := c.Remove(context.Background(), "mcsd-s1")
	if err == nil {
		t.Fatal("expected Remove to fail")
	}
	if errors.Is(err, errRemovalInProgress) {
		t.Fatalf("Remove err = %v, want a plain error (not removal-in-progress)", err)
	}
}
