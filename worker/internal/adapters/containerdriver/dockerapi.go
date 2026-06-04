package containerdriver

import (
	"bufio"
	"context"
	"os"
	"strings"
	"time"
)

// Container label keys. The worker-id label scopes the startup orphan sweep to
// this Worker's containers; the server-id label identifies which server a
// container runs.
const (
	labelWorkerID = "mcsd.worker.id"
	labelServerID = "mcsd.server.id"
)

// containerNamePrefix prefixes every container name so the deterministic name is
// recognisable and collision-free with non-Worker containers.
const containerNamePrefix = "mcsd-"

// containerName is the deterministic container name for a server id.
func containerName(serverID string) string {
	return containerNamePrefix + serverID
}

// dockerAPI is the narrow Docker Engine seam the driver needs. The real adapter
// (dockerclient.go) speaks the Engine API over the unix socket; tests substitute
// a fake. Keeping the surface to the handful of endpoints the lifecycle uses
// keeps the hand-rolled client small.
type dockerAPI interface {
	// Create creates a container from spec and returns its id.
	Create(ctx context.Context, spec CreateSpec) (string, error)
	// Start starts a created container.
	Start(ctx context.Context, id string) error
	// Stop sends SIGTERM and, after timeout, SIGKILL (the `docker stop` semantics).
	Stop(ctx context.Context, id string, timeout time.Duration) error
	// Kill force-terminates a container (SIGKILL).
	Kill(ctx context.Context, id string) error
	// Wait blocks until the container exits and returns its exit code.
	Wait(ctx context.Context, id string) (int64, error)
	// Remove deletes the container (force).
	Remove(ctx context.Context, id string) error
	// List returns the containers carrying the given label key/value pair,
	// including stopped ones.
	List(ctx context.Context, labelKey, labelValue string) ([]Container, error)
}

// CreateSpec describes a container to create. Only the fields the driver sets are
// modelled; resource limits are deferred to M2+.
type CreateSpec struct {
	Name       string
	Image      string
	Cmd        []string
	WorkingDir string
	// Binds are host:container bind-mount specs.
	Binds []string
	// Ports are the container→host port publications.
	Ports []PortMapping
	// Labels are attached for identification and the orphan sweep.
	Labels map[string]string
}

// PortMapping publishes a container TCP port on a host interface/port.
type PortMapping struct {
	ContainerPort string
	HostIP        string
	HostPort      string
}

// Container is a listed container: its id and name, used by the orphan sweep.
type Container struct {
	ID   string
	Name string
}

// readProperties parses a Java .properties file into a map, returning an empty
// map when the file is absent or unreadable (the caller then uses defaults).
// Lines that are blank or comments (# or !) are skipped.
func readProperties(path string) map[string]string {
	out := map[string]string{}
	f, err := os.Open(path) //nolint:gosec // path is the server's own working dir, not user-controlled.
	if err != nil {
		return out
	}
	defer func() { _ = f.Close() }()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "!") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		out[strings.TrimSpace(key)] = strings.TrimSpace(value)
	}
	return out
}
