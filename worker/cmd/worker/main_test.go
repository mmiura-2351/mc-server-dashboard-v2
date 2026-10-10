package main

import (
	"context"
	"io"
	"log/slog"
	"maps"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/clock"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/controlplane"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager"
	controlplanev1 "github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/controlplane/mcsd/controlplane/v1"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/session"
)

// Keep client pings above the gRPC-Go floor and API enforcement floor to avoid silent adjustment or GOAWAY.
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

// TestResolveRconHost pins the RCON host-resolution gate: only a container-driven server consults the container
// driver's resolver; every other server (and a worker with no container driver built) dials the host loopback
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

// Exercise reclaims through boot so missing wiring cannot pass helper-only tests.
func TestBootReclaimsScratchLeftovers(t *testing.T) {
	scratch := t.TempDir()
	gone := []string{
		seedScratchTree(t, filepath.Join(scratch, ".hydrate-s1-123456")),
		seedScratchTree(t, filepath.Join(scratch, ".hydrate-s1-superseded-654321")),
		seedScratchTree(t, filepath.Join(scratch, ".sweeping-s1-123456")),
	}
	// A live working set and a displaced recovery tree are NOT boot garbage: the first is what the held-set scan
	// advertises, the second is retained for operator recovery until a successful snapshot sweeps it.
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

// Gate the orphan sweep to verify no scratch tree is deleted while a container may still write it.
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

// A failed orphan sweep must leave all recovery and hydrate leftovers intact.
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

// A failed sweep must skip fsck without suppressing the held-generation advertisement.
func TestBootDoesNotJudgeHeldWorldsWhenTheOrphanSweepFails(t *testing.T) {
	scratch := t.TempDir()
	seedTornWorkingSet(t, filepath.Join(scratch, "s1"), 9)
	cp := newRegistrationRecorder()
	// A docker socket that does not exist: the sweep's container list fails, which
	// buildInstanceManager logs and does not treat as fatal, so boot continues unquiesced.
	setBootEnv(t, scratch, "unix://"+filepath.Join(t.TempDir(), "no-such-docker.sock"), cp.start(t))
	t.Setenv("MCD_WORKER_LOG_LEVEL", "warn")
	logged := captureStderr(t)

	stop := runInBackground(t)
	first := cp.nextRegister(t)
	stop()

	// The set must still be ENUMERATED, at its recorded generation: dropping it from the
	// advertisement would report nothing held and make the API hydrate every server on this
	// Worker, and a 0 would send a hydrate over what may be a running orphan's live world.
	if want := map[string]uint64{"s1": 9}; !maps.Equal(first, want) {
		t.Errorf("Register #1 held_servers = %v, want %v: a boot whose orphan sweep failed must "+
			"advertise the recorded generation unjudged (issue #3171)", first, want)
	}
	if got, err := os.ReadFile(filepath.Join(scratch, "s1", ".mcsd_generation")); err != nil || string(got) != "9" {
		t.Errorf("s1 marker = %q (err %v) after an unquiesced boot, want \"9\" untouched: a boot "+
			"that cannot prove no orphan is writing must not persist a torn verdict (issue #3171)", got, err)
	}
	out := logged()
	if strings.Contains(out, "has a corrupt region") {
		t.Errorf("boot judged s1's world torn although the container orphan sweep failed: the "+
			"sweep is what proves nothing is writing into it, and a mid-write read of a running "+
			"orphan's live world reads as torn (issue #3171)\nboot log:\n%s", out)
	}
	for _, want := range []string{"skipping the held-set region fsck", `"server_id":"s1"`, `"generation":9`} {
		if !strings.Contains(out, want) {
			t.Errorf("boot log does not contain %q: the scan must still advertise s1 at the "+
				"generation its marker records (issue #3171)\nboot log:\n%s", want, out)
		}
	}
}

// Assert generation zero on first registration and reconnect so the held-map replacement cannot revive a torn
// generation.
func TestBootJudgesATornHeldWorldWhenTheOrphanSweepSucceeds(t *testing.T) {
	scratch := t.TempDir()
	seedTornWorkingSet(t, filepath.Join(scratch, "s1"), 9)
	cp := newRegistrationRecorder()
	// The sweep must SUCCEED here, so boot is given a docker socket that answers its
	// container list — the same arrangement TestBootReclaimsScratchLeftovers needs.
	setBootEnv(t, scratch, fakeDockerSocket(t), cp.start(t))
	t.Setenv("MCD_WORKER_LOG_LEVEL", "warn")
	logged := captureStderr(t)

	stop := runInBackground(t)
	// The recorder ends each stream right after its ack, so the second Register is the
	// Worker's reconnect within the same process — no reboot, no second boot scan.
	first := cp.nextRegister(t)
	second := cp.nextRegister(t)
	stop()

	want := map[string]uint64{"s1": 0}
	for i, got := range []map[string]uint64{first, second} {
		if !maps.Equal(got, want) {
			t.Errorf("Register #%d held_servers = %v, want %v: a torn held set must reach the API "+
				"at generation 0 on every registration of a boot whose orphan sweep succeeded, "+
				"or the marker the power loss made durable boots the torn world (issues #834, "+
				"#3178)", i+1, got, want)
		}
	}
	out := logged()
	for _, want := range []string{
		"held set has a corrupt region; its generation marker now reads 0",
		`"server_id":"s1"`,
		// The rewrite erases the original generation from disk; this line is where an
		// operator recovering a displaced torn tree finds it (STORAGE.md Section 4.6).
		`"recorded_generation":9`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("boot log does not contain %q (issues #834, #3178)\nboot log:\n%s", want, out)
		}
	}
}

// Repair the torn tree through fake hydrate, then require registration to read the newly written generation
// marker.
func TestHydratedTornHeldSetIsAdvertisedAtItsServedGenerationWithoutAReboot(t *testing.T) {
	scratch := t.TempDir()
	seedTornWorkingSet(t, filepath.Join(scratch, "s1"), 9)
	boot := instancemanager.ScanHeldServers(scratch, true, nil)
	manager := instancemanager.New(nil, scratch, nil).WithTransfer(soundTreeTransfer{gen: 9})
	t.Cleanup(manager.Close)

	cp := newRegistrationRecorder()
	results := make(chan *controlplanev1.CommandResult, 1)
	// On the first stream only, play the API's part: dispatch the hydrate the 0 asked for,
	// wait for its result, then end the stream so the Worker re-registers.
	cp.afterAck = func(n int, stream controlplanev1.WorkerService_SessionServer) error {
		if n != 1 {
			return nil
		}
		if err := stream.Send(&controlplanev1.ApiMessage{
			CorrelationId: "hydrate-s1",
			Payload: &controlplanev1.ApiMessage_ApiCommand{ApiCommand: &controlplanev1.ApiCommand{
				CommandId: "hydrate-s1",
				ServerId:  "s1",
				Command: &controlplanev1.ApiCommand_Hydrate{Hydrate: &controlplanev1.HydrateTrigger{
					TransferUrl: "http://data-plane.invalid/hydrate", TransferToken: "token",
				}},
			}},
		}); err != nil {
			return err
		}
		for {
			msg, err := stream.Recv()
			if err != nil {
				return err
			}
			if r := msg.GetCommandResult(); r != nil {
				results <- r
				return nil
			}
		}
	}
	conn, err := grpc.NewClient(cp.start(t), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	runner := session.NewRunner(
		controlplane.NewDialer(conn, "test-credential", clock.System{}),
		session.Capabilities{WorkerID: "worker-repair", Drivers: []string{"container"}, HeldServers: boot},
		clock.System{}, slog.New(slog.NewTextHandler(io.Discard, nil)),
		session.WithCommandHandler(manager),
		session.WithBackoff(session.Backoff{Initial: time.Millisecond, Max: time.Millisecond, Multiplier: 2}),
	)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = runner.Run(ctx); close(done) }()
	t.Cleanup(func() { cancel(); <-done })

	if got, want := cp.nextRegister(t), map[string]uint64{"s1": 0}; !maps.Equal(got, want) {
		t.Fatalf("Register #1 held_servers = %v, want %v: the boot judged s1 torn (issue #3178)", got, want)
	}
	select {
	case r := <-results:
		if !r.GetSuccess() {
			t.Fatalf("hydrate result = %v, want success", r)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("no CommandResult for the hydrate reached the control plane")
	}
	if got, want := cp.nextRegister(t), map[string]uint64{"s1": 9}; !maps.Equal(got, want) {
		t.Errorf("Register #2 held_servers = %v, want %v: a hydrate that repaired the set must "+
			"stop it being advertised as torn without a reboot (issue #3178)", got, want)
	}
}

// soundTreeTransfer is the repair leg's data plane: Hydrate replaces the working set with
// a structurally sound one and reports gen as the store generation it served. It writes
// no marker itself, so the marker the next Register reads is handleHydrate's own write.
// The embedded Transfer is nil, so any other data-plane call panics the test — nothing
// else should be reached.
type soundTreeTransfer struct {
	instancemanager.Transfer
	gen uint64
}

func (s soundTreeTransfer) Hydrate(_ context.Context, _, _, workingDir string) (uint64, error) {
	if err := os.RemoveAll(workingDir); err != nil {
		return 0, err
	}
	if err := os.MkdirAll(filepath.Join(workingDir, "region"), 0o750); err != nil {
		return 0, err
	}
	// Two zeroed header sectors: a region file that references no chunk, which no rule can
	// call torn.
	return s.gen, os.WriteFile(filepath.Join(workingDir, "region", "r.0.0.mca"), make([]byte, 2*4096), 0o640)
}

// registrationRecorder is an in-process control plane that records the held_servers of
// every Register, acks it, and then runs afterAck — by default nothing, which ends the
// stream and so drives the Worker's reconnect and its next Register.
type registrationRecorder struct {
	controlplanev1.UnimplementedWorkerServiceServer

	registers chan map[string]uint64
	afterAck  func(n int, stream controlplanev1.WorkerService_SessionServer) error

	mu sync.Mutex
	n  int
}

func newRegistrationRecorder() *registrationRecorder {
	return &registrationRecorder{registers: make(chan map[string]uint64, 16)}
}

func (s *registrationRecorder) Session(stream controlplanev1.WorkerService_SessionServer) error {
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	if first.GetRegister() == nil {
		return status.Error(codes.FailedPrecondition, "first message must be Register")
	}
	held := map[string]uint64{}
	for _, h := range first.GetRegister().GetHeldServers() {
		held[h.GetServerId()] = h.GetGeneration()
	}
	s.mu.Lock()
	s.n++
	n := s.n
	s.mu.Unlock()
	select {
	case s.registers <- held:
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
	if err := stream.Send(&controlplanev1.ApiMessage{
		Payload: &controlplanev1.ApiMessage_RegisterAck{RegisterAck: &controlplanev1.RegisterAck{
			HeartbeatInterval: durationpb.New(time.Minute),
		}},
	}); err != nil {
		return err
	}
	if s.afterAck != nil {
		return s.afterAck(n, stream)
	}
	return nil
}

// start serves the recorder on a loopback TCP port — run() dials a real endpoint — and
// returns its address.
func (s *registrationRecorder) start(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	gs := grpc.NewServer()
	controlplanev1.RegisterWorkerServiceServer(gs, s)
	go func() { _ = gs.Serve(l) }()
	t.Cleanup(gs.Stop)
	return l.Addr().String()
}

// nextRegister returns the held_servers of the next Register the recorder received.
func (s *registrationRecorder) nextRegister(t *testing.T) map[string]uint64 {
	t.Helper()
	select {
	case held := <-s.registers:
		return held
	case <-time.After(30 * time.Second):
		t.Fatal("no Register reached the control plane")
		return nil
	}
}

// runInBackground starts run() and returns the func that shuts it down, as a signal does,
// and checks it returned nil. The shutdown is also registered as cleanup — after
// captureStderr's when called after it, so it runs first — so a t.Fatal in between cannot
// leave boot running into a restored stderr.
func runInBackground(t *testing.T) func() {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- run(ctx) }()
	var once sync.Once
	stop := func() {
		once.Do(func() {
			cancel()
			if err := <-done; err != nil {
				t.Errorf("run() = %v, want nil: boot must reach the session, and a clean "+
					"shutdown returns nil", err)
			}
		})
	}
	t.Cleanup(stop)
	return stop
}

// Set every Worker environment input so ambient settings cannot change the boot path under test.
func setBootEnv(t *testing.T, scratch, dockerHost, grpcEndpoint string) {
	t.Helper()
	keys := map[string]string{
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
	}
	for k, v := range keys {
		t.Setenv(k, v)
	}
	// Reject inherited Worker variables missing from the fixture map so new config inputs cannot silently affect
	// boot tests.
	for _, kv := range os.Environ() {
		name, _, _ := strings.Cut(kv, "=")
		if !strings.HasPrefix(name, "MCD_WORKER_") {
			continue
		}
		if _, isolated := keys[name]; !isolated {
			t.Fatalf("%s is set in the environment but setBootEnv does not isolate it: these "+
				"tests would exercise the ambient value, and a malformed one would fail them "+
				"in config loading rather than at the boot step under test", name)
		}
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

// Seed an 8 KiB region whose first chunk points exactly at EOF, alongside its generation marker.
// The successful-sweep test ensures this shared fixture still reads as torn.
func seedTornWorkingSet(t *testing.T, dir string, gen int) {
	t.Helper()
	region := make([]byte, 2*4096)
	region[2] = 2 // location entry 0: sector offset 2, which is EOF in an 8 KiB file.
	region[3] = 1 // one sector.
	if err := os.MkdirAll(filepath.Join(dir, "region"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "region", "r.0.0.mca"), region, 0o640); err != nil {
		t.Fatal(err)
	}
	// scratchformat.GenerationMarkerFile; a rename there fails these tests loudly,
	// because a set with no marker is advertised at generation 0 either way.
	if err := os.WriteFile(filepath.Join(dir, ".mcsd_generation"), []byte(strconv.Itoa(gen)), 0o640); err != nil {
		t.Fatal(err)
	}
}

// Capture stderr because run constructs its own logger; cleanup restores it even after test failure.
// Keep output below the pipe buffer until the final read to avoid blocking boot.
func captureStderr(t *testing.T) func() string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	orig := os.Stderr
	os.Stderr = w
	var once sync.Once
	var out string
	read := func() string {
		once.Do(func() {
			os.Stderr = orig
			_ = w.Close()
			b, readErr := io.ReadAll(r)
			_ = r.Close()
			if readErr != nil {
				t.Errorf("read captured stderr: %v", readErr)
			}
			out = string(b)
		})
		return out
	}
	t.Cleanup(func() { read() })
	return read
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
