//go:build e2e

// Exercise back-to-back restart against Docker with a Java shim that blocks until SIGTERM.
// This covers deterministic-name reuse and asynchronous container removal without a JVM download.
package e2e

import (
	"context"
	"errors"
	"net"
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/containerdriver"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// stubJavaMajor is the Java major the stub image is registered under in the
// ImageSelector. mcVersion is chosen to resolve to it (1.20.4 → Java 17 per
// javaruntime.MajorsFor), so Select returns the stub image.
const (
	stubJavaMajor = 17
	stubMCVersion = "1.20.4"
)

// Seed the exact marker required by launchReserved; keep its name aligned with
// scratchformat.GenerationMarkerFile.
const generationMarkerFile = ".mcsd_generation"

// seedWorkingSet materialises a working set for serverID under scratchDir: the
// caller's {working-dir-relative path: content}, plus the generation marker (above)
// without which no launch in this harness gets past instancemanager's guard.
// Content "0" matches what the unit fixtures seed — readGeneration then answers
// what an unmarked dir answered before the guard existed (a never-hydrated set).
func seedWorkingSet(t *testing.T, scratchDir, serverID string, files map[string]string) {
	t.Helper()
	tree := map[string]string{serverID + "/" + generationMarkerFile: "0"}
	for rel, content := range files {
		tree[serverID+"/"+rel] = content
	}
	writeTree(t, scratchDir, tree)
}

// errNoRCON is returned by the RCON openers so graceful stop falls back to
// `docker stop`: the stub image runs no RCON listener.
var errNoRCON = errors.New("e2e: rcon unavailable for stub")

// Require a new running container after restart so deterministic-name teardown cannot be mistaken for reuse.
func TestContainerRestartRecreatesContainer(t *testing.T) {
	if os.Getenv("MCD_E2E_DOCKER") == "" {
		t.Skip("MCD_E2E_DOCKER not set; skipping container-driver e2e (needs a Docker daemon)")
	}
	image := env(t, "MCD_E2E_STUB_IMAGE")

	// Reclaim stub containers leaked by previous harness runs that were killed by a panic or `go test -timeout`
	// before t.Cleanup could run. It runs only here, after both env gates above, so it stays inert in the plain `go
	// test./...` pass. Best-effort: a leaked orphan must not block a green run, so a reaper error is logged, not
	// fatal.
	reapCtx, reapCancel := context.WithTimeout(context.Background(), 30*time.Second)
	if err := reapStaleE2EContainers(reapCtx); err != nil {
		t.Logf("e2e reaper: %v", err)
	}
	reapCancel()

	docker, err := containerdriver.NewEngineClient("")
	if err != nil {
		t.Fatalf("docker engine client: %v", err)
	}

	// A unique worker id scopes the driver's startup sweep and orphan labels to
	// THIS harness run, so it never touches the running compose stack's
	// container-driver servers (which carry a different worker id) — the sweep
	// removes only containers labelled mcsd.worker.id=<workerID>.
	workerID := "e2e-restart-" + newServerID(t)

	// Short conflict-resolution tuning keeps the test responsive: the production
	// defaults (10s deadline) still apply by feel, but a tight poll surfaces a
	// regression promptly rather than after a long wait.
	driver := containerdriver.New(
		docker,
		containerdriver.NewImageSelector(map[int]string{stubJavaMajor: image}),
		// RCON is unavailable for the stub, so graceful stop falls back to
		// `docker stop`. Returning an error here is that fallback's trigger.
		func(context.Context, execution.InstanceSpec, string) (execution.ServerControl, error) {
			return nil, errNoRCON
		},
		containerdriver.Options{
			WorkerID:             workerID,
			StopTimeout:          5 * time.Second,
			ConflictPollInterval: 100 * time.Millisecond,
			ConflictDeadline:     10 * time.Second,
		},
	)

	scratchDir := t.TempDir()
	mgr := instancemanager.New(
		map[string]execution.ExecutionDriver{"container": driver},
		scratchDir,
		// The manager's ServerCommand RCON opener is unused in this scenario.
		func(context.Context, string, string, string) (execution.ServerControl, error) {
			return nil, errNoRCON
		},
	)
	// Close the manager before container cleanup so metrics and status pumps cannot outlive their Docker targets.
	defer mgr.Close()

	serverID := newServerID(t)
	name := "mcsd-" + serverID

	// The driver publishes the game and RCON ports on the host, defaulting to
	// Minecraft's 25565/25575 (containerdriver.ports). Those fixed ports collide
	// with any other server on the host (e.g. the running compose stack). Pre-seed
	// the working dir's server.properties with free ephemeral ports so the harness
	// container binds its own ports and never fights for the defaults.
	game, rcon := freePort(t), freePort(t)
	seedWorkingSet(t, scratchDir, serverID, map[string]string{
		"server.properties": "server-port=" + game + "\nrcon.port=" + rcon + "\n",
	})

	// Always remove the harness's own container, even on a mid-test failure, so a
	// rerun starts clean and nothing is left behind. Scoped to this server's
	// deterministic name; never touches other containers.
	t.Cleanup(func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		if info, err := docker.Inspect(cleanupCtx, name); err == nil {
			_ = docker.Remove(cleanupCtx, info.ID)
		}
	})

	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	// Start the stub "server".
	start := session.Command{
		CommandID:        "start-1",
		ServerID:         serverID,
		Kind:             "StartServer",
		Driver:           "container",
		JarRelpath:       "server.jar",
		MinecraftVersion: stubMCVersion,
	}
	if res := mgr.Handle(ctx, start); !res.Success {
		t.Fatalf("StartServer failed: code=%v msg=%s", res.ErrorCode, res.ErrorMessage)
	}

	firstInfo, err := docker.Inspect(ctx, name)
	if err != nil {
		t.Fatalf("inspect after start: %v", err)
	}
	if !firstInfo.Running {
		t.Fatalf("container %s not running after StartServer", name)
	}

	// Restart through the worker's command path. This is the race: Stop triggers
	// the async removal of firstInfo's container while Start re-creates the same
	// deterministic name.
	restart := session.Command{
		CommandID: "restart-1",
		ServerID:  serverID,
		Kind:      "RestartServer",
	}
	if res := mgr.Handle(ctx, restart); !res.Success {
		t.Fatalf("RestartServer failed: code=%v msg=%s", res.ErrorCode, res.ErrorMessage)
	}

	secondInfo, err := docker.Inspect(ctx, name)
	if err != nil {
		t.Fatalf("inspect after restart: %v", err)
	}
	if !secondInfo.Running {
		t.Fatalf("container %s not running after RestartServer", name)
	}
	if secondInfo.ID == firstInfo.ID {
		t.Fatalf("RestartServer reused the same container %s; expected a new container", firstInfo.ID)
	}

	// Stop the server to leave the daemon clean (Cleanup also force-removes).
	stop := session.Command{
		CommandID: "stop-1",
		ServerID:  serverID,
		Kind:      "StopServer",
		Force:     true,
	}
	if res := mgr.Handle(ctx, stop); !res.Success {
		t.Fatalf("StopServer failed: code=%v msg=%s", res.ErrorCode, res.ErrorMessage)
	}
}

// freePort returns a currently-free TCP port on loopback as a string. There is a
// small window between the probe and the container binding it, but the harness
// runs one server at a time with fresh ports per run, so a collision is
// improbable; a failure surfaces as a clear "port already allocated" start error.
func freePort(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("reserve free port: %v", err)
	}
	defer func() { _ = l.Close() }()
	return strconv.Itoa(l.Addr().(*net.TCPAddr).Port)
}
