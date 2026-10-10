package containerdriver

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/javaproperties"
)

// errNameConflict marks Engine create 409 and permits bounded deterministic-name recovery.
var errNameConflict = errors.New("containerdriver: container name already in use")

// errNotFound marks Inspect 404; a conflicting name may have been removed before inspection.
var errNotFound = errors.New("containerdriver: container not found")

// errRemovalInProgress marks forced-remove 409; conflict recovery keeps polling while teardown finishes.
var errRemovalInProgress = errors.New("containerdriver: container removal already in progress")

// Container label keys. The worker-id label scopes the startup orphan sweep to
// this Worker's containers; the server-id label identifies which server a
// container runs.
const (
	labelWorkerID = "mcsd.worker.id"
	labelServerID = "mcsd.server.id"
	// Store Minecraft version on the container so orphan recovery can decode its RCON password after Worker state
	// is lost.
	labelMCVersion = "mcsd.mc.version"
)

// containerNamePrefix prefixes every container name so the deterministic name is
// recognisable and collision-free with non-Worker containers.
const containerNamePrefix = "mcsd-"

// containerName is the deterministic container name for a server id.
func containerName(serverID string) string {
	return containerNamePrefix + serverID
}

// Keep install names distinct from launch names; both carry the Worker label for orphan sweep.
func installContainerName(serverID string) string {
	return containerNamePrefix + serverID + "-install"
}

// dockerAPI is the narrow Docker Engine seam the driver needs. The real adapter
// (dockerclient.go) speaks the Engine API over the unix socket; tests substitute
// a fake. Keeping the surface to the handful of endpoints the lifecycle uses
// keeps the hand-rolled client small.
type dockerAPI interface {
	// Create creates a container from spec and returns its id.
	Create(ctx context.Context, spec CreateSpec) (string, error)
	// ImagePull pulls the image ref ("name:tag") from its registry, draining the Engine's progress stream to
	// completion. It errors when the daemon rejects the request outright OR when the progress stream ends on an
	// error message (an offline host, a denied or unknown image). The driver runs it once on an image-missing
	// create failure, then retries the create.
	ImagePull(ctx context.Context, image string) error
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
	// Inspect returns the labels and running state of the container with the given name (the deterministic
	// mcsd-<server-id> name). It is used to resolve a create name conflict: the driver only removes the conflicting
	// container when it carries this Worker's label and is not running. A container that no longer exists returns
	// an error.
	Inspect(ctx context.Context, name string) (ContainerInfo, error)
	// List returns the containers carrying the given label key/value pair, including stopped ones, each with its
	// Engine container State.
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

// CreateSpec describes a container to create. Only the fields the driver sets are modelled. Memory is enforced
// as a hard container limit; CPU is a soft per-server relative share; disk limits remain deferred.
type CreateSpec struct {
	Name       string
	Image      string
	Cmd        []string
	WorkingDir string
	// Binds are host:container bind-mount specs.
	Binds []string
	// Ports are the container→host port publications.
	Ports []PortMapping
	// Network is the user-defined Docker network the container attaches to. Empty leaves the container on the
	// default bridge.
	Network string
	// Labels are attached for identification and the orphan sweep.
	Labels map[string]string
	// MemoryLimitBytes caps total container memory; zero leaves it unconstrained.
	// The derived JVM heap reserves headroom below this ceiling.
	MemoryLimitBytes int64
	// CPUShares is the container's relative CPU weight, derived from InstanceSpec.CPUMillis (1024 shares = 1 core).
	// It is a SOFT share that only arbitrates contention, never a hard quota, so MC tick latency is not throttled.
	// An unset allocation (CPUMillis == 0) falls back to the fixed default weight.
	CPUShares int64
	// User is the numeric "uid:gid" the container's process runs as, and CapDrop the capabilities removed from its
	// default set. The driver sets both on every container it creates (Driver.createServerContainer); empty leaves
	// the Engine defaults (the image's user, the full default capability set).
	User    string
	CapDrop []string
}

// PortMapping publishes a container TCP port on a host interface/port.
type PortMapping struct {
	ContainerPort string
	HostIP        string
	HostPort      string
}

// Container is a listed container: its id, name, and state, used by the orphan sweep. State is the Engine's
// container state string ("running", "exited", "created",...); the sweep gracefully stops a "running" orphan
// before removing it so the MC server's SIGTERM shutdown hook can save.
type Container struct {
	ID    string
	Name  string
	State string
	// Labels are the container's labels, the ones labels attached at create. The sweep reads the Minecraft version
	// from them.
	Labels map[string]string
}

// ContainerInfo carries identity, ownership labels, and liveness for conflict recovery.
// Install failure diagnosis also uses the daemon OOM flag and recorded exit code.
type ContainerInfo struct {
	ID        string
	Labels    map[string]string
	Running   bool
	OOMKilled bool
	ExitCode  int64
}

// Use Java properties grammar; absent files yield defaults, other read errors fail to avoid publishing a wrong
// port.
func readProperties(path string) (map[string]string, error) {
	data, err := os.ReadFile(path) //nolint:gosec // path is the server's own working dir, not user-controlled.
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return map[string]string{}, nil
		}
		return nil, fmt.Errorf("containerdriver: read %s: %w", path, err)
	}
	return javaproperties.Parse(data), nil
}
