package hostprocess

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// spawnRec records one spawn call's argv and working dir.
type spawnRec struct {
	args []string
	dir  string
}

// queueSpawn returns a spawnFunc that hands out the queued processes in order and
// records each call. A spawn past the queue end fails the test.
func queueSpawn(t *testing.T, procs ...*fakeProcess) (spawnFunc, *[]spawnRec) {
	t.Helper()
	var mu sync.Mutex
	var recs []spawnRec
	idx := 0
	spawn := func(_ context.Context, _ string, args []string, dir string) (process, error) {
		mu.Lock()
		defer mu.Unlock()
		recs = append(recs, spawnRec{args: args, dir: dir})
		if idx >= len(procs) {
			t.Errorf("unexpected spawn #%d: %v", idx, args)
			return newFakeProcess(), nil
		}
		p := procs[idx]
		idx++
		if p.startErr != nil {
			return nil, p.startErr
		}
		return p, nil
	}
	return spawn, &recs
}

func forgeDriver(spawn spawnFunc) *Driver {
	return New(fixedSelector{}, spawn, func(context.Context, execution.InstanceSpec) (execution.ServerControl, error) {
		return nil, errors.New("no rcon")
	}, Options{StopTimeout: 50 * time.Millisecond, ReadinessTimeout: 20 * time.Millisecond})
}

func forgeSpec(dir string) execution.InstanceSpec {
	return execution.InstanceSpec{
		ServerID:         "s1",
		WorkingDir:       dir,
		MinecraftVersion: "1.20.1",
		JarRelpath:       "server.jar",
		LaunchMode:       execution.LaunchModeForgeArgsfile,
	}
}

const forgeArgsRel = "libraries/net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt"

// A Forge start with an args file already present launches directly via the args
// file (no install), reaching running.
func TestForgeStartArgsfilePresentLaunches(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, forgeArgsRel)), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, forgeArgsRel), []byte("-x"), 0o600); err != nil {
		t.Fatal(err)
	}
	launch := newFakeProcess()
	spawn, recs := queueSpawn(t, launch)
	d := forgeDriver(spawn)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateRunning)

	if len(*recs) != 1 {
		t.Fatalf("spawns = %d, want 1 (launch only, no install)", len(*recs))
	}
	if !hasArg((*recs)[0].args, "@"+filepath.Join(dir, filepath.FromSlash(forgeArgsRel))) {
		t.Fatalf("launch args = %v, want the @argsfile", (*recs)[0].args)
	}
	launch.exit(nil)
}

// A Forge start with no args file runs the supervised installer first; on a
// successful install (which produces the args file) it launches as the SAME
// instance and reaches running.
func TestForgeInstallThenLaunch(t *testing.T) {
	dir := t.TempDir()
	installer := newFakeProcess()
	launch := newFakeProcess()
	spawn, recs := queueSpawn(t, installer, launch)
	d := forgeDriver(spawn)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	// Still starting during install.
	if inst.Status() != execution.StateStarting {
		t.Fatalf("Status during install = %v, want starting", inst.Status())
	}

	// The installer produces the args file, then exits cleanly.
	if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, forgeArgsRel)), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, forgeArgsRel), []byte("-x"), 0o600); err != nil {
		t.Fatal(err)
	}
	installer.exit(nil)

	drainTo(t, inst.Events(), execution.StateRunning)
	if len(*recs) != 2 {
		t.Fatalf("spawns = %d, want 2 (install + launch)", len(*recs))
	}
	if !hasArg((*recs)[0].args, "--installServer") {
		t.Fatalf("install args = %v, want --installServer", (*recs)[0].args)
	}
	if !hasArg((*recs)[1].args, "@"+filepath.Join(dir, filepath.FromSlash(forgeArgsRel))) {
		t.Fatalf("launch args = %v, want the @argsfile", (*recs)[1].args)
	}
	launch.exit(nil)
}

// A Forge install that exits non-zero reports crashed and never launches the
// server (issue #305). Install failure surfaces as crashed via the status pump,
// not a command error code.
func TestForgeInstallFailureCrashesNoLaunch(t *testing.T) {
	dir := t.TempDir()
	installer := newFakeProcess()
	// Only the installer is queued; a launch spawn would fail the test.
	spawn, recs := queueSpawn(t, installer)
	d := forgeDriver(spawn)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	installer.exit(errors.New("exit status 1"))

	drainTo(t, inst.Events(), execution.StateCrashed)
	if len(*recs) != 1 {
		t.Fatalf("spawns = %d, want 1 (install only, no launch)", len(*recs))
	}
}

// A Forge install that exits cleanly but leaves no args file is a failed install:
// the re-plan still needs install, so the instance crashes rather than looping.
func TestForgeInstallCleanButNoArgsfileCrashes(t *testing.T) {
	dir := t.TempDir()
	installer := newFakeProcess()
	spawn, _ := queueSpawn(t, installer)
	d := forgeDriver(spawn)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	installer.exit(nil) // clean exit, but produced no args file

	drainTo(t, inst.Events(), execution.StateCrashed)
}

// Stop during the install phase terminates the installer and reports stopped; no
// launch is spawned (issue #305).
func TestForgeStopDuringInstall(t *testing.T) {
	dir := t.TempDir()
	installer := newFakeProcess()
	// The installer survives SIGTERM; Stop escalates to Kill, which releases Wait.
	spawn, recs := queueSpawn(t, installer)
	d := forgeDriver(spawn)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	if inst.Status() != execution.StateStarting {
		t.Fatalf("Status during install = %v, want starting", inst.Status())
	}

	if err := inst.Stop(context.Background(), false); err != nil {
		t.Fatalf("Stop during install: %v", err)
	}
	drainTo(t, inst.Events(), execution.StateStopped)
	if !installer.killed && !installer.gotSignal(os.Interrupt) {
		// Kill is the escalation when SIGTERM is ignored by the fake.
		t.Fatal("expected the installer to be terminated")
	}
	if len(*recs) != 1 {
		t.Fatalf("spawns = %d, want 1 (install only; Stop prevents launch)", len(*recs))
	}
}

// The supervised installer's output is captured to logs/forge-install.log in the
// working dir so an operator can read it through the files API (issue #305).
func TestForgeInstallOutputWrittenToLog(t *testing.T) {
	dir := t.TempDir()
	installer := newFakeProcess()
	installer.stdout = strings.NewReader("downloading libraries\n")
	installer.stderr = strings.NewReader("a warning\n")
	launch := newFakeProcess()
	spawn, _ := queueSpawn(t, installer, launch)
	d := forgeDriver(spawn)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, forgeArgsRel)), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, forgeArgsRel), []byte("-x"), 0o600); err != nil {
		t.Fatal(err)
	}
	installer.exit(nil)
	drainTo(t, inst.Events(), execution.StateRunning)

	data, err := os.ReadFile(filepath.Join(dir, filepath.FromSlash(execution.ForgeInstallLogRelpath)))
	if err != nil {
		t.Fatalf("read install log: %v", err)
	}
	text := string(data)
	if !strings.Contains(text, "downloading libraries") || !strings.Contains(text, "a warning") {
		t.Fatalf("install log = %q, want the installer output", text)
	}
	launch.exit(nil)
}

// A Stop that wins the stopping latch after the installer has exited but before
// the launch is spawned must abort the launch: no server process is started, Stop
// reports cleanly (no survived-SIGKILL error), and the instance ends stopped with
// no orphan record of the dead installer (issue #306). The beforeLaunch hook fires
// in the exact install-exit→launch window the critical section must close, driving
// a Stop to win the latch there.
func TestForgeStopWinsLatchBeforeLaunch(t *testing.T) {
	dir := t.TempDir()
	installer := newFakeProcess()
	// Only the installer is queued; a launch spawn would fail the test, proving no
	// launch happened once the Stop won the latch.
	spawn, recs := queueSpawn(t, installer)
	d := forgeDriver(spawn)

	inst, err := d.Start(context.Background(), forgeSpec(dir))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	hostInst := inst.(*instance)

	stopErr := make(chan error, 1)
	hostInst.beforeLaunch = func() {
		// The installer has exited and the args file is present; a Stop arriving now
		// must win the latch and abort the launch. Run it and wait until it has
		// latched stopping before returning so the handoff observes a set latch.
		go func() { stopErr <- inst.Stop(context.Background(), false) }()
		for inst.Status() != execution.StateStopping {
			time.Sleep(time.Millisecond)
		}
	}

	// The installer produces the args file (so the re-plan finds it and reaches the
	// handoff window) then exits cleanly.
	writeForgeArgsfile(t, dir)
	installer.exit(nil)

	drainTo(t, inst.Events(), execution.StateStopped)

	if err := <-stopErr; err != nil {
		t.Fatalf("Stop returned %v, want clean nil (no survived-SIGKILL orphan)", err)
	}
	if inst.Status() != execution.StateStopped {
		t.Fatalf("final status = %v, want stopped (no server left running)", inst.Status())
	}
	if len(*recs) != 1 {
		t.Fatalf("spawns = %d, want 1 (install only; the launch must be aborted)", len(*recs))
	}
}

func writeForgeArgsfile(t *testing.T, dir string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, forgeArgsRel)), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, forgeArgsRel), []byte("-x"), 0o600); err != nil {
		t.Fatal(err)
	}
}

func hasArg(args []string, want string) bool {
	for _, a := range args {
		if a == want {
			return true
		}
	}
	return false
}
