package containerdriver

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"path/filepath"
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
			{"Id": "a", "Names": []string{"/mcsd-s1"}},
		})
	})
	c := d.client(t)

	got, err := c.List(context.Background(), labelWorkerID, "w1")
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(got) != 1 || got[0].ID != "a" || got[0].Name != "/mcsd-s1" {
		t.Fatalf("List = %v", got)
	}
	req := d.requests[0]
	if req.method != http.MethodGet || req.path != "/v1.43/containers/json" {
		t.Fatalf("request = %s %s", req.method, req.path)
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
