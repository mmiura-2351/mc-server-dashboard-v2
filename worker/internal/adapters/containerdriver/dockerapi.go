package containerdriver

import (
	"bufio"
	"context"
	"errors"
	"io"
	"os"
	"strings"
	"time"
)

// errNameConflict is the error Create returns when the daemon answers 409
// Conflict because the deterministic container name is already in use. The
// driver matches it with errors.Is to drive the remove-on-conflict retry (issue
// #226); any other Create failure is returned unwrapped and never triggers the
// retry.
var errNameConflict = errors.New("containerdriver: container name already in use")

// errNotFound is the error Inspect returns when the daemon answers 404 because
// the named container no longer exists. During conflict resolution the driver
// matches it with errors.Is to mean the conflict is already resolved — the async
// exit-watcher removed the container between the create's 409 and the inspect —
// so it retries the create directly instead of taking the conservative fallback
// (issue #229).
var errNotFound = errors.New("containerdriver: container not found")

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
	// Inspect returns the labels and running state of the container with the given
	// name (the deterministic mcsd-<server-id> name). It is used to resolve a
	// create name conflict (issue #226): the driver only removes the conflicting
	// container when it carries this Worker's label and is not running. A container
	// that no longer exists returns an error.
	Inspect(ctx context.Context, name string) (ContainerInfo, error)
	// List returns the containers carrying the given label key/value pair,
	// including stopped ones.
	List(ctx context.Context, labelKey, labelValue string) ([]Container, error)
	// Logs opens a following stdout+stderr log stream for a running container
	// (FR-MON-2). The returned reader carries Docker's multiplexed stream frames
	// (non-TTY); the caller demuxes them. Closing the reader ends the follow.
	Logs(ctx context.Context, id string) (io.ReadCloser, error)
	// Stats reads a one-shot resource sample for a running container (FR-MON-3).
	Stats(ctx context.Context, id string) (ContainerStats, error)
}

// ContainerStats is a one-shot resource sample from the Engine stats endpoint
// (FR-MON-3). Fields the daemon does not report are zero.
type ContainerStats struct {
	// CPUMillis is CPU usage in thousandths of a core, derived from the cpu/
	// precpu deltas the stats endpoint reports.
	CPUMillis uint32
	// MemoryBytes is the container's resident memory usage.
	MemoryBytes uint64
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
	// Network is the user-defined Docker network the container attaches to. Empty
	// leaves the container on the default bridge (issue #218).
	Network string
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

// ContainerInfo is the subset of a container inspection the driver needs to
// resolve a create name conflict (issue #226): the id to remove it, the labels
// to confirm it is this Worker's, and whether it is running.
type ContainerInfo struct {
	ID      string
	Labels  map[string]string
	Running bool
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
