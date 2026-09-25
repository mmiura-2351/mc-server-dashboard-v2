package main

import (
	"context"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestControlPlaneKeepaliveMatchesServerContract pins the client-side keepalive
// parameters the control-plane dial applies (issue #1709). The values are a
// cross-module contract with the API server's enforcement policy
// (api/src/mc_server_dashboard_api/fleet/adapters/grpc_server.py
// _keepalive_options): Time must stay at or above twice the server's
// grpc.http2.min_ping_interval_without_data_ms (10s) or the server answers the
// ping cadence with GOAWAY ENHANCE_YOUR_CALM, and at or above gRPC-Go's 10s
// client floor (below it the library silently raises it).
func TestControlPlaneKeepaliveMatchesServerContract(t *testing.T) {
	if got, want := controlPlaneKeepalive.Time, 20*time.Second; got != want {
		t.Errorf("controlPlaneKeepalive.Time = %v, want %v", got, want)
	}
	if got, want := controlPlaneKeepalive.Timeout, 10*time.Second; got != want {
		t.Errorf("controlPlaneKeepalive.Timeout = %v, want %v", got, want)
	}
	if !controlPlaneKeepalive.PermitWithoutStream {
		t.Error("controlPlaneKeepalive.PermitWithoutStream = false, want true (probe between Session streams)")
	}
}

// TestRunFailsFastOnMissingConfig verifies the wiring surfaces a fatal config
// error at boot when required keys are absent (CONFIGURATION.md Section 2). The
// test clears the relevant env so Load sees nothing.
func TestRunFailsFastOnMissingConfig(t *testing.T) {
	for _, k := range []string{
		"MCD_WORKER_CONFIG",
		"MCD_WORKER_API_GRPC_ENDPOINT",
		"MCD_WORKER_API_CREDENTIAL",
		"MCD_WORKER_WORKER_SCRATCH_DIR",
	} {
		t.Setenv(k, "")
	}

	err := run(context.Background())
	if err == nil {
		t.Fatal("run() with no config returned nil, want a fatal config error")
	}
	if !strings.Contains(err.Error(), "config") {
		t.Errorf("run() error = %v, want a config error", err)
	}
}

// TestResolveRconHost pins the RCON host-resolution gate (issue #218): only a
// container-driven server consults the container driver's resolver; every other
// server (and a worker with no container driver built) dials the host loopback
// (empty host).
func TestResolveRconHost(t *testing.T) {
	containerResolver := func(serverID string) string {
		if serverID == "srv-1" {
			return "mc-srv-1"
		}
		return ""
	}
	noContainerDriver := func(string) string { return "" }

	tests := []struct {
		name              string
		driver            string
		containerRconHost func(string) string
		serverID          string
		want              string
	}{
		{
			name:              "container driver with network resolves to the container name",
			driver:            "container",
			containerRconHost: containerResolver,
			serverID:          "srv-1",
			want:              "mc-srv-1",
		},
		{
			name:              "non-container driver keeps the loopback",
			driver:            "other",
			containerRconHost: containerResolver,
			serverID:          "srv-1",
			want:              "",
		},
		{
			name:              "container driver with no container driver built keeps the loopback",
			driver:            "container",
			containerRconHost: noContainerDriver,
			serverID:          "srv-1",
			want:              "",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := resolveRconHost(tc.driver, tc.containerRconHost, tc.serverID)
			if got != tc.want {
				t.Errorf("resolveRconHost(%q, _, %q) = %q, want %q", tc.driver, tc.serverID, got, tc.want)
			}
		})
	}
}

// The boot-time scratch reclaims are pinned HERE, at the call site, and not only as
// functions (PR #3170 review round 1, issues #3167 / #2799). Both are package-level
// helpers nothing else invokes, so a unit test of the function leaves the wiring free:
// deleting either line from run() kept the whole Worker suite green while boot stopped
// reclaiming world-sized trees, which is exactly the acceptance criterion ("removed at
// the next Worker boot") that the function test does not reach.
//
// run() is reachable without Docker or a live API. The control-plane dial is lazy
// (grpc.NewClient connects on first use), the container driver's client construction only
// parses the docker host (NewEngineClient), and Run returns nil at once on a cancelled
// context. TestRunFailsFastOnMissingConfig already calls run() for the config leg; this
// covers the scratch legs.
//
// What this test must NOT fake is the orphan sweep's success: since review round 2 the
// reclaims are gated on it (see run()), so boot is given a docker socket that ANSWERS the
// sweep's container list — with no containers, which is the quiescence the reclaims
// require. The failed-sweep half is TestBootLeavesScratchLeftoversWhenTheOrphanSweepFails.
func TestBootReclaimsScratchLeftovers(t *testing.T) {
	scratch := t.TempDir()
	gone := []string{
		seedScratchTree(t, filepath.Join(scratch, ".hydrate-s1-123456")),
		seedScratchTree(t, filepath.Join(scratch, ".hydrate-s1-superseded-654321")),
		seedScratchTree(t, filepath.Join(scratch, ".sweeping-s1-123456")),
	}
	// A live working set and a displaced recovery tree are NOT boot garbage: the first
	// is what the held-set scan advertises, the second is retained for operator
	// recovery until a successful snapshot sweeps it (issue #911).
	kept := []string{
		seedScratchTree(t, filepath.Join(scratch, "s1")),
		seedScratchTree(t, filepath.Join(scratch, ".displaced-s2")),
	}

	ctx, cancel := context.WithCancel(context.Background())
	// The session's first dial is the end of the boot sequence — everything asserted below
	// has already happened by then — so that connection is where this test sends the
	// shutdown, rather than pre-cancelling a context the orphan sweep's own Engine call
	// needs. The timer only backstops it: a boot that never reaches the session then fails
	// on the assertions instead of hanging the package.
	endpoint := cancelOnFirstDial(t, cancel)
	backstop := time.AfterFunc(30*time.Second, cancel)
	t.Cleanup(func() { backstop.Stop() })
	setBootEnv(t, scratch, fakeDockerSocket(t), endpoint)

	if err := run(ctx); err != nil {
		t.Fatalf("run() = %v, want nil: this test needs the boot sequence to complete and the "+
			"session to return, as a clean shutdown does", err)
	}

	for _, dir := range gone {
		if _, err := os.Stat(dir); !os.IsNotExist(err) {
			t.Errorf("%s survived boot (stat err = %v): nothing else ever reclaims one — the "+
				"id's scratch dir is gone, so it is never advertised as held and no per-id "+
				"sweep is offered it again (issues #3167 / #2799)", filepath.Base(dir), err)
		}
	}
	for _, dir := range kept {
		if _, err := os.Stat(dir); err != nil {
			t.Errorf("boot removed %s, which is not boot garbage: %v", filepath.Base(dir), err)
		}
	}
}

// The reclaims run AFTER the container orphan sweep, and that order is the load-bearing
// one: the sweep is what stops a container from still writing into a tree it holds under
// its pre-park-aside name, and #2799 / #3167 both rest on "no writer is left". The sweep
// lives inside buildInstanceManager, so this test fails the manager build outright — an
// unsupported docker host, which NewEngineClient refuses without contacting Docker — and
// asserts the trees are untouched. Hoisting either reclaim above buildInstanceManager
// therefore fails here.
//
// The remaining ordering — the two reclaims against ScanHeldServers below them — is not
// pinned, and cannot regress into a fault: both scans skip .hydrate- and .sweeping- names
// by prefix (isReservedScratchName, pinned by TestHeldScansSkipTheSharedHydrateTempPrefix
// and TestInterruptedDisplacedSweepIsNotAdvertisedAsHeld), so the held set is identical
// whichever side of the scan the reclaims run on.
func TestBootReclaimsWaitForTheContainerOrphanSweep(t *testing.T) {
	scratch := t.TempDir()
	leftovers := []string{
		seedScratchTree(t, filepath.Join(scratch, ".hydrate-s1-123456")),
		seedScratchTree(t, filepath.Join(scratch, ".sweeping-s1-123456")),
	}
	// The endpoint is never dialled: the context is already cancelled, so the session
	// returns before it would be.
	setBootEnv(t, scratch, "tcp://127.0.0.1:2375", "127.0.0.1:1")

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	err := run(ctx)
	if err == nil {
		t.Fatal("run() with an unsupported docker host returned nil, want the instance-manager " +
			"build to fail: this test places that failure between the orphan sweep and the " +
			"reclaims, and cannot observe the order without it")
	}

	for _, dir := range leftovers {
		if _, statErr := os.Stat(dir); statErr != nil {
			t.Errorf("%s was reclaimed although the instance-manager build failed (%v): the boot "+
				"reclaims must run after the container orphan sweep, which that build owns — a "+
				"container still writing into the tree would otherwise be swept out from under "+
				"(issues #3167 / #2799); stat err = %v", filepath.Base(dir), err, statErr)
		}
	}
}

// A FAILED container orphan sweep leaves the boot reclaims with nothing to stand on, so
// they must delete nothing (PR #3170 review round 2). Both of them rest on one premise —
// no container this Worker started is still writing into a tree they are about to
// recursively delete — and the sweep is what establishes it. A failed sweep is
// deliberately non-fatal (buildInstanceManager logs it: a docker socket flap must not stop
// the Worker from serving), so the premise can simply be false here.
//
// The path that makes it dangerous rather than untidy: a sweep that failed at an EARLIER
// boot leaves an orphan running with <scratch>/<id> bind-mounted, and the Worker does not
// know about it (nothing re-adopts containers, so its instance map is empty). A
// HydrateTrigger for that id therefore proceeds and renames the live tree aside — to
// .hydrate-<id>-superseded-* when the .displaced-<id> slot is occupied (issue #2278), and
// to .displaced-<id> otherwise, from where a later successful snapshot's sweep renames it
// to .sweeping-<id>-*. The orphan's mount follows the inode, so it keeps writing into the
// renamed tree. A boot whose sweep fails again would then delete a LIVE world, and the
// server's open descriptors would keep writing into unlinked inodes, losing everything
// after that too.
//
// Skipping costs a delay: the trees wait for a boot whose sweep succeeds, and that is
// strictly better than deleting a live world, because the leak is recoverable and the
// deletion is not.
func TestBootLeavesScratchLeftoversWhenTheOrphanSweepFails(t *testing.T) {
	scratch := t.TempDir()
	leftovers := []string{
		seedScratchTree(t, filepath.Join(scratch, ".hydrate-s1-123456")),
		seedScratchTree(t, filepath.Join(scratch, ".hydrate-s1-superseded-654321")),
		seedScratchTree(t, filepath.Join(scratch, ".sweeping-s1-123456")),
	}
	// A docker socket that does not exist: the sweep's container list fails, which
	// buildInstanceManager logs and does not treat as fatal, so boot continues.
	setBootEnv(t, scratch, "unix://"+filepath.Join(t.TempDir(), "no-such-docker.sock"), "127.0.0.1:1")

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := run(ctx); err != nil {
		t.Fatalf("run() = %v, want nil: a failed orphan sweep is non-fatal by design, and this "+
			"test needs boot to reach the reclaims and decline them", err)
	}

	for _, dir := range leftovers {
		if _, err := os.Stat(dir); err != nil {
			t.Errorf("%s was reclaimed although the container orphan sweep failed: the sweep is "+
				"what proves no orphan is still writing into that tree, and without it this can "+
				"be a running server's live world (%v)", filepath.Base(dir), err)
		}
	}
}

// setBootEnv gives run() the configuration Load accepts, pointed at the test's own scratch
// dir, docker host and control-plane endpoint — the three knobs that decide what the boot
// sequence does. EVERY key applyEnv reads is set here, including the ones set to empty:
// leaving one inherited lets a malformed ambient value fail these tests during
// configuration loading instead of at the boot step they exercise (PR #3170 review round
// 2). The comment on each is what the boot path does with it.
func setBootEnv(t *testing.T, scratch, dockerHost, grpcEndpoint string) {
	t.Helper()
	for k, v := range map[string]string{
		"MCD_WORKER_CONFIG":                          "", // no TOML layer: defaults + env only
		"MCD_WORKER_API_GRPC_ENDPOINT":               grpcEndpoint,
		"MCD_WORKER_API_CREDENTIAL":                  "test-credential",
		"MCD_WORKER_API_TLS_INSECURE":                "true", // no CA file to build, plaintext dial
		"MCD_WORKER_API_TLS_CA_FILE":                 "",
		"MCD_WORKER_API_TLS_CLIENT_CERT_FILE":        "",
		"MCD_WORKER_API_TLS_CLIENT_KEY_FILE":         "",
		"MCD_WORKER_WORKER_ID":                       "11111111-1111-1111-1111-111111111111",
		"MCD_WORKER_WORKER_SCRATCH_DIR":              scratch,
		"MCD_WORKER_WORKER_DRIVERS":                  "container", // the only driver config accepts
		"MCD_WORKER_WORKER_MAX_SERVERS":              "1",
		"MCD_WORKER_WORKER_METRICS_INTERVAL_SECONDS": "15", // the stock cadence
		"MCD_WORKER_DRIVER_CONTAINER_IMAGES":         "21=eclipse-temurin:21-jre",
		"MCD_WORKER_DRIVER_CONTAINER_NETWORK":        "",
		"MCD_WORKER_DRIVER_CONTAINER_GAME_BIND_IP":   "127.0.0.1", // validated as an IP
		"MCD_WORKER_DRIVER_CONTAINER_DOCKER_HOST":    dockerHost,
		// The boot path warns about the plaintext dial and, in the failed-sweep tests,
		// about the sweep it could not run; neither is what these tests assert, so keep
		// the suite output clean.
		"MCD_WORKER_LOG_LEVEL":  "error",
		"MCD_WORKER_LOG_FORMAT": "json",
	} {
		t.Setenv(k, v)
	}
}

// fakeDockerSocket serves the one Engine call a no-container orphan sweep makes — the
// labelled container list — over a unix socket, so cd.Sweep SUCCEEDS and boot's quiescence
// premise holds for real rather than by assumption. Anything else the sweep might call is
// a test failure, not a 404 to shrug at: it would mean this fake no longer stands in for
// the sweep it is imitating.
func fakeDockerSocket(t *testing.T) string {
	t.Helper()
	sock := filepath.Join(t.TempDir(), "docker.sock")
	l, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{
		ReadHeaderTimeout: 5 * time.Second,
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !strings.HasSuffix(r.URL.Path, "/containers/json") {
				t.Errorf("fake docker: unexpected Engine call %s %s, want only the orphan "+
					"sweep's container list", r.Method, r.URL.Path)
				http.NotFound(w, r)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte("[]"))
		}),
	}
	go func() { _ = srv.Serve(l) }()
	t.Cleanup(func() { _ = srv.Close() })
	return "unix://" + sock
}

// cancelOnFirstDial returns a control-plane endpoint whose first inbound connection
// cancels the boot context. The session's dial is the first thing in run() to open that
// connection (grpc.NewClient is lazy, so nothing before the session touches it), which
// makes it a signal that the whole boot sequence has run — without pre-cancelling a
// context the orphan sweep's own Engine call needs.
func cancelOnFirstDial(t *testing.T, cancel context.CancelFunc) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = l.Close() })
	go func() {
		for {
			conn, err := l.Accept()
			if err != nil {
				return
			}
			cancel()
			_ = conn.Close()
		}
	}()
	return l.Addr().String()
}

// seedScratchTree creates a world-shaped directory: what a live working set, a crash-left
// hydrate tree and an interrupted displaced sweep's tree all look like on disk, which is
// the point — only the NAME tells the boot reclaims which is which.
func seedScratchTree(t *testing.T, dir string) string {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, "world"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "world", "level.dat"), []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}
	return dir
}
