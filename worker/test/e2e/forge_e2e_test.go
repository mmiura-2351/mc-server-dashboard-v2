//go:build e2e

// This file adds the container-driver FORGE supervised-install scenario
// (issue #326) to the cross-language e2e harness. Like restart_e2e_test.go it
// exercises the REAL Docker daemon through the real container ExecutionDriver and
// the real instancemanager.Manager, but it drives the install-then-launch
// sequence Forge introduced (PR #305/#306): when the Forge args file is absent
// the driver runs a supervised install container (mcsd-<id>-install) to
// completion, then creates+starts the launch container as the same instance.
//
// Why a real daemon: the install→launch handoff spans two containers, a
// supervisor goroutine, the post-install re-plan that globs the working set, and
// the install-output capture to logs/forge-install.log. The dockerAPI fake cannot
// model the bind-mounted working dir that the stub installer writes the args file
// into, nor the real two-container lifecycle. This scenario is the structural
// guard that the cross-boundary install path reaches a running launch container
// end to end.
//
// No real forge download: the stub image's `java` shim (worker/test/e2e/stub)
// branches on its argv — `--installServer` creates the version-stamped
// unix_args.txt the re-plan expects and exits 0, every other invocation blocks
// until SIGTERM like a running server.
//
// It is gated the same three ways as restart_e2e_test.go (the `e2e` build tag,
// MCD_E2E_DOCKER, and MCD_E2E_STUB_IMAGE), so it never runs in the ordinary
// `go test ./...` pass. See worker/README.md for a local run.
package e2e

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/containerdriver"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// forgeArgsfileRel is the working-dir-relative path the stub installer writes the
// Forge args file to (worker/test/e2e/stub/java). It mirrors the glob the driver
// re-plans against (execution.forgeArgsfileGlob,
// libraries/net/minecraftforge/forge/*/unix_args.txt) at a fixed stub version.
const forgeArgsfileRel = "libraries/net/minecraftforge/forge/0.0.0-stub/unix_args.txt"

// TestContainerForgeInstallThenLaunch drives a Forge args-file StartServer whose
// working set is NOT yet installed against a real Docker daemon and asserts the
// supervised install runs, produces the args file, and the instance proceeds to a
// running launch container — the install-then-launch sequence PR #305/#306 added.
//
// The install container (mcsd-<id>-install) runs the stub installer, which writes
// the args file into the bind-mounted working dir and exits 0; the driver's
// supervisor then re-plans (the args file is now present), creates+starts the
// launch container under the deterministic launch name, and removes the exited
// install container. Reaching running on the launch name, with the args file on
// disk, the install container gone, and logs/forge-install.log written, proves
// the whole cross-boundary path completed.
func TestContainerForgeInstallThenLaunch(t *testing.T) {
	if os.Getenv("MCD_E2E_DOCKER") == "" {
		t.Skip("MCD_E2E_DOCKER not set; skipping container-driver e2e (needs a Docker daemon)")
	}
	image := env(t, "MCD_E2E_STUB_IMAGE")

	// Reclaim stub containers leaked by previous harness runs killed before
	// t.Cleanup could run (issue #256). Best-effort: a leaked orphan must not block
	// a green run, so a reaper error is logged, not fatal.
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
	// THIS harness run so it never touches the live stack (whose worker id is a
	// bare UUID). The "e2e-forge-" prefix lets the cross-run reaper reclaim a
	// leaked container while still excluding the live stack (reaper.go matches the
	// shared "e2e-" prefix).
	workerID := "e2e-forge-" + newServerID(t)

	driver := containerdriver.New(
		docker,
		containerdriver.NewImageSelector(map[int]string{stubJavaMajor: image}),
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
		func(context.Context, string, string) (execution.ServerControl, error) {
			return nil, errNoRCON
		},
	)

	serverID := newServerID(t)
	launchName := "mcsd-" + serverID
	installName := launchName + "-install"
	workingDir := filepath.Join(scratchDir, serverID)

	// Seed the working dir WITHOUT the Forge args file so the launch plan needs the
	// install step. Free ephemeral ports avoid colliding with the default
	// 25565/25575 (and anything else on the host).
	game, rcon := freePort(t), freePort(t)
	writeTree(t, scratchDir, map[string]string{
		serverID + "/server.properties": "server-port=" + game + "\nrcon.port=" + rcon + "\n",
	})

	// Always remove both the harness's launch and install containers, even on a
	// mid-test failure. Scoped to this server's deterministic names; never touches
	// other containers.
	t.Cleanup(func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		for _, n := range []string{launchName, installName} {
			if info, err := docker.Inspect(cleanupCtx, n); err == nil {
				_ = docker.Remove(cleanupCtx, info.ID)
			}
		}
	})

	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	// StartServer in Forge args-file mode. Start returns once the install container
	// is started; the launch happens asynchronously in the driver's supervisor.
	start := session.Command{
		CommandID:        "forge-start-1",
		ServerID:         serverID,
		Kind:             "StartServer",
		Driver:           "container",
		JarRelpath:       "server.jar",
		MinecraftVersion: stubMCVersion,
		LaunchMode:       "forge-argsfile",
	}
	if res := mgr.Handle(ctx, start); !res.Success {
		t.Fatalf("StartServer (forge) failed: code=%v msg=%s", res.ErrorCode, res.ErrorMessage)
	}

	// Wait for the supervised install to finish and the launch container to reach
	// running. Reaching running on the LAUNCH name proves the install completed and
	// the post-install re-plan + launch succeeded.
	waitRunning(ctx, t, docker, launchName)

	// The install produced the args file in the bind-mounted working dir.
	if _, err := os.Stat(filepath.Join(workingDir, filepath.FromSlash(forgeArgsfileRel))); err != nil {
		t.Fatalf("forge args file not created by install: %v", err)
	}

	// The exited install container is reaped once the launch is published. The
	// driver starts the launch container (so it is running, the wait above wins)
	// and only then removes the install container, so the removal trails the launch
	// reaching running by a small window; poll until it is gone rather than racing
	// the single removal.
	waitGone(ctx, t, docker, installName)

	// The install output was captured to logs/forge-install.log for the operator.
	if _, err := os.Stat(filepath.Join(workingDir, filepath.FromSlash(execution.ForgeInstallLogRelpath))); err != nil {
		t.Fatalf("forge install log not written: %v", err)
	}

	// Stop the server to leave the daemon clean (Cleanup also force-removes).
	stop := session.Command{
		CommandID: "forge-stop-1",
		ServerID:  serverID,
		Kind:      "StopServer",
		Force:     true,
	}
	if res := mgr.Handle(ctx, stop); !res.Success {
		t.Fatalf("StopServer failed: code=%v msg=%s", res.ErrorCode, res.ErrorMessage)
	}
}

// TestContainerForgeInstalledSkipsInstall is the cheap companion assertion: a
// Forge args-file StartServer whose working set ALREADY has the args file must
// launch directly, never running an install container. It seeds the args file up
// front, so the launch plan finds it and skips the install step; the launch
// container reaching running while the install name was never created proves the
// re-plan short-circuit (the path a restart of an installed server takes).
func TestContainerForgeInstalledSkipsInstall(t *testing.T) {
	if os.Getenv("MCD_E2E_DOCKER") == "" {
		t.Skip("MCD_E2E_DOCKER not set; skipping container-driver e2e (needs a Docker daemon)")
	}
	image := env(t, "MCD_E2E_STUB_IMAGE")

	reapCtx, reapCancel := context.WithTimeout(context.Background(), 30*time.Second)
	if err := reapStaleE2EContainers(reapCtx); err != nil {
		t.Logf("e2e reaper: %v", err)
	}
	reapCancel()

	docker, err := containerdriver.NewEngineClient("")
	if err != nil {
		t.Fatalf("docker engine client: %v", err)
	}

	workerID := "e2e-forge-" + newServerID(t)
	driver := containerdriver.New(
		docker,
		containerdriver.NewImageSelector(map[int]string{stubJavaMajor: image}),
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
		func(context.Context, string, string) (execution.ServerControl, error) {
			return nil, errNoRCON
		},
	)

	serverID := newServerID(t)
	launchName := "mcsd-" + serverID
	installName := launchName + "-install"

	// Seed the working dir WITH the args file already present, so the launch plan
	// skips the install step.
	game, rcon := freePort(t), freePort(t)
	writeTree(t, scratchDir, map[string]string{
		serverID + "/server.properties":   "server-port=" + game + "\nrcon.port=" + rcon + "\n",
		serverID + "/" + forgeArgsfileRel: "stub forge args file\n",
	})

	t.Cleanup(func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		for _, n := range []string{launchName, installName} {
			if info, err := docker.Inspect(cleanupCtx, n); err == nil {
				_ = docker.Remove(cleanupCtx, info.ID)
			}
		}
	})

	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	start := session.Command{
		CommandID:        "forge-skip-start-1",
		ServerID:         serverID,
		Kind:             "StartServer",
		Driver:           "container",
		JarRelpath:       "server.jar",
		MinecraftVersion: stubMCVersion,
		LaunchMode:       "forge-argsfile",
	}
	if res := mgr.Handle(ctx, start); !res.Success {
		t.Fatalf("StartServer (forge, installed) failed: code=%v msg=%s", res.ErrorCode, res.ErrorMessage)
	}

	// With the args file present, the launch container is started synchronously by
	// Start, so it is already running here; assert it directly.
	info, err := docker.Inspect(ctx, launchName)
	if err != nil {
		t.Fatalf("inspect launch container after start: %v", err)
	}
	if !info.Running {
		t.Fatalf("launch container %s not running after start", launchName)
	}

	// No install container was ever created: an installed working set skips the
	// install step entirely.
	if info, err := docker.Inspect(ctx, installName); err == nil {
		t.Fatalf("install container %s was created (id=%s); an installed working set must skip install", installName, info.ID)
	}

	stop := session.Command{
		CommandID: "forge-skip-stop-1",
		ServerID:  serverID,
		Kind:      "StopServer",
		Force:     true,
	}
	if res := mgr.Handle(ctx, stop); !res.Success {
		t.Fatalf("StopServer failed: code=%v msg=%s", res.ErrorCode, res.ErrorMessage)
	}
}

// waitRunning polls the named container until it is running or ctx expires. The
// Forge install runs asynchronously after StartServer returns, so the launch
// container appears only once the install completes; a fatal on timeout names the
// last observed state for diagnosis.
func waitRunning(ctx context.Context, t *testing.T, docker *containerdriver.EngineClient, name string) {
	t.Helper()
	deadline := time.Now().Add(60 * time.Second)
	for {
		info, err := docker.Inspect(ctx, name)
		if err == nil && info.Running {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("container %s did not reach running: lastErr=%v", name, err)
		}
		select {
		case <-ctx.Done():
			t.Fatalf("context cancelled waiting for %s to run: %v", name, ctx.Err())
		case <-time.After(200 * time.Millisecond):
		}
	}
}

// waitGone polls until the named container no longer exists or ctx expires. The
// driver removes the exited install container just after starting the launch, so
// its disappearance trails the launch reaching running by a small window.
func waitGone(ctx context.Context, t *testing.T, docker *containerdriver.EngineClient, name string) {
	t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	for {
		info, err := docker.Inspect(ctx, name)
		if err != nil {
			return // not found: the container is gone.
		}
		if time.Now().After(deadline) {
			t.Fatalf("container %s still present (id=%s); expected it removed after launch", name, info.ID)
		}
		select {
		case <-ctx.Done():
			t.Fatalf("context cancelled waiting for %s to be removed: %v", name, ctx.Err())
		case <-time.After(200 * time.Millisecond):
		}
	}
}
